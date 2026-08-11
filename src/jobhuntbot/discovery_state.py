"""Private JSON state for deterministic incremental discovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .sources import SourceJob


class DiscoveryStateError(ValueError):
    """Raised when the private state file is malformed or cannot be saved."""


@dataclass(slots=True)
class IncrementalDecision:
    status: str
    should_analyze: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DiscoveryStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.jobs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryStateError(f"Could not load discovery state {self.path}: {exc}") from exc
        if not isinstance(value, Mapping) or not isinstance(value.get("jobs", {}), Mapping):
            raise DiscoveryStateError("Discovery state must contain a jobs object.")
        self.jobs = {
            str(job_id): dict(record)
            for job_id, record in value.get("jobs", {}).items()
            if isinstance(record, Mapping)
        }

    def observe(
        self,
        *,
        job_id: str,
        source_job: SourceJob,
        content_fingerprint: str,
        seen_at: str,
        result_exists: bool,
        force_reanalyze: bool = False,
    ) -> IncrementalDecision:
        record = self.jobs.get(job_id)
        if record is None:
            self.jobs[job_id] = {
                "job_id": job_id,
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "last_analyzed_at": "",
                "source": source_job.source,
                "source_job_id": source_job.source_job_id,
                "content_fingerprint": content_fingerprint,
                "last_score": None,
                "last_decision": "",
                "last_resume": "",
            }
            return IncrementalDecision("new", True, "Stable job_id has not been seen before.")

        record["last_seen_at"] = seen_at
        record["source"] = source_job.source
        record["source_job_id"] = source_job.source_job_id
        previous = str(record.get("content_fingerprint") or "")
        if previous != content_fingerprint:
            record["content_fingerprint"] = content_fingerprint
            return IncrementalDecision(
                "changed", True, "Content fingerprint changed since the previous run."
            )
        if force_reanalyze:
            return IncrementalDecision(
                "forced", True, "Explicit re-analysis of unchanged jobs was requested."
            )
        if not record.get("last_analyzed_at") or not result_exists:
            return IncrementalDecision(
                "recovery", True, "No reusable prior analysis artifact is available."
            )
        return IncrementalDecision(
            "unchanged", False, "Stable job_id and content fingerprint are unchanged."
        )

    def mark_analyzed(
        self,
        *,
        job_id: str,
        analyzed_at: str,
        score: float,
        decision: str,
        resume: str,
    ) -> None:
        record = self.jobs[job_id]
        record["last_analyzed_at"] = analyzed_at
        record["last_score"] = score
        record["last_decision"] = decision
        record["last_resume"] = resume

    def mark_relevance(
        self,
        *,
        job_id: str,
        checked_at: str,
        matched_track: str,
        relevant: bool,
    ) -> None:
        record = self.jobs[job_id]
        record["last_relevance_checked_at"] = checked_at
        record["last_matched_track"] = matched_track
        record["last_relevant"] = relevant

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": 1,
            "jobs": {key: self.jobs[key] for key in sorted(self.jobs)},
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            raise DiscoveryStateError(f"Could not save discovery state {self.path}: {exc}") from exc


def content_fingerprint(job: SourceJob) -> str:
    value = {
        "company": job.company,
        "title": job.title,
        "description": job.description,
        "location": job.location,
        "work_mode": job.work_mode,
        "employment_type": job.employment_type,
        "posted_date": job.posted_date,
        "updated_date": job.updated_date,
        "salary": None if job.salary is None else job.salary.to_dict(),
        "source_url": job.source_url,
        "apply_url": job.apply_url,
    }
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
