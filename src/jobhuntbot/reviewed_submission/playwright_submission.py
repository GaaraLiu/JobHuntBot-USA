"""Explicitly approved Playwright execution; never used by offline tests."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..browser_forms import validate_public_browser_url
from ..browser_forms.playwright_browser import _DOM_EXTRACTION_SCRIPT, PlaywrightReadOnlyBrowser
from ..controlled_fill.execution import sanitize_network_url
from ..controlled_fill.playwright_fill import PlaywrightControlledFillBrowser
from .execution import SubmissionContext, SubmissionExecutionPolicy, utc_now
from .fingerprints import file_sha256
from .firewall import ScopedSubmissionFirewall
from .models import SubmissionApproval, SubmissionResult
from .transitions import ConfirmationDetector, NON_FINAL_LABELS, StepTransitionGuard, SubmitControlDetector


_REVIEWED_SUBMISSION_GUARD = r"""
(() => {
  window.__jobhuntbotApprovedFinalSubmit = false;
  window.__jobhuntbotSubmissionAttempts = 0;
  const nativeSubmit = HTMLFormElement.prototype.submit;
  const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
  HTMLFormElement.prototype.submit = function () {
    if (!window.__jobhuntbotApprovedFinalSubmit) {
      window.__jobhuntbotSubmissionAttempts += 1;
      return undefined;
    }
    return nativeSubmit.call(this);
  };
  HTMLFormElement.prototype.requestSubmit = function (submitter) {
    if (!window.__jobhuntbotApprovedFinalSubmit) {
      window.__jobhuntbotSubmissionAttempts += 1;
      return undefined;
    }
    return nativeRequestSubmit.call(this, submitter);
  };
  document.addEventListener('submit', (event) => {
    if (!window.__jobhuntbotApprovedFinalSubmit) {
      window.__jobhuntbotSubmissionAttempts += 1;
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target && event.target.closest('form')) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
})();
"""


class SubmissionDependencyError(RuntimeError):
    pass


class PlaywrightReviewedSubmissionBrowser:
    def __init__(self, *, executable_path: str | None = None):
        self.executable_path = executable_path or os.environ.get(
            "JOBHUNTBOT_PLAYWRIGHT_EXECUTABLE", ""
        )

    def execute(
        self,
        context: SubmissionContext,
        approval: SubmissionApproval,
        policy: SubmissionExecutionPolicy,
    ) -> SubmissionResult:
        if not policy.submit or approval.approval_status != "APPROVED_FOR_SUBMISSION":
            raise ValueError("Playwright submission requires explicit submit and submission approval.")
        target = validate_public_browser_url(
            context.rendered.final_url or context.form.application_url
        )
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SubmissionDependencyError("Playwright is required for controlled submission execution.") from exc
        if self.executable_path and not Path(self.executable_path).is_file():
            raise SubmissionDependencyError(
                f"Configured Chromium executable does not exist: {self.executable_path}"
            )

        started = utc_now()
        approved_paths = {
            urlsplit(str(context.form.application_url or "")).path,
            urlsplit(str(context.rendered.final_url or "")).path,
        } - {""}
        firewall = ScopedSubmissionFirewall(allowed_mutation_paths=approved_paths)
        attempted = completed = confirmed = False
        resume_uploaded = cover_uploaded = False
        final_url = target
        summary = reference = error = ""
        status = "FAILED"
        interventions = list(approval.approval_scope.resolved_manual_item_ids)
        flags = [
            "EXPLICIT_SUBMIT_FLAG", "APPROVAL_BOUND", "ORIGIN_SCOPED_FIREWALL",
            "NO_PASSWORD_STORAGE", "NO_AUTOMATED_LEGAL_CONSENT",
        ]

        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"headless": policy.headless}
            if self.executable_path:
                launch["executable_path"] = self.executable_path
            try:
                browser = playwright.chromium.launch(**launch)
            except Exception as exc:
                raise SubmissionDependencyError(f"Chromium could not be launched: {exc}") from exc
            browser_context = browser.new_context(
                accept_downloads=False, service_workers="block", locale="en-US"
            )
            page = browser_context.new_page()
            page.set_default_timeout(policy.timeout_ms)
            page.add_init_script(_REVIEWED_SUBMISSION_GUARD)

            def route_request(route: Any, request: Any) -> None:
                if firewall.allows(
                    request.method,
                    request.url,
                    application_id=context.review.application_id,
                ):
                    route.continue_()
                else:
                    route.abort("blockedbyclient")

            browser_context.route("**/*", route_request)
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=policy.timeout_ms)
                self._settle(page, PlaywrightTimeoutError, policy.timeout_ms)
                live = self._snapshot(page, target, firewall)
                if live.anti_bot:
                    status = "PAUSED_FOR_CAPTCHA"
                    interventions.append("USER_CAPTCHA_REQUIRED")
                elif live.login_wall:
                    status = "PAUSED_FOR_LOGIN"
                    interventions.append("USER_LOGIN_REQUIRED")
                elif PlaywrightControlledFillBrowser._structure_signature(live) != PlaywrightControlledFillBrowser._structure_signature(context.rendered):
                    status = "FORM_CHANGED_REVIEW_REQUIRED"
                    error = "Live form structure differs from the reviewed BrowserRenderedForm."
                elif firewall.blocked:
                    status = "SUBMISSION_BLOCKED"
                    error = "The page required a mutation before approval checks completed."
                else:
                    firewall.arm(approval)
                    for field in (item for item in context.fill_plan.fields if item.action == "FILL"):
                        PlaywrightControlledFillBrowser._fill_one(page, field)
                        if not PlaywrightControlledFillBrowser._verify_one(page, field):
                            raise RuntimeError(f"Local verification failed for {field.canonical_id}")
                    resume_uploaded = self._upload_document(
                        page, context, approval, "resume_upload", "resume"
                    )
                    cover_uploaded = self._upload_document(
                        page, context, approval, "cover_letter_upload", "cover_letter"
                    )
                    decision = self._control_decision(page)
                    step_count = 0
                    while decision.status == "NON_FINAL_NEXT" and step_count < 5:
                        before = self._snapshot(page, target, firewall)
                        self._click_non_final(page)
                        self._settle(page, PlaywrightTimeoutError, policy.timeout_ms)
                        after = self._snapshot(page, target, firewall)
                        if firewall.blocked:
                            status = "SUBMISSION_BLOCKED"
                            error = "An unauthorized navigation mutation was blocked."
                            break
                        transition = StepTransitionGuard.evaluate(before, after)
                        if transition == "PAUSED_FOR_CAPTCHA":
                            status = "PAUSED_FOR_CAPTCHA"
                            interventions.append("USER_CAPTCHA_REQUIRED")
                            break
                        if transition == "PAUSED_FOR_LOGIN":
                            status = "PAUSED_FOR_LOGIN"
                            interventions.append("USER_LOGIN_REQUIRED")
                            break
                        if transition in {"NEW_QUESTIONS_REVIEW_REQUIRED", "FORM_CHANGED_REVIEW_REQUIRED"}:
                            status = "FORM_CHANGED_REVIEW_REQUIRED"
                            error = transition
                            break
                        step_count += 1
                        decision = self._control_decision(page)
                    else:
                        if decision.status != "FINAL_SUBMIT":
                            status = "SUBMISSION_BLOCKED"
                            error = decision.reason or "Final submit control is ambiguous."
                        else:
                            final_check = self._snapshot(page, target, firewall)
                            if final_check.anti_bot:
                                status = "PAUSED_FOR_CAPTCHA"
                                interventions.append("USER_CAPTCHA_REQUIRED")
                            elif final_check.login_wall:
                                status = "PAUSED_FOR_LOGIN"
                                interventions.append("USER_LOGIN_REQUIRED")
                            else:
                                firewall.begin_submission(context.review.application_id)
                                page.evaluate("() => { window.__jobhuntbotApprovedFinalSubmit = true; }")
                                attempted = True
                                page.get_by_role("button", name=decision.label, exact=True).click()
                                completed = True
                                self._settle(page, PlaywrightTimeoutError, policy.timeout_ms)
                                if firewall.blocked:
                                    completed = False
                                    status = "SUBMITTED_UNCONFIRMED"
                                    error = "An unauthorized submission mutation was blocked."
                                    continue_submission = False
                                else:
                                    continue_submission = True
                                final_url = sanitize_network_url(page.url)
                                if continue_submission:
                                    confirmed, summary, reference = ConfirmationDetector.detect(
                                        page.locator("body").inner_text(), final_url
                                    )
                                    status = "SUBMITTED_CONFIRMED" if confirmed else "SUBMITTED_UNCONFIRMED"
            except Exception as exc:
                error = type(exc).__name__
                status = "SUBMITTED_UNCONFIRMED" if attempted else "FAILED"
            finally:
                firewall.close()
                page.close()
                browser_context.close()
                browser.close()

        flags.extend(
            [
                f"ALLOWED_MUTATIONS={len(firewall.allowed_mutations)}",
                f"BLOCKED_MUTATIONS={len(firewall.blocked)}",
                "SUBMISSION_FIREWALL_CLOSED",
                "SENSITIVE_VALUES_NOT_LOGGED",
            ]
        )
        return SubmissionResult(
            application_id=context.review.application_id,
            job_id=context.review.job_id,
            company=str(context.review.job.get("company", "")),
            title=str(context.review.job.get("title", "")),
            ats=context.mapping.ats,
            started_at=started,
            submitted_at=utc_now() if attempted else "",
            submission_attempted=attempted,
            submission_completed=completed,
            confirmation_detected=confirmed,
            confirmation_text_summary=summary,
            confirmation_reference_id=reference,
            final_url=final_url,
            resume_uploaded=resume_uploaded,
            cover_letter_uploaded=cover_uploaded,
            manual_interventions=list(dict.fromkeys(interventions)),
            unresolved_fields=[],
            safety_flags=flags,
            status=status,
            error=error,
        )

    @staticmethod
    def _snapshot(page: Any, target: str, firewall: ScopedSubmissionFirewall):
        payload = page.evaluate(_DOM_EXTRACTION_SCRIPT)
        return PlaywrightReadOnlyBrowser._snapshot(
            requested_url=target,
            final_url=page.url,
            page_title=str(payload.get("page_title", "")),
            payload=payload,
            blocked_requests=firewall.blocked,
            public_network=[],
            navigation_followed=False,
            error_message="",
        )

    @staticmethod
    def _settle(page: Any, timeout_error: type[Exception], timeout_ms: int) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5_000))
        except timeout_error:
            pass

    @staticmethod
    def _control_decision(page: Any):
        labels = page.locator(
            'button:visible, input[type="submit"]:visible, [role="button"]:visible'
        ).evaluate_all(
            "els => els.map(el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()).filter(Boolean)"
        )
        return SubmitControlDetector.detect([str(item) for item in labels])

    @staticmethod
    def _click_non_final(page: Any) -> None:
        controls = []
        for label in sorted(NON_FINAL_LABELS):
            locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            if locator.count() == 1:
                controls.append(locator.first)
        if len(controls) != 1:
            raise RuntimeError("Non-final navigation control is ambiguous.")
        controls[0].click()

    @staticmethod
    def _upload_document(
        page: Any,
        context: SubmissionContext,
        approval: SubmissionApproval,
        canonical_id: str,
        document_type: str,
    ) -> bool:
        mapped = next(
            (item for item in context.mapping.fields if item.canonical_question_id == canonical_id),
            None,
        )
        if mapped is None:
            return False
        document = next(
            (item for item in context.review.documents if item.document_type == document_type),
            None,
        )
        if document is None or document.status != "READY":
            if mapped.required:
                raise RuntimeError(f"Required {document_type} is not upload-ready.")
            return False
        allowed = (
            approval.approval_scope.allow_resume_upload
            if document_type == "resume"
            else approval.approval_scope.allow_cover_letter_upload
        )
        if not allowed:
            if mapped.required:
                raise RuntimeError(f"Approval does not permit required {document_type} upload.")
            return False
        approved_document = next(
            (
                item for item in approval.approved_documents
                if item.get("document_type") == document_type
                and item.get("document_id") == document.document_id
            ),
            None,
        )
        if approved_document is None or any(
            approved_document.get(field) != getattr(document, field)
            for field in ("path", "sha256", "size_bytes")
        ):
            raise RuntimeError(f"{document_type} does not match the approved document binding.")
        try:
            current_hash = file_sha256(Path(document.path).resolve(strict=True))
        except OSError as exc:
            raise RuntimeError(f"Approved {document_type} is no longer readable.") from exc
        if current_hash != document.sha256:
            raise RuntimeError(f"Approved {document_type} content changed before upload.")
        form_field = next((item for item in context.form.fields if item.field_id == mapped.field_id), None)
        if form_field is None or form_field.field_type not in {"file", "resume", "cover_letter"}:
            raise RuntimeError(f"{document_type} field is not confidently identified.")
        locator = page.locator(form_field.source_path)
        if locator.count() != 1:
            raise RuntimeError(f"{document_type} upload locator is ambiguous.")
        locator.set_input_files(document.path)
        return True
