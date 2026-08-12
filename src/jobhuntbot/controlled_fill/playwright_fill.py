"""Playwright controlled fill with a hard network and submission firewall."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..application_forms.models import ApplicationForm
from ..browser_forms import BrowserRenderedForm, validate_public_browser_url
from ..browser_forms.playwright_browser import _DOM_EXTRACTION_SCRIPT, PlaywrightReadOnlyBrowser
from .execution import MutationFirewall, sanitize_network_url, utc_now
from .models import ControlledFillPlan, ControlledFillPolicy, ControlledFillResult


_SUBMISSION_FIREWALL_SCRIPT = r"""
(() => {
  window.__jobhuntbotSubmissionAttempts = Number(window.__jobhuntbotSubmissionAttempts || 0);
  const block = (event) => {
    window.__jobhuntbotSubmissionAttempts += 1;
    if (event) { event.preventDefault(); event.stopImmediatePropagation(); }
    return false;
  };
  HTMLFormElement.prototype.submit = function () { return block(); };
  HTMLFormElement.prototype.requestSubmit = function () { return block(); };
  document.addEventListener('submit', block, true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target && event.target.closest('form')) block(event);
  }, true);
})();
"""


class FillDependencyError(RuntimeError):
    pass


class PlaywrightControlledFillBrowser:
    """Populate an allowlisted plan, verify it locally, and close before submit."""

    def __init__(self, *, executable_path: str | None = None):
        self.executable_path = executable_path or os.environ.get(
            "JOBHUNTBOT_PLAYWRIGHT_EXECUTABLE", ""
        )

    def fill(
        self,
        url: str,
        plan: ControlledFillPlan,
        form: ApplicationForm,
        rendered: BrowserRenderedForm,
        policy: ControlledFillPolicy,
    ) -> ControlledFillResult:
        target = validate_public_browser_url(url)
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FillDependencyError(
                "Playwright is required for explicit controlled fill. Preview remains available."
            ) from exc
        if self.executable_path and not Path(self.executable_path).is_file():
            raise FillDependencyError(
                f"Configured Chromium executable does not exist: {self.executable_path}"
            )

        started = utc_now()
        firewall = MutationFirewall()
        fill_active = False
        events: list[dict[str, str]] = []
        attempted = filled = failed = 0
        captcha = login = submission_attempted = False
        status = "FILLED_SAFE_FIELDS"
        flags = ["MUTATION_FIREWALL_ACTIVE", "SUBMISSION_FIREWALL_ACTIVE", "NO_FILE_UPLOAD"]

        with sync_playwright() as playwright:
            options: dict[str, Any] = {"headless": policy.headless}
            if self.executable_path:
                options["executable_path"] = self.executable_path
            try:
                browser = playwright.chromium.launch(**options)
            except Exception as exc:
                raise FillDependencyError(f"Chromium could not be launched: {exc}") from exc
            context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
                locale="en-US",
            )
            page = context.new_page()
            page.set_default_timeout(policy.timeout_ms)
            page.add_init_script(_SUBMISSION_FIREWALL_SCRIPT)

            def route_request(route: Any, request: Any) -> None:
                method = request.method.upper()
                reactive_get = (
                    fill_active
                    and method == "GET"
                    and request.resource_type in {
                        "document", "xhr", "fetch", "websocket", "eventsource"
                    }
                )
                if reactive_get:
                    firewall.blocked.append(
                        {"method": "GET", "url": sanitize_network_url(request.url)}
                    )
                    route.abort("blockedbyclient")
                elif firewall.allows(method, request.url):
                    route.continue_()
                else:
                    route.abort("blockedbyclient")

            context.route("**/*", route_request)
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=policy.timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(policy.timeout_ms, 5_000))
                except PlaywrightTimeoutError:
                    pass
                payload = page.evaluate(_DOM_EXTRACTION_SCRIPT)
                live_rendered = PlaywrightReadOnlyBrowser._snapshot(
                    requested_url=target,
                    final_url=page.url,
                    page_title=str(payload.get("page_title", "")),
                    payload=payload,
                    blocked_requests=firewall.blocked,
                    public_network=[],
                    navigation_followed=False,
                    error_message="",
                )
                captcha = live_rendered.anti_bot
                login = live_rendered.login_wall
                structural = self._structure_signature(live_rendered)
                expected = self._structure_signature(rendered)
                if captcha:
                    status = "BLOCKED_BY_CAPTCHA"
                    flags.append("CAPTCHA_DETECTED_BEFORE_FILL")
                elif login:
                    status = "NEEDS_USER_INPUT"
                    flags.append("LOGIN_NOT_PERMITTED")
                elif firewall.blocked:
                    status = "LIVE_FILL_UNSAFE"
                    flags.append("PAGE_REQUIRED_MUTATION_BEFORE_FILL")
                elif structural != expected:
                    status = "FORM_CHANGED_REVIEW_REQUIRED"
                    flags.append("LIVE_STRUCTURE_DIFFERS_FROM_VERIFIED_SNAPSHOT")
                else:
                    page.evaluate(_SUBMISSION_FIREWALL_SCRIPT)
                    fill_active = True
                    for field in (item for item in plan.fields if item.action == "FILL"):
                        attempted += 1
                        before_blocked = len(firewall.blocked)
                        neighbor_state = self._control_state_digest(page, plan, exclude=field.field_id)
                        try:
                            self._fill_one(page, field)
                            if not self._verify_one(page, field):
                                raise RuntimeError("local field verification failed")
                            if neighbor_state != self._control_state_digest(page, plan, exclude=field.field_id):
                                raise RuntimeError("a neighboring planned control changed unexpectedly")
                            if len(firewall.blocked) > before_blocked:
                                status = "LIVE_FILL_UNSAFE"
                                flags.append("REACTIVE_MUTATION_REQUEST_BLOCKED")
                                failed += 1
                                events.append(self._event(field, "BLOCKED", "Reactive network mutation was blocked."))
                                break
                            filled += 1
                            events.append(self._event(field, "FILLED", "Local value verification passed."))
                        except Exception as exc:
                            failed += 1
                            events.append(self._event(field, "FAILED", type(exc).__name__))
                            status = "FAILED"
                            break
                submission_attempted = bool(
                    page.evaluate("() => Number(window.__jobhuntbotSubmissionAttempts || 0) > 0")
                )
                if submission_attempted:
                    status = "LIVE_FILL_UNSAFE"
                    flags.append("SUBMISSION_ATTEMPT_BLOCKED")
            except Exception as exc:
                failed += 1
                status = "FAILED"
                events.append(
                    {
                        "field_id": "",
                        "canonical_id": "",
                        "status": "FAILED",
                        "detail": type(exc).__name__,
                        "candidate_value_logged": "false",
                    }
                )
                flags.append("BROWSER_OPERATION_FAILED_CLOSED_WITHOUT_SUBMISSION")
            finally:
                page.close()
                context.close()
                browser.close()

        if firewall.blocked and status == "FILLED_SAFE_FIELDS":
            status = "LIVE_FILL_UNSAFE"
            flags.append("MUTATION_REQUEST_BLOCKED")
        return ControlledFillResult(
            application_id=plan.application_id,
            form_fingerprint=plan.form_fingerprint,
            started_at=started,
            completed_at=utc_now(),
            total_fields=plan.total_fields,
            attempted_fields=attempted,
            filled_fields=filled,
            skipped_fields=plan.skipped_fields,
            manual_only_fields=plan.manual_only_fields,
            unresolved_fields=(plan.unresolved_fields + plan.wait_for_user_fields),
            failed_fields=failed,
            blocked_mutation_requests=firewall.blocked,
            captcha_detected=captcha,
            login_required=login,
            submission_attempted=submission_attempted,
            submission_completed=False,
            overall_status=status,
            field_events=events,
            safety_flags=[*dict.fromkeys([*flags, "NO_SUBMISSION", "NO_SENSITIVE_VALUES_LOGGED"])],
        )

    @staticmethod
    def _fill_one(page: Any, field: Any) -> None:
        locator = page.locator(field.locator)
        if locator.count() < 1:
            raise RuntimeError("verified locator is no longer present")
        control = locator.first
        if not control.is_visible() or not control.is_enabled():
            raise RuntimeError("verified control is no longer visible and enabled")
        if field.field_type in {"text", "textarea", "email", "phone", "number", "date", "address"}:
            control.fill(str(field.resolved_value))
        elif field.field_type == "select":
            tag_name = control.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "select":
                control.select_option(label=field.intended_option or str(field.resolved_value))
            else:
                control.click()
                page.get_by_role("option", name=field.intended_option, exact=True).click()
        elif field.field_type in {"radio", "checkbox", "boolean"}:
            group = page.locator(field.locator)
            if field.field_type in {"checkbox", "boolean"} and not field.intended_option and group.count() == 1:
                if field.resolved_value is True:
                    group.first.check()
                elif field.resolved_value is False:
                    group.first.uncheck()
                else:
                    raise RuntimeError("safe checkbox requires an exact boolean answer")
                return
            desired = field.intended_option.casefold()
            selected = None
            for index in range(group.count()):
                candidate = group.nth(index)
                name = (candidate.get_attribute("aria-label") or "").strip()
                if not name:
                    name = (candidate.locator("xpath=ancestor-or-self::label[1]").inner_text() or "").strip()
                if name.casefold() == desired:
                    selected = candidate
                    break
            if selected is None:
                raise RuntimeError("intended choice is no longer an exact visible option")
            selected.check()
        else:
            raise RuntimeError("unsupported controlled-fill field type")

    @staticmethod
    def _verify_one(page: Any, field: Any) -> bool:
        control = page.locator(field.locator).first
        if field.field_type in {"radio", "checkbox", "boolean"}:
            group = page.locator(field.locator)
            checked = [group.nth(index).is_checked() for index in range(group.count())]
            if field.field_type in {"checkbox", "boolean"} and not field.intended_option and group.count() == 1:
                return checked[0] is bool(field.resolved_value)
            return any(checked)
        if field.field_type == "select":
            tag_name = control.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "select":
                return control.locator("option:checked").inner_text().strip().casefold() == field.intended_option.casefold()
        return control.input_value() == str(field.resolved_value)

    @staticmethod
    def _control_state_digest(page: Any, plan: ControlledFillPlan, *, exclude: str) -> str:
        values: list[Any] = []
        for item in plan.fields:
            if item.action != "FILL" or item.field_id == exclude:
                continue
            locator = page.locator(item.locator)
            if locator.count() < 1:
                values.append([item.field_id, "missing"])
                continue
            control = locator.first
            if item.field_type in {"radio", "checkbox", "boolean"}:
                values.append([item.field_id, [locator.nth(i).is_checked() for i in range(locator.count())]])
            else:
                values.append([item.field_id, control.input_value()])
        encoded = json.dumps(values, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _structure_signature(rendered: BrowserRenderedForm) -> list[tuple[Any, ...]]:
        return [
            (
                item.field_id,
                item.locator,
                item.label or item.accessible_name,
                item.dom_type,
                item.required,
                item.visible,
                item.disabled,
                tuple(item.options),
            )
            for item in rendered.fields
        ]

    @staticmethod
    def _event(field: Any, status: str, detail: str) -> dict[str, str]:
        return {
            "field_id": field.field_id,
            "canonical_id": field.canonical_id,
            "status": status,
            "detail": detail,
            "candidate_value_logged": "false",
        }
