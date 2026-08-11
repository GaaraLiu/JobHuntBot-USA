"""Configurable public-ATS discovery integrated with the frozen Phase 1 pipeline."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import AppConfig
from .deduplication import DuplicateRecord, deduplicate_jobs
from .job_parser import DeterministicJobParser
from .models import (
    CandidateProfile,
    NormalizedJob,
    PipelineResult,
    RawJob,
    SalaryRange,
)
from .normalization import build_job_id, normalize_text_key
from .pipeline import JobHuntPipeline
from .resume_router import ResumeRouter
from .sources import (
    DiscoveryTarget,
    JsonHttpClient,
    PoliteJsonClient,
    SourceAdapter,
    SourceFailure,
    SourceJob,
    create_adapter,
    supported_sources,
)


class DiscoveryConfigError(ValueError):
    """Raised when a discovery configuration is missing or invalid."""


@dataclass(slots=True)
class DiscoveryRequestSettings:
    timeout_seconds: float = 15.0
    retries: int = 2
    min_interval_seconds: float = 0.5
    user_agent: str = "JobHuntBot/0.2 (local public-job discovery)"


@dataclass(slots=True)
class DiscoveryConfig:
    schema_version: int = 1
    job_families: dict[str, list[str]] = field(default_factory=dict)
    targets: list[DiscoveryTarget] = field(default_factory=list)
    request: DiscoveryRequestSettings = field(default_factory=DiscoveryRequestSettings)
    max_jobs_per_target: int = 100
    max_fetch_per_target: int = 100


@dataclass(slots=True)
class DiscoveryRunSummary:
    started_at: str
    completed_at: str = ""
    selected_sources: list[str] = field(default_factory=list)
    targets_attempted: int = 0
    jobs_fetched: int = 0
    jobs_after_filters: int = 0
    jobs_after_deduplication: int = 0
    duplicates_removed: int = 0
    jobs_normalized: int = 0
    jobs_analyzed: int = 0
    source_errors: list[SourceFailure] = field(default_factory=list)
    analysis_errors: list[SourceFailure] = field(default_factory=list)
    duplicates: list[DuplicateRecord] = field(default_factory=list)
    discovery_only: bool = False
    output_dir: str = ""
    job_pool: str = ""

    @property
    def failure_count(self) -> int:
        return len(self.source_errors) + len(self.analysis_errors)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_errors"] = [item.to_dict() for item in self.source_errors]
        value["analysis_errors"] = [item.to_dict() for item in self.analysis_errors]
        value["duplicates"] = [item.to_dict() for item in self.duplicates]
        value["failure_count"] = self.failure_count
        return value


def load_discovery_config(path: str | Path) -> DiscoveryConfig:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryConfigError(
            f"Could not load discovery configuration {config_path}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise DiscoveryConfigError("Discovery configuration root must be an object.")

    families_value = value.get("job_families", {})
    if not isinstance(families_value, Mapping):
        raise DiscoveryConfigError("job_families must be an object.")
    families: dict[str, list[str]] = {}
    for name, definition in families_value.items():
        if isinstance(definition, Mapping):
            definition = definition.get("keywords", [])
        families[str(name).strip()] = _string_list(
            definition, f"job_families.{name}.keywords"
        )

    targets_value = value.get("targets", [])
    if not isinstance(targets_value, list):
        raise DiscoveryConfigError("targets must be an array.")
    targets: list[DiscoveryTarget] = []
    for index, item in enumerate(targets_value):
        if not isinstance(item, Mapping):
            raise DiscoveryConfigError(f"targets[{index}] must be an object.")
        try:
            target = DiscoveryTarget.from_mapping(item)
        except ValueError as exc:
            raise DiscoveryConfigError(f"targets[{index}]: {exc}") from exc
        if target.source not in supported_sources():
            raise DiscoveryConfigError(
                f"targets[{index}] uses unsupported source '{target.source}'."
            )
        unknown_families = sorted(set(target.job_families) - set(families))
        if unknown_families:
            raise DiscoveryConfigError(
                f"targets[{index}] references unknown job families: "
                + ", ".join(unknown_families)
            )
        targets.append(target)

    request_value = value.get("request", {})
    if not isinstance(request_value, Mapping):
        raise DiscoveryConfigError("request must be an object.")
    request = DiscoveryRequestSettings(
        timeout_seconds=_positive_float(
            request_value.get("timeout_seconds", 15.0), "request.timeout_seconds"
        ),
        retries=_nonnegative_int(request_value.get("retries", 2), "request.retries"),
        min_interval_seconds=_nonnegative_float(
            request_value.get("min_interval_seconds", 0.5),
            "request.min_interval_seconds",
        ),
        user_agent=str(
            request_value.get(
                "user_agent", "JobHuntBot/0.2 (local public-job discovery)"
            )
        ).strip(),
    )
    if not request.user_agent:
        raise DiscoveryConfigError("request.user_agent cannot be empty.")
    return DiscoveryConfig(
        schema_version=_positive_int(value.get("schema_version", 1), "schema_version"),
        job_families=families,
        targets=targets,
        request=request,
        max_jobs_per_target=_positive_int(
            value.get("max_jobs_per_target", 100), "max_jobs_per_target"
        ),
        max_fetch_per_target=_positive_int(
            value.get("max_fetch_per_target", 100), "max_fetch_per_target"
        ),
    )


class SourceAwareParser:
    """Overlay only explicit ATS fields after canonical Phase 1 parsing.

    Precedence: the Phase 1 parser remains canonical for JD-derived facts. An
    explicit ATS work mode, employment type, or numeric salary range wins for
    that same field. Empty adapter values never erase parser output.
    """

    def __init__(self, config: AppConfig):
        self.base_parser = DeterministicJobParser(config)

    def parse(self, raw_job: RawJob) -> NormalizedJob:
        job = self.base_parser.parse(raw_job)
        source_fields = raw_job.metadata.get("discovery_source_fields", {})
        if not isinstance(source_fields, Mapping):
            return job

        work_mode = str(source_fields.get("work_mode") or "").strip()
        if work_mode:
            job.remote_policy = work_mode
            job.parser_evidence.setdefault("remote_policy", []).append(
                f"ATS structured work mode: {work_mode}"
            )
        employment_type = str(source_fields.get("employment_type") or "").strip()
        if employment_type:
            job.employment_type = employment_type
            job.parser_evidence.setdefault("employment_type", []).append(
                f"ATS structured employment type: {employment_type}"
            )
        salary = source_fields.get("salary")
        if isinstance(salary, Mapping):
            minimum = _optional_number(salary.get("minimum"))
            maximum = _optional_number(salary.get("maximum"))
            if minimum is not None or maximum is not None:
                current = job.salary
                job.salary = SalaryRange(
                    minimum=minimum,
                    maximum=maximum,
                    currency=str(
                        salary.get("currency")
                        or (current.currency if current is not None else "")
                    ).upper(),
                    period=str(
                        salary.get("period")
                        or (current.period if current is not None else "")
                    ),
                    raw_text=str(
                        salary.get("raw_text")
                        or (current.raw_text if current is not None else "")
                    ),
                )
                job.parser_evidence.setdefault("salary", []).append(
                    "ATS structured published salary range"
                )
        return job


AdapterFactory = Callable[[str, JsonHttpClient], SourceAdapter]


class DiscoveryRunner:
    """Fetch, filter, deduplicate, normalize, analyze, and privately persist jobs."""

    def __init__(
        self,
        *,
        profile: CandidateProfile,
        app_config: AppConfig,
        resume_router: ResumeRouter,
        discovery_config: DiscoveryConfig,
        output_dir: str | Path,
        client: JsonHttpClient | None = None,
        adapter_factory: AdapterFactory = create_adapter,
    ):
        self.profile = profile
        self.app_config = app_config
        self.resume_router = resume_router
        self.config = discovery_config
        self.output_dir = Path(output_dir)
        self.client = client or PoliteJsonClient(
            timeout_seconds=discovery_config.request.timeout_seconds,
            retries=discovery_config.request.retries,
            min_interval_seconds=discovery_config.request.min_interval_seconds,
            user_agent=discovery_config.request.user_agent,
        )
        self.adapter_factory = adapter_factory

    def run(
        self,
        *,
        sources: Sequence[str] | None = None,
        limit: int | None = None,
        discovery_only: bool = False,
    ) -> DiscoveryRunSummary:
        if limit is not None and limit < 1:
            raise DiscoveryConfigError("Run limit must be positive.")
        selected = {str(item).strip().casefold() for item in (sources or []) if str(item).strip()}
        unsupported = sorted(selected - set(supported_sources()))
        if unsupported:
            raise DiscoveryConfigError("Unsupported selected sources: " + ", ".join(unsupported))
        targets = [
            target
            for target in self.config.targets
            if target.enabled and (not selected or target.source in selected)
        ]
        started_at = _utc_now()
        summary = DiscoveryRunSummary(
            started_at=started_at,
            selected_sources=sorted(selected or {target.source for target in targets}),
            discovery_only=discovery_only,
            output_dir=str(self.output_dir),
        )
        self._prepare_output()
        self._log("run_started", {"started_at": started_at, "sources": summary.selected_sources})

        fetched: list[SourceJob] = []
        for target in targets:
            summary.targets_attempted += 1
            try:
                adapter = self.adapter_factory(target.source, self.client)
                fetch_limit = min(
                    int(target.options.get("fetch_limit", self.config.max_fetch_per_target)),
                    self.config.max_fetch_per_target,
                )
                batch = adapter.fetch(target, limit=fetch_limit)
            except Exception as exc:  # isolate one configured source from the run
                failure = SourceFailure(
                    source=target.source,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
                summary.source_errors.append(failure)
                self._log("source_error", failure.to_dict())
                continue
            summary.jobs_fetched += len(batch.jobs)
            summary.source_errors.extend(batch.errors)
            for failure in batch.errors:
                self._log("source_error", failure.to_dict())
            retained = [job for job in batch.jobs if self._matches_target(job, target)]
            per_target_limit = target.max_results or self.config.max_jobs_per_target
            retained = retained[: min(per_target_limit, self.config.max_jobs_per_target)]
            fetched.extend(retained)
            self._log(
                "source_complete",
                {
                    "source": target.source,
                    "board": target.board,
                    "fetched": len(batch.jobs),
                    "retained": len(retained),
                },
            )

        summary.jobs_after_filters = len(fetched)
        for source_job in fetched:
            self._write_raw(source_job, started_at)

        deduplicated = deduplicate_jobs(fetched)
        jobs = deduplicated.jobs[:limit] if limit is not None else deduplicated.jobs
        summary.duplicates = deduplicated.duplicates
        summary.duplicates_removed = len(deduplicated.duplicates)
        summary.jobs_after_deduplication = len(jobs)
        for duplicate in deduplicated.duplicates:
            self._log("duplicate_merged", duplicate.to_dict())

        parser = SourceAwareParser(self.app_config)
        pipeline = JobHuntPipeline(
            profile=self.profile,
            config=self.app_config,
            resume_router=self.resume_router,
            parser=parser,
        )
        results: list[PipelineResult] = []
        for source_job in jobs:
            raw_job = source_job.to_raw_job(discovered_date=started_at[:10])
            try:
                if discovery_only:
                    normalized = parser.parse(raw_job)
                    self._write_normalized(normalized)
                    summary.jobs_normalized += 1
                    continue
                result = pipeline.analyze(raw_job)
                self._write_normalized(result.job)
                self._write_result(result)
                summary.jobs_normalized += 1
                summary.jobs_analyzed += 1
                results.append(result)
            except Exception as exc:  # isolate one malformed/unscorable posting
                failure = SourceFailure(
                    source=source_job.source,
                    url=source_job.source_url,
                    job_id=source_job.source_job_id,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
                summary.analysis_errors.append(failure)
                self._log("analysis_error", failure.to_dict())

        if not discovery_only:
            pool_path = self.output_dir / "job_pool.csv"
            self._write_job_pool(pool_path, results, started_at)
            summary.job_pool = str(pool_path)
        summary.completed_at = _utc_now()
        self._log("run_completed", summary.to_dict())
        return summary

    def _matches_target(self, job: SourceJob, target: DiscoveryTarget) -> bool:
        keywords = list(target.search_keywords)
        for family in target.job_families:
            keywords.extend(self.config.job_families.get(family, []))
        if keywords:
            haystack = normalize_text_key(f"{job.title}\n{job.description}")
            if not any(normalize_text_key(keyword) in haystack for keyword in keywords):
                return False
        if target.location_filter and job.location:
            location = normalize_text_key(job.location)
            if not any(normalize_text_key(value) in location for value in target.location_filter):
                return False
        if target.employment_types and job.employment_type:
            accepted = {normalize_text_key(value) for value in target.employment_types}
            if normalize_text_key(job.employment_type) not in accepted:
                return False
        return True

    def _prepare_output(self) -> None:
        for name in ("raw", "normalized", "results"):
            (self.output_dir / name).mkdir(parents=True, exist_ok=True)

    def _write_raw(self, job: SourceJob, discovered_at: str) -> None:
        raw = job.to_raw_job(discovered_date=discovered_at[:10])
        identity = build_job_id(
            source=raw.source,
            source_native_id=raw.source_native_id,
            apply_url=raw.apply_url,
            source_url=raw.source_url,
            company=raw.company,
            title=raw.title,
            location=raw.location,
        )
        _write_json(
            self.output_dir / "raw" / f"{_safe_filename(identity)}.json",
            {"discovered_at": discovered_at, **job.to_dict()},
        )

    def _write_normalized(self, job: NormalizedJob) -> None:
        _write_json(
            self.output_dir / "normalized" / f"{_safe_filename(job.job_id)}.json",
            job.to_dict(),
        )

    def _write_result(self, result: PipelineResult) -> None:
        _write_json(
            self.output_dir / "results" / f"{_safe_filename(result.job.job_id)}.json",
            result.to_dict(),
        )

    def _write_job_pool(
        self,
        path: Path,
        results: list[PipelineResult],
        discovered_at: str,
    ) -> None:
        order = {"APPLY": 0, "REVIEW": 1, "SKIP": 2}
        ranked = sorted(
            results,
            key=lambda item: (
                order[item.score.recommendation.value],
                -item.score.overall_score,
                -item.score.coverage,
                item.job.company.casefold(),
                item.job.title.casefold(),
                item.job.job_id,
            ),
        )
        fields = [
            "job_id",
            "company",
            "title",
            "source",
            "source_url",
            "location",
            "work_mode",
            "salary",
            "fit_score",
            "evidence_coverage",
            "decision",
            "selected_resume",
            "hard_blockers",
            "seniority_risk",
            "discovery_timestamp",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for result in ranked:
                writer.writerow(
                    {
                        "job_id": result.job.job_id,
                        "company": result.job.company,
                        "title": result.job.title,
                        "source": result.job.source,
                        "source_url": result.job.source_url,
                        "location": result.job.location,
                        "work_mode": result.job.remote_policy,
                        "salary": _format_salary(result.job.salary),
                        "fit_score": f"{result.score.overall_score:.1f}",
                        "evidence_coverage": f"{result.score.coverage:.4f}",
                        "decision": result.score.recommendation.value,
                        "selected_resume": result.resume.resume_id,
                        "hard_blockers": json.dumps(result.score.hard_blockers, ensure_ascii=False),
                        "seniority_risk": json.dumps(
                            result.score.seniority_experience_risk.__dict__
                            if hasattr(result.score.seniority_experience_risk, "__dict__")
                            else asdict(result.score.seniority_experience_risk),
                            ensure_ascii=False,
                        ),
                        "discovery_timestamp": discovered_at,
                    }
                )

    def _log(self, event: str, details: Mapping[str, Any]) -> None:
        record = {"timestamp": _utc_now(), "event": event, **dict(details)}
        log_path = self.output_dir / "discovery_log.jsonl"
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _format_salary(value: SalaryRange | None) -> str:
    if value is None:
        return ""
    if value.minimum is not None and value.maximum is not None:
        amount = f"{value.minimum:g}-{value.maximum:g}"
    elif value.minimum is not None:
        amount = f"{value.minimum:g}+"
    elif value.maximum is not None:
        amount = f"up to {value.maximum:g}"
    else:
        amount = ""
    structured = " ".join(part for part in (value.currency, amount) if part).strip()
    if structured and value.period:
        structured += f"/{value.period}"
    return structured or value.raw_text


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._") or "job"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DiscoveryConfigError(f"{field_name} must be an array.")
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise DiscoveryConfigError(f"{field_name} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DiscoveryConfigError(f"{field_name} must be a positive integer.") from exc
    if parsed < 1:
        raise DiscoveryConfigError(f"{field_name} must be positive.")
    return parsed


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise DiscoveryConfigError(f"{field_name} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DiscoveryConfigError(f"{field_name} must be a non-negative integer.") from exc
    if parsed < 0:
        raise DiscoveryConfigError(f"{field_name} cannot be negative.")
    return parsed


def _positive_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise DiscoveryConfigError(f"{field_name} must be positive.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DiscoveryConfigError(f"{field_name} must be positive.") from exc
    if parsed <= 0:
        raise DiscoveryConfigError(f"{field_name} must be positive.")
    return parsed


def _nonnegative_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise DiscoveryConfigError(f"{field_name} cannot be negative.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DiscoveryConfigError(f"{field_name} cannot be negative.") from exc
    if parsed < 0:
        raise DiscoveryConfigError(f"{field_name} cannot be negative.")
    return parsed


def _optional_number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
