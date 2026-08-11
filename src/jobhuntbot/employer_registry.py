"""Private employer-registry parsing, validation, target generation, and health state."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .sources import DiscoveryTarget, SourceConfigurationError, supported_sources
from .sources.workday import validate_workday_target


class EmployerRegistryError(ValueError):
    """Raised when an employer registry cannot be loaded or used safely."""


@dataclass(slots=True)
class RegistryIssue:
    field: str
    message: str
    severity: str = "error"
    employer_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class EmployerRecord:
    employer_id: str
    name: str
    enabled: bool
    source: str
    ats: dict[str, Any]
    career_tracks: list[str] = field(default_factory=list)
    priority: int | None = 0
    geography: list[str] = field(default_factory=list)
    notes: str = ""
    validation: dict[str, Any] = field(default_factory=dict)
    index: int = 0


@dataclass(slots=True)
class EmployerRegistry:
    schema_version: int
    employers: list[EmployerRecord]
    defaults: dict[str, Any] = field(default_factory=dict)
    discovery_config: str = ""
    parse_issues: list[RegistryIssue] = field(default_factory=list)
    path: Path | None = None


@dataclass(slots=True)
class RegistryValidationReport:
    registry_path: str
    employer_count: int
    enabled_count: int
    issues: list[RegistryIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_path": self.registry_path,
            "valid": self.valid,
            "employer_count": self.employer_count,
            "enabled_count": self.enabled_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


HEALTH_STATUSES = {
    "healthy",
    "empty",
    "temporarily_failed",
    "invalid_config",
    "unsupported",
}


class EmployerHealthStore:
    """JSON-backed private health state keyed by stable employer_id."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict[str, Any]] = {}
        self._load()

    def record(
        self,
        *,
        employer_id: str,
        employer: str,
        source: str,
        status: str,
        checked_at: str,
        jobs_found: int = 0,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in HEALTH_STATUSES:
            raise EmployerRegistryError(f"Unknown employer health status '{status}'.")
        prior = self.entries.get(employer_id, {})
        value = {
            "employer_id": employer_id,
            "employer": employer,
            "source": source,
            "status": status,
            "last_checked": checked_at,
            "last_success": prior.get("last_success", ""),
            "last_error": error,
            "jobs_found": int(jobs_found),
        }
        if status in {"healthy", "empty"}:
            value["last_success"] = checked_at
        self.entries[employer_id] = value
        return dict(value)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {"schema_version": 1, "employers": self.entries},
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EmployerRegistryError(
                f"Could not load employer health state {self.path}: {exc}"
            ) from exc
        entries = value.get("employers") if isinstance(value, Mapping) else None
        if not isinstance(entries, Mapping):
            raise EmployerRegistryError(
                f"Employer health state {self.path} must contain an employers object."
            )
        self.entries = {
            str(key): dict(item)
            for key, item in entries.items()
            if isinstance(item, Mapping)
        }


def load_employer_registry(path: str | Path) -> EmployerRegistry:
    registry_path = Path(path)
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmployerRegistryError(
            f"Could not load employer registry {registry_path}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise EmployerRegistryError("Employer registry root must be an object.")
    employers_value = value.get("employers")
    if not isinstance(employers_value, list):
        raise EmployerRegistryError("Employer registry employers must be an array.")
    defaults = value.get("defaults", {})
    if not isinstance(defaults, Mapping):
        raise EmployerRegistryError("Employer registry defaults must be an object.")

    issues: list[RegistryIssue] = []
    employers: list[EmployerRecord] = []
    for index, item in enumerate(employers_value):
        prefix = f"employers[{index}]"
        if not isinstance(item, Mapping):
            issues.append(RegistryIssue(prefix, "Employer record must be an object."))
            continue
        employer_id = str(item.get("employer_id") or "").strip()
        ats_value = item.get("ats", {})
        if not isinstance(ats_value, Mapping):
            issues.append(
                RegistryIssue(f"{prefix}.ats", "ats must be an object.", employer_id=employer_id)
            )
            ats_value = {}
        tracks_value = item.get("career_tracks", [])
        if not isinstance(tracks_value, list):
            issues.append(
                RegistryIssue(
                    f"{prefix}.career_tracks",
                    "career_tracks must be an array.",
                    employer_id=employer_id,
                )
            )
            tracks_value = []
        geography_value = item.get("geography", [])
        if isinstance(geography_value, str):
            geography_value = [geography_value]
        elif not isinstance(geography_value, list):
            issues.append(
                RegistryIssue(
                    f"{prefix}.geography",
                    "geography must be a string or array.",
                    employer_id=employer_id,
                )
            )
            geography_value = []
        priority_value = item.get("priority", 0)
        priority: int | None
        if isinstance(priority_value, bool):
            priority = None
        else:
            try:
                priority = int(priority_value)
            except (TypeError, ValueError):
                priority = None
        validation = item.get("validation", {})
        if not isinstance(validation, Mapping):
            issues.append(
                RegistryIssue(
                    f"{prefix}.validation",
                    "validation must be an object.",
                    employer_id=employer_id,
                )
            )
            validation = {}
        employers.append(
            EmployerRecord(
                employer_id=employer_id,
                name=str(item.get("name") or "").strip(),
                enabled=item.get("enabled", True) is not False,
                source=str(item.get("source") or "").strip().casefold(),
                ats=dict(ats_value),
                career_tracks=[str(value).strip() for value in tracks_value if str(value).strip()],
                priority=priority,
                geography=[str(value).strip() for value in geography_value if str(value).strip()],
                notes=str(item.get("notes") or "").strip(),
                validation=dict(validation),
                index=index,
            )
        )
    schema_version = value.get("schema_version", 1)
    if isinstance(schema_version, bool):
        parsed_version = 0
    else:
        try:
            parsed_version = int(schema_version)
        except (TypeError, ValueError):
            parsed_version = 0
    return EmployerRegistry(
        schema_version=parsed_version,
        employers=employers,
        defaults=dict(defaults),
        discovery_config=str(value.get("discovery_config") or "").strip(),
        parse_issues=issues,
        path=registry_path,
    )


def validate_employer_registry(
    registry: EmployerRegistry,
    *,
    known_tracks: Sequence[str] | None = None,
) -> RegistryValidationReport:
    issues = list(registry.parse_issues)
    if registry.schema_version != 1:
        issues.append(RegistryIssue("schema_version", "schema_version must be 1."))
    known = {str(value).strip() for value in (known_tracks or []) if str(value).strip()}
    seen: set[str] = set()
    for record in registry.employers:
        prefix = f"employers[{record.index}]"
        if not record.employer_id:
            issues.append(RegistryIssue(f"{prefix}.employer_id", "employer_id is required."))
        else:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", record.employer_id):
                issues.append(
                    RegistryIssue(
                        f"{prefix}.employer_id",
                        "employer_id must use lowercase letters, numbers, dots, underscores, or hyphens.",
                        employer_id=record.employer_id,
                    )
                )
            if record.employer_id in seen:
                issues.append(
                    RegistryIssue(
                        f"{prefix}.employer_id",
                        "employer_id must be unique.",
                        employer_id=record.employer_id,
                    )
                )
        if record.employer_id:
            seen.add(record.employer_id)
        if not record.name:
            issues.append(
                RegistryIssue(f"{prefix}.name", "name is required.", employer_id=record.employer_id)
            )
        if record.source not in supported_sources():
            issues.append(
                RegistryIssue(
                    f"{prefix}.source",
                    f"Unsupported ATS source '{record.source}'.",
                    employer_id=record.employer_id,
                )
            )
        if record.priority is None or record.priority < 0:
            issues.append(
                RegistryIssue(
                    f"{prefix}.priority",
                    "priority must be a non-negative integer.",
                    employer_id=record.employer_id,
                )
            )
        if not record.career_tracks:
            issues.append(
                RegistryIssue(
                    f"{prefix}.career_tracks",
                    "At least one career track is required.",
                    employer_id=record.employer_id,
                )
            )
        if known:
            unknown = sorted(set(record.career_tracks) - known)
            if unknown:
                issues.append(
                    RegistryIssue(
                        f"{prefix}.career_tracks",
                        "Unknown career tracks: " + ", ".join(unknown),
                        employer_id=record.employer_id,
                    )
                )
        if record.source == "workday":
            try:
                validate_workday_target(_record_target(record, registry.defaults))
            except SourceConfigurationError as exc:
                issues.append(
                    RegistryIssue(
                        f"{prefix}.ats",
                        str(exc),
                        employer_id=record.employer_id,
                    )
                )
        elif record.source in supported_sources() and not str(record.ats.get("board") or "").strip():
            issues.append(
                RegistryIssue(
                    f"{prefix}.ats.board",
                    f"{record.source} requires a board identifier.",
                    employer_id=record.employer_id,
                )
            )
    return RegistryValidationReport(
        registry_path=str(registry.path or ""),
        employer_count=len(registry.employers),
        enabled_count=sum(record.enabled for record in registry.employers),
        issues=issues,
    )


def generate_registry_targets(
    registry: EmployerRegistry,
    *,
    known_tracks: Sequence[str] | None = None,
    tracks: Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
    minimum_priority: int | None = None,
    max_employers: int | None = None,
) -> list[DiscoveryTarget]:
    report = validate_employer_registry(registry, known_tracks=known_tracks)
    if not report.valid:
        details = "; ".join(f"{issue.field}: {issue.message}" for issue in report.issues)
        raise EmployerRegistryError("Employer registry is invalid: " + details)
    selected_tracks = {str(value).strip() for value in (tracks or []) if str(value).strip()}
    selected_sources = {
        str(value).strip().casefold() for value in (sources or []) if str(value).strip()
    }
    if selected_tracks and known_tracks:
        unknown_tracks = selected_tracks - set(known_tracks)
        if unknown_tracks:
            raise EmployerRegistryError(
                "Unknown selected career tracks: " + ", ".join(sorted(unknown_tracks))
            )
    if selected_sources - set(supported_sources()):
        raise EmployerRegistryError(
            "Unsupported selected sources: "
            + ", ".join(sorted(selected_sources - set(supported_sources())))
        )
    if minimum_priority is not None and minimum_priority < 0:
        raise EmployerRegistryError("minimum_priority cannot be negative.")
    if max_employers is not None and max_employers < 1:
        raise EmployerRegistryError("max_employers must be positive.")

    records = [
        record
        for record in registry.employers
        if record.enabled
        and (not selected_tracks or bool(set(record.career_tracks) & selected_tracks))
        and (not selected_sources or record.source in selected_sources)
        and (minimum_priority is None or (record.priority or 0) >= minimum_priority)
    ]
    records.sort(key=lambda record: (-(record.priority or 0), record.employer_id))
    if max_employers is not None:
        records = records[:max_employers]
    return [_record_target(record, registry.defaults) for record in records]


def resolve_registry_discovery_config(registry: EmployerRegistry) -> Path | None:
    if not registry.discovery_config:
        return None
    path = Path(registry.discovery_config)
    if path.is_absolute() or registry.path is None:
        return path
    return registry.path.parent / path


def _record_target(record: EmployerRecord, defaults: Mapping[str, Any]) -> DiscoveryTarget:
    allowed_defaults = {
        "search_keywords",
        "location_filter",
        "employment_types",
        "max_results",
        "max_pages",
        "posted_within_days",
    }
    value = {key: item for key, item in defaults.items() if key in allowed_defaults}
    value.update(record.ats)
    value.update(
        {
            "source": record.source,
            "board": str(record.ats.get("board") or record.ats.get("tenant") or "").strip(),
            "company": record.name,
            "enabled": record.enabled,
            "job_families": list(record.career_tracks),
            "employer_id": record.employer_id,
            "registry_priority": record.priority,
            "registry_geography": list(record.geography),
        }
    )
    try:
        return DiscoveryTarget.from_mapping(value)
    except SourceConfigurationError as exc:
        raise EmployerRegistryError(
            f"Employer '{record.employer_id}' cannot generate a discovery target: {exc}"
        ) from exc


def target_employer_id(target: DiscoveryTarget) -> str:
    configured = str(target.options.get("employer_id") or "").strip()
    return configured or f"{target.source}:{target.board}"


def target_health_status(errors: Sequence[Any], job_count: int) -> tuple[str, str]:
    reasons = [str(getattr(item, "reason", "") or "") for item in errors]
    error_text = "; ".join(reason for reason in reasons if reason)
    types = {str(getattr(item, "error_type", "") or "") for item in errors}
    if job_count > 0:
        return "healthy", error_text
    if not errors:
        return "empty", ""
    if "SourceConfigurationError" in types:
        return "invalid_config", error_text
    if any("Unsupported" in value for value in types):
        return "unsupported", error_text
    return "temporarily_failed", error_text
