"""Value-minimizing plans and results for non-submitting controlled form fill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


FILL_ACTIONS = {"FILL", "SKIP_OPTIONAL", "WAIT_FOR_USER", "MANUAL_ONLY", "BLOCKED", "UNRESOLVED"}
FILL_STATUSES = {
    "PREVIEW_READY",
    "FILLED_SAFE_FIELDS",
    "NEEDS_USER_INPUT",
    "BLOCKED_BY_CAPTCHA",
    "FORM_CHANGED_REVIEW_REQUIRED",
    "LIVE_FILL_UNSAFE",
    "FAILED",
}


@dataclass(frozen=True, slots=True)
class ControlledFillPolicy:
    execute_fill: bool = False
    allow_partial_fill: bool = False
    allow_file_upload: bool = False
    timeout_ms: int = 20_000
    headless: bool = True

    def __post_init__(self) -> None:
        if self.timeout_ms < 1_000 or self.timeout_ms > 120_000:
            raise ValueError("Fill timeout must be between 1,000 and 120,000 ms.")
        if self.allow_file_upload and self.execute_fill:
            raise ValueError("Live file upload is disabled in Phase 3.3.")


@dataclass(slots=True)
class ControlledFillFieldPlan:
    field_id: str
    canonical_id: str
    action: str
    reason: str
    field_type: str
    locator: str
    required: bool
    mapping_confidence: str
    safety_class: str
    source_reference: str = ""
    value_kind: str = ""
    intended_option: str = ""
    resolved_value: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.action not in FILL_ACTIONS:
            raise ValueError(f"Unsupported controlled-fill action: {self.action}")

    def to_dict(self) -> dict[str, Any]:
        """Serialize no candidate answer values."""

        value = asdict(self)
        value.pop("resolved_value", None)
        return value


@dataclass(slots=True)
class ControlledFillPlan:
    application_id: str
    form_fingerprint: str
    generated_at: str
    overall_status: str
    fields: list[ControlledFillFieldPlan]
    total_fields: int
    fill_fields: int
    skipped_fields: int
    wait_for_user_fields: int
    manual_only_fields: int
    blocked_fields: int
    unresolved_fields: int
    required_not_fillable: int
    intended_resume: dict[str, Any]
    blockers: list[str]
    safety_flags: list[str]

    def __post_init__(self) -> None:
        if self.overall_status not in FILL_STATUSES:
            raise ValueError(f"Unsupported controlled-fill status: {self.overall_status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "form_fingerprint": self.form_fingerprint,
            "generated_at": self.generated_at,
            "overall_status": self.overall_status,
            "fields": [item.to_dict() for item in self.fields],
            "total_fields": self.total_fields,
            "fill_fields": self.fill_fields,
            "skipped_fields": self.skipped_fields,
            "wait_for_user_fields": self.wait_for_user_fields,
            "manual_only_fields": self.manual_only_fields,
            "blocked_fields": self.blocked_fields,
            "unresolved_fields": self.unresolved_fields,
            "required_not_fillable": self.required_not_fillable,
            "intended_resume": self.intended_resume,
            "blockers": self.blockers,
            "safety_flags": self.safety_flags,
        }


@dataclass(slots=True)
class ControlledFillResult:
    application_id: str
    form_fingerprint: str
    started_at: str
    completed_at: str
    total_fields: int
    attempted_fields: int
    filled_fields: int
    skipped_fields: int
    manual_only_fields: int
    unresolved_fields: int
    failed_fields: int
    blocked_mutation_requests: list[dict[str, Any]]
    captcha_detected: bool
    login_required: bool
    submission_attempted: bool
    submission_completed: bool
    overall_status: str
    field_events: list[dict[str, str]] = field(default_factory=list)
    safety_flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.overall_status not in FILL_STATUSES:
            raise ValueError(f"Unsupported controlled-fill result status: {self.overall_status}")
        if self.submission_completed:
            raise ValueError("Phase 3.3 cannot represent a completed submission.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
