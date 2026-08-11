"""Deterministic aggregator URL resolution and authoritative ATS handoff."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .aggregators import ExternalJobCandidate
from .normalization import canonicalize_url
from .sources import (
    DiscoveryTarget,
    JsonHttpClient,
    SourceAdapter,
    SourceFailure,
    SourceJob,
    create_adapter,
)


RESOLUTION_STATUSES = (
    "resolved_supported_ats",
    "resolved_company_page",
    "unresolved_external",
    "insufficient_content",
    "blocked_or_unavailable",
)


@dataclass(slots=True)
class SupportedAtsResolution:
    source: str
    canonical_url: str
    native_job_id: str
    target: DiscoveryTarget

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "canonical_url": self.canonical_url,
            "native_job_id": self.native_job_id,
            "target": asdict(self.target),
        }


@dataclass(slots=True)
class CandidateResolution:
    candidate_id: str
    status: str
    canonical_external_url: str = ""
    authoritative_source: str = ""
    authoritative_job_id: str = ""
    reason: str = ""
    ats: SupportedAtsResolution | None = None
    handoff_status: str = "not_attempted"
    handoff_errors: list[SourceFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "canonical_external_url": self.canonical_external_url or None,
            "authoritative_source": self.authoritative_source or None,
            "authoritative_job_id": self.authoritative_job_id or None,
            "reason": self.reason,
            "ats": None if self.ats is None else self.ats.to_dict(),
            "handoff_status": self.handoff_status,
            "handoff_errors": [item.to_dict() for item in self.handoff_errors],
        }


@dataclass(slots=True)
class HandoffResult:
    job: SourceJob | None = None
    errors: list[SourceFailure] = field(default_factory=list)
    pages_fetched: int = 0
    reason: str = ""


AdapterFactory = Callable[[str, JsonHttpClient], SourceAdapter]


def canonicalize_candidate_url(value: str | None) -> str:
    """Unwrap common redirect URLs and strip non-identifying tracking noise."""

    current = str(value or "").strip()
    if not current:
        return ""
    for _ in range(3):
        try:
            parts = urlsplit(current)
        except ValueError:
            return ""
        if (
            parts.scheme.casefold() not in {"http", "https"}
            or not parts.netloc
            or parts.username
            or parts.password
        ):
            return ""
        query = parse_qsl(parts.query, keep_blank_values=True)
        unwrapped = ""
        for key, item in query:
            if key.casefold() in _REDIRECT_KEYS:
                candidate = unquote(item).strip()
                parsed = urlsplit(candidate)
                if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
                    unwrapped = candidate
                    break
        if not unwrapped or unwrapped == current:
            break
        current = unwrapped

    parts = urlsplit(current)
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _EXTRA_TRACKING_KEYS:
            continue
        query.append((key, item))
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    cleaned = urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )
    return canonicalize_url(cleaned)


def resolve_candidate(candidate: ExternalJobCandidate) -> CandidateResolution:
    """Map an aggregator lead to a supported ATS only with sufficient identifiers."""

    canonical = canonicalize_candidate_url(candidate.external_url)
    if not canonical:
        return CandidateResolution(
            candidate_id=candidate.identity,
            status="insufficient_content" if candidate.excerpt else "unresolved_external",
            reason=(
                "Candidate has only a short aggregator excerpt and no authoritative external URL."
                if candidate.excerpt
                else "Candidate does not expose an authoritative external URL."
            ),
        )
    ats = resolve_supported_ats_url(
        canonical,
        company=candidate.company or "",
        title=candidate.title or "",
        job_families=candidate.job_families,
    )
    if ats is not None:
        return CandidateResolution(
            candidate_id=candidate.identity,
            status="resolved_supported_ats",
            canonical_external_url=canonical,
            authoritative_source=ats.source,
            authoritative_job_id=ats.native_job_id,
            reason=f"External URL deterministically mapped to {ats.source} with required identifiers.",
            ats=ats,
        )
    host = urlsplit(canonical).hostname or ""
    if _is_aggregator_host(host):
        return CandidateResolution(
            candidate_id=candidate.identity,
            status="unresolved_external",
            canonical_external_url=canonical,
            reason="External URL still points to an aggregator and exposes no supported ATS target.",
        )
    return CandidateResolution(
        candidate_id=candidate.identity,
        status="resolved_company_page",
        canonical_external_url=canonical,
        reason="External URL points to a company/career page outside the supported ATS adapters.",
    )


def resolve_supported_ats_url(
    url: str,
    *,
    company: str = "",
    title: str = "",
    job_families: list[str] | None = None,
) -> SupportedAtsResolution | None:
    canonical = canonicalize_candidate_url(url)
    if not canonical:
        return None
    parsed = urlsplit(canonical)
    host = (parsed.hostname or "").casefold()
    segments = [unquote(item) for item in parsed.path.split("/") if item]
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    tracks = list(job_families or [])

    if host == "jobs.smartrecruiters.com" and len(segments) >= 2:
        board = segments[0].strip()
        native_id = _smartrecruiters_id(segments[1])
        if board and native_id:
            target = _target(
                "smartrecruiters", board, company, title, tracks,
                resolved_job_id=native_id,
            )
            return SupportedAtsResolution("smartrecruiters", canonical, native_id, target)

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        board = ""
        native_id = ""
        if len(segments) >= 3 and segments[1].casefold() == "jobs":
            board, native_id = segments[0], segments[2]
        elif segments and segments[0].casefold() == "embed":
            board, native_id = query.get("for", ""), query.get("token", "")
        elif segments:
            board = segments[0]
            native_id = query.get("gh_jid", "")
        if board and native_id:
            target = _target(
                "greenhouse", board, company, title, tracks,
                resolved_job_id=native_id,
            )
            return SupportedAtsResolution("greenhouse", canonical, native_id, target)

    if host == "jobs.lever.co" and len(segments) >= 2:
        board, native_id = segments[0].strip(), segments[1].strip()
        if board and native_id:
            target = _target(
                "lever", board, company, title, tracks,
                resolved_job_id=native_id,
            )
            return SupportedAtsResolution("lever", canonical, native_id, target)

    if host == "jobs.ashbyhq.com" and len(segments) >= 2:
        board, native_id = segments[0].strip(), segments[1].strip()
        if board and native_id:
            target = _target(
                "ashby", board, company, title, tracks,
                resolved_job_id=native_id,
            )
            return SupportedAtsResolution("ashby", canonical, native_id, target)

    if host.endswith(".myworkdayjobs.com"):
        workday = _workday_identifiers(parsed, segments)
        if workday is not None:
            tenant, locale, career_site, external_path, native_id = workday
            target = _target(
                "workday", tenant, company, title, tracks,
                max_pages=2,
                base_url=f"{parsed.scheme}://{parsed.netloc}",
                tenant=tenant,
                career_site=career_site,
                locale=locale,
                resolved_job_id=native_id,
                resolved_external_path=external_path,
                server_query=title or native_id,
            )
            return SupportedAtsResolution("workday", canonical, native_id, target)
    return None


class AuthoritativeHandoff:
    """Retrieve a resolved job through its existing public ATS adapter."""

    def __init__(
        self,
        client: JsonHttpClient,
        *,
        adapter_factory: AdapterFactory = create_adapter,
        fetch_limit: int = 100,
    ):
        self.client = client
        self.adapter_factory = adapter_factory
        self.fetch_limit = max(1, fetch_limit)

    def fetch(
        self,
        candidate: ExternalJobCandidate,
        resolution: CandidateResolution,
    ) -> HandoffResult:
        if resolution.ats is None:
            return HandoffResult(reason="Candidate has no supported authoritative ATS mapping.")
        target = resolution.ats.target
        try:
            adapter = self.adapter_factory(resolution.ats.source, self.client)
            batch = adapter.fetch(target, limit=self.fetch_limit)
        except Exception as exc:  # source isolation at the handoff boundary
            failure = SourceFailure(
                source=resolution.ats.source,
                url=resolution.ats.canonical_url,
                job_id=resolution.ats.native_job_id,
                error_type=type(exc).__name__,
                reason=str(exc),
            )
            return HandoffResult(errors=[failure], reason=str(exc))

        selected = _select_authoritative_job(batch.jobs, resolution.ats)
        if selected is None:
            return HandoffResult(
                errors=list(batch.errors),
                pages_fetched=batch.pages_fetched,
                reason=(
                    "Resolved authoritative posting was not returned by the bounded public ATS request."
                    if not batch.errors
                    else "Authoritative ATS request did not yield the resolved posting."
                ),
            )
        selected = copy.deepcopy(selected)
        provenance = {
            "discovered_via": candidate.aggregator_source,
            "aggregator_job_id": candidate.aggregator_job_id,
            "aggregator_url": candidate.aggregator_url,
            "external_url": candidate.external_url,
            "resolved_url": resolution.ats.canonical_url,
            "authoritative_source": resolution.ats.source,
            "authoritative_job_id": selected.source_job_id,
        }
        metadata = dict(selected.metadata)
        metadata["authoritative_source"] = resolution.ats.source
        metadata["discovered_via"] = list(
            dict.fromkeys(
                [*(_string_list(metadata.get("discovered_via"))), candidate.aggregator_source]
            )
        )
        metadata["_discovery_provenance"] = _merge_records(
            metadata.get("_discovery_provenance"), [provenance]
        )
        metadata["_discovery_tracks"] = list(
            dict.fromkeys(
                [*(_string_list(metadata.get("_discovery_tracks"))), *candidate.job_families]
            )
        )
        selected.metadata = metadata
        return HandoffResult(
            job=selected,
            errors=list(batch.errors),
            pages_fetched=batch.pages_fetched,
            reason="Authoritative full job description retrieved through existing ATS adapter.",
        )


def append_candidate_provenance(
    job: SourceJob, candidate: ExternalJobCandidate, resolution: CandidateResolution
) -> None:
    """Merge a second aggregator lead into one already-retrieved ATS job."""

    record = {
        "discovered_via": candidate.aggregator_source,
        "aggregator_job_id": candidate.aggregator_job_id,
        "aggregator_url": candidate.aggregator_url,
        "external_url": candidate.external_url,
        "resolved_url": resolution.canonical_external_url,
        "authoritative_source": resolution.authoritative_source,
        "authoritative_job_id": job.source_job_id,
    }
    metadata = dict(job.metadata)
    metadata["discovered_via"] = list(
        dict.fromkeys(
            [*(_string_list(metadata.get("discovered_via"))), candidate.aggregator_source]
        )
    )
    metadata["_discovery_provenance"] = _merge_records(
        metadata.get("_discovery_provenance"), [record]
    )
    metadata["_discovery_tracks"] = list(
        dict.fromkeys(
            [*(_string_list(metadata.get("_discovery_tracks"))), *candidate.job_families]
        )
    )
    job.metadata = metadata


def resolution_identity(resolution: CandidateResolution) -> str:
    if resolution.authoritative_source and resolution.authoritative_job_id:
        return f"native:{resolution.authoritative_source}:{resolution.authoritative_job_id}"
    if resolution.canonical_external_url:
        return f"url:{resolution.canonical_external_url}"
    return f"candidate:{resolution.candidate_id}"


def _target(
    source: str,
    board: str,
    company: str,
    title: str,
    tracks: list[str],
    *,
    max_pages: int = 1,
    **options: Any,
) -> DiscoveryTarget:
    if title and source in {"smartrecruiters", "workday"}:
        options.setdefault("server_query", title)
    return DiscoveryTarget(
        source=source,
        board=board,
        company=company,
        job_families=list(tracks),
        max_results=100,
        max_pages=max_pages,
        options=options,
    )


def _select_authoritative_job(
    jobs: list[SourceJob], resolution: SupportedAtsResolution
) -> SourceJob | None:
    wanted = resolution.native_job_id.casefold()
    for job in jobs:
        if str(job.source_job_id or "").strip().casefold() == wanted:
            return job
    canonical = resolution.canonical_url
    for job in jobs:
        values = {
            canonicalize_candidate_url(job.source_url),
            canonicalize_candidate_url(job.apply_url),
        }
        if canonical in values:
            return job
    return None


def _smartrecruiters_id(segment: str) -> str:
    match = re.match(r"^(\d{8,})(?:-|$)", segment)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9a-fA-F-]{20,}", segment):
        return segment
    return ""


def _workday_identifiers(parsed, segments: list[str]):
    job_index = next(
        (index for index, value in enumerate(segments) if value.casefold() == "job"),
        None,
    )
    if job_index is None or job_index < 1 or job_index >= len(segments) - 1:
        return None
    locale = segments[0] if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]) else "en-US"
    site_index = 1 if locale == segments[0] else 0
    if site_index >= job_index:
        return None
    career_site = segments[site_index]
    tenant = (parsed.hostname or "").split(".", 1)[0]
    external_path = "/" + "/".join(segments[job_index:])
    final = segments[-1]
    match = re.search(r"_([^_/?#]+)$", final)
    native_id = match.group(1) if match else final
    if not all((tenant, career_site, external_path, native_id)):
        return None
    return tenant, locale, career_site, external_path, native_id


def _is_aggregator_host(host: str) -> bool:
    return (
        host == "linkedin.com"
        or host.endswith(".linkedin.com")
        or host == "indeed.com"
        or host.endswith(".indeed.com")
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _merge_records(existing: Any, additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = [item for item in (existing or []) if isinstance(item, Mapping)]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*values, *additions]:
        record = dict(item)
        key = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


_REDIRECT_KEYS = {
    "url", "u", "target", "dest", "destination", "redirect", "redirect_url",
    "external_url", "externalurl", "apply_url", "applyurl",
}

_EXTRA_TRACKING_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer", "source",
    "trk", "trackingid", "refid", "lipi", "e_bp", "from", "campaignid",
}
