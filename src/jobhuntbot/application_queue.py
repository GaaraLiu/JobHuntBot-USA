"""Private application queue, package builder, and minimal audit history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .answer_bank import (
    AnswerBank,
    AnswerResolver,
    ApplicationQuestion,
    PreparedAnswer,
    UnresolvedQuestion,
)
from .application_profile import CandidateApplicationProfile


QUEUE_STATUSES = {
    "READY_TO_PREPARE",
    "PREPARING",
    "WAITING_FOR_USER",
    "NEEDS_REVIEW",
    "PACKAGE_READY",
    "READY_FOR_APPLICATION",
    "IN_PROGRESS",
    "FAILED",
    "WITHDRAWN",
    "SKIPPED",
}
PACKAGE_STATUSES = {"READY", "NEEDS_USER_INPUT", "BLOCKED", "INVALID"}
AUDIT_EVENTS = {
    "package_created",
    "answer_resolved",
    "answer_changed",
    "user_confirmation",
    "readiness_changed",
}
AUDIT_METADATA_FIELDS = {
    "package_created": {"readiness", "prepared_answer_count", "unresolved_count"},
    "answer_resolved": {"question_id", "canonical_id", "safety_class"},
    "answer_changed": {"question_id", "canonical_id", "previous_status", "new_status"},
    "user_confirmation": {"question_id", "canonical_id", "confirmation_status"},
    "readiness_changed": {"previous_status", "readiness", "queue_status"},
}


class ApplicationQueueError(ValueError):
    """Raised when private queue/package state is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _private_path(path: Path) -> Path:
    resolved = path.resolve()
    if "my-materials" not in {part.casefold() for part in resolved.parts}:
        raise ApplicationQueueError(f"Private application output must be under my-materials/: {path}")
    return resolved


def _stable_application_id(job_id: str) -> str:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:20]
    return f"application_{digest}"


