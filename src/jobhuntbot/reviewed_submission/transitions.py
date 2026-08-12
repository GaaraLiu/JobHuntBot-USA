"""Pure deterministic multi-step, final-control, and confirmation checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..browser_forms import BrowserRenderedForm


FINAL_SUBMIT_LABELS = {
    "submit application", "submit", "apply", "send application",
}
NON_FINAL_LABELS = {"next", "continue", "save and continue"}


@dataclass(frozen=True, slots=True)
class SubmitControlDecision:
    status: str
    label: str = ""
    reason: str = ""


class SubmitControlDetector:
    @staticmethod
    def detect(labels: list[str]) -> SubmitControlDecision:
        normalized = [(" ".join(str(item).split()).casefold(), str(item)) for item in labels]
        final = [original for value, original in normalized if value in FINAL_SUBMIT_LABELS]
        if len(final) == 1:
            return SubmitControlDecision("FINAL_SUBMIT", final[0], "One exact final submission control was found.")
        if len(final) > 1:
            return SubmitControlDecision("STOP_FOR_REVIEW", "", "Multiple possible final submission controls were found.")
        if any(value in NON_FINAL_LABELS for value, _ in normalized):
            return SubmitControlDecision("NON_FINAL_NEXT", "", "Only non-final navigation controls were found.")
        return SubmitControlDecision("STOP_FOR_REVIEW", "", "No unambiguous final submission control was found.")


class StepTransitionGuard:
    @staticmethod
    def evaluate(before: BrowserRenderedForm, after: BrowserRenderedForm) -> str:
        if after.anti_bot or after.status == "BLOCKED_BY_ANTI_BOT":
            return "PAUSED_FOR_CAPTCHA"
        if after.login_wall or after.status == "LOGIN_REQUIRED":
            return "PAUSED_FOR_LOGIN"
        before_fields = {item.field_id: item for item in before.fields}
        after_fields = {item.field_id: item for item in after.fields}
        new_visible = [item for key, item in after_fields.items() if key not in before_fields and item.visible]
        if new_visible:
            return "NEW_QUESTIONS_REVIEW_REQUIRED"
        for field_id, prior in before_fields.items():
            current = after_fields.get(field_id)
            if current is None:
                continue
            if (
                prior.locator != current.locator
                or (prior.label or prior.accessible_name) != (current.label or current.accessible_name)
                or prior.dom_type != current.dom_type
                or prior.required != current.required
            ):
                return "FORM_CHANGED_REVIEW_REQUIRED"
        return "STEP_COMPATIBLE"


class ConfirmationDetector:
    PATTERNS = (
        (r"\bapplication (has been )?(received|submitted)\b", "Application received/submitted confirmation detected."),
        (r"\bthank you for (applying|your application)\b", "Thank-you application confirmation detected."),
        (r"\bsuccessfully submitted\b", "Successful submission confirmation detected."),
    )
    REFERENCE = re.compile(r"\b(?:confirmation|application|reference)\s*(?:id|number|#)\s*[:#-]?\s*([A-Z0-9-]{4,40})\b", re.I)

    @classmethod
    def detect(cls, text: str, final_url: str = "") -> tuple[bool, str, str]:
        clean = " ".join(str(text or "").split())
        for pattern, summary in cls.PATTERNS:
            if re.search(pattern, clean, re.I):
                match = cls.REFERENCE.search(clean)
                return True, summary, match.group(1) if match else ""
        if re.search(r"/(confirmation|submitted|thank-you)(?:/|$)", final_url, re.I):
            return True, "Confirmation URL pattern detected.", ""
        return False, "", ""
