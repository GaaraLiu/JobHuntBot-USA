"""Value-conscious review, approval, and submission result contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


APPROVAL_STATES = {
    "NOT_REVIEWED", "CHANGES_REQUIRED", "APPROVED_FOR_FILL",
    "APPROVED_FOR_SUBMISSION", "EXPIRED",
}
SUBMISSION_STATUSES = {
    "DRY_RUN_READY", "SUBMITTED_CONFIRMED", "SUBMITTED_UNCONFIRMED",
    "PAUSED_FOR_CAPTCHA", "PAUSED_FOR_LOGIN", "NEEDS_USER_INPUT",
    "FORM_CHANGED_REVIEW_REQUIRED", "SUBMISSION_BLOCKED", "FAILED",
}


@dataclass(slots=True)
class ReviewAnswer:
    field_id: str
    canonical_id: str
    proposed_answer: Any
    source: str
    provenance: list[str]
    safety_class: str
    action: str
    required: bool
    sensitive: bool = False
    highlighted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewDocument:
    document_type: str
    status: str
    document_id: str
    path: str
    sha256: str
    size_bytes: int
    required: bool
    approved_in_package: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewManualItem:
    item_id: str
    category: str
    label: str
    required: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewPackage:
    application_id: str
    job_id: str
    form_fingerprint: str
    package_fingerprint: str
    generated_at: str
    review_status: str
    job: dict[str, Any]
    answers: list[ReviewAnswer]
    documents: list[ReviewDocument]
    manual_items: list[ReviewManualItem]
    unresolved_required_count: int
    salary_state: dict[str, Any]
    work_authorization_state: str
    sponsorship_state: str
    blockers: list[str]
    safety_flags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "job_id": self.job_id,
            "form_fingerprint": self.form_fingerprint,
            "package_fingerprint": self.package_fingerprint,
            "generated_at": self.generated_at,
            "review_status": self.review_status,
            "job": self.job,
            "answers": [item.to_dict() for item in self.answers],
            "documents": [item.to_dict() for item in self.documents],
            "manual_items": [item.to_dict() for item in self.manual_items],
            "unresolved_required_count": self.unresolved_required_count,
            "salary_state": self.salary_state,
            "work_authorization_state": self.work_authorization_state,
            "sponsorship_state": self.sponsorship_state,
            "blockers": self.blockers,
            "safety_flags": self.safety_flags,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReviewPackage":
        return cls(
            application_id=str(value.get("application_id", "")),
            job_id=str(value.get("job_id", "")),
            form_fingerprint=str(value.get("form_fingerprint", "")),
            package_fingerprint=str(value.get("package_fingerprint", "")),
            generated_at=str(value.get("generated_at", "")),
            review_status=str(value.get("review_status", "CHANGES_REQUIRED")),
            job=dict(value.get("job", {})),
            answers=[ReviewAnswer(**item) for item in value.get("answers", [])],
            documents=[ReviewDocument(**item) for item in value.get("documents", [])],
            manual_items=[ReviewManualItem(**item) for item in value.get("manual_items", [])],
            unresolved_required_count=int(value.get("unresolved_required_count", 0)),
            salary_state=dict(value.get("salary_state", {})),
            work_authorization_state=str(value.get("work_authorization_state", "UNKNOWN")),
            sponsorship_state=str(value.get("sponsorship_state", "UNKNOWN")),
            blockers=[str(item) for item in value.get("blockers", [])],
            safety_flags=[str(item) for item in value.get("safety_flags", [])],
        )


@dataclass(slots=True)
class ApprovalScope:
    allow_fill: bool
    allow_submission: bool
    allow_resume_upload: bool
    allow_cover_letter_upload: bool
    approved_origin: str
    resolved_manual_item_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SubmissionApproval:
    application_id: str
    form_fingerprint: str
    package_fingerprint: str
    package_version: str
    selected_resume: dict[str, Any]
    approved_documents: list[dict[str, Any]]
    unresolved_required_count: int
    manual_only_items: list[str]
    salary_answer_state: dict[str, Any]
    work_authorization_state: str
    sponsorship_state: str
    legal_consent_fields: list[str]
    approval_status: str
    approved_at: str
    approval_scope: ApprovalScope
    review_generated_at: str
    invalidation_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.approval_status not in APPROVAL_STATES:
            raise ValueError(f"Unsupported approval status: {self.approval_status}")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["approval_scope"] = self.approval_scope.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SubmissionApproval":
        return cls(
            application_id=str(value.get("application_id", "")),
            form_fingerprint=str(value.get("form_fingerprint", "")),
            package_fingerprint=str(value.get("package_fingerprint", "")),
            package_version=str(value.get("package_version", "")),
            selected_resume=dict(value.get("selected_resume", {})),
            approved_documents=[dict(item) for item in value.get("approved_documents", [])],
            unresolved_required_count=int(value.get("unresolved_required_count", 0)),
            manual_only_items=[str(item) for item in value.get("manual_only_items", [])],
            salary_answer_state=dict(value.get("salary_answer_state", {})),
            work_authorization_state=str(value.get("work_authorization_state", "UNKNOWN")),
            sponsorship_state=str(value.get("sponsorship_state", "UNKNOWN")),
            legal_consent_fields=[str(item) for item in value.get("legal_consent_fields", [])],
            approval_status=str(value.get("approval_status", "NOT_REVIEWED")),
            approved_at=str(value.get("approved_at", "")),
            approval_scope=ApprovalScope(**dict(value.get("approval_scope", {}))),
            review_generated_at=str(value.get("review_generated_at", "")),
            invalidation_reasons=[str(item) for item in value.get("invalidation_reasons", [])],
        )


@dataclass(slots=True)
class SubmissionResult:
    application_id: str
    job_id: str
    company: str
    title: str
    ats: str
    started_at: str
    submitted_at: str
    submission_attempted: bool
    submission_completed: bool
    confirmation_detected: bool
    confirmation_text_summary: str
    confirmation_reference_id: str
    final_url: str
    resume_uploaded: bool
    cover_letter_uploaded: bool
    manual_interventions: list[str]
    unresolved_fields: list[str]
    safety_flags: list[str]
    status: str
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in SUBMISSION_STATUSES:
            raise ValueError(f"Unsupported submission status: {self.status}")
        if self.status == "SUBMITTED_CONFIRMED" and not (
            self.submission_attempted
            and self.submission_completed
            and self.confirmation_detected
        ):
            raise ValueError("SUBMITTED_CONFIRMED requires positive completed confirmation evidence.")
        if not self.submission_attempted and self.submission_completed:
            raise ValueError("A submission cannot complete without an attempt.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
