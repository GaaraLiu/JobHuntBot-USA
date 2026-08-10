"""Canonical CSV schema loading and deterministic job-pool persistence."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import PipelineResult, Recommendation


class StorageError(RuntimeError):
    """Base error for dashboard persistence."""


class SchemaMismatchError(StorageError):
    """Raised instead of silently rewriting a CSV with an unexpected header."""


@dataclass(frozen=True, slots=True)
class DashboardSchema:
    schema_version: int
    tables: dict[str, dict[str, Any]]
    source_path: Path

    def columns(self, table: str) -> list[str]:
        try:
            return list(self.tables[table]["columns"])
        except (KeyError, TypeError) as exc:
            raise SchemaMismatchError(f"Canonical schema does not define table '{table}'") from exc


def default_schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / "dashboard.schema.json"


def load_dashboard_schema(path: str | Path | None = None) -> DashboardSchema:
    schema_path = Path(path) if path is not None else default_schema_path()
    try:
        with schema_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaMismatchError(f"Could not load canonical dashboard schema {schema_path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("tables"), dict):
        raise SchemaMismatchError("Canonical dashboard schema must define a tables object")
    version = value.get("schema_version")
    if not isinstance(version, int) or version < 1:
        raise SchemaMismatchError("Canonical dashboard schema requires a positive schema_version")
    return DashboardSchema(version, dict(value["tables"]), schema_path)


def canonical_json(value: Any) -> str:
    """Serialize structured CSV cells deterministically and without ASCII loss."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_csv_rows(path: str | Path, expected_columns: list[str] | None = None) -> tuple[list[str], list[dict[str, str]]]:
    csv_path = Path(path)
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            rows = [{key: value if value is not None else "" for key, value in row.items()} for row in reader]
    except OSError as exc:
        raise StorageError(f"Could not read CSV {csv_path}: {exc}") from exc
    if expected_columns is not None and header != expected_columns:
        raise SchemaMismatchError(
            f"CSV header mismatch for {csv_path}. Expected {expected_columns!r}, got {header!r}."
        )
    return header, rows


def write_csv_rows(path: str | Path, columns: list[str], rows: Iterable[Mapping[str, Any]]) -> None:
    """Atomically write RFC-4180-style quoted CSV with CRLF line endings."""

    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{csv_path.name}.", suffix=".tmp", dir=csv_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=columns,
                extrasaction="ignore",
                quoting=csv.QUOTE_ALL,
                lineterminator="\r\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in columns})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, csv_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def initialize_csv(path: str | Path, table: str, schema: DashboardSchema | None = None) -> None:
    schema = schema or load_dashboard_schema()
    csv_path = Path(path)
    if csv_path.exists():
        read_csv_rows(csv_path, schema.columns(table))
        return
    write_csv_rows(csv_path, schema.columns(table), [])


def _lifecycle_status(result: PipelineResult) -> str:
    return {
        Recommendation.APPLY: "Pending",
        Recommendation.REVIEW: "Needs user",
        Recommendation.SKIP: "Skipped",
    }[result.score.recommendation]


def _priority(result: PipelineResult) -> str:
    if result.score.recommendation == Recommendation.APPLY:
        return "High"
    if result.score.recommendation == Recommendation.REVIEW:
        return "Medium"
    return "Low"


def _next_action(result: PipelineResult) -> str:
    if result.score.recommendation == Recommendation.APPLY:
        return "Review the analysis and decide whether to begin an application."
    if result.score.recommendation == Recommendation.REVIEW:
        return "Review missing or unknown requirements before applying."
    return "Do not apply unless the profile, job facts, or configured constraints change."


def result_to_job_pool_row(result: PipelineResult) -> dict[str, str]:
    job = result.job
    score = result.score
    resume = result.resume
    salary = job.salary
    experience = job.experience_required
    return {
        "date_found": job.discovered_date,
        "company": job.company,
        "job_title": job.title,
        "role_family": job.job_family,
        "location": job.location,
        "remote_policy": job.remote_policy,
        "source": job.source,
        "job_url": job.apply_url or job.source_url,
        "posted_date": job.posted_date,
        "priority": _priority(result),
        "status": _lifecycle_status(result),
        "resume_variant": resume.resume_id,
        "skip_reason": "; ".join(score.hard_blockers) if score.recommendation == Recommendation.SKIP else "",
        "blocker": "; ".join(score.hard_blockers),
        "next_action": _next_action(result),
        "job_id": job.job_id,
        "source_native_id": job.source_native_id,
        "source_url": job.source_url,
        "apply_url": job.apply_url,
        "job_description": job.job_description,
        "salary_min": "" if salary is None or salary.minimum is None else f"{salary.minimum:g}",
        "salary_max": "" if salary is None or salary.maximum is None else f"{salary.maximum:g}",
        "salary_currency": "" if salary is None else salary.currency,
        "salary_period": "" if salary is None else salary.period,
        "employment_type": job.employment_type,
        "experience_required_min_years": "" if experience is None or experience.minimum_years is None else f"{experience.minimum_years:g}",
        "experience_required_max_years": "" if experience is None or experience.maximum_years is None else f"{experience.maximum_years:g}",
        "education_required": canonical_json(job.education_required),
        "skills_required": canonical_json(job.skills_required),
        "preferred_skills": canonical_json(job.preferred_skills),
        "industry": canonical_json(job.industries),
        "fit_score": f"{score.overall_score:.1f}",
        "score_coverage": f"{score.coverage:.3f}",
        "recommendation": score.recommendation.value,
        "score_breakdown": canonical_json([item.to_dict() for item in score.components]),
        "matched_requirements": canonical_json(score.matched_requirements),
        "missing_requirements": canonical_json(score.missing_requirements),
        "unknown_requirements": canonical_json(score.unknown_requirements),
        "hard_blockers": canonical_json(score.hard_blockers),
        "recommended_resume": resume.file_path,
        "resume_reasoning": canonical_json({"reason": resume.reason, "evidence": resume.evidence}),
        "analyzed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def save_job_result(
    path: str | Path,
    result: PipelineResult,
    schema: DashboardSchema | None = None,
) -> str:
    """Insert or update a job-pool row by deterministic job_id."""

    schema = schema or load_dashboard_schema()
    columns = schema.columns("job_pool")
    csv_path = Path(path)
    if csv_path.exists():
        _, rows = read_csv_rows(csv_path, columns)
    else:
        rows = []

    incoming = result_to_job_pool_row(result)
    existing = next((row for row in rows if row.get("job_id") == result.job.job_id), None)
    if existing is None:
        row = {column: "" for column in columns}
        row.update(incoming)
        rows.append(row)
        action = "inserted"
    else:
        protected_status = existing.get("status") in {"Submitted", "Offer", "Rejected"}
        preserved_status = existing.get("status", "")
        existing.update(incoming)
        if protected_status:
            existing["status"] = preserved_status
        action = "updated"

    write_csv_rows(csv_path, columns, rows)
    return action

