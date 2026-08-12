"""Build a concise private human review from existing Phase 3 artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..application_forms.models import ApplicationForm, ApplicationMappingPlan
from ..browser_forms import BrowserRenderedForm
from ..controlled_fill.models import ControlledFillPlan
from .documents import validate_pdf_document
from .fingerprints import package_fingerprint
from .models import ReviewAnswer, ReviewDocument, ReviewManualItem, ReviewPackage


SENSITIVE_CONCEPTS = {
    "legal_first_name", "legal_middle_name", "legal_last_name", "legal_full_name",
    "contact_email", "contact_phone", "address.street", "address.line_2",
    "address.city", "address.state", "address.postal_code", "address.country",
    "work_authorization_us", "sponsorship_now", "sponsorship_future",
}
HIGHLIGHT_PREFIXES = (
    "desired_salary", "sponsorship", "work_authorization", "willing_to_relocate",
    "available_start_date", "willing_to_travel", "years_",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_review_package(path: str | Path) -> ReviewPackage:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Review package root must be an object.")
    return ReviewPackage.from_dict(value)


class ReviewPackageBuilder:
    def build(
        self,
        package: Mapping[str, Any],
        form: ApplicationForm,
        mapping: ApplicationMappingPlan,
        plan: ControlledFillPlan,
        rendered: BrowserRenderedForm,
        *,
        queue_record: Mapping[str, Any] | None = None,
    ) -> ReviewPackage:
        application_id = str(package.get("application_id", ""))
        job = package.get("job_snapshot", {})
        job = job if isinstance(job, Mapping) else {}
        queue_record = queue_record or {}
        answers = [self._answer(package, item) for item in plan.fields if item.canonical_id]
        documents = self._documents(package, form, mapping)
        manual = self._manual_items(package, plan, rendered)
        package_required = sum(
            bool(item.get("required")) and not bool(item.get("resolved", False))
            for item in package.get("unresolved_questions", [])
            if isinstance(item, Mapping)
        )
        ready_documents = {
            "resume_upload": any(item.document_type == "resume" and item.status == "READY" for item in documents),
            "cover_letter_upload": any(item.document_type == "cover_letter" and item.status == "READY" for item in documents),
        }
        plan_required = sum(
            item.required
            and item.action in {"WAIT_FOR_USER", "UNRESOLVED", "BLOCKED"}
            and not ready_documents.get(item.canonical_id, False)
            and not any(token in item.reason.casefold() for token in ("captcha", "login"))
            for item in plan.fields
        )
        unresolved = max(plan_required, package_required)
        blockers = self._hard_blockers(package, plan, documents, unresolved)
        status = "CHANGES_REQUIRED" if blockers or unresolved else "READY_FOR_APPROVAL"
        return ReviewPackage(
            application_id=application_id,
            job_id=str(job.get("job_id", queue_record.get("job_id", ""))),
            form_fingerprint=form.fingerprint,
            package_fingerprint=package_fingerprint(package),
            generated_at=utc_now(),
            review_status=status,
            job={
                "company": str(job.get("company", queue_record.get("company", ""))),
                "title": str(job.get("title", queue_record.get("title", ""))),
                "location": str(job.get("location", "")),
                "salary": job.get("salary"),
                "score": queue_record.get("fit_score", job.get("fit_score")),
                "selected_resume": str(package.get("selected_resume", {}).get("resume_id", ""))
                if isinstance(package.get("selected_resume"), Mapping) else "",
                "ats": mapping.ats,
            },
            answers=answers,
            documents=documents,
            manual_items=manual,
            unresolved_required_count=unresolved,
            salary_state=self._salary_state(package),
            work_authorization_state=self._answer_state(package, "work_authorization_us"),
            sponsorship_state=self._sponsorship_state(package),
            blockers=blockers,
            safety_flags=[
                "PRIVATE_HUMAN_REVIEW",
                "APPROVAL_NOT_IMPLIED",
                "SENSITIVE_VALUES_MINIMIZED",
                "LEGAL_ITEMS_REQUIRE_INDIVIDUAL_USER_ACTION",
                "NO_SUBMISSION_DURING_REVIEW",
            ],
        )

    @staticmethod
    def _answer(package: Mapping[str, Any], field: Any) -> ReviewAnswer:
        value = field.resolved_value
        sensitive = field.canonical_id in SENSITIVE_CONCEPTS
        if field.action != "FILL":
            display: Any = f"[{field.action}] {field.reason}"
        elif sensitive:
            display = f"Confirmed private value for {field.canonical_id} (not duplicated)."
        else:
            display = value
        provenance: list[str] = []
        facts = package.get("candidate_facts", {})
        if isinstance(facts, Mapping) and field.source_reference in facts:
            fact = facts.get(field.source_reference, {})
            source = fact.get("source", []) if isinstance(fact, Mapping) else []
            provenance = [str(item) for item in source] if isinstance(source, list) else [str(source)] if source else []
        for prepared in package.get("prepared_answers", []):
            if isinstance(prepared, Mapping) and prepared.get("canonical_id") == field.canonical_id:
                provenance.extend(str(item) for item in prepared.get("provenance", []))
        return ReviewAnswer(
            field_id=field.field_id,
            canonical_id=field.canonical_id,
            proposed_answer=display,
            source=field.source_reference or field.value_kind,
            provenance=list(dict.fromkeys(provenance)),
            safety_class=field.safety_class,
            action=field.action,
            required=field.required,
            sensitive=sensitive,
            highlighted=field.canonical_id.startswith(HIGHLIGHT_PREFIXES),
        )

    @staticmethod
    def _documents(
        package: Mapping[str, Any],
        form: ApplicationForm,
        mapping: ApplicationMappingPlan,
    ) -> list[ReviewDocument]:
        form_fields = {item.field_id: item for item in form.fields}
        resume_mapping = next(
            (item for item in mapping.fields if item.canonical_question_id == "resume_upload"),
            None,
        )
        resume_field = form_fields.get(resume_mapping.field_id) if resume_mapping else None
        resume = package.get("selected_resume", {})
        resume = resume if isinstance(resume, Mapping) else {}
        result = [validate_pdf_document(
            document_type="resume",
            document_id=str(resume.get("resume_id", "")),
            configured_path=str(resume.get("path", "")),
            required=bool(resume_mapping and resume_mapping.required),
            approved_in_package=bool(resume.get("verified_exists")) and not bool(resume.get("substitution_allowed")),
            constraints={} if resume_field is None else resume_field.validation_rules,
        )]

        cover_mapping = next(
            (item for item in mapping.fields if item.canonical_question_id == "cover_letter_upload"),
            None,
        )
        cover_status = str(package.get("cover_letter_status", "unknown"))
        cover_required = bool(cover_mapping and cover_mapping.required) or cover_status == "required"
        cover_field = form_fields.get(cover_mapping.field_id) if cover_mapping else None
        approved_cover = next(
            (
                item for item in package.get("optional_documents", [])
                if isinstance(item, Mapping)
                and item.get("document_type") == "cover_letter"
                and bool(item.get("approved"))
            ),
            None,
        )
        if cover_required or approved_cover or cover_status not in {"not_required", "optional"}:
            approved_cover = approved_cover or {}
            result.append(validate_pdf_document(
                document_type="cover_letter",
                document_id=str(approved_cover.get("document_id", "")),
                configured_path=str(approved_cover.get("path", "")),
                required=cover_required,
                approved_in_package=bool(approved_cover),
                constraints={} if cover_field is None else cover_field.validation_rules,
            ))
        return result

    @staticmethod
    def _manual_items(
        package: Mapping[str, Any],
        plan: ControlledFillPlan,
        rendered: BrowserRenderedForm,
    ) -> list[ReviewManualItem]:
        items: list[ReviewManualItem] = []
        legal_markers = (
            "consent", "signature", "attestation", "arbitration", "privacy",
            "background_check", "conflict_of_interest", "certif",
        )
        for field in plan.fields:
            if field.action not in {"MANUAL_ONLY", "WAIT_FOR_USER", "UNRESOLVED", "BLOCKED"}:
                continue
            if field.action == "BLOCKED" and any(
                token in field.reason.casefold() for token in ("captcha", "login")
            ):
                continue
            if field.canonical_id in {"resume_upload", "cover_letter_upload"}:
                category = "document"
            else:
                category = "legal" if any(marker in field.canonical_id for marker in legal_markers) else "question"
            items.append(ReviewManualItem(
                item_id=field.field_id,
                category=category,
                label=field.canonical_id or field.field_id,
                required=field.required,
                reason=field.reason,
            ))
        for unresolved in package.get("unresolved_questions", []):
            if not isinstance(unresolved, Mapping) or bool(unresolved.get("resolved", False)):
                continue
            item_id = str(unresolved.get("question_id", unresolved.get("canonical_id", "unresolved")))
            if not any(item.item_id == item_id for item in items):
                items.append(ReviewManualItem(
                    item_id=item_id,
                    category="unresolved_question",
                    label=str(unresolved.get("canonical_id", item_id)),
                    required=bool(unresolved.get("required")),
                    reason=str(unresolved.get("reason", unresolved.get("why_unresolved", "User input required."))),
                ))
        if rendered.anti_bot or rendered.status == "BLOCKED_BY_ANTI_BOT":
            items.append(ReviewManualItem("captcha", "captcha", "CAPTCHA", True, "User must solve the challenge manually."))
        if rendered.login_wall or rendered.status == "LOGIN_REQUIRED":
            items.append(ReviewManualItem("login", "login", "Login/account wall", True, "User must complete login manually; passwords are never stored."))
        unique: dict[str, ReviewManualItem] = {}
        for item in items:
            unique.setdefault(item.item_id, item)
        return list(unique.values())

    @staticmethod
    def _hard_blockers(
        package: Mapping[str, Any],
        plan: ControlledFillPlan,
        documents: list[ReviewDocument],
        unresolved: int,
    ) -> list[str]:
        blockers = [
            item for item in plan.blockers
            if not any(token in item.casefold() for token in ("captcha", "login", "unresolved required question"))
        ]
        if str(package.get("package_readiness", "")).upper() in {"BLOCKED", "INVALID"}:
            blockers.append(f"ApplicationPackage readiness is {package.get('package_readiness')}.")
        if unresolved:
            blockers.append(f"{unresolved} required field(s) remain unresolved or unavailable.")
        for document in documents:
            if document.required and (
                document.status != "READY" or not document.approved_in_package
            ):
                blockers.append(f"Required {document.document_type} is not upload-ready: {document.reason}")
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _salary_state(package: Mapping[str, Any]) -> dict[str, Any]:
        answers = package.get("job_dependent_answers", {})
        salary = answers.get("salary", {}) if isinstance(answers, Mapping) else {}
        salary = salary if isinstance(salary, Mapping) else {}
        value = salary.get("proposed_answer")
        return {
            "state": "RESOLVED" if value is not None and value != "" and not salary.get("requires_confirmation", True) else "UNRESOLVED",
            "proposed_answer": value if value is not None and value != "" else None,
            "approved": bool(salary.get("approved", False)) or (
                isinstance(value, str) and value.strip().casefold() == "negotiable"
                and not bool(salary.get("requires_confirmation", True))
            ),
        }

    @staticmethod
    def _answer_state(package: Mapping[str, Any], canonical: str) -> str:
        for item in package.get("prepared_answers", []):
            if isinstance(item, Mapping) and item.get("canonical_id") == canonical:
                return "CONFIRMED" if bool(item.get("allowed_autofill")) else "REVIEW_REQUIRED"
        return "UNKNOWN"

    @classmethod
    def _sponsorship_state(cls, package: Mapping[str, Any]) -> str:
        states = [cls._answer_state(package, key) for key in ("sponsorship_now", "sponsorship_future")]
        if all(item == "CONFIRMED" for item in states):
            return "CONFIRMED"
        if any(item == "REVIEW_REQUIRED" for item in states):
            return "REVIEW_REQUIRED"
        return "UNKNOWN"
