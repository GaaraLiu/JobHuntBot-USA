"""Approval-gated orchestration; dry-run is the default."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..application_forms.models import ApplicationForm, ApplicationMappingPlan
from ..browser_forms import BrowserRenderedForm
from ..controlled_fill.models import ControlledFillPlan
from .approval import ApprovalService
from .history import SubmissionHistoryStore, SubmissionQueueStatusManager
from .models import ReviewPackage, SubmissionApproval, SubmissionResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class SubmissionContext:
    package: dict
    form: ApplicationForm
    mapping: ApplicationMappingPlan
    fill_plan: ControlledFillPlan
    rendered: BrowserRenderedForm
    review: ReviewPackage


@dataclass(frozen=True, slots=True)
class SubmissionExecutionPolicy:
    submit: bool = False
    allow_duplicate_submission: bool = False
    timeout_ms: int = 30_000
    headless: bool = True

    def __post_init__(self) -> None:
        if self.timeout_ms < 1_000 or self.timeout_ms > 120_000:
            raise ValueError("Submission timeout must be between 1,000 and 120,000 ms.")


class SubmissionBackend(Protocol):
    def execute(
        self,
        context: SubmissionContext,
        approval: SubmissionApproval,
        policy: SubmissionExecutionPolicy,
    ) -> SubmissionResult:
        """Execute one approved application and return structured evidence."""


class SubmissionExecutionService:
    def __init__(
        self,
        backend: SubmissionBackend | None = None,
        *,
        history: SubmissionHistoryStore | None = None,
        queue: SubmissionQueueStatusManager | None = None,
    ):
        self.backend = backend
        self.history = history
        self.queue = queue or SubmissionQueueStatusManager(None)

    def run(
        self,
        context: SubmissionContext,
        approval: SubmissionApproval,
        policy: SubmissionExecutionPolicy,
    ) -> SubmissionResult:
        current = ApprovalService().validate(
            approval, context.review, context.package, context.form
        )
        if current.approval_status == "EXPIRED":
            return self._finish(context, self._local(context, "FORM_CHANGED_REVIEW_REQUIRED", error="; ".join(current.invalidation_reasons)))
        if not policy.submit:
            return self._finish(context, self._local(context, "DRY_RUN_READY"))
        if current.approval_status != "APPROVED_FOR_SUBMISSION" or not current.approval_scope.allow_submission:
            return self._finish(context, self._local(context, "SUBMISSION_BLOCKED", error="APPROVED_FOR_SUBMISSION is required."))
        if context.review.blockers or context.review.unresolved_required_count:
            return self._finish(context, self._local(context, "NEEDS_USER_INPUT", unresolved=[str(item) for item in context.review.blockers]))
        legal = set(current.legal_consent_fields)
        resolved = set(current.approval_scope.resolved_manual_item_ids)
        if legal - resolved:
            return self._finish(context, self._local(context, "NEEDS_USER_INPUT", unresolved=sorted(legal - resolved), error="Legal/manual items require individual user action."))
        if context.rendered.anti_bot or context.rendered.status == "BLOCKED_BY_ANTI_BOT":
            return self._finish(context, self._local(context, "PAUSED_FOR_CAPTCHA", interventions=["USER_CAPTCHA_REQUIRED"]))
        if context.rendered.login_wall or context.rendered.status == "LOGIN_REQUIRED":
            return self._finish(context, self._local(context, "PAUSED_FOR_LOGIN", interventions=["USER_LOGIN_REQUIRED"]))
        if self.history and self.history.has_confirmed(
            application_id=context.review.application_id, job_id=context.review.job_id
        ) and not policy.allow_duplicate_submission:
            return self._finish(context, self._local(context, "SUBMISSION_BLOCKED", error="Duplicate confirmed submission detected."))
        if self.backend is None:
            return self._finish(context, self._local(context, "FAILED", error="No submission backend is configured."))
        self.queue.mark_in_progress(context.review.application_id)
        self._history(context, "execution_started", "IN_PROGRESS")
        result = self.backend.execute(context, current, policy)
        return self._finish(context, result)

    def _finish(self, context: SubmissionContext, result: SubmissionResult) -> SubmissionResult:
        self.queue.apply_result(result)
        self._history(context, "submission_result", result.status)
        return result

    def _history(self, context: SubmissionContext, event_type: str, status: str) -> None:
        if not self.history:
            return
        self.history.append(
            event_type,
            application_id=context.review.application_id,
            job_id=context.review.job_id,
            company=str(context.review.job.get("company", "")),
            title=str(context.review.job.get("title", "")),
            ats=context.mapping.ats,
            status=status,
        )

    @staticmethod
    def _local(
        context: SubmissionContext,
        status: str,
        *,
        error: str = "",
        interventions: list[str] | None = None,
        unresolved: list[str] | None = None,
    ) -> SubmissionResult:
        now = utc_now()
        return SubmissionResult(
            application_id=context.review.application_id,
            job_id=context.review.job_id,
            company=str(context.review.job.get("company", "")),
            title=str(context.review.job.get("title", "")),
            ats=context.mapping.ats,
            started_at=now,
            submitted_at="",
            submission_attempted=False,
            submission_completed=False,
            confirmation_detected=False,
            confirmation_text_summary="",
            confirmation_reference_id="",
            final_url=context.form.application_url,
            resume_uploaded=False,
            cover_letter_uploaded=False,
            manual_interventions=interventions or [],
            unresolved_fields=unresolved or [],
            safety_flags=["EXPLICIT_SUBMISSION_GATE", "NO_PASSWORD_STORAGE", "SENSITIVE_LOGGING_MINIMIZED"],
            status=status,
            error=error,
        )
