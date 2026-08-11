"""Compliant aggregator discovery inputs for LinkedIn and Indeed.

Aggregator records are leads, not authoritative job descriptions.  This
module intentionally supports only ordinary public HTTP pages and private
structured inputs supplied by the user.  It contains no login, CAPTCHA,
anti-bot bypass, browser automation, or application behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from .normalization import canonicalize_url
from .sources.base import html_to_text, normalize_posted_date


AGGREGATOR_SOURCES = ("indeed", "linkedin")


class AggregatorConfigError(ValueError):
    """Raised when a private aggregator configuration is invalid."""


class AggregatorRequestError(RuntimeError):
    """Raised when an ordinary public aggregator request cannot be completed."""

    def __init__(self, url: str, reason: str, *, status_code: int | None = None):
        super().__init__(reason)
        self.url = url
        self.reason = reason
        self.status_code = status_code


@dataclass(slots=True)
class ExternalJobCandidate:
    aggregator_source: str
    aggregator_job_id: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    posted_date: str | None = None
    aggregator_url: str | None = None
    external_url: str | None = None
    excerpt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    job_families: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def identity(self) -> str:
        if self.aggregator_job_id:
            seed = f"{self.aggregator_source}|{self.aggregator_job_id}"
        elif self.aggregator_url:
            seed = canonicalize_url(self.aggregator_url)
        else:
            seed = "|".join(
                str(item or "")
                for item in (self.aggregator_source, self.company, self.title, self.location)
            )
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
        return f"{self.aggregator_source}_{digest}"


@dataclass(slots=True)
class AggregatorFailure:
    source: str
    status: str
    url: str = ""
    error_type: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class AggregatorBatch:
    source: str
    status: str = "success"
    candidates: list[ExternalJobCandidate] = field(default_factory=list)
    errors: list[AggregatorFailure] = field(default_factory=list)
    access_method: str = ""
    requests_made: int = 0


@dataclass(slots=True)
class AggregatorTarget:
    source: str
    enabled: bool = True
    search_url: str = ""
    input_file: Path | None = None
    job_families: list[str] = field(default_factory=list)
    search_keywords: list[str] = field(default_factory=list)
    location_filter: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    max_results: int = 10
    posted_within_days: int | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AggregatorConfig:
    schema_version: int = 1
    targets: list[AggregatorTarget] = field(default_factory=list)


class TextHttpClient(Protocol):
    def get_text(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> str:
        ...


class PoliteTextClient:
    """Bounded public HTML client with no cookies, authentication, or evasion."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        retries: int = 1,
        min_interval_seconds: float = 0.5,
        user_agent: str = "JobHuntBot/0.2 (local public-job discovery)",
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if retries < 0:
            raise ValueError("retries cannot be negative.")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative.")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.min_interval_seconds = min_interval_seconds
        self.user_agent = user_agent
        self._last_request_at: float | None = None

    def get_text(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> str:
        request_headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": self.user_agent,
        }
        request_headers.update(dict(headers or {}))
        last_reason = ""
        for attempt in range(self.retries + 1):
            self._pace()
            try:
                request = Request(url, headers=request_headers, method="GET")
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                self._last_request_at = time.monotonic()
                return payload.decode(charset, errors="replace")
            except HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_reason = f"HTTP {exc.code}: {exc.reason}"
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.retries:
                    raise AggregatorRequestError(
                        url, last_reason, status_code=exc.code
                    ) from exc
            except (URLError, TimeoutError) as exc:
                self._last_request_at = time.monotonic()
                last_reason = str(getattr(exc, "reason", exc))
                if attempt >= self.retries:
                    raise AggregatorRequestError(url, last_reason) from exc
            if attempt < self.retries:
                time.sleep(min(2**attempt, 4))
        raise AggregatorRequestError(url, last_reason or "Request failed.")

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)


