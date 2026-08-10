"""Deterministic normalization helpers and stable job identity."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import NormalizedJob, ValidationIssue, ValidationResult


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "source",
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def normalize_text_key(value: str) -> str:
    value = normalize_space(value).casefold()
    value = re.sub(r"[^\w+#.]+", " ", value, flags=re.UNICODE)
    return normalize_space(value)


def normalize_skill(value: str, aliases: dict[str, str] | None = None) -> str:
    key = normalize_text_key(value)
    alias_map = {normalize_text_key(k): normalize_text_key(v) for k, v in (aliases or {}).items()}
    return alias_map.get(key, key)


def normalize_skills(values: list[str], aliases: dict[str, str] | None = None) -> list[str]:
    normalized = [normalize_skill(value, aliases) for value in values]
    return list(dict.fromkeys(value for value in normalized if value))


def canonicalize_url(value: str) -> str:
    """Remove fragments/tracking noise while preserving job-identifying query data."""

    value = normalize_space(value)
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc:
        return value
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        query.append((key, item))
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    path = re.sub(r"/{2,}", "/", parts.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(query), ""))


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text_key(value)).strip("-")
    return slug[:30] or "source"


def build_job_id(
    *,
    source: str,
    source_native_id: str = "",
    apply_url: str = "",
    source_url: str = "",
    company: str,
    title: str,
    location: str,
) -> str:
    """Build a repeatable ID using native ID, canonical URL, then fingerprint."""

    source_key = _slug(source)
    native_key = normalize_space(source_native_id)
    if native_key:
        digest = hashlib.sha256(f"{source_key}|{native_key}".encode("utf-8")).hexdigest()[:20]
        return f"native_{source_key}_{digest}"

    canonical_url = canonicalize_url(apply_url) or canonicalize_url(source_url)
    if canonical_url:
        digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]
        return f"url_{digest}"

    fingerprint = "|".join(
        normalize_text_key(part) for part in (company, title, location, source)
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
    return f"fp_{digest}"


def validate_normalized_job(job: NormalizedJob) -> ValidationResult:
    """Validate the normalized contract without filling absent source facts."""

    issues: list[ValidationIssue] = []

    def add(severity: str, field_name: str, code: str, message: str) -> None:
        issues.append(ValidationIssue(severity, field_name, code, message))

    for field_name, value in (
        ("job_id", job.job_id),
        ("title", job.title),
        ("company", job.company),
        ("source", job.source),
        ("job_description", job.job_description),
        ("discovered_date", job.discovered_date),
    ):
        if not str(value or "").strip():
            add("error", field_name, f"missing_{field_name}", f"Normalized job requires {field_name}.")
    if not job.location and not job.remote_policy:
        add("warning", "location", "missing_job_location", "Location fit will be unknown.")
    if not job.source_url and not job.apply_url and not job.source_native_id:
        add("warning", "source_url", "missing_source_reference", "No native source ID or URL was supplied; the fallback fingerprint is used.")
    return ValidationResult(issues)
