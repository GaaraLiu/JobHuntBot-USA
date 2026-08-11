"""Deterministic form-field normalization and read-only mapping plans."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..answer_bank import AnswerBank, AnswerResolver, ApplicationQuestion
from ..application_profile import CandidateApplicationProfile
from .base import normalize_label
from .models import (
    ApplicationField,
    ApplicationForm,
    ApplicationMappingPlan,
    FieldMappingPlan,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_LEGAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("electronic_signature", r"\b(electronic|digital)?\s*signature\b"),
    ("background_check_consent", r"\b(background check|background screening).*(consent|authorize|authorization)\b"),
    ("arbitration_agreement", r"\b(arbitration|dispute resolution).*(agree|agreement|acknowledg)\b"),
    ("privacy_acknowledgment", r"\b(privacy (notice|policy)|data processing).*(agree|consent|acknowledg)\b"),
    ("conflict_of_interest_certification", r"\bconflict of interest.*(certif|attest|acknowledg)\b"),
    ("disability_attestation", r"\bdisability.*(certif|attest|signature)\b"),
    ("accuracy_attestation", r"\b(certify|attest).*(accurate|true|complete|correct)\b"),
)

_HIGH_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("legal_first_name", (r"^(legal )?first name$", r"^given name$")),
    ("legal_middle_name", (r"^(legal )?middle name$", r"^middle initial$")),
    ("legal_last_name", (r"^(legal )?(last|family|surname) name$", r"^surname$")),
    ("preferred_name", (r"^(preferred|professional) name$", r"^name you go by$")),
    ("contact_email", (r"^(email|email address)$", r"^primary email$")),
    ("contact_phone", (r"^(phone|phone number|mobile|mobile phone)$",)),
    ("address.street", (r"^(street address|address line 1|address 1)$",)),
    ("address.line_2", (r"^(address line 2|address 2|apartment|suite|unit)$",)),
    ("address.city", (r"^(city|town)$",)),
    ("address.state", (r"^(state|state/province|province|region)$",)),
    ("address.postal_code", (r"^(zip|zip code|postal code)$",)),
    ("address.country", (r"^(country|country/region)$",)),
    ("address.combined", (r"^(current |home |mailing )?address$",)),
    ("linkedin", (r"^(linkedin|linkedin url|linkedin profile)$",)),
    ("github", (r"^(github|github url|github profile)$",)),
    ("portfolio", (r"^(portfolio|portfolio url|personal website|website)$",)),
    ("work_authorization_us", (
        r"^are you (legally )?authorized to work in (the )?(u\.?s\.?|united states)( without restriction)?\??$",
        r"^legally authorized to work in (the )?(u\.?s\.?|united states)\??$",
    )),
    ("sponsorship_future", (
        r"^(will|do) you (now or in the future )?(require|need).*(employment )?(visa )?sponsorship\??$",
        r"^will you require sponsorship (now or )?in the future\??$",
    )),
    ("sponsorship_now", (r"^(do|will) you (currently|now) (require|need).*(visa )?sponsorship\??$",)),
    ("us_citizen", (r"^are you (a )?(u\.?s\.?|united states) citizen\??$", r"^(u\.?s\.? )?citizenship status$")),
    ("permanent_resident", (r"^are you (a )?(u\.?s\.? )?(lawful )?permanent resident\??$", r"^green card holder\??$")),
    ("security_clearance", (r"^(do you (currently )?(hold|have)|what is your).*(security )?clearance", r"^security clearance( level| status)?$")),
    ("driver_license", (r"^(do you (possess|have)|have you got).*(valid )?driver'?s? licen[cs]e\??$", r"^driver'?s? licen[cs]e$")),
    ("professional_license", (r"^professional licen[cs]e(s)?$", r"^licen[cs]e number$")),
    ("certifications", (r"^(professional )?certifications?( and credentials)?$",)),
    ("desired_salary", (
        r"^(desired|expected|minimum|required) (base )?(salary|compensation|pay)$",
        r"^(salary|compensation) (expectation|requirement)s?$",
        r"^(desired|expected) hourly rate$",
    )),
    ("willing_to_relocate", (r"^are you willing to relocate( for this (role|position))?\??$", r"^relocation willingness$")),
    ("preferred_location", (r"^(preferred|desired) (work )?location$",)),
    ("available_start_date", (r"^(available|earliest|desired) start date$", r"^when can you start\??$")),
    ("willing_to_travel", (r"^are you willing to travel\??$", r"^(maximum |preferred )?travel percentage$")),
    ("years_of_experience", (r"^(total )?(years|number of years) of (professional|full.?time) experience$",)),
    ("gender", (r"^gender( identity)?$", r"^sex$")),
    ("race_ethnicity", (r"^(race|ethnicity|race/ethnicity)$",)),
    ("veteran_status", (r"^(protected )?veteran status$", r"^veteran$")),
    ("disability", (r"^(disability|disability status|self.?identification of disability)$",)),
    ("previously_employed_by_company", (r"^have you (ever )?(previously )?(worked|been employed) (for|by) (us|this company)\??$",)),
    ("related_to_current_employee", (r"^are you related to (a|any) (current )?employee\??$",)),
    ("non_compete", (r"^(are you subject to|do you have).*(non.?compete|restrictive covenant)\??$",)),
    ("government_employment", (r"^(have you|are you).*(government|public official).*(employed|employment|service)\??$",)),
)

_PROFILE_PATHS = {
    "legal_first_name": "identity.legal_first_name",
    "legal_middle_name": "identity.legal_middle_name",
    "legal_last_name": "identity.legal_last_name",
    "preferred_name": "identity.preferred_name",
    "contact_email": "identity.email",
    "contact_phone": "identity.phone",
    "address.street": "identity.current_address",
    "address.city": "identity.current_city",
    "address.state": "identity.current_state",
    "address.postal_code": "identity.postal_code",
    "address.country": "identity.current_country",
    "linkedin": "links.linkedin",
    "github": "links.github",
    "portfolio": "links.portfolio",
}


class FieldConceptMapper:
    """Map labels conservatively; sensitive/legal mappings require exact evidence."""

    def map_field(self, field: ApplicationField) -> tuple[str, str, str]:
        label = normalize_label(field.label or field.placeholder)
        for canonical_id, pattern in _LEGAL_PATTERNS:
            if re.search(pattern, label):
                return canonical_id, "HIGH", "MANUAL_ONLY"
        if field.field_type == "resume":
            return "resume_upload", "HIGH", "MANUAL_ONLY"
        if field.field_type == "cover_letter":
            return "cover_letter_upload", "HIGH", "MANUAL_ONLY"
        if field.field_type == "signature":
            return "electronic_signature", "HIGH", "MANUAL_ONLY"
        if field.field_type == "consent":
            return "legal_consent", "MEDIUM", "MANUAL_ONLY"
        if field.field_type == "file":
            return self._file_concept(label), "HIGH", "MANUAL_ONLY"
        repeated = self._repeat_concept(field, label)
        if repeated:
            return repeated, "HIGH", "CONFIRM_BEFORE_USE"
        if field.field_type == "email" and label in {"email", "email address", "primary email"}:
            return "contact_email", "HIGH", "STATIC_CONFIRMED"
        if field.field_type == "phone" and label in {"phone", "phone number", "mobile", "mobile phone"}:
            return "contact_phone", "HIGH", "STATIC_CONFIRMED"
        for canonical_id, patterns in _HIGH_PATTERNS:
            if any(re.search(pattern, label) for pattern in patterns):
                return canonical_id, "HIGH", "UNKNOWN"
        domain = self._domain_experience(label)
        if domain:
            return domain, "HIGH", "NEVER_GUESS"
        if re.search(r"\b(salary|compensation|hourly rate|pay requirement)\b", label):
            return "desired_salary", "MEDIUM", "JOB_DEPENDENT"
        if re.search(r"\bwork authori[sz]ation\b", label):
            return "", "LOW", "NEVER_GUESS"
        return "", "UNMAPPED", "UNKNOWN"

    @staticmethod
    def _file_concept(label: str) -> str:
        if "transcript" in label:
            return "transcript_upload"
        if "portfolio" in label:
            return "portfolio_upload"
        return "other_attachment"

    @staticmethod
    def _domain_experience(label: str) -> str:
        match = re.search(r"(?:how many |number of )?years(?: of)? (.+?) experience", label)
        if not match:
            return ""
        domain = re.sub(r"[^a-z0-9]+", "_", match.group(1)).strip("_")
        aliases = {
            "python": "python",
            "data_analysis": "data_analysis",
            "machine_learning_engineering": "ml_engineering",
            "mle": "ml_engineering",
            "power_bi": "power_bi",
        }
        normalized = aliases.get(domain, domain)
        return f"years_{normalized}_experience" if normalized else ""

    @staticmethod
    def _repeat_concept(field: ApplicationField, label: str) -> str:
        group = (field.repeat_group or field.section).casefold()
        if "education" in group:
            prefix = "education"
            aliases = {"school": "institution", "university": "institution", "college": "institution", "major": "field_of_study", "graduation date": "graduation_date"}
        elif "employment" in group or "work history" in group or "experience" == group.strip():
            prefix = "employment"
            aliases = {"company": "employer", "organization": "employer", "job title": "title", "position": "title"}
        elif "certification" in group or "credential" in group:
            prefix = "certifications"
            aliases = {"certification": "name", "credential": "name", "date issued": "issued"}
        else:
            return ""
        normalized = aliases.get(label, re.sub(r"[^a-z0-9]+", "_", label).strip("_"))
        allowed = {
            "education": {"institution", "degree", "field_of_study", "start_date", "end_date", "graduation_date", "gpa"},
            "employment": {"employer", "title", "start_date", "end_date", "location", "currently_employed", "approved_description"},
            "certifications": {"name", "issuer", "issued", "credential_id"},
        }
        return f"{prefix}.{normalized}" if normalized in allowed[prefix] else ""


class ApplicationFormMapper:
    def __init__(
        self,
        profile: CandidateApplicationProfile,
        answer_bank: AnswerBank,
        *,
        concept_mapper: FieldConceptMapper | None = None,
    ):
        self.profile = profile
        self.answer_bank = answer_bank
        self.concept_mapper = concept_mapper or FieldConceptMapper()

    def build_plan(
        self,
        form: ApplicationForm,
        *,
        application_id: str = "",
        job: Mapping[str, Any] | None = None,
        selected_resume: Mapping[str, Any] | None = None,
    ) -> ApplicationMappingPlan:
        job = job or {}
        selected_resume = selected_resume or {}
        plans: list[FieldMappingPlan] = []
        blockers = list(form.blockers)
        for field in form.fields:
            canonical_id, confidence, default_safety = self.concept_mapper.map_field(field)
            field.canonical_question_id = canonical_id
            field.mapping_confidence = confidence
            action, safety, status, reason, source = self._plan_field(
                field,
                canonical_id,
                confidence,
                default_safety,
                job,
                selected_resume,
            )
            field.safety_class = safety
            field.answer_status = status
            plans.append(
                FieldMappingPlan(
                    field_id=field.field_id,
                    label=field.label,
                    required=field.required,
                    canonical_question_id=canonical_id,
                    mapping_confidence=confidence,
                    safety_class=safety,
                    answer_status=status,
                    action=action,
                    reason=reason,
                    source_reference=source,
                    repeat_group=field.repeat_group,
                    repeat_index=field.repeat_index,
                )
            )

        required_unresolved = sum(item.required and item.action == "UNRESOLVED" for item in plans)
        required_human_action = sum(
            item.required and item.action in {"USER_CONFIRMATION", "MANUAL_ONLY"}
            for item in plans
        )
        if form.detection_status not in {"detected", "partial"} or blockers:
            readiness = "BLOCKED"
        elif required_unresolved or required_human_action:
            readiness = "NEEDS_USER_INPUT"
        else:
            readiness = "READY"
        return ApplicationMappingPlan(
            application_id=application_id,
            job_id=form.job_id,
            form_fingerprint=form.fingerprint or form.compute_fingerprint(),
            ats=form.ats,
            generated_at=_now(),
            fields=plans,
            total_fields=len(plans),
            mapped_fields=sum(bool(item.canonical_question_id) for item in plans),
            required_fields=sum(item.required for item in plans),
            required_unresolved=required_unresolved,
            manual_only=sum(item.action == "MANUAL_ONLY" for item in plans),
            potential_future_autofill=sum(item.action == "AUTO_READY_FUTURE" for item in plans),
            user_confirmation=sum(item.action == "USER_CONFIRMATION" for item in plans),
            optional_skip=sum(item.action == "OPTIONAL_SKIP" for item in plans),
            package_readiness=readiness,
            blockers=blockers,
            safety_flags=[
                "READ_DETECT_MAP_ONLY",
                "NO_LIVE_FORM_VALUES",
                "NO_FILE_UPLOAD",
                "NO_LEGAL_CONSENT",
                "NO_SUBMISSION",
            ],
        )

    def _plan_field(
        self,
        field: ApplicationField,
        canonical_id: str,
        confidence: str,
        default_safety: str,
        job: Mapping[str, Any],
        selected_resume: Mapping[str, Any],
    ) -> tuple[str, str, str, str, str]:
        if default_safety == "MANUAL_ONLY" or field.field_type in {"consent", "signature", "file", "resume", "cover_letter"}:
            status = "manual_required"
            if canonical_id == "resume_upload" and selected_resume.get("verified_exists"):
                status = "selected_resume_reference_available"
            return "MANUAL_ONLY", "MANUAL_ONLY", status, "Detected structure only; Phase 3.1 never accepts, signs, or uploads.", ""
        if not canonical_id:
            if field.required:
                return "UNRESOLVED", default_safety, "unmapped", "No deterministic canonical concept matched the required field.", ""
            return "OPTIONAL_SKIP", default_safety, "unmapped_optional", "Optional unmapped field may be skipped or reviewed.", ""
        if confidence != "HIGH":
            return "USER_CONFIRMATION", default_safety, "mapping_review", "Only HIGH-confidence mappings may be future autofill candidates.", ""

        repeated = self._repeat_profile_status(field, canonical_id)
        if repeated is not None:
            status, source = repeated
            if status == "confirmed":
                return "USER_CONFIRMATION", "CONFIRM_BEFORE_USE", "profile_group_available", "Repeatable profile data exists; field/group alignment requires review.", source
            if not field.required:
                return "OPTIONAL_SKIP", "CONFIRM_BEFORE_USE", status, "Optional repeatable value is unavailable.", source
            return "UNRESOLVED", "CONFIRM_BEFORE_USE", status, "Required repeatable value is unavailable in the private profile.", source

        profile_path = _PROFILE_PATHS.get(canonical_id)
        if profile_path:
            fact = self.profile.get_fact(profile_path)
            if fact and fact.usable and not self.profile.has_unresolved_conflict(profile_path):
                return "AUTO_READY_FUTURE", "STATIC_CONFIRMED", "confirmed", "Confirmed private profile fact; eligible only for a future fill phase.", profile_path
            if not field.required:
                return "OPTIONAL_SKIP", "UNKNOWN", "unknown", "Optional private profile fact is unavailable.", profile_path
            return "UNRESOLVED", "UNKNOWN", "unknown", "Required private profile fact is not confirmed.", profile_path

        entry = self.answer_bank.entries.get(canonical_id)
        if entry is None:
            if field.required:
                return "UNRESOLVED", default_safety, "no_answer_bank_entry", "Canonical concept has no answer-bank entry.", ""
            return "OPTIONAL_SKIP", default_safety, "no_answer_bank_entry", "Optional concept has no answer-bank entry.", ""
        safety = entry.safety_class
        question_type = field.field_type if field.field_type in {"text", "number", "boolean", "select", "date"} else "text"
        question = ApplicationQuestion(
            question_id=field.field_id,
            text=field.label,
            answer_type=question_type,
            required=field.required,
            options=field.options,
            canonical_id=canonical_id,
            source="application_form_mapping",
        )
        result = AnswerResolver(self.profile, self.answer_bank).resolve([question], job)
        if result.answers:
            answer = result.answers[0]
            if safety == "STATIC_CONFIRMED" and entry.allowed_autofill:
                return "AUTO_READY_FUTURE", safety, "prepared_confirmed", "HIGH-confidence mapping to a confirmed static fact; no live fill occurred.", entry.profile_fact_path
            if safety == "OPTIONAL_PREFER_NOT_TO_ANSWER" and not field.required:
                return "OPTIONAL_SKIP", safety, "prefer_not_to_answer", "Existing optional demographic policy applies; no protected value was inferred.", "demographics_policy"
            return "USER_CONFIRMATION", safety, "prepared_review", "Answer can be prepared but is not future-autofill eligible under its safety class.", entry.profile_fact_path
        unresolved = result.unresolved[0] if result.unresolved else None
        status = unresolved.reason_code if unresolved else "unresolved"
        if safety == "OPTIONAL_PREFER_NOT_TO_ANSWER" and not field.required:
            return "OPTIONAL_SKIP", safety, status, "Optional demographic field remains isolated and may be skipped.", "demographics_policy"
        if not field.required:
            return "OPTIONAL_SKIP", safety, status, "Optional unresolved field does not block readiness.", entry.profile_fact_path
        return "UNRESOLVED", safety, status, unresolved.reason if unresolved else "Required answer is unresolved.", entry.profile_fact_path

    def _repeat_profile_status(self, field: ApplicationField, canonical_id: str) -> tuple[str, str] | None:
        if "." not in canonical_id:
            return None
        group, fact_name = canonical_id.split(".", 1)
        collection_name = {"education": "education", "employment": "employment_history", "certifications": "certifications"}.get(group)
        if not collection_name:
            return None
        collection = self.profile.raw.get(collection_name, [])
        if not isinstance(collection, list) or not collection:
            return "unknown", collection_name
        index = field.repeat_index if field.repeat_index is not None else 0
        if index < 0 or index >= len(collection) or not isinstance(collection[index], Mapping):
            return "unknown", f"{collection_name}[{index}]"
        value = collection[index].get(fact_name)
        if isinstance(value, Mapping):
            if value.get("status") == "confirmed" and value.get("value") not in {None, ""}:
                return "confirmed", f"{collection_name}[{index}].{fact_name}"
            return "unknown", f"{collection_name}[{index}].{fact_name}"
        if value is not None and value != "":
            return "confirmed", f"{collection_name}[{index}].{fact_name}"
        return "unknown", f"{collection_name}[{index}].{fact_name}"


def load_application_form(path: str | Path) -> ApplicationForm:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Application form JSON root must be an object.")
    return ApplicationForm.from_dict(value)


def save_private_json(path: str | Path, value: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    if "my-materials" not in {part.casefold() for part in target.parts}:
        raise ValueError("Real application forms and mappings must be saved under my-materials/.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