def load_aggregator_config(path: str | Path) -> AggregatorConfig:
    """Load private aggregator targets without embedding search terms in code."""

    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AggregatorConfigError(
            f"Could not load aggregator configuration {config_path}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise AggregatorConfigError("Aggregator configuration root must be an object.")
    targets_value = value.get("targets", [])
    if not isinstance(targets_value, list):
        raise AggregatorConfigError("targets must be an array.")

    targets: list[AggregatorTarget] = []
    for index, item in enumerate(targets_value):
        if not isinstance(item, Mapping):
            raise AggregatorConfigError(f"targets[{index}] must be an object.")
        source = str(item.get("source") or "").strip().casefold()
        if source not in AGGREGATOR_SOURCES:
            raise AggregatorConfigError(
                f"targets[{index}] uses unsupported aggregator source '{source}'."
            )
        search_url = str(item.get("search_url") or "").strip()
        input_value = str(item.get("input_file") or "").strip()
        input_file = None
        if input_value:
            candidate_path = Path(input_value)
            input_file = (
                candidate_path
                if candidate_path.is_absolute()
                else (config_path.parent / candidate_path).resolve()
            )
        enabled = item.get("enabled", True) is not False
        if enabled and bool(search_url) == bool(input_file):
            raise AggregatorConfigError(
                f"targets[{index}] requires exactly one of search_url or input_file."
            )
        if enabled and search_url:
            parsed_url = urlsplit(search_url)
            if (
                parsed_url.scheme != "https"
                or not parsed_url.netloc
                or parsed_url.username
                or parsed_url.password
            ):
                raise AggregatorConfigError(
                    f"targets[{index}].search_url must be a public HTTPS URL without credentials."
                )
        known = {
            "source", "enabled", "search_url", "input_file", "job_families",
            "search_keywords", "location_filter", "employment_types",
            "max_results", "posted_within_days",
        }
        targets.append(
            AggregatorTarget(
                source=source,
                enabled=enabled,
                search_url=search_url,
                input_file=input_file,
                job_families=_strings(item.get("job_families"), f"targets[{index}].job_families"),
                search_keywords=_strings(item.get("search_keywords"), f"targets[{index}].search_keywords"),
                location_filter=_strings(item.get("location_filter"), f"targets[{index}].location_filter"),
                employment_types=_strings(item.get("employment_types"), f"targets[{index}].employment_types"),
                max_results=_positive_int(item.get("max_results", 10), f"targets[{index}].max_results"),
                posted_within_days=_optional_positive_int(
                    item.get("posted_within_days"), f"targets[{index}].posted_within_days"
                ),
                options={key: entry for key, entry in item.items() if key not in known},
            )
        )
    return AggregatorConfig(
        schema_version=_positive_int(value.get("schema_version", 1), "schema_version"),
        targets=targets,
    )


class AggregatorAdapter:
    """Normalize one configured public page or private structured candidate feed."""

    def __init__(self, client: TextHttpClient):
        self.client = client

    def fetch(self, target: AggregatorTarget, *, limit: int | None = None) -> AggregatorBatch:
        maximum = min(target.max_results, limit) if limit is not None else target.max_results
        if target.input_file is not None:
            return self._fetch_file(target, maximum)
        return self._fetch_public(target, maximum)

    def _fetch_file(self, target: AggregatorTarget, maximum: int) -> AggregatorBatch:
        batch = AggregatorBatch(
            source=target.source, access_method="private structured candidate input"
        )
        try:
            records = _read_records(target.input_file)
            batch.candidates = [
                normalize_candidate(target.source, item, target.job_families)
                for item in records[:maximum]
            ]
            batch.status = "success" if batch.candidates else "empty"
        except (OSError, json.JSONDecodeError, AggregatorConfigError) as exc:
            batch.status = "invalid_config"
            batch.errors.append(
                AggregatorFailure(
                    source=target.source,
                    status=batch.status,
                    url=str(target.input_file),
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
            )
        return batch

    def _fetch_public(self, target: AggregatorTarget, maximum: int) -> AggregatorBatch:
        batch = AggregatorBatch(
            source=target.source, access_method="ordinary public HTML request"
        )
        try:
            html = self.client.get_text(target.search_url)
            batch.requests_made = 1
            candidates = parse_public_candidates(
                target.source, html, target.search_url, target.job_families
            )
            batch.candidates = candidates[:maximum]
            if batch.candidates:
                batch.status = "success"
            elif _looks_blocked(html):
                batch.status = "blocked_or_unavailable"
                batch.errors.append(
                    AggregatorFailure(
                        source=target.source,
                        status=batch.status,
                        url=target.search_url,
                        error_type="PublicAccessBlocked",
                        reason="Public response contained an authentication, CAPTCHA, or access-block page.",
                    )
                )
            else:
                batch.status = "empty"
        except AggregatorRequestError as exc:
            batch.requests_made = 1
            batch.status = (
                "blocked_or_unavailable"
                if exc.status_code in {401, 403, 429}
                else "temporarily_failed"
            )
            batch.errors.append(
                AggregatorFailure(
                    source=target.source,
                    status=batch.status,
                    url=exc.url,
                    error_type=type(exc).__name__,
                    reason=exc.reason,
                )
            )
        return batch


def normalize_candidate(
    source: str,
    value: Mapping[str, Any],
    job_families: Sequence[str] = (),
) -> ExternalJobCandidate:
    """Normalize common LinkedIn/Indeed result fields without inventing values."""

    if not isinstance(value, Mapping):
        raise AggregatorConfigError("Aggregator candidate must be an object.")
    source_key = str(source or "").strip().casefold()
    aliases = _ALIASES[source_key]
    posted_raw = _first(value, aliases["posted_date"])
    posted = normalize_posted_date(posted_raw)
    excerpt = html_to_text(_first(value, aliases["excerpt"]))
    if len(excerpt) > 1000:
        excerpt = excerpt[:997].rstrip() + "..."
    metadata = value.get("metadata")
    return ExternalJobCandidate(
        aggregator_source=source_key,
        aggregator_job_id=_optional_text(_first(value, aliases["job_id"])),
        title=_optional_text(_first(value, aliases["title"])),
        company=_optional_text(_first(value, aliases["company"])),
        location=_optional_text(_first(value, aliases["location"])),
        posted_date=posted or None,
        aggregator_url=_optional_url(_first(value, aliases["aggregator_url"])),
        external_url=_optional_url(_first(value, aliases["external_url"])),
        excerpt=excerpt or None,
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        job_families=list(dict.fromkeys(str(item).strip() for item in job_families if str(item).strip())),
    )


def parse_public_candidates(
    source: str, html: str, base_url: str, job_families: Sequence[str] = ()
) -> list[ExternalJobCandidate]:
    """Parse only structured/publicly rendered candidate facts from one page."""

    parser = _PublicJobParser(source, base_url)
    parser.feed(html)
    parser.close()
    records = [*parser.cards, *_json_ld_records(parser.scripts, base_url)]
    candidates: list[ExternalJobCandidate] = []
    seen: set[str] = set()
    for record in records:
        candidate = normalize_candidate(source, record, job_families)
        if candidate.identity in seen:
            continue
        seen.add(candidate.identity)
        candidates.append(candidate)
    return candidates


_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "linkedin": {
        "job_id": ("aggregator_job_id", "job_id", "id", "entityUrn", "entity_urn"),
        "title": ("title", "jobTitle"),
        "company": ("company", "companyName", "company_name"),
        "location": ("location", "formattedLocation", "formatted_location"),
        "posted_date": ("posted_date", "datePosted", "listedAt", "listed_at"),
        "aggregator_url": ("aggregator_url", "jobPostingUrl", "job_url", "url"),
        "external_url": ("external_url", "externalApplyUrl", "applyUrl", "apply_url"),
        "excerpt": ("excerpt", "descriptionSnippet", "snippet", "description_snippet"),
    },
    "indeed": {
        "job_id": ("aggregator_job_id", "jobkey", "jobKey", "job_id", "id"),
        "title": ("title", "jobTitle"),
        "company": ("company", "companyName", "company_name"),
        "location": ("location", "formattedLocation", "formatted_location"),
        "posted_date": ("posted_date", "datePosted", "pubDate", "listedAt"),
        "aggregator_url": ("aggregator_url", "viewJobLink", "job_url", "url"),
        "external_url": ("external_url", "externalApplyUrl", "applyUrl", "apply_url"),
        "excerpt": ("excerpt", "snippet", "descriptionSnippet", "description_snippet"),
    },
}


class _PublicJobParser(HTMLParser):
    def __init__(self, source: str, base_url: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.base_url = base_url
        self.cards: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self._script = False
        self._script_parts: list[str] = []
        self._card: dict[str, Any] | None = None
        self._card_depth = 0
        self._capture: str | None = None
        self._capture_tag = ""
        self._capture_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key): str(value or "") for key, value in attrs}
        classes = values.get("class", "")
        if tag == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._script = True
            self._script_parts = []
        is_linkedin_card = self.source == "linkedin" and "base-search-card" in classes
        is_indeed_card = self.source == "indeed" and (
            "job_seen_beacon" in classes or "resultContent" in classes
        )
        if self._card is None and (is_linkedin_card or is_indeed_card):
            self._card = {}
            self._card_depth = 1
            entity = values.get("data-entity-urn", "")
            if entity:
                self._card["job_id"] = entity.rsplit(":", 1)[-1]
        elif self._card is not None and tag == "div":
            self._card_depth += 1

        if self._card is None:
            return
        if self.source == "linkedin":
            if tag == "a" and "base-card__full-link" in classes:
                self._card["aggregator_url"] = urljoin(self.base_url, values.get("href", ""))
            if tag == "h3" and "base-search-card__title" in classes:
                self._begin_capture("title", tag)
            elif tag == "h4" and "base-search-card__subtitle" in classes:
                self._begin_capture("company", tag)
            elif tag == "span" and "job-search-card__location" in classes:
                self._begin_capture("location", tag)
            elif tag == "time":
                if values.get("datetime"):
                    self._card["posted_date"] = values["datetime"]
        else:
            if tag == "a" and "jcs-JobTitle" in classes:
                self._card["aggregator_url"] = urljoin(self.base_url, values.get("href", ""))
                identifier = values.get("data-jk") or values.get("id", "").removeprefix("job_")
                if identifier:
                    self._card["jobkey"] = identifier
                self._begin_capture("title", tag)
            elif values.get("data-testid") == "company-name":
                self._begin_capture("company", tag)
            elif values.get("data-testid") == "text-location":
                self._begin_capture("location", tag)

    def handle_endtag(self, tag: str) -> None:
        if self._script and tag == "script":
            self.scripts.append("".join(self._script_parts))
            self._script = False
        if self._capture == tag or (self._capture is not None and tag == self._capture_tag):
            if self._card is not None:
                text = " ".join("".join(self._capture_parts).split())
                if text:
                    self._card[self._capture] = text
            self._capture = None
            self._capture_tag = ""
            self._capture_parts = []
        if self._card is not None and tag == "div":
            self._card_depth -= 1
            if self._card_depth == 0:
                if self._card.get("title") or self._card.get("aggregator_url"):
                    self.cards.append(self._card)
                self._card = None

    def handle_data(self, data: str) -> None:
        if self._script:
            self._script_parts.append(data)
        if self._capture is not None:
            self._capture_parts.append(data)

    def _begin_capture(self, field_name: str, tag: str) -> None:
        self._capture = field_name
        self._capture_tag = tag
        self._capture_parts = []


