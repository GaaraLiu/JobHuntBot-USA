"""Configurable public-ATS discovery integrated with the frozen Phase 1 pipeline."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import AppConfig
from .deduplication import DuplicateRecord, deduplicate_jobs
from .discovery_state import DiscoveryStateStore, content_fingerprint
from .employer_registry import (
    EmployerHealthStore,
    target_employer_id,
    target_health_status,
)
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
from .relevance import (
    CareerTrack,
    JobRelevanceEvaluator,
    LocationPolicy,
    RelevancePolicy,
    RelevanceResult,
    career_track_from_mapping,
    location_policy_from_mapping,
    relevance_policy_from_mapping,
)
from .sources import (
    DiscoveryTarget,
    JsonHttpClient,
    PoliteJsonClient,
    SourceAdapter,
    SourceConfigurationError,
    SourceFailure,
    SourceJob,
    create_adapter,
    supported_sources,
)
from .sources.workday import validate_workday_target


class DiscoveryConfigError(ValueError):
    """Raised when a discovery configuration is missing or invalid."""


@dataclass(slots=True)
class DiscoveryRequestSettings:
    timeout_seconds: float = 15.0
    retries: int = 2
    min_interval_seconds: float = 0.5
    user_agent: str = "JobHuntBot/0.2 (local public-job discovery)"


@dataclass(slots=True)
class IncrementalPolicy:
    enabled: bool = False
    state_file: str = "state/discovery_state.json"
    reanalyze_unchanged: bool = False


@dataclass(slots=True)
class DiscoveryConfig:
    schema_version: int = 1
    job_families: dict[str, list[str]] = field(default_factory=dict)
    targets: list[DiscoveryTarget] = field(default_factory=list)
    request: DiscoveryRequestSettings = field(default_factory=DiscoveryRequestSettings)
    max_jobs_per_target: int = 100
    max_fetch_per_target: int = 100
    max_pages: int = 1
    posted_within_days: int | None = None
    career_tracks: dict[str, CareerTrack] = field(default_factory=dict)
    relevance_policy: RelevancePolicy = field(default_factory=RelevancePolicy)
    location_policy: LocationPolicy = field(default_factory=LocationPolicy)
    incremental: IncrementalPolicy = field(default_factory=IncrementalPolicy)


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
    jobs_relevant: int = 0
    jobs_irrelevant: int = 0
    jobs_new: int = 0
    jobs_changed: int = 0
    jobs_recovered: int = 0
    jobs_forced: int = 0
    jobs_skipped_unchanged: int = 0
    freshness_unknown: int = 0
    apply_count: int = 0
    review_count: int = 0
    skip_count: int = 0
    source_pages: dict[str, int] = field(default_factory=dict)
    source_errors: list[SourceFailure] = field(default_factory=list)
    analysis_errors: list[SourceFailure] = field(default_factory=list)
    duplicates: list[DuplicateRecord] = field(default_factory=list)
    discovery_only: bool = False
    output_dir: str = ""
    job_pool: str = ""
    state_path: str = ""
    health_path: str = ""
    employer_health: dict[str, dict[str, Any]] = field(default_factory=dict)

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

    tracks_value = value.get("career_tracks", {})
    if not isinstance(tracks_value, Mapping):
        raise DiscoveryConfigError("career_tracks must be an object.")
    tracks: dict[str, CareerTrack] = {}
    try:
        for name, definition in tracks_value.items():
            if not isinstance(definition, Mapping):
                raise DiscoveryConfigError(f"career_tracks.{name} must be an object.")
            normalized_name = str(name).strip()
            if not normalized_name:
                raise DiscoveryConfigError("career track names cannot be empty.")
            tracks[normalized_name] = career_track_from_mapping(
                normalized_name, definition
            )

        relevance_value = value.get("relevance_policy", {})
        if not isinstance(relevance_value, Mapping):
            raise DiscoveryConfigError("relevance_policy must be an object.")
        relevance_policy = relevance_policy_from_mapping(relevance_value)
        if relevance_policy.enabled and not tracks:
            raise DiscoveryConfigError(
                "career_tracks must be configured when relevance_policy.enabled is true."
            )

        location_value = value.get("location_policy", {})
        if not isinstance(location_value, Mapping):
            raise DiscoveryConfigError("location_policy must be an object.")
        location_policy = location_policy_from_mapping(location_value)
    except ValueError as exc:
        raise DiscoveryConfigError(str(exc)) from exc

    global_max_pages = _positive_int(value.get("max_pages", 1), "max_pages")
    global_posted_within = _optional_positive_int(
        value.get("posted_within_days"), "posted_within_days"
    )

    targets_value = value.get("targets", [])
    if not isinstance(targets_value, list):
        raise DiscoveryConfigError("targets must be an array.")
    targets: list[DiscoveryTarget] = []
    for index, item in enumerate(targets_value):
        if not isinstance(item, Mapping):
            raise DiscoveryConfigError(f"targets[{index}] must be an object.")
        target_value = dict(item)
        target_value.setdefault("max_pages", global_max_pages)
        if global_posted_within is not None:
            target_value.setdefault("posted_within_days", global_posted_within)
        try:
            target = DiscoveryTarget.from_mapping(target_value)
        except ValueError as exc:
            raise DiscoveryConfigError(f"targets[{index}]: {exc}") from exc
        if target.source not in supported_sources():
            raise DiscoveryConfigError(
                f"targets[{index}] uses unsupported source '{target.source}'."
            )
        if target.source == "workday":
            try:
                validate_workday_target(target)
            except SourceConfigurationError as exc:
                raise DiscoveryConfigError(f"targets[{index}]: {exc}") from exc
        known_tracks = set(families) | set(tracks)
        unknown_families = sorted(set(target.job_families) - known_tracks)
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

    incremental_value = value.get("incremental", {})
    if not isinstance(incremental_value, Mapping):
        raise DiscoveryConfigError("incremental must be an object.")
    state_file = str(
        incremental_value.get("state_file", "state/discovery_state.json")
    ).strip()
    state_path = Path(state_file)
    if (
        not state_file
        or state_path.is_absolute()
        or ".." in state_path.parts
    ):
        raise DiscoveryConfigError(
            "incremental.state_file must be a relative path inside the private output directory."
        )
    incremental = IncrementalPolicy(
        enabled=incremental_value.get("enabled", False) is True,
        state_file=state_file,
        reanalyze_unchanged=(
            incremental_value.get("reanalyze_unchanged", False) is True
        ),
    )
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
        max_pages=global_max_pages,
        posted_within_days=global_posted_within,
        career_tracks=tracks,
        relevance_policy=relevance_policy,
        location_policy=location_policy,
        incremental=incremental,
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
        reanalyze_unchanged: bool = False,
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
        health_path = self.output_dir / "health" / "employer_health.json"
        health = EmployerHealthStore(health_path)
        summary.health_path = str(health_path)
        self._log("run_started", {"started_at": started_at, "sources": summary.selected_sources})

        relevance_evaluator = JobRelevanceEvaluator(
            self.config.career_tracks,
            self.config.relevance_policy,
            self.config.location_policy,
        )
        state: DiscoveryStateStore | None = None
        if self.config.incremental.enabled:
            state_path = self.output_dir / self.config.incremental.state_file
            state = DiscoveryStateStore(state_path)
            summary.state_path = str(state_path)

        recommended_records: list[dict[str, Any]] = []
        irrelevant_records: list[dict[str, Any]] = []

        fetched: list[SourceJob] = []
        for target in targets:
            employer_id = target_employer_id(target)
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
                self._write_error(failure, "source")
                status = (
                    "invalid_config"
                    if isinstance(exc, SourceConfigurationError)
                    else "unsupported"
                    if "Unsupported" in type(exc).__name__
                    else "temporarily_failed"
                )
                summary.employer_health[employer_id] = health.record(
                    employer_id=employer_id,
                    employer=target.company or target.board,
                    source=target.source,
                    status=status,
                    checked_at=started_at,
                    error=str(exc),
                )
                continue
            summary.jobs_fetched += len(batch.jobs)
            summary.source_pages[target.source] = (
                summary.source_pages.get(target.source, 0) + batch.pages_fetched
            )
            summary.source_errors.extend(batch.errors)
            for failure in batch.errors:
                self._log("source_error", failure.to_dict())
                self._write_error(failure, "source")
            health_status, health_error = target_health_status(
                batch.errors, len(batch.jobs)
            )
            summary.employer_health[employer_id] = health.record(
                employer_id=employer_id,
                employer=target.company or target.board,
                source=target.source,
                status=health_status,
                checked_at=started_at,
                jobs_found=len(batch.jobs),
                error=health_error,
            )
            retained: list[SourceJob] = []
            for job in batch.jobs:
                tracks = job.metadata.get("_discovery_tracks", [])
                if not isinstance(tracks, list):
                    tracks = []
                job.metadata["_discovery_tracks"] = list(
                    dict.fromkeys([*tracks, *target.job_families])
                )
                job.metadata["_discovery_employer_id"] = employer_id
                job.metadata["_discovery_registry_priority"] = target.options.get(
                    "registry_priority"
                )
                self._write_raw(job, started_at)
                matched, reason, freshness = self._matches_target(job, target)
                if freshness == "unknown":
                    summary.freshness_unknown += 1
                if matched:
                    retained.append(job)
                    continue
                record = self._target_filter_record(job, reason, freshness, started_at)
                irrelevant_records.append(record)
                summary.jobs_irrelevant += 1
                self._write_queue_record("irrelevant", record)
            per_target_limit = target.max_results or self.config.max_jobs_per_target
            retained = retained[: min(per_target_limit, self.config.max_jobs_per_target)]
            fetched.extend(retained)
            self._log(
                "source_complete",
                {
                    "source": target.source,
                    "board": target.board,
                    "employer_id": employer_id,
                    "fetched": len(batch.jobs),
                    "retained": len(retained),
                    "pages_fetched": batch.pages_fetched,
                },
            )

        summary.jobs_after_filters = len(fetched)

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
        for source_job in jobs:
            raw_job = source_job.to_raw_job(discovered_date=started_at[:10])
            try:
                normalized = parser.parse(raw_job)
                self._write_normalized(normalized)
                summary.jobs_normalized += 1
                result_path = self._result_path(normalized.job_id)
                incremental = None
                if state is not None:
                    incremental = state.observe(
                        job_id=normalized.job_id,
                        source_job=source_job,
                        content_fingerprint=content_fingerprint(source_job),
                        seen_at=started_at,
                        result_exists=result_path.exists(),
                        force_reanalyze=(
                            reanalyze_unchanged
                            or self.config.incremental.reanalyze_unchanged
                        ),
                    )

                relevance = relevance_evaluator.evaluate(source_job, normalized)
                if state is not None:
                    state.mark_relevance(
                        job_id=normalized.job_id,
                        checked_at=started_at,
                        matched_track=relevance.matched_track,
                        relevant=relevance.relevant,
                    )
                if not relevance.relevant:
                    summary.jobs_irrelevant += 1
                    record = self._queue_record(
                        source_job,
                        normalized,
                        relevance,
                        None if incremental is None else incremental.to_dict(),
                        None,
                        started_at,
                    )
                    irrelevant_records.append(record)
                    self._write_queue_record("irrelevant", record)
                    continue

                summary.jobs_relevant += 1
                if discovery_only:
                    record = self._queue_record(
                        source_job,
                        normalized,
                        relevance,
                        None if incremental is None else incremental.to_dict(),
                        None,
                        started_at,
                    )
                    recommended_records.append(record)
                    self._write_queue_record("recommended", record)
                    continue

                should_analyze = incremental is None or incremental.should_analyze
                incremental_data = (
                    {"status": "disabled", "should_analyze": True,
                     "reason": "Incremental discovery is disabled."}
                    if incremental is None
                    else incremental.to_dict()
                )
                result_data: dict[str, Any]
                if should_analyze:
                    result = pipeline.analyze(raw_job)
                    self._write_result(result)
                    result_data = result.to_dict()
                    summary.jobs_analyzed += 1
                    if incremental is not None:
                        self._count_incremental(summary, incremental.status)
                        state.mark_analyzed(
                            job_id=normalized.job_id,
                            analyzed_at=started_at,
                            score=result.score.overall_score,
                            decision=result.score.recommendation.value,
                            resume=result.resume.resume_id,
                        )
                else:
                    result_data = self._load_result(result_path)
                    summary.jobs_skipped_unchanged += 1

                self._count_decision(summary, result_data)
                record = self._queue_record(
                    source_job,
                    normalized,
                    relevance,
                    incremental_data,
                    result_data,
                    started_at,
                )
                recommended_records.append(record)
                self._write_queue_record("recommended", record)
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
                self._write_error(failure, "analysis")

        if state is not None:
            state.save()
        health.save()
        _write_json(self.output_dir / "recommended" / "current_queue.json", recommended_records)
        _write_json(self.output_dir / "irrelevant" / "current_queue.json", irrelevant_records)
        _write_json(
            self.output_dir / "errors" / "current_queue.json",
            [item.to_dict() for item in [*summary.source_errors, *summary.analysis_errors]],
        )
        if not discovery_only:
            pool_path = self.output_dir / "job_pool.csv"
            self._write_job_pool(pool_path, recommended_records, started_at)
            summary.job_pool = str(pool_path)
        summary.completed_at = _utc_now()
        self._log("run_completed", summary.to_dict())
        return summary

    def _matches_target(
        self, job: SourceJob, target: DiscoveryTarget
    ) -> tuple[bool, str, str]:
        keywords = list(target.search_keywords)
        if not self.config.relevance_policy.enabled:
            for family in target.job_families:
                keywords.extend(self.config.job_families.get(family, []))
        if keywords:
            haystack = normalize_text_key(f"{job.title}\n{job.description}")
            if not any(normalize_text_key(keyword) in haystack for keyword in keywords):
                return False, "No configured target search keyword matched.", "not_assessed"
        if target.location_filter and job.location:
            location = normalize_text_key(job.location)
            if not any(normalize_text_key(value) in location for value in target.location_filter):
                return False, "Location did not match the target location filter.", "not_assessed"
        if target.employment_types and job.employment_type:
            accepted = {normalize_text_key(value) for value in target.employment_types}
            if normalize_text_key(job.employment_type) not in accepted:
                return False, "Employment type did not match the target filter.", "not_assessed"

        within_days = target.posted_within_days
        if within_days is None:
            return True, "", "not_configured"
        source_date = job.updated_date or job.posted_date
        if not source_date:
            return True, "Source date is unavailable and was preserved as UNKNOWN.", "unknown"
        try:
            posted = date.fromisoformat(source_date[:10])
        except ValueError:
            return True, "Source date is unparseable and was preserved as UNKNOWN.", "unknown"
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=within_days)
        if posted < cutoff:
            return False, f"Posting is older than posted_within_days={within_days}.", "stale"
        return True, "", "current"

    def _prepare_output(self) -> None:
        for name in (
            "raw",
            "normalized",
            "results",
            "recommended",
            "irrelevant",
            "errors",
            "state",
            "health",
        ):
            (self.output_dir / name).mkdir(parents=True, exist_ok=True)

    def _result_path(self, job_id: str) -> Path:
        return self.output_dir / "results" / f"{_safe_filename(job_id)}.json"

    def _load_result(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryConfigError(f"Could not reuse prior analysis {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise DiscoveryConfigError(f"Prior analysis {path} must be a JSON object.")
        return value

    @staticmethod
    def _count_incremental(summary: DiscoveryRunSummary, status: str) -> None:
        field_name = {
            "new": "jobs_new",
            "changed": "jobs_changed",
            "recovery": "jobs_recovered",
            "forced": "jobs_forced",
        }.get(status)
        if field_name:
            setattr(summary, field_name, getattr(summary, field_name) + 1)

    @staticmethod
    def _count_decision(summary: DiscoveryRunSummary, result: Mapping[str, Any]) -> None:
        score = result.get("score", {})
        decision = str(score.get("recommendation", "")) if isinstance(score, Mapping) else ""
        field_name = {
            "APPLY": "apply_count",
            "REVIEW": "review_count",
            "SKIP": "skip_count",
        }.get(decision)
        if field_name:
            setattr(summary, field_name, getattr(summary, field_name) + 1)

    def _target_filter_record(
        self,
        job: SourceJob,
        reason: str,
        freshness: str,
        discovered_at: str,
    ) -> dict[str, Any]:
        job_id = _source_job_identity(job)
        return {
            "job_id": job_id,
            "company": job.company,
            "title": job.title,
            "source": job.source,
            "source_url": job.source_url,
            "location": job.location,
            "discovered_at": discovered_at,
            "stage": "target_filter",
            "freshness": freshness,
            "relevance": RelevanceResult(
                excluded=True,
                exclusion_type="target_filter",
                reason=reason,
            ).to_dict(),
            "source_job": job.to_dict(),
        }

    @staticmethod
    def _queue_record(
        source_job: SourceJob,
        normalized: NormalizedJob,
        relevance: RelevanceResult,
        incremental: Mapping[str, Any] | None,
        result: Mapping[str, Any] | None,
        discovered_at: str,
    ) -> dict[str, Any]:
        return {
            "job_id": normalized.job_id,
            "company": normalized.company,
            "title": normalized.title,
            "source": normalized.source,
            "source_url": normalized.source_url,
            "location": normalized.location,
            "discovered_at": discovered_at,
            "stage": "relevance",
            "relevance": relevance.to_dict(),
            "incremental": None if incremental is None else dict(incremental),
            "source_job": source_job.to_dict(),
            "result": None if result is None else dict(result),
        }

    def _write_queue_record(self, queue: str, record: Mapping[str, Any]) -> None:
        job_id = str(record.get("job_id") or "job")
        _write_json(
            self.output_dir / queue / f"{_safe_filename(job_id)}.json",
            record,
        )

    def _write_error(self, failure: SourceFailure, stage: str) -> None:
        identity = failure.job_id or f"{failure.source}_{stage}_{_utc_now()}"
        _write_json(
            self.output_dir / "errors" / f"{_safe_filename(identity)}.json",
            {"stage": stage, **failure.to_dict()},
        )

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
        _write_json(self._result_path(result.job.job_id), result.to_dict())

    def _write_job_pool(
        self,
        path: Path,
        records: list[Mapping[str, Any]],
        discovered_at: str,
    ) -> None:
        order = {"APPLY": 0, "REVIEW": 1, "SKIP": 2}
        analyzed = [item for item in records if isinstance(item.get("result"), Mapping)]

        def rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
            result = item["result"]
            job = result.get("job", {})
            score = result.get("score", {})
            return (
                order.get(str(score.get("recommendation", "SKIP")), 3),
                -float(score.get("overall_score", 0)),
                -float(score.get("coverage", 0)),
                str(job.get("company", "")).casefold(),
                str(job.get("title", "")).casefold(),
                str(job.get("job_id", "")),
            )

        ranked = sorted(analyzed, key=rank)
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
            "matched_track",
            "relevance_strength",
            "relevance_reason",
            "incremental_status",
            "discovery_timestamp",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for record in ranked:
                result = record["result"]
                job = result.get("job", {})
                score = result.get("score", {})
                resume = result.get("resume", {})
                relevance = record.get("relevance", {})
                incremental = record.get("incremental", {})
                writer.writerow(
                    {
                        "job_id": job.get("job_id", ""),
                        "company": job.get("company", ""),
                        "title": job.get("title", ""),
                        "source": job.get("source", ""),
                        "source_url": job.get("source_url", ""),
                        "location": job.get("location", ""),
                        "work_mode": job.get("remote_policy", ""),
                        "salary": _format_salary_mapping(job.get("salary")),
                        "fit_score": f"{float(score.get('overall_score', 0)):.1f}",
                        "evidence_coverage": f"{float(score.get('coverage', 0)):.4f}",
                        "decision": score.get("recommendation", ""),
                        "selected_resume": resume.get("resume_id", ""),
                        "hard_blockers": json.dumps(
                            score.get("hard_blockers", []), ensure_ascii=False
                        ),
                        "seniority_risk": json.dumps(
                            score.get("seniority_experience_risk", {}),
                            ensure_ascii=False,
                        ),
                        "matched_track": relevance.get("matched_track", ""),
                        "relevance_strength": relevance.get("strength", ""),
                        "relevance_reason": relevance.get("reason", ""),
                        "incremental_status": incremental.get("status", ""),
                        "discovery_timestamp": discovered_at,
                    }
                )

    def _log(self, event: str, details: Mapping[str, Any]) -> None:
        record = {"timestamp": _utc_now(), "event": event, **dict(details)}
        log_path = self.output_dir / "discovery_log.jsonl"
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _write_json(path: Path, value: Any) -> None:
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


def _format_salary_mapping(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    minimum = _optional_number(value.get("minimum"))
    maximum = _optional_number(value.get("maximum"))
    if minimum is not None and maximum is not None:
        amount = f"{minimum:g}-{maximum:g}"
    elif minimum is not None:
        amount = f"{minimum:g}+"
    elif maximum is not None:
        amount = f"up to {maximum:g}"
    else:
        amount = ""
    currency = str(value.get("currency") or "")
    structured = " ".join(part for part in (currency, amount) if part).strip()
    period = str(value.get("period") or "")
    if structured and period:
        structured += f"/{period}"
    return structured or str(value.get("raw_text") or "")


def _source_job_identity(job: SourceJob) -> str:
    raw = job.to_raw_job()
    return build_job_id(
        source=raw.source,
        source_native_id=raw.source_native_id,
        apply_url=raw.apply_url,
        source_url=raw.source_url,
        company=raw.company,
        title=raw.title,
        location=raw.location,
    )


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


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, field_name)


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
