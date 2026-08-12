"""Material-state fingerprints used to bind and expire approval."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def package_fingerprint(package: Mapping[str, Any]) -> str:
    material = {
        key: package.get(key)
        for key in (
            "application_id", "job_snapshot", "selected_resume", "candidate_facts",
            "prepared_answers", "job_dependent_answers", "unresolved_questions",
            "optional_documents", "cover_letter_status", "package_readiness", "blockers",
            "updated_at",
        )
    }
    return canonical_hash(material)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_origin(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    port = parsed.port
    default = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    netloc = host if not port or default else f"{host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, "", "", ""))