def _json_ld_records(scripts: Sequence[str], base_url: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for text in scripts:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        for item in _walk_json(value):
            if not isinstance(item, Mapping) or str(item.get("@type") or "").casefold() != "jobposting":
                continue
            organization = item.get("hiringOrganization")
            company = organization.get("name") if isinstance(organization, Mapping) else None
            location = _json_ld_location(item.get("jobLocation"))
            url = _optional_url(item.get("url"))
            identifier = item.get("identifier")
            if isinstance(identifier, Mapping):
                identifier = identifier.get("value") or identifier.get("name")
            records.append(
                {
                    "job_id": identifier,
                    "title": item.get("title"),
                    "company": company,
                    "location": location,
                    "datePosted": item.get("datePosted"),
                    "aggregator_url": urljoin(base_url, url) if url else base_url,
                    # schema.org directApply is a boolean, not an employer URL.
                    "external_url": None,
                    "excerpt": item.get("description"),
                    "metadata": {"input_format": "json-ld"},
                }
            )
    return records


def _walk_json(value: Any):
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _json_ld_location(value: Any) -> str | None:
    if isinstance(value, list):
        return _json_ld_location(value[0]) if value else None
    if not isinstance(value, Mapping):
        return _optional_text(value)
    address = value.get("address", value)
    if not isinstance(address, Mapping):
        return _optional_text(address)
    parts = [
        _optional_text(address.get("addressLocality")),
        _optional_text(address.get("addressRegion")),
        _optional_text(address.get("addressCountry")),
    ]
    text = ", ".join(item for item in parts if item)
    return text or None


def _read_records(path: Path) -> list[Mapping[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.casefold() == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        value = json.loads(text)
        if isinstance(value, Mapping):
            value = value.get("jobs", value.get("candidates"))
        values = value
    if not isinstance(values, list) or not all(isinstance(item, Mapping) for item in values):
        raise AggregatorConfigError("Aggregator input must be an array of candidate objects.")
    return list(values)


def _looks_blocked(value: str) -> bool:
    text = value.casefold()
    markers = (
        "captcha", "verify you are human", "unusual traffic", "access denied",
        "authwall", "sign in to continue", "security verification",
    )
    return any(marker in text for marker in markers)


def _first(value: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if value.get(key) not in (None, ""):
            return value[key]
    return None


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _optional_url(value: Any) -> str | None:
    if isinstance(value, bool) or isinstance(value, (Mapping, list)):
        return None
    text = _optional_text(value)
    return text or None


def _strings(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AggregatorConfigError(f"{field_name} must be an array.")
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise AggregatorConfigError(f"{field_name} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AggregatorConfigError(f"{field_name} must be a positive integer.") from exc
    if parsed < 1:
        raise AggregatorConfigError(f"{field_name} must be positive.")
    return parsed


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, field_name)
