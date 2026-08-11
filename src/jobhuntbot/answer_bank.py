"""Deterministic candidate answer-bank validation and question resolution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .application_profile import CandidateApplicationProfile


ANSWER_TYPES = {
    "KNOWN_FACT",
    "JOB_DEPENDENT",
    "USER_CONFIRMATION",
    "OPTIONAL_PREFER_NOT",
    "UNKNOWN",
    "NEVER_GUESS",
}
SAFETY_CLASSES = {
    "STATIC_CONFIRMED",
    "JOB_DEPENDENT",
    "CONFIRM_BEFORE_USE",
    "OPTIONAL_PREFER_NOT_TO_ANSWER",
    "MANUAL_ONLY",
    "UNKNOWN",
    "NEVER_GUESS",
}
ENTRY_STATUSES = {"confirmed", "unknown", "needs_confirmation", "deprecated"}
QUESTION_TYPES = {"boolean", "text", "number", "select", "date"}


class AnswerBankError(ValueError):
    """Raised for invalid answer-bank input."""


@dataclass(frozen=True, slots=True)
class AnswerBankEntry:
    canonical_id: str
    category: str
    answer_type: str
    value: Any
    status: str
    confidence: str
    provenance: list[str]
    reusable: bool
    job_dependent: bool
    requires_user_confirmation: bool
    allowed_autofill: bool
    safety_class: str
    profile_fact_path: str = ""
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AnswerBankEntry":
        provenance = value.get("provenance", [])
        if isinstance(provenance, str):
            provenance = [provenance]
        return cls(
            canonical_id=str(value.get("canonical_id") or value.get("canonical_question_id", "")),
            category=str(value.get("category", "")),
            answer_type=str(value.get("answer_type", "UNKNOWN")),
            value=value.get("value"),
            status=str(value.get("status", "unknown")),
            confidence=str(value.get("confidence", "")),
            provenance=[str(item) for item in provenance],
            reusable=bool(value.get("reusable", False)),
            job_dependent=bool(value.get("job_dependent", False)),
            requires_user_confirmation=bool(value.get("requires_user_confirmation", False)),
            allowed_autofill=bool(value.get("allowed_autofill", value.get("allowed_auto_fill", False))),
            safety_class=str(value.get("safety_class", "UNKNOWN")),
            profile_fact_path=str(value.get("profile_fact_path", "")),
            notes=str(value.get("notes", "")),
        )


@dataclass(slots=True)
class AnswerBank:
    schema_version: int
    entries: dict[str, AnswerBankEntry]
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApplicationQuestion:
    question_id: str
    text: str
    answer_type: str = "text"
    required: bool = True
    options: list[str] = field(default_factory=list)
    canonical_id: str = ""
    source: str = "manual_structured_input"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ApplicationQuestion":
        text = str(value.get("text") or value.get("question_text", "")).strip()
        digest = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:16]
        return cls(
            question_id=str(value.get("question_id") or f"question_{digest}"),
            text=text,
            answer_type=str(value.get("answer_type") or value.get("field_type", "text")),
            required=bool(value.get("required", True)),
            options=[str(item) for item in value.get("options", [])],
            canonical_id=str(value.get("canonical_id", "")),
            source=str(value.get("source", "manual_structured_input")),
        )


@dataclass(frozen=True, slots=True)
class PreparedAnswer:
    question_id: str
    canonical_id: str
    value: Any
    safety_class: str
    provenance: list[str]
    allowed_autofill: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "canonical_id": self.canonical_id,
            "value": self.value,
            "safety_class": self.safety_class,
            "provenance": self.provenance,
            "allowed_autofill": self.allowed_autofill,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class UnresolvedQuestion:
    question_id: str
    canonical_id: str
    question_text: str
    required: bool
    reason_code: str
    reason: str
    safety_class: str
    resolution_needed: str

    def to_dict(self, application_id: str = "") -> dict[str, Any]:
        return {
            "application_id": application_id,
            "question_id": self.question_id,
            "canonical_id": self.canonical_id,
            "original_question": self.question_text,
            "normalized_question": self.canonical_id,
            "question_text": self.question_text,
            "required": self.required,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "why_unresolved": self.reason,
            "risk_level": "HIGH" if self.required else "LOW",
            "safety_class": self.safety_class,
            "resolution_needed": self.resolution_needed,
            "suggested_user_action": self.resolution_needed,
            "answer_type": "user_input",
            "resolved": False,
            "resolved_value": None,
            "resolved_at": "",
        }


@dataclass(slots=True)
class ResolutionResult:
    answers: list[PreparedAnswer] = field(default_factory=list)
    unresolved: list[UnresolvedQuestion] = field(default_factory=list)


_QUESTION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("work_authorization_us", (r"authorized to work.*(u\.?s\.?|united states)", r"legally authorized.*work")),
    ("sponsorship_future", (r"sponsorship.*future", r"now or in the future.*sponsor")),
    ("sponsorship_now", (r"require.*sponsorship", r"need.*sponsorship")),
    ("us_citizen", (r"u\.?s\.? citizen", r"citizen of the united states")),
    ("permanent_resident", (r"permanent resident", r"green card")),
    ("security_clearance", (r"security clearance", r"active clearance")),
    ("desired_salary", (r"salary", r"compensation", r"pay expectation")),
    ("willing_to_relocate", (r"willing.*relocat", r"relocation")),
    ("preferred_location", (r"preferred.*location", r"work location preference")),
    ("years_python_experience", (r"years.*python", r"python.*years")),
    ("years_data_analysis_experience", (r"years.*data analys", r"data analys.*years")),
    ("years_ml_engineering_experience", (r"years.*machine learning engineer", r"mle.*years")),
    ("years_of_experience", (r"years of (professional )?experience", r"total.*years.*experience")),
    ("highest_degree", (r"highest.*degree", r"highest.*education")),
    ("available_start_date", (r"start date", r"available to start")),
    ("willing_to_travel", (r"willing.*travel", r"travel.*percent")),
    ("previously_employed_by_company", (r"previously.*employ", r"worked.*(us|company) before")),
    ("related_to_current_employee", (r"related.*employee", r"relative.*work")),
    ("non_compete", (r"non.?compete", r"restrictive covenant")),
    ("government_employment", (r"government.*employ", r"public official")),
    ("driver_license", (r"driver.?s license", r"driving licen[cs]e")),
    ("professional_license", (r"professional licen[cs]e", r"certification number")),
    ("race_ethnicity", (r"race", r"ethnicity")),
    ("gender", (r"gender", r"sex")),
    ("disability", (r"disability", r"disabled")),
    ("veteran_status", (r"veteran", r"military status")),
)


def normalize_question(question: ApplicationQuestion) -> str:
    if question.canonical_id:
        return question.canonical_id
    lowered = " ".join(question.text.lower().split())
    for canonical_id, patterns in _QUESTION_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            return canonical_id
    return ""


def load_answer_bank(path: str | Path) -> AnswerBank:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnswerBankError(f"Could not read answer bank {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnswerBankError(f"Invalid JSON in answer bank {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AnswerBankError("Answer bank root must be a JSON object.")
    entries: dict[str, AnswerBankEntry] = {}
    for index, value in enumerate(raw.get("entries", [])):
        if not isinstance(value, Mapping):
            raise AnswerBankError(f"entries[{index}] must be an object.")
        entry = AnswerBankEntry.from_mapping(value)
        if not entry.canonical_id:
            raise AnswerBankError(f"entries[{index}].canonical_id is required.")
        if entry.canonical_id in entries:
            raise AnswerBankError(f"Duplicate canonical_id: {entry.canonical_id}")
        entries[entry.canonical_id] = entry
    bank = AnswerBank(schema_version=int(raw.get("schema_version", 1)), entries=entries, raw=raw)
    errors = [item for item in validate_answer_bank(bank) if item[0] == "error"]
    if errors:
        raise AnswerBankError("; ".join(f"{path}: {message}" for _, path, message in errors))
    return bank


def validate_answer_bank(bank: AnswerBank) -> list[tuple[str, str, str]]:
    issues: list[tuple[str, str, str]] = []
    if bank.schema_version != 1:
        issues.append(("error", "schema_version", "Only schema_version 1 is supported."))
    for canonical_id, entry in bank.entries.items():
        prefix = f"entries.{canonical_id}"
        if entry.answer_type not in ANSWER_TYPES:
            issues.append(("error", f"{prefix}.answer_type", "Unsupported answer type."))
        if entry.safety_class not in SAFETY_CLASSES:
            issues.append(("error", f"{prefix}.safety_class", "Unsupported safety class."))
        if entry.status not in ENTRY_STATUSES:
            issues.append(("error", f"{prefix}.status", "Unsupported status."))
        if entry.allowed_autofill and (
            entry.safety_class != "STATIC_CONFIRMED"
            or entry.status != "confirmed"
            or entry.requires_user_confirmation
            or entry.job_dependent
        ):
            issues.append(("error", f"{prefix}.allowed_autofill", "Only static confirmed answers may be auto-fill eligible."))
        if entry.status == "confirmed" and not entry.provenance:
            issues.append(("error", f"{prefix}.provenance", "Confirmed answers need provenance."))
        if entry.answer_type == "KNOWN_FACT" and not entry.profile_fact_path and entry.value is None:
            issues.append(("error", prefix, "Known facts need a value or profile_fact_path."))
    return issues


class AnswerResolver:
    """Resolve only structured questions; it does not scrape or inspect forms."""

    def __init__(self, profile: CandidateApplicationProfile, bank: AnswerBank):
        self.profile = profile
        self.bank = bank

    def resolve(self, questions: list[ApplicationQuestion], job: Mapping[str, Any]) -> ResolutionResult:
        result = ResolutionResult()
        for question in questions:
            if question.answer_type not in QUESTION_TYPES:
                result.unresolved.append(self._unresolved(question, "", "INVALID_QUESTION", "Unsupported question type.", "MANUAL_ONLY"))
                continue
            canonical_id = normalize_question(question)
            entry = self.bank.entries.get(canonical_id)
            if not canonical_id or entry is None:
                result.unresolved.append(self._unresolved(question, canonical_id, "NO_ANSWER_BANK_ENTRY", "No deterministic answer-bank concept matched this question.", "UNKNOWN"))
                continue
            prepared, unresolved = self._resolve_entry(question, entry, job)
            if prepared is not None:
                result.answers.append(prepared)
            else:
                result.unresolved.append(unresolved)  # type: ignore[arg-type]
        return result

    def _resolve_entry(
        self,
        question: ApplicationQuestion,
        entry: AnswerBankEntry,
        job: Mapping[str, Any],
    ) -> tuple[PreparedAnswer | None, UnresolvedQuestion | None]:
        if entry.status == "deprecated":
            return None, self._unresolved(question, entry.canonical_id, "DEPRECATED_ANSWER", "The stored answer is deprecated.", entry.safety_class)
        if entry.safety_class == "OPTIONAL_PREFER_NOT_TO_ANSWER":
            policy = self.profile.demographics_policy.get("default_response")
            fact = self._fact_from_value(policy)
            if not question.required and fact and fact.usable:
                return self._answer(question, entry, fact.value, [self._source_label(fact.source)], False), None
            return None, self._unresolved(question, entry.canonical_id, "OPTIONAL_MANUAL", "Optional protected information is not inferred; use the configured preference manually.", entry.safety_class)
        if entry.job_dependent or entry.safety_class == "JOB_DEPENDENT":
            return self._resolve_job_dependent(question, entry, job)
        if entry.safety_class == "MANUAL_ONLY":
            return None, self._unresolved(question, entry.canonical_id, "MANUAL_ONLY", "This answer must be supplied manually for this application.", entry.safety_class)

        value = entry.value
        provenance = list(entry.provenance)
        if entry.profile_fact_path:
            if self.profile.has_unresolved_conflict(entry.profile_fact_path):
                return None, self._unresolved(question, entry.canonical_id, "SOURCE_CONFLICT", "Candidate sources conflict for this fact.", entry.safety_class)
            fact = self.profile.get_fact(entry.profile_fact_path)
            if fact is None or not fact.usable:
                status = "missing" if fact is None else fact.status
                return None, self._unresolved(question, entry.canonical_id, "FACT_NOT_CONFIRMED", f"Candidate fact is {status}; no answer was inferred.", entry.safety_class)
            value = fact.value
            provenance = [self._source_label(fact.source)]
        elif entry.status != "confirmed" or value is None or value == "":
            return None, self._unresolved(question, entry.canonical_id, "ANSWER_UNKNOWN", "The answer bank does not contain a confirmed value.", entry.safety_class)

        if entry.requires_user_confirmation or entry.safety_class == "CONFIRM_BEFORE_USE":
            return None, self._unresolved(question, entry.canonical_id, "CONFIRM_BEFORE_USE", "A user confirmation is required before this answer can be used.", entry.safety_class)
        return self._answer(question, entry, value, provenance, entry.allowed_autofill), None

    def _resolve_job_dependent(
        self,
        question: ApplicationQuestion,
        entry: AnswerBankEntry,
        job: Mapping[str, Any],
    ) -> tuple[PreparedAnswer | None, UnresolvedQuestion | None]:
        if entry.canonical_id == "desired_salary":
            policy_fact = self.profile.get_fact("job_preferences.salary_response_policy")
            policy = policy_fact.value if policy_fact and policy_fact.usable and isinstance(policy_fact.value, Mapping) else {}
            strategy = str(policy.get("strategy", "manual"))
            salary = job.get("salary") or {}
            minimum = salary.get("minimum") if isinstance(salary, Mapping) else None
            maximum = salary.get("maximum") if isinstance(salary, Mapping) else None
            if strategy == "posted_range_midpoint" and minimum is not None and maximum is not None:
                value = (float(minimum) + float(maximum)) / 2
                return self._answer(question, entry, value, ["job.posted_salary", "application_profile.job_preferences.salary_response_policy"], False), None
            if strategy == "explicit_amount" and policy.get("amount") is not None:
                amount = float(policy["amount"])
                if minimum is not None and maximum is not None and not (float(minimum) <= amount <= float(maximum)):
                    return None, self._unresolved(question, entry.canonical_id, "SALARY_CONFLICT", "Configured amount conflicts with the posted range.", entry.safety_class)
                return self._answer(question, entry, amount, ["application_profile.job_preferences.salary_response_policy"], False), None
            return None, self._unresolved(question, entry.canonical_id, "SALARY_USER_INPUT", "Salary is job-dependent and the configured policy requires a manual response.", entry.safety_class)

        if entry.canonical_id in {"willing_to_relocate", "preferred_location"}:
            location = str(job.get("location", "")).strip()
            policy_fact = self.profile.get_fact("job_preferences.location_policy")
            policy = policy_fact.value if policy_fact and policy_fact.usable and isinstance(policy_fact.value, Mapping) else {}
            normalized = location.casefold()
            local_markets = [str(value).casefold() for value in policy.get("local_acceptable", [])]
            relocation_markets = [str(value).casefold() for value in policy.get("relocation_allowed", [])]
            if not location:
                return None, self._unresolved(question, entry.canonical_id, "JOB_LOCATION_UNKNOWN", "Job location is unknown.", entry.safety_class)
            if any(term and term in normalized for term in local_markets):
                value = True if entry.canonical_id == "willing_to_relocate" else location
                return self._answer(question, entry, value, ["job.location", "application_profile.job_preferences.location_policy"], False), None
            if any(term and term in normalized for term in relocation_markets):
                if policy.get("relocation_requires_confirmation", True):
                    return None, self._unresolved(question, entry.canonical_id, "RELOCATION_CONFIRMATION", "This configured relocation market requires confirmation for the specific job.", entry.safety_class)
                value = True if entry.canonical_id == "willing_to_relocate" else location
                return self._answer(question, entry, value, ["job.location", "application_profile.job_preferences.location_policy"], False), None
            return None, self._unresolved(question, entry.canonical_id, "LOCATION_POLICY_UNRESOLVED", "The current private location policy does not establish this answer.", entry.safety_class)

        return None, self._unresolved(question, entry.canonical_id, "JOB_DEPENDENT_MANUAL", "This job-dependent question requires an explicit per-job answer.", entry.safety_class)

    @staticmethod
    def _fact_from_value(value: Any):
        if isinstance(value, Mapping) and "status" in value:
            from .application_profile import ApplicationFact
            return ApplicationFact.from_mapping(value)
        return None

    @staticmethod
    def _source_label(source: str | list[str]) -> str:
        return ", ".join(source) if isinstance(source, list) else source

    @staticmethod
    def _answer(question: ApplicationQuestion, entry: AnswerBankEntry, value: Any, provenance: list[str], allowed_autofill: bool) -> PreparedAnswer:
        return PreparedAnswer(
            question_id=question.question_id,
            canonical_id=entry.canonical_id,
            value=value,
            safety_class=entry.safety_class,
            provenance=[item for item in provenance if item],
            allowed_autofill=allowed_autofill,
            notes="Prepared for manual review; no form interaction occurred.",
        )

    @staticmethod
    def _unresolved(question: ApplicationQuestion, canonical_id: str, code: str, reason: str, safety_class: str) -> UnresolvedQuestion:
        return UnresolvedQuestion(
            question_id=question.question_id,
            canonical_id=canonical_id,
            question_text=question.text,
            required=question.required,
            reason_code=code,
            reason=reason,
            safety_class=safety_class,
            resolution_needed="User confirmation or an explicit sourced fact is required.",
        )
