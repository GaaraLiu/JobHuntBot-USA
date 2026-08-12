"""Execution gates shared by preview, tests, and the Playwright backend."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from ..application_forms.models import ApplicationForm
from ..browser_forms import BrowserRenderedForm
from .models import ControlledFillPlan, ControlledFillPolicy, ControlledFillResult


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_network_url(url: str) -> str:
    """Retain endpoint identity while dropping queries, fragments, and credentials."""

    parsed = urlsplit(str(url or ""))
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


class MutationFirewall:
    """Pure policy object used by the browser route handler and offline tests."""

    def __init__(self) -> None:
        self.blocked: list[dict[str, str]] = []

    def allows(self, method: str, url: str) -> bool:
        normalized = str(method or "GET").upper()
        if normalized in MUTATING_METHODS:
            self.blocked.append({"method": normalized, "url": sanitize_network_url(url)})
            return False
        return normalized in {"GET", "HEAD", "OPTIONS"}


class ControlledFillBackend(Protocol):
    def fill(
        self,
        url: str,
        plan: ControlledFillPlan,
        form: ApplicationForm,
        rendered: BrowserRenderedForm,
        policy: ControlledFillPolicy,
    ) -> ControlledFillResult:
        """Populate only planned fields and return without submitting."""


class ControlledFillService:
    """Keep preview and safety failures outside the browser process."""

    def __init__(self, backend: ControlledFillBackend | None = None):
        self.backend = backend

    def run(
        self,
        url: str,
        plan: ControlledFillPlan,
        form: ApplicationForm,
        rendered: BrowserRenderedForm,
        policy: ControlledFillPolicy,
    ) -> ControlledFillResult:
        if not policy.execute_fill:
            return self._local_result(
                plan,
                plan.overall_status,
                ["PREVIEW_ONLY_NO_BROWSER_MUTATION"],
            )
        if plan.overall_status == "BLOCKED_BY_CAPTCHA":
            return self._local_result(plan, "BLOCKED_BY_CAPTCHA", ["CAPTCHA_GATE_STOPPED_BEFORE_BROWSER_FILL"])
        if rendered.login_wall or rendered.status == "LOGIN_REQUIRED":
            return self._local_result(plan, "NEEDS_USER_INPUT", ["LOGIN_NOT_PERMITTED"])
        if plan.overall_status == "FORM_CHANGED_REVIEW_REQUIRED":
            return self._local_result(plan, "FORM_CHANGED_REVIEW_REQUIRED", ["STALE_FORM_GATE"])
        if plan.required_not_fillable and not policy.allow_partial_fill:
            return self._local_result(plan, "NEEDS_USER_INPUT", ["REQUIRED_FIELDS_NOT_FILLABLE"])
        if self.backend is None:
            raise RuntimeError("Explicit execution requires a controlled-fill browser backend.")
        return self.backend.fill(url, plan, form, rendered, policy)

    @staticmethod
    def _local_result(
        plan: ControlledFillPlan,
        status: str,
        flags: list[str],
    ) -> ControlledFillResult:
        now = utc_now()
        return ControlledFillResult(
            application_id=plan.application_id,
            form_fingerprint=plan.form_fingerprint,
            started_at=now,
            completed_at=now,
            total_fields=plan.total_fields,
            attempted_fields=0,
            filled_fields=0,
            skipped_fields=plan.skipped_fields,
            manual_only_fields=plan.manual_only_fields,
            unresolved_fields=(
                plan.unresolved_fields + plan.wait_for_user_fields + plan.blocked_fields
            ),
            failed_fields=0,
            blocked_mutation_requests=[],
            captcha_detected=status == "BLOCKED_BY_CAPTCHA",
            login_required="LOGIN_NOT_PERMITTED" in flags,
            submission_attempted=False,
            submission_completed=False,
            overall_status=status,
            field_events=[],
            safety_flags=[*flags, "NO_SUBMISSION", "NO_SENSITIVE_VALUES_LOGGED"],
        )


def sanitized_fill_log(result: ControlledFillResult) -> dict[str, Any]:
    """Return the deliberate value-free log schema."""

    return {
        "application_id": result.application_id,
        "form_fingerprint": result.form_fingerprint,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "overall_status": result.overall_status,
        "attempted_fields": result.attempted_fields,
        "filled_fields": result.filled_fields,
        "failed_fields": result.failed_fields,
        "blocked_mutation_requests": result.blocked_mutation_requests,
        "captcha_detected": result.captcha_detected,
        "login_required": result.login_required,
        "submission_attempted": result.submission_attempted,
        "submission_completed": False,
        "field_events": result.field_events,
        "safety_flags": result.safety_flags,
        "candidate_values_logged": False,
    }
