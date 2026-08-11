"""Private, provenance-aware candidate facts used for application preparation.

This module is deliberately separate from the Phase 1 matching profile.  It
does not infer application answers and it never mutates scoring inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


FACT_STATUSES = {
    "confirmed",
    "inferred_not_allowed",
    "unknown",
    "needs_confirmation",
    "deprecated",
}


class ApplicationProfileError(ValueError):
    """Raised when a private application profile cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class ApplicationFact:
    value: Any = None
    status: str = "unknown"
    source: str | list[str] = ""
    last_verified_at: str = ""
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ApplicationFact":
        return cls(
            value=value.get("value"),
            status=str(value.get("status", "unknown")),
            source=value.get("source", ""),
            last_verified_at=str(value.get("last_verified_at", "")),
            notes=str(value.get("notes", "")),
        )

    @property
    def usable(self) -> bool:
        return self.status == "confirmed" and self.value is not None and self.value != ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "status": self.status,
            "source": self.source,
            "last_verified_at": self.last_verified_at,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class ProfileConflict:
    fact_path: str
    sources: list[str] = field(default_factory=list)
    reason: str = ""
    status: str = "unresolved"


@dataclass(slots=True)
class CandidateApplicationProfile:
    schema_version: int
    identity: dict[str, Any]
    work_authorization: dict[str, Any]
    education: list[dict[str, Any]]
    employment_history: list[dict[str, Any]]
    links: dict[str, Any]
    job_preferences: dict[str, Any]
    application_policies: dict[str, Any]
    demographics_policy: dict[str, Any]
    conflicts: list[ProfileConflict]
    required_for_ready: list[str]
    raw: dict[str, Any] = field(repr=False)

    def get_fact(self, path: str) -> ApplicationFact | None:
        """Return a fact at a dotted mapping path; list indexing is not inferred."""

        current: Any = self.raw
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        if not isinstance(current, Mapping) or "status" not in current:
            return None
        return ApplicationFact.from_mapping(current)

    def has_unresolved_conflict(self, path: str) -> bool:
        return any(
            item.status == "unresolved"
            and (item.fact_path == path or item.fact_path.startswith(f"{path}."))
            for item in self.conflicts
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ApplicationProfileError(f"Could not read application profile {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApplicationProfileError(f"Invalid JSON in application profile {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ApplicationProfileError("Application profile root must be a JSON object.")
    return value


def load_application_profile(path: str | Path) -> CandidateApplicationProfile:
    source = Path(path)
    raw = _read_json(source)
    conflicts: list[ProfileConflict] = []
    for item in raw.get("conflicts", []):
        if isinstance(item, Mapping):
            conflicts.append(
                ProfileConflict(
                    fact_path=str(item.get("fact_path", "")),
                    sources=[str(value) for value in item.get("sources", [])],
                    reason=str(item.get("reason", "")),
                    status=str(item.get("status", "unresolved")),
                )
            )
    profile = CandidateApplicationProfile(
        schema_version=int(raw.get("schema_version", 1)),
        identity=dict(raw.get("identity", {})),
        work_authorization=dict(raw.get("work_authorization", {})),
        education=list(raw.get("education", [])),
        employment_history=list(raw.get("employment_history", [])),
        links=dict(raw.get("links", {})),
        job_preferences=dict(raw.get("job_preferences", {})),
        application_policies=dict(raw.get("application_policies", {})),
        demographics_policy=dict(raw.get("demographics_policy", {})),
        conflicts=conflicts,
        required_for_ready=[str(value) for value in raw.get("required_for_ready", [])],
        raw=raw,
    )
    errors = [issue for issue in validate_application_profile(profile) if issue[0] == "error"]
    if errors:
        details = "; ".join(f"{path}: {message}" for _, path, message in errors)
        raise ApplicationProfileError(details)
    return profile


def validate_application_profile(
    profile: CandidateApplicationProfile,
) -> list[tuple[str, str, str]]:
    """Return ``(severity, path, message)`` tuples without mutating the profile."""

    issues: list[tuple[str, str, str]] = []
    if profile.schema_version != 1:
        issues.append(("error", "schema_version", "Only schema_version 1 is supported."))

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            if "status" in value and "value" in value:
                status = str(value.get("status", ""))
                if status not in FACT_STATUSES:
                    issues.append(("error", path, f"Unsupported fact status: {status}"))
                if status == "confirmed":
                    fact_value = value.get("value")
                    if fact_value is None or fact_value == "":
                        issues.append(("error", path, "Confirmed facts must have a value."))
                    if not value.get("source"):
                        issues.append(("error", path, "Confirmed facts must include provenance."))
                return
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(profile.raw, "")
    for index, conflict in enumerate(profile.conflicts):
        if not conflict.fact_path:
            issues.append(("error", f"conflicts[{index}].fact_path", "Conflict path is required."))
        if conflict.status not in {"unresolved", "resolved"}:
            issues.append(("error", f"conflicts[{index}].status", "Conflict status is invalid."))
    return issues
