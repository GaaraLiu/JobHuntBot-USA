"""Private metadata-only submission history and queue state adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..application_queue import ApplicationQueueStore
from .models import SubmissionResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def private_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    if "my-materials" not in {part.casefold() for part in resolved.parts}:
        raise ValueError("Submission history must remain under my-materials/.")
    return resolved


class SubmissionHistoryStore:
    def __init__(self, path: str | Path):
        self.path = private_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event_type: str,
        *,
        application_id: str,
        job_id: str,
        company: str,
        title: str,
        ats: str,
        status: str,
    ) -> None:
        event = {
            "timestamp": utc_now(),
            "event_type": event_type,
            "application_id": application_id,
            "job_id": job_id,
            "company": company,
            "title": title,
            "ats": ats,
            "status": status,
            "candidate_values_logged": False,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        return records

    def has_confirmed(self, *, application_id: str, job_id: str) -> bool:
        return any(
            item.get("status") == "SUBMITTED_CONFIRMED"
            and (
                item.get("application_id") == application_id
                or (job_id and item.get("job_id") == job_id)
            )
            for item in self.records()
        )


class SubmissionQueueStatusManager:
    def __init__(self, store: ApplicationQueueStore | None):
        self.store = store

    def mark_review_ready(self, application_id: str) -> None:
        self._set(application_id, "READY_FOR_USER_REVIEW")

    def mark_in_progress(self, application_id: str) -> None:
        self._set(application_id, "IN_PROGRESS")

    def apply_result(self, result: SubmissionResult) -> None:
        status = {
            "SUBMITTED_CONFIRMED": "SUBMITTED",
            "SUBMITTED_UNCONFIRMED": "IN_PROGRESS",
            "PAUSED_FOR_CAPTCHA": "WAITING_FOR_USER",
            "PAUSED_FOR_LOGIN": "WAITING_FOR_USER",
            "NEEDS_USER_INPUT": "WAITING_FOR_USER",
            "FORM_CHANGED_REVIEW_REQUIRED": "NEEDS_REVIEW",
            "SUBMISSION_BLOCKED": "READY_FOR_USER_REVIEW",
            "FAILED": "FAILED" if result.submission_attempted else "READY_FOR_USER_REVIEW",
            "DRY_RUN_READY": "READY_FOR_USER_REVIEW",
        }[result.status]
        self._set(result.application_id, status)

    def _set(self, application_id: str, status: str) -> None:
        if self.store is None:
            return
        record = self.store.find(application_id)
        if record.get("application_status") == "SUBMITTED" and status != "SUBMITTED":
            return
        record["application_status"] = status
        record["updated_at"] = utc_now()
        self.store.replace(record)
