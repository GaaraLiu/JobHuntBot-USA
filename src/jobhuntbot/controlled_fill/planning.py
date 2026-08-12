"""Conservative planning from existing private package and Phase 3.1/3.2 artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..application_forms.base import normalize_field_type, normalize_label
from ..application_forms.models import (
    ApplicationField,
    ApplicationForm,
    ApplicationMappingPlan,
    FieldMappingPlan,
)
from ..browser_forms import BrowserRenderedForm
from .models import ControlledFillFieldPlan, ControlledFillPlan, ControlledFillPolicy


ALLOWED_STATIC_CONCEPTS = {
    "legal_first_name", "legal_middle_name", "legal_last_name", "legal_full_name",
    "preferred_name", "contact_email", "contact_phone",
    "address.street", "address.line_2", "address.city", "address.state",
    "address.postal_code", "address.country", "address.combined",
    "linkedin", "github", "portfolio", "work_authorization_us",
    "sponsorship_now", "sponsorship_future", "driver_license",
}
DEMOGRAPHIC_CONCEPTS = {"gender", "race_ethnicity", "veteran_status", "disability"}
TOTAL_EXPERIENCE_CONCEPTS = {"years_of_experience"}
LEGAL_CONCEPT_PREFIXES = (
    "electronic_signature", "background_check_consent", "arbitration_agreement",
    "privacy_acknowledgment", "conflict_of_interest_certification",
    "accuracy_attestation", "legal_consent",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_private_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {source}")
    return value


def load_mapping_plan(path: str | Path) -> ApplicationMappingPlan:
    raw = load_private_object(path)
    fields = [FieldMappingPlan(**item) for item in raw.get("fields", [])]
    return ApplicationMappingPlan(
        application_id=str(raw.get("application_id", "")),
        job_id=str(raw.get("job_id", "")),
        form_fingerprint=str(raw.get("form_fingerprint", "")),
        ats=str(raw.get("ats", "unknown")),
        generated_at=str(raw.get("generated_at", "")),
        fields=fields,
        total_fields=int(raw.get("total_fields", len(fields))),
        mapped_fields=int(raw.get("mapped_fields", 0)),
        required_fields=int(raw.get("required_fields", 0)),
        required_unresolved=int(raw.get("required_unresolved", 0)),
        manual_only=int(raw.get("manual_only", 0)),
        potential_future_autofill=int(raw.get("potential_future_autofill", 0)),
        user_confirmation=int(raw.get("user_confirmation", 0)),
        optional_skip=int(raw.get("optional_skip", 0)),
        package_readiness=str(raw.get("package_readiness", "INVALID")),
        blockers=[str(item) for item in raw.get("blockers", [])],
        safety_flags=[str(item) for item in raw.get("safety_flags", [])],
    )


class ControlledFillPlanner:
    def build(
        self,
        package: Mapping[str, Any],
        mapping: ApplicationMappingPlan,
        form: ApplicationForm,
        rendered: BrowserRenderedForm,
        *,
        policy: ControlledFillPolicy | None = None,
    ) -> ControlledFillPlan:
        policy = policy or ControlledFillPolicy()
        application_id = str(package.get("application_id", ""))
        blockers: list[str] = []
        structure_changed = not mapping.form_fingerprint or mapping.form_fingerprint != form.fingerprint
        compatibility_blocked = structure_changed
        if structure_changed:
            blockers.append("ApplicationForm fingerprint does not match the MappingPlan.")
        if mapping.application_id and application_id and mapping.application_id != application_id:
            blockers.append("ApplicationPackage and MappingPlan application_id values differ.")
            compatibility_blocked = True
        captcha = rendered.anti_bot or rendered.status == "BLOCKED_BY_ANTI_BOT"
        login = rendered.login_wall or rendered.status == "LOGIN_REQUIRED"
        if captcha:
            blockers.append("CAPTCHA or anti-bot protection blocks controlled fill.")
        if login:
            blockers.append("Login or account creation is required; credentials are not permitted.")
        package_status = str(package.get("package_readiness", "")).upper()
        package_blocked = package_status in {"BLOCKED", "INVALID"}
        if package_blocked:
            blockers.append(f"ApplicationPackage readiness is {package_status}.")
        package_required_unresolved = sum(
            bool(item.get("required")) and not bool(item.get("resolved", False))
            for item in package.get("unresolved_questions", [])
            if isinstance(item, Mapping)
        )
        if package_required_unresolved:
            blockers.append(
                f"ApplicationPackage has {package_required_unresolved} unresolved required question(s)."
            )

        form_fields = {item.field_id: item for item in form.fields}
        rendered_fields = {item.field_id: item for item in rendered.fields}
        fields: list[ControlledFillFieldPlan] = []
        for mapped in mapping.fields:
            canonical = mapped.canonical_question_id
            form_field = form_fields.get(mapped.field_id)
            browser_field = rendered_fields.get(mapped.field_id)
            structural_issue = self._structural_issue(form_field, browser_field)
            if structural_issue:
                if structural_issue != "Field is hidden or disabled and is not eligible for fill.":
                    structure_changed = True
                    compatibility_blocked = True
                fields.append(self._field(mapped, form_field, "BLOCKED", structural_issue))
                continue
            if captcha or login:
                fields.append(self._field(mapped, form_field, "BLOCKED", blockers[-1]))
                continue
            fields.append(self._plan_field(package, mapped, form_field, browser_field, policy))

        if structure_changed and "Application form structure changed; review is required." not in blockers:
            blockers.append("Application form structure changed; review is required.")
        mapped_required_not_fillable = sum(
            item.required and item.action not in {"FILL", "SKIP_OPTIONAL"} for item in fields
        )
        required_not_fillable = max(mapped_required_not_fillable, package_required_unresolved)
        if captcha:
            status = "BLOCKED_BY_CAPTCHA"
        elif compatibility_blocked:
            status = "FORM_CHANGED_REVIEW_REQUIRED"
        elif login or package_blocked or required_not_fillable:
            status = "NEEDS_USER_INPUT"
        else:
            status = "PREVIEW_READY"
        resume = self._resume_intent(package, policy)
        if resume.get("error"):
            blockers.append(str(resume["error"]))
            if any(item.canonical_id == "resume_upload" and item.required for item in fields):
                status = "NEEDS_USER_INPUT"
        return ControlledFillPlan(
            application_id=application_id,
            form_fingerprint=form.fingerprint,
            generated_at=_now(),
            overall_status=status,
            fields=fields,
            total_fields=len(fields),
            fill_fields=sum(item.action == "FILL" for item in fields),
            skipped_fields=sum(item.action == "SKIP_OPTIONAL" for item in fields),
            wait_for_user_fields=sum(item.action == "WAIT_FOR_USER" for item in fields),
            manual_only_fields=sum(item.action == "MANUAL_ONLY" for item in fields),
            blocked_fields=sum(item.action == "BLOCKED" for item in fields),
            unresolved_fields=sum(item.action == "UNRESOLVED" for item in fields),
            required_not_fillable=required_not_fillable,
            intended_resume=resume,
            blockers=list(dict.fromkeys(blockers)),
            safety_flags=[
                "PREVIEW_DEFAULT",
                "EXPLICIT_EXECUTE_REQUIRED",
                "MUTATION_FIREWALL_REQUIRED",
                "LIVE_FILE_UPLOAD_DISABLED",
                "NO_LEGAL_CONSENT",
                "NO_SUBMISSION",
                "NO_SENSITIVE_VALUES_IN_PLAN_JSON",
            ],
        )

    def _plan_field(
        self,
        package: Mapping[str, Any],
        mapped: FieldMappingPlan,
        form_field: ApplicationField,
        browser_field: Any,
        policy: ControlledFillPolicy,
    ) -> ControlledFillFieldPlan:
        canonical = mapped.canonical_question_id
        if mapped.safety_class == "MANUAL_ONLY" or canonical.startswith(LEGAL_CONCEPT_PREFIXES):
            return self._field(mapped, form_field, "MANUAL_ONLY", "Legal, consent, signature, or file control requires the user.")
        if form_field.field_type in {"file", "resume", "cover_letter", "signature", "consent"}:
            return self._field(mapped, form_field, "MANUAL_ONLY", "Phase 3.3 does not upload files or accept legal controls.")
        if canonical in DEMOGRAPHIC_CONCEPTS:
            action = "SKIP_OPTIONAL" if not mapped.required else "MANUAL_ONLY"
            return self._field(mapped, form_field, action, "Protected demographic disclosure is never auto-filled.")
        if canonical in TOTAL_EXPERIENCE_CONCEPTS:
            return self._field(mapped, form_field, "WAIT_FOR_USER", "Total professional experience remains UNKNOWN and is never calculated.")
        if not mapped.required and mapped.action == "OPTIONAL_SKIP":
            return self._field(mapped, form_field, "SKIP_OPTIONAL", "Existing mapping marks this optional field as skippable.")
        if mapped.action == "MANUAL_ONLY":
            return self._field(mapped, form_field, "MANUAL_ONLY", "Existing Phase 3.1 mapping requires manual handling.")
        if mapped.action == "USER_CONFIRMATION":
            return self._field(mapped, form_field, "WAIT_FOR_USER", "Existing mapping requires user confirmation.")
        if mapped.action != "AUTO_READY_FUTURE" or mapped.mapping_confidence != "HIGH":
            action = "UNRESOLVED" if mapped.required else "SKIP_OPTIONAL"
            return self._field(mapped, form_field, action, "HIGH-confidence AUTO_READY_FUTURE eligibility is absent.")
        if self._has_unresolved_package_question(package, mapped):
            action = "UNRESOLVED" if mapped.required else "SKIP_OPTIONAL"
            return self._field(
                mapped,
                form_field,
                action,
                "ApplicationPackage still records this question as unresolved.",
            )
        if canonical == "desired_salary":
            return self._job_dependent(package, mapped, form_field, "salary")
        if canonical in {"willing_to_relocate", "available_start_date", "willing_to_travel"}:
            package_key = "relocation" if canonical == "willing_to_relocate" else canonical
            return self._job_dependent(package, mapped, form_field, package_key)
        allowed = canonical in ALLOWED_STATIC_CONCEPTS or canonical.startswith(
            ("education.", "employment.", "certifications.")
        )
        if canonical.startswith("years_") and canonical.endswith("_experience"):
            allowed = canonical != "years_of_experience"
        if not allowed:
            return self._field(mapped, form_field, "UNRESOLVED", "Canonical category is outside the conservative Phase 3.3 allowlist.")
        if mapped.safety_class not in {"STATIC_CONFIRMED", "NEVER_GUESS"}:
            return self._field(mapped, form_field, "WAIT_FOR_USER", "Safety class does not permit controlled autofill.")
        value, value_kind = self._resolved_value(package, mapped)
        if value is None or value == "":
            action = "UNRESOLVED" if mapped.required else "SKIP_OPTIONAL"
            return self._field(mapped, form_field, action, "No confirmed conflict-free value exists in the ApplicationPackage.")
        intended = self._match_option(value, form_field.options) if form_field.field_type in {"select", "radio", "checkbox", "boolean", "multiselect"} else ""
        if form_field.options and intended == "":
            return self._field(mapped, form_field, "WAIT_FOR_USER", "Confirmed answer does not match a deterministic visible option.")
        return self._field(
            mapped,
            form_field,
            "FILL",
            "Confirmed value passed mapping, safety, structure, and category checks.",
            resolved_value=value,
            value_kind=value_kind,
            intended_option=intended,
        )

    def _job_dependent(
        self,
        package: Mapping[str, Any],
        mapped: FieldMappingPlan,
        form_field: ApplicationField,
        key: str,
    ) -> ControlledFillFieldPlan:
        answers = package.get("job_dependent_answers", {})
        entry = answers.get(key, {}) if isinstance(answers, Mapping) else {}
        if not isinstance(entry, Mapping):
            entry = {}
        value = entry.get("proposed_answer")
        confirmed = not bool(entry.get("requires_confirmation", True))
        approved = bool(entry.get("approved", False))
        if key == "salary" and isinstance(value, str) and value.strip().casefold() == "negotiable":
            confirmed = confirmed and form_field.field_type in {"text", "textarea"}
        elif key == "salary" and isinstance(value, (int, float)):
            confirmed = confirmed and approved
        else:
            confirmed = confirmed and approved
        if not confirmed or value is None or value == "":
            return self._field(mapped, form_field, "WAIT_FOR_USER", "Job-dependent answer is not explicitly resolved and approved in the package.")
        intended = (
            self._match_option(value, form_field.options)
            if form_field.field_type in {"select", "radio", "checkbox", "boolean", "multiselect"}
            else ""
        )
        if form_field.options and not intended:
            return self._field(
                mapped,
                form_field,
                "WAIT_FOR_USER",
                "Approved job-dependent answer does not match one exact visible option.",
            )
        return self._field(
            mapped,
            form_field,
            "FILL",
            "Resolved per-application answer is approved in the package.",
            resolved_value=value,
            value_kind="job_dependent_package",
            intended_option=intended,
        )

    @staticmethod
    def _resolved_value(package: Mapping[str, Any], mapped: FieldMappingPlan) -> tuple[Any, str]:
        facts = package.get("candidate_facts", {})
        source = mapped.source_reference
        if source.startswith("compose("):
            paths = [
                "identity.legal_first_name", "identity.legal_middle_name", "identity.legal_last_name"
            ]
            values = []
            for index, path in enumerate(paths):
                raw = facts.get(path, {}) if isinstance(facts, Mapping) else {}
                value = raw.get("value") if isinstance(raw, Mapping) and raw.get("status") == "confirmed" else None
                if index in {0, 2} and value in {None, ""}:
                    return None, ""
                if value not in {None, ""} and str(value).strip().casefold() not in {"none", "n/a", "not applicable"}:
                    values.append(str(value).strip())
            return " ".join(values), "composed_profile_facts"
        if source and isinstance(facts, Mapping):
            raw = facts.get(source)
            if isinstance(raw, Mapping) and raw.get("status") == "confirmed":
                return raw.get("value"), "candidate_fact"
        for answer in package.get("prepared_answers", []):
            if not isinstance(answer, Mapping):
                continue
            if answer.get("canonical_id") == mapped.canonical_question_id and bool(answer.get("allowed_autofill")):
                return answer.get("value"), "prepared_answer"
        return None, ""

    @staticmethod
    def _match_option(value: Any, options: list[str]) -> str:
        if not options:
            return ""
        desired = "yes" if value is True else "no" if value is False else str(value).strip().casefold()
        exact = [item for item in options if item.strip().casefold() == desired]
        return exact[0] if len(exact) == 1 else ""

    @staticmethod
    def _structural_issue(form_field: ApplicationField | None, browser_field: Any | None) -> str:
        if form_field is None or browser_field is None:
            return "Mapped field is missing from the current form or browser snapshot."
        if not form_field.source_path or form_field.source_path != browser_field.locator:
            return "Field locator no longer matches the verified browser snapshot."
        rendered_label = browser_field.label or browser_field.accessible_name
        if rendered_label and normalize_label(form_field.label) != normalize_label(rendered_label):
            return "Field label/accessibility metadata changed."
        browser_type = normalize_field_type(browser_field.dom_type, rendered_label)
        if form_field.field_type != browser_type:
            return "Field type changed."
        if form_field.required != browser_field.required:
            return "Field required state changed."
        if not browser_field.visible or browser_field.disabled:
            expected_visible = form_field.metadata.get("browser_visible")
            expected_disabled = form_field.metadata.get("browser_disabled")
            if (
                expected_visible is not None
                and expected_disabled is not None
                and (
                    bool(expected_visible) != bool(browser_field.visible)
                    or bool(expected_disabled) != bool(browser_field.disabled)
                )
            ):
                return "Field visible/enabled state changed."
            return "Field is hidden or disabled and is not eligible for fill."
        return ""

    @staticmethod
    def _field(
        mapped: FieldMappingPlan,
        form_field: ApplicationField | None,
        action: str,
        reason: str,
        *,
        resolved_value: Any = None,
        value_kind: str = "",
        intended_option: str = "",
    ) -> ControlledFillFieldPlan:
        return ControlledFillFieldPlan(
            field_id=mapped.field_id,
            canonical_id=mapped.canonical_question_id,
            action=action,
            reason=reason,
            field_type="unknown" if form_field is None else form_field.field_type,
            locator="" if form_field is None else form_field.source_path,
            required=mapped.required,
            mapping_confidence=mapped.mapping_confidence,
            safety_class=mapped.safety_class,
            source_reference=mapped.source_reference,
            value_kind=value_kind,
            intended_option=intended_option,
            resolved_value=resolved_value,
        )

    @staticmethod
    def _resume_intent(package: Mapping[str, Any], policy: ControlledFillPolicy) -> dict[str, Any]:
        raw = package.get("selected_resume", {})
        if not isinstance(raw, Mapping):
            raw = {}
        path = Path(str(raw.get("path", ""))) if raw.get("path") else None
        valid = False
        if bool(raw.get("verified_exists")) and path is not None:
            try:
                valid = path.is_file() and path.suffix.casefold() == ".pdf"
                if valid:
                    with path.open("rb") as handle:
                        valid = handle.read(4) == b"%PDF"
            except OSError:
                valid = False
        return {
            "resume_id": str(raw.get("resume_id", "")),
            "path": "" if path is None else str(path),
            "verified": valid,
            "allow_file_upload": policy.allow_file_upload,
            "live_upload_enabled": False,
            "error": "" if valid else "Selected resume PDF is missing, unreadable, or invalid.",
        }

    @staticmethod
    def _has_unresolved_package_question(
        package: Mapping[str, Any], mapped: FieldMappingPlan
    ) -> bool:
        for item in package.get("unresolved_questions", []):
            if not isinstance(item, Mapping) or bool(item.get("resolved", False)):
                continue
            if item.get("canonical_id") == mapped.canonical_question_id:
                return True
            if item.get("question_id") == mapped.field_id:
                return True
        return False
