"""Deterministic duplicate detection for jobs discovered from public ATS feeds."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any

from .normalization import canonicalize_url, normalize_text_key
from .sources import SourceJob


@dataclass(slots=True)
class DuplicateRecord:
    kept_source: str
    kept_source_job_id: str
    duplicate_source: str
    duplicate_source_job_id: str
    reason: str
    identity: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class DeduplicationResult:
    jobs: list[SourceJob] = field(default_factory=list)
    duplicates: list[DuplicateRecord] = field(default_factory=list)


def deduplicate_jobs(jobs: list[SourceJob]) -> DeduplicationResult:
    """Keep the first job for each stable identity and explain every merge."""

    result = DeduplicationResult()
    identities: dict[str, int] = {}
    for job in jobs:
        keys = _identity_keys(job)
        match = next(((key, identities[key]) for key in keys if key in identities), None)
        if match is None:
            index = len(result.jobs)
            result.jobs.append(job)
            for key in keys:
                identities.setdefault(key, index)
            continue

        identity, kept_index = match
        kept = result.jobs[kept_index]
        _merge_missing_fields(kept, job)
        for key in keys:
            identities.setdefault(key, kept_index)
        result.duplicates.append(
            DuplicateRecord(
                kept_source=kept.source,
                kept_source_job_id=kept.source_job_id,
                duplicate_source=job.source,
                duplicate_source_job_id=job.source_job_id,
                reason=_reason(identity),
                identity=identity,
            )
        )
    return result


def _identity_keys(job: SourceJob) -> list[str]:
    keys: list[str] = []
    source = normalize_text_key(job.source)
    native_id = str(job.source_job_id or "").strip()
    if source and native_id:
        keys.append(f"native:{source}:{native_id}")
    for value in (job.apply_url, job.source_url):
        canonical = canonicalize_url(value)
        if canonical:
            keys.append(f"url:{canonical}")
    fingerprint_parts = (
        normalize_text_key(job.company),
        normalize_text_key(job.title),
        normalize_text_key(job.location),
    )
    if not keys and fingerprint_parts[0] and fingerprint_parts[1]:
        keys.append("fingerprint:" + "|".join(fingerprint_parts))
    return list(dict.fromkeys(keys))


def _reason(identity: str) -> str:
    if identity.startswith("native:"):
        return "same source and native job id"
    if identity.startswith("url:"):
        return "same canonical job/apply URL after tracking-parameter removal"
    return "same normalized company, title, and location fingerprint"


def _merge_missing_fields(kept: SourceJob, duplicate: SourceJob) -> None:
    for field_name in (
        "company",
        "title",
        "description",
        "location",
        "work_mode",
        "employment_type",
        "posted_date",
        "updated_date",
        "source_url",
        "apply_url",
    ):
        if not getattr(kept, field_name) and getattr(duplicate, field_name):
            setattr(kept, field_name, getattr(duplicate, field_name))
    if kept.salary is None and duplicate.salary is not None:
        kept.salary = duplicate.salary
    metadata: dict[str, Any] = dict(kept.metadata)
    kept_tracks = metadata.get("_discovery_tracks", [])
    duplicate_tracks = duplicate.metadata.get("_discovery_tracks", [])
    if isinstance(kept_tracks, list) and isinstance(duplicate_tracks, list):
        metadata["_discovery_tracks"] = list(
            dict.fromkeys([*kept_tracks, *duplicate_tracks])
        )
    for key in ("discovered_via",):
        kept_values = metadata.get(key, [])
        duplicate_values = duplicate.metadata.get(key, [])
        if isinstance(kept_values, str):
            kept_values = [kept_values]
        if isinstance(duplicate_values, str):
            duplicate_values = [duplicate_values]
        if isinstance(kept_values, list) and isinstance(duplicate_values, list):
            metadata[key] = list(dict.fromkeys([*kept_values, *duplicate_values]))
    kept_provenance = metadata.get("_discovery_provenance", [])
    duplicate_provenance = duplicate.metadata.get("_discovery_provenance", [])
    if isinstance(kept_provenance, list) and isinstance(duplicate_provenance, list):
        merged_provenance: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in [*kept_provenance, *duplicate_provenance]:
            if not isinstance(item, dict):
                continue
            identity = json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged_provenance.append(dict(item))
        metadata["_discovery_provenance"] = merged_provenance
    if not metadata.get("authoritative_source") and duplicate.metadata.get(
        "authoritative_source"
    ):
        metadata["authoritative_source"] = duplicate.metadata["authoritative_source"]
    duplicate_sources = list(metadata.get("duplicate_sources") or [])
    duplicate_sources.append(
        {
            "source": duplicate.source,
            "source_job_id": duplicate.source_job_id,
            "source_url": duplicate.source_url,
        }
    )
    metadata["duplicate_sources"] = duplicate_sources
    kept.metadata = metadata