def _material_fingerprint(candidate: Mapping[str, Any]) -> str:
    job = candidate.get("job", {}) if isinstance(candidate.get("job"), Mapping) else {}
    score = candidate.get("score", {}) if isinstance(candidate.get("score"), Mapping) else {}
    resume = candidate.get("resume", {}) if isinstance(candidate.get("resume"), Mapping) else {}
    selected = {
        "job_id": job.get("job_id"),
        "company": job.get("company"),
        "title": job.get("title"),
        "source": job.get("source"),
        "source_url": job.get("source_url"),
        "apply_url": job.get("apply_url"),
        "location": job.get("location"),
        "remote_policy": job.get("remote_policy"),
        "employment_type": job.get("employment_type"),
        "salary": job.get("salary"),
        "experience_required": job.get("experience_required"),
        "skills_required": job.get("skills_required"),
        "preferred_skills": job.get("preferred_skills"),
        "recommendation": score.get("recommendation"),
        "resume_id": resume.get("resume_id"),
        "resume_path": resume.get("file_path"),
    }
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_from_record(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if all(isinstance(value.get(key), Mapping) for key in ("job", "score", "resume")):
        return value
    result = value.get("result")
    if isinstance(result, Mapping) and all(
        isinstance(result.get(key), Mapping) for key in ("job", "score", "resume")
    ):
        return result
    return None


def load_pipeline_candidates(path: str | Path) -> list[Mapping[str, Any]]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ApplicationQueueError(f"Could not read candidate results {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApplicationQueueError(f"Invalid candidate-results JSON {source}: {exc}") from exc
    raw_records: list[Any]
    if isinstance(value, list):
        raw_records = value
    elif isinstance(value, Mapping):
        for key in ("jobs", "results", "records"):
            if isinstance(value.get(key), list):
                raw_records = list(value[key])
                break
        else:
            raw_records = [value]
    else:
        raise ApplicationQueueError("Candidate results must be a JSON object or array.")
    candidates = []
    for record in raw_records:
        if isinstance(record, Mapping):
            candidate = _candidate_from_record(record)
            if candidate is not None:
                copied = dict(candidate)
                if candidate is not record:
                    copied["discovery_provenance"] = {
                        key: record.get(key)
                        for key in (
                            "source",
                            "source_url",
                            "discovered_at",
                            "source_job",
                            "relevance",
                            "incremental",
                            "aggregator_provenance",
                            "resolution_provenance",
                        )
                        if record.get(key) is not None
                    }
                candidates.append(copied)
    return candidates


@dataclass(slots=True)
class QueueImportReport:
    examined: int = 0
    added: int = 0
    duplicate: int = 0
    changed: int = 0
    review_excluded: int = 0
    skip_excluded: int = 0
    invalid: int = 0
    promoted_review: int = 0
    application_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApplicationQueueStore:
    def __init__(self, queue_path: str | Path):
        self.path = _private_path(Path(queue_path))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "applications": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplicationQueueError(f"Could not load application queue {self.path}: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("applications"), list):
            raise ApplicationQueueError("Application queue must contain an applications array.")
        return value

    def save(self, value: Mapping[str, Any]) -> None:
        self.path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    def import_candidates(
        self,
        candidates: Iterable[Mapping[str, Any]],
        *,
        source_path: str,
        promote_review_job_ids: set[str] | None = None,
    ) -> QueueImportReport:
        promote_review_job_ids = promote_review_job_ids or set()
        queue = self.load()
        existing = {
            str(item.get("job_id")): item
            for item in queue["applications"]
            if isinstance(item, dict) and item.get("job_id")
        }
        report = QueueImportReport()
        changed_queue = False
        for candidate in candidates:
            report.examined += 1
            job = candidate.get("job", {})
            score = candidate.get("score", {})
            resume = candidate.get("resume", {})
            job_id = str(job.get("job_id", "")) if isinstance(job, Mapping) else ""
            decision = str(score.get("recommendation", "")) if isinstance(score, Mapping) else ""
            if not job_id or decision not in {"APPLY", "REVIEW", "SKIP"}:
                report.invalid += 1
                continue
            promoted = decision == "REVIEW" and job_id in promote_review_job_ids
            if decision == "SKIP":
                report.skip_excluded += 1
                continue
            if decision == "REVIEW" and not promoted:
                report.review_excluded += 1
                continue
            fingerprint = _material_fingerprint(candidate)
            prior = existing.get(job_id)
            if prior is not None:
                if prior.get("material_fingerprint") == fingerprint:
                    report.duplicate += 1
                else:
                    prior["application_status"] = "NEEDS_REVIEW"
                    prior["updated_at"] = _now()
                    prior["pending_update"] = {
                        "detected_at": _now(),
                        "source_path": source_path,
                        "material_fingerprint": fingerprint,
                        "job_snapshot": dict(job),
                        "score_snapshot": dict(score),
                        "resume_snapshot": dict(resume),
                    }
                    report.changed += 1
                    changed_queue = True
                continue

            created = _now()
            application_id = _stable_application_id(job_id)
            record = {
                "application_id": application_id,
                "job_id": job_id,
                "company": str(job.get("company", "")),
                "title": str(job.get("title", "")),
                "source": str(job.get("source", "")),
                "authoritative_source": str(job.get("source", "")),
                "source_url": str(job.get("source_url", "")),
                "apply_url": str(job.get("apply_url", "")),
                "fit_score": score.get("overall_score"),
                "decision": decision,
                "selected_resume_id": str(resume.get("resume_id", "")),
                "selected_resume_path": str(resume.get("file_path", "")),
                "application_status": "READY_TO_PREPARE",
                "created_at": created,
                "updated_at": created,
                "package_status": "",
                "unresolved_question_count": 0,
                "blockers": [],
                "notes": "Explicit REVIEW promotion." if promoted else "",
                "promotion": {
                    "promoted": promoted,
                    "original_decision": decision,
                    "reason": "Explicit CLI promotion for application preparation." if promoted else "",
                },
                "import_provenance": {
                    "source_path": source_path,
                    "imported_at": created,
                    "discovery": candidate.get("discovery_provenance", {}),
                },
                "material_fingerprint": fingerprint,
                "job_snapshot": dict(job),
                "score_snapshot": dict(score),
                "resume_snapshot": dict(resume),
            }
            queue["applications"].append(record)
            existing[job_id] = record
            report.added += 1
            report.promoted_review += int(promoted)
            report.application_ids.append(application_id)
            changed_queue = True
        if changed_queue or not self.path.exists():
            self.save(queue)
        return report

    def find(self, application_id: str) -> dict[str, Any]:
        queue = self.load()
        for item in queue["applications"]:
            if isinstance(item, dict) and item.get("application_id") == application_id:
                return item
        raise ApplicationQueueError(f"Unknown application_id: {application_id}")

    def replace(self, record: Mapping[str, Any]) -> None:
        queue = self.load()
        application_id = record.get("application_id")
        for index, item in enumerate(queue["applications"]):
            if isinstance(item, dict) and item.get("application_id") == application_id:
                queue["applications"][index] = dict(record)
                self.save(queue)
                return
        raise ApplicationQueueError(f"Unknown application_id: {application_id}")


class ApplicationHistoryStore:
    """Append metadata-only events; answer values are intentionally rejected."""

    def __init__(self, path: str | Path):
        self.path = _private_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, application_id: str, **metadata: Any) -> None:
        if event_type not in AUDIT_EVENTS:
            raise ApplicationQueueError(f"Unsupported application history event: {event_type}")
        unexpected = set(metadata) - AUDIT_METADATA_FIELDS[event_type]
        if unexpected:
            raise ApplicationQueueError(
                "History accepts metadata identifiers/statuses only; unsupported fields: "
                + ", ".join(sorted(unexpected))
            )
        event = {
            "timestamp": _now(),
            "event_type": event_type,
            "application_id": application_id,
            "metadata": metadata,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass(slots=True)
class ApplicationPackage:
    application_id: str
    job_snapshot: dict[str, Any]
    selected_resume: dict[str, Any]
    candidate_facts: dict[str, Any]
    prepared_answers: list[dict[str, Any]]
    job_dependent_answers: dict[str, Any]
    unresolved_questions: list[dict[str, Any]]
    optional_documents: list[dict[str, Any]]
    cover_letter_status: str
    package_readiness: str
    safety_flags: list[str]
    blockers: list[str]
    generated_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApplicationPackageBuilder:
    def __init__(
        self,
        *,
        queue_store: ApplicationQueueStore,
        application_root: str | Path,
        profile: CandidateApplicationProfile,
        answer_bank: AnswerBank,
    ):
        self.queue_store = queue_store
        self.root = _private_path(Path(application_root))
        self.profile = profile
        self.answer_bank = answer_bank
        self.history = ApplicationHistoryStore(self.root / "history" / "application_history.jsonl")

    def build(
        self,
        application_id: str,
        questions: list[ApplicationQuestion],
        *,
        cover_letter_status: str = "unknown",
    ) -> ApplicationPackage:
        record = self.queue_store.find(application_id)
        if record.get("application_status") in {"WITHDRAWN", "FAILED", "SKIPPED"}:
            raise ApplicationQueueError(f"Application status does not allow package preparation: {record.get('application_status')}")
        record["application_status"] = "PREPARING"
        record["updated_at"] = _now()
        self.queue_store.replace(record)

        blockers: list[str] = []
        resume_path = self._resolve_resume_path(str(record.get("selected_resume_path", "")))
        resume_snapshot = record.get("resume_snapshot", {})
        resume_id = str(record.get("selected_resume_id", ""))
        if not resume_id:
            blockers.append("Selected resume_id is missing.")
        elif not isinstance(resume_snapshot, Mapping) or (
            str(resume_snapshot.get("resume_id", "")) != resume_id
            or not bool(resume_snapshot.get("selected", False))
        ):
            blockers.append("Selected resume_id is not verified by the preserved Phase 1 snapshot.")
        if resume_path is None:
            blockers.append("Selected resume PDF is missing, unreadable, or not a PDF.")

        candidate_facts: dict[str, Any] = {}
        unresolved: list[UnresolvedQuestion] = []
        for path in self.profile.required_for_ready:
            fact = self.profile.get_fact(path)
            if fact is not None and fact.usable and not self.profile.has_unresolved_conflict(path):
                candidate_facts[path] = fact.to_dict()
            else:
                status = "missing" if fact is None else fact.status
                reason = "Source conflict is unresolved." if self.profile.has_unresolved_conflict(path) else f"Required fact is {status}."
                unresolved.append(
                    UnresolvedQuestion(
                        question_id=f"candidate_fact:{path}",
                        canonical_id=path,
                        question_text=f"Confirm required candidate fact: {path}",
                        required=True,
                        reason_code="REQUIRED_CANDIDATE_FACT",
                        reason=reason,
                        safety_class="CONFIRM_BEFORE_USE",
                        resolution_needed="Confirm the fact and its provenance in candidate_application_profile.json.",
                    )
                )

        result = AnswerResolver(self.profile, self.answer_bank).resolve(
            questions, record.get("job_snapshot", {})
        )
        unresolved.extend(result.unresolved)
        prepared_by_concept = {item.canonical_id: item for item in result.answers}
        unresolved_concepts = {item.canonical_id for item in result.unresolved}
        salary_policy = self.profile.get_fact("job_preferences.salary_response_policy")
        job_snapshot = record.get("job_snapshot", {})
        job_dependent_answers = {
            "salary": {
                "salary_range_in_job": job_snapshot.get("salary") if isinstance(job_snapshot, Mapping) else None,
                "candidate_strategy": salary_policy.value if salary_policy and salary_policy.usable else None,
                "proposed_answer": prepared_by_concept.get("desired_salary").value if "desired_salary" in prepared_by_concept else None,
                "requires_confirmation": "desired_salary" in unresolved_concepts
                or bool(self.answer_bank.entries.get("desired_salary") and self.answer_bank.entries["desired_salary"].requires_user_confirmation),
            },
            "relocation": {
                "job_location": job_snapshot.get("location", "") if isinstance(job_snapshot, Mapping) else "",
                "proposed_answer": prepared_by_concept.get("willing_to_relocate").value if "willing_to_relocate" in prepared_by_concept else None,
                "requires_confirmation": "willing_to_relocate" in unresolved_concepts,
            },
        }
        required_unresolved = [item for item in unresolved if item.required]
        if blockers:
            readiness = "BLOCKED"
        elif required_unresolved:
            readiness = "NEEDS_USER_INPUT"
        else:
            readiness = "READY"
        if readiness not in PACKAGE_STATUSES:
            readiness = "INVALID"

        now = _now()
        package = ApplicationPackage(
            application_id=application_id,
            job_snapshot=dict(job_snapshot),
            selected_resume={
                "resume_id": str(record.get("selected_resume_id", "")),
                "path": "" if resume_path is None else str(resume_path),
                "verified_exists": resume_path is not None,
                "substitution_allowed": False,
            },
            candidate_facts=candidate_facts,
            prepared_answers=[item.to_dict() for item in result.answers],
            job_dependent_answers=job_dependent_answers,
            unresolved_questions=[item.to_dict(application_id=application_id) for item in unresolved],
            optional_documents=[],
            cover_letter_status=cover_letter_status,
            package_readiness=readiness,
            safety_flags=[
                "NO_BROWSER_INTERACTION",
                "NO_FORM_FILLING",
                "NO_FILE_UPLOAD",
                "NO_SUBMISSION",
                "USER_REVIEW_REQUIRED",
            ],
            blockers=blockers,
            generated_at=now,
            updated_at=now,
        )
        package_dir = self.root / "packages"
        unresolved_dir = self.root / "unresolved"
        package_dir.mkdir(parents=True, exist_ok=True)
        unresolved_dir.mkdir(parents=True, exist_ok=True)
        package_path = package_dir / f"{application_id}.json"
        unresolved_path = unresolved_dir / f"{application_id}.json"
        package_path.write_text(json.dumps(package.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        unresolved_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "application_id": application_id,
                    "unresolved_questions": package.unresolved_questions,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        old_status = record.get("application_status")
        record["package_status"] = readiness
        record["unresolved_question_count"] = len(unresolved)
        record["blockers"] = blockers
        record["application_status"] = {
            "READY": "PACKAGE_READY",
            "NEEDS_USER_INPUT": "WAITING_FOR_USER",
            "BLOCKED": "FAILED",
            "INVALID": "FAILED",
        }[readiness]
        record["package_path"] = str(package_path)
        record["unresolved_path"] = str(unresolved_path)
        record["updated_at"] = now
        self.queue_store.replace(record)

        self.history.append("package_created", application_id, readiness=readiness, prepared_answer_count=len(result.answers), unresolved_count=len(unresolved))
        for item in result.answers:
            self.history.append("answer_resolved", application_id, question_id=item.question_id, canonical_id=item.canonical_id, safety_class=item.safety_class)
        self.history.append("readiness_changed", application_id, previous_status=old_status, readiness=readiness, queue_status=record["application_status"])
        return package

    def _resolve_resume_path(self, configured: str) -> Path | None:
        if not configured:
            return None
        candidate = Path(configured)
        if not candidate.is_absolute():
            # application root is normally <repo>/my-materials/application
            candidate = self.root.parent.parent / candidate
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.suffix.casefold() != ".pdf" or not resolved.is_file():
                return None
            with resolved.open("rb") as handle:
                if handle.read(4) != b"%PDF":
                    return None
            return resolved
        except OSError:
            return None


def load_questions(path: str | Path) -> list[ApplicationQuestion]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplicationQueueError(f"Could not load structured questions {source}: {exc}") from exc
    values = value.get("questions", []) if isinstance(value, Mapping) else value
    if not isinstance(values, list):
        raise ApplicationQueueError("Structured questions must be an array or contain a questions array.")
    questions = [ApplicationQuestion.from_mapping(item) for item in values if isinstance(item, Mapping)]
    if len(questions) != len(values):
        raise ApplicationQueueError("Every structured question must be a JSON object.")
    return questions
