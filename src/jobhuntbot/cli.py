"""Command-line interface for local matching and public-ATS discovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .aggregator_discovery import AggregatorDiscoveryCoordinator
from .aggregators import (
    AGGREGATOR_SOURCES,
    AggregatorConfigError,
    PoliteTextClient,
    load_aggregator_config,
)
from .config import ConfigError, load_config
from .discovery import DiscoveryConfigError, DiscoveryRunner, load_discovery_config
from .employer_registry import (
    EmployerRegistryError,
    generate_registry_targets,
    load_employer_registry,
    resolve_registry_discovery_config,
    target_employer_id,
    target_health_status,
    validate_employer_registry,
)
from .models import PipelineResult, RawJob
from .pipeline import JobHuntPipeline, PipelineValidationError
from .profile import ProfileLoadError, load_candidate_profile, validate_candidate_profile
from .resume_router import ResumeRouter, ResumeRoutingError, load_resume_routing
from .sources import PoliteJsonClient, SourceFailure, create_adapter, supported_sources
from .storage import SchemaMismatchError, StorageError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhuntbot",
        description="Local, deterministic, explainable job discovery and matching.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-profile", help="Validate a candidate profile JSON file.")
    validate.add_argument("--profile", required=True, type=Path)
    validate.add_argument("--json", action="store_true", dest="as_json")

    analyze = subparsers.add_parser("analyze-job", help="Parse, score, classify, and route one raw job description.")
    analyze.add_argument("--profile", required=True, type=Path)
    analyze.add_argument("--description-file", required=True, type=Path)
    analyze.add_argument("--title", required=True)
    analyze.add_argument("--company", required=True)
    analyze.add_argument("--location", default="")
    analyze.add_argument("--source", default="manual")
    analyze.add_argument("--source-native-id", default="")
    analyze.add_argument("--source-url", default="")
    analyze.add_argument("--apply-url", default="")
    analyze.add_argument("--posted-date", default="")
    analyze.add_argument("--discovered-date", default="")
    analyze.add_argument("--resume-routing", type=Path)
    analyze.add_argument("--config", type=Path)
    analyze.add_argument("--save", action="store_true")
    analyze.add_argument("--job-pool", type=Path, default=Path("dashboard/job_pool.csv"))
    analyze.add_argument("--json", action="store_true", dest="as_json")

    discover = subparsers.add_parser(
        "discover",
        help="Discover public ATS jobs, deduplicate, and optionally run Phase 1 analysis.",
    )
    discover.add_argument("--profile", required=True, type=Path)
    discover.add_argument("--discovery-config", type=Path)
    discover.add_argument(
        "--aggregator-config",
        type=Path,
        help="Private LinkedIn/Indeed discovery-candidate configuration.",
    )
    discover.add_argument(
        "--employer-registry",
        type=Path,
        help="Private employer registry used to generate discovery targets.",
    )
    discover.add_argument("--resume-routing", type=Path)
    discover.add_argument("--scoring-config", type=Path)
    discover.add_argument(
        "--output-dir",
        type=Path,
        default=Path("my-materials/discovery"),
        help="Private output directory (default: my-materials/discovery).",
    )
    discover.add_argument(
        "--source",
        action="append",
        choices=tuple(sorted({*supported_sources(), *AGGREGATOR_SOURCES})),
        help="Limit the run to configured ATS and/or aggregator discovery sources.",
    )
    discover.add_argument("--limit", type=int, help="Maximum unique jobs for this run.")
    discover.add_argument(
        "--track",
        action="append",
        help="With --employer-registry, select employers serving one or more career tracks.",
    )
    discover.add_argument(
        "--min-priority",
        type=int,
        help="With --employer-registry, include employers at or above this priority.",
    )
    discover.add_argument(
        "--max-employers",
        type=int,
        help="With --employer-registry, cap generated targets after deterministic sorting.",
    )
    discover.add_argument(
        "--discovery-only",
        action="store_true",
        help="Fetch, filter, deduplicate, and normalize without scoring or resume routing.",
    )
    discover.add_argument(
        "--reanalyze-unchanged",
        action="store_true",
        help="Explicitly re-analyze unchanged jobs when incremental discovery is enabled.",
    )
    discover.add_argument("--json", action="store_true", dest="as_json")

    validate_registry = subparsers.add_parser(
        "validate-registry",
        help="Validate an employer registry offline, with an optional explicit live check.",
    )
    validate_registry.add_argument("--employer-registry", required=True, type=Path)
    validate_registry.add_argument("--discovery-config", type=Path)
    validate_registry.add_argument(
        "--track", action="append", help="Validate/select only registry targets for a track."
    )
    validate_registry.add_argument(
        "--source", action="append", choices=supported_sources()
    )
    validate_registry.add_argument("--min-priority", type=int)
    validate_registry.add_argument("--max-employers", type=int)
    validate_registry.add_argument(
        "--live",
        action="store_true",
        help="Explicitly make bounded public endpoint checks (never enabled by default).",
    )
    validate_registry.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_profile_validation(path: Path, result, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"profile": str(path), **result.to_dict()}, ensure_ascii=False, indent=2))
        return
    print(f"Profile: {path}")
    print(f"Valid: {'yes' if result.valid else 'no'}")
    if not result.issues:
        print("No validation issues.")
    for issue in result.issues:
        print(f"[{issue.severity.upper()}] {issue.field}: {issue.message}")


def _print_items(title: str, values: list[str]) -> None:
    print(f"\n{title}:")
    if not values:
        print("  - None")
        return
    for value in values:
        print(f"  - {value}")


def _print_pipeline_result(result: PipelineResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    job = result.job
    score = result.score
    resume = result.resume
    print(f"Job: {job.company} — {job.title}")
    print(f"Job ID: {job.job_id}")
    print(f"Recommendation: {score.recommendation.value}")
    print(f"Fit score: {score.overall_score:.1f}/100 (evidence coverage {score.coverage:.0%})")
    if job.seniority is not None:
        print(f"Seniority: {job.seniority.level} ({job.seniority.evidence})")
    print("\nScore breakdown:")
    for component in score.components:
        awarded = "unknown" if component.awarded_points is None else f"{component.awarded_points:.1f}"
        print(f"  - {component.component}: {awarded}/{component.maximum_points:g}")
        print(f"    Reason: {component.reason}")
        if component.evidence:
            print(f"    Evidence: {'; '.join(component.evidence)}")
    _print_items("Matched requirements", score.matched_requirements)
    _print_items("Missing requirements", score.missing_requirements)
    _print_items("Unknown requirements", score.unknown_requirements)
    _print_items("Hard blockers", score.hard_blockers)
    risk = score.seniority_experience_risk
    print("\nSeniority/experience risk:")
    print(f"  Triggered: {'yes' if risk.triggered else 'no'}")
    print(f"  Reason: {risk.reason}")
    if risk.evidence:
        print(f"  Evidence: {'; '.join(risk.evidence)}")
    print("\nResume recommendation:")
    if resume.selected:
        print(f"  {resume.resume_id}: {resume.file_path}")
    else:
        print("  No resume selected")
    print(f"  Reason: {resume.reason}")
    if resume.evidence:
        print(f"  Evidence: {'; '.join(resume.evidence)}")
    _print_items("Decision reasoning", score.reasoning)
    if result.saved:
        print(f"\nJob pool: {result.storage_action}")


def _print_discovery_summary(summary, as_json: bool) -> None:
    value = summary.to_dict()
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"Discovery completed: {summary.completed_at}")
    print(f"Sources: {', '.join(summary.selected_sources) or 'none'}")
    print(f"Targets attempted: {summary.targets_attempted}")
    print(f"Jobs fetched: {summary.jobs_fetched}")
    print(f"Jobs retained after filters: {summary.jobs_after_filters}")
    print(f"Unique jobs after deduplication: {summary.jobs_after_deduplication}")
    print(f"Duplicates removed: {summary.duplicates_removed}")
    print(f"Jobs normalized: {summary.jobs_normalized}")
    print(f"Jobs analyzed: {summary.jobs_analyzed}")
    print(f"Relevant jobs: {summary.jobs_relevant}")
    print(f"Irrelevant jobs: {summary.jobs_irrelevant}")
    print(f"Unchanged jobs skipped: {summary.jobs_skipped_unchanged}")
    print(
        "Decisions: "
        f"APPLY={summary.apply_count}, REVIEW={summary.review_count}, SKIP={summary.skip_count}"
    )
    print(f"Failures captured: {summary.failure_count}")
    if summary.aggregator_source_status:
        print(f"Aggregator candidates: {summary.aggregator_candidates}")
        print(f"Aggregator ATS resolutions: {summary.aggregator_resolved}")
        print(f"Aggregator unresolved: {summary.aggregator_unresolved}")
        for source, status in summary.aggregator_source_status.items():
            print(f"Aggregator {source}: {status.get('status', 'unknown')}")
    print(f"Private output: {summary.output_dir}")
    if summary.job_pool:
        print(f"Ranked job pool: {summary.job_pool}")


def _print_registry_validation(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"Employer registry: {value['registry_path']}")
    print(f"Valid: {'yes' if value['valid'] else 'no'}")
    print(f"Employers: {value['employer_count']} ({value['enabled_count']} enabled)")
    for issue in value.get("issues", []):
        employer = f" [{issue['employer_id']}]" if issue.get("employer_id") else ""
        print(f"[{issue['severity'].upper()}]{employer} {issue['field']}: {issue['message']}")
    for check in value.get("live_checks", []):
        print(
            f"[LIVE] {check['employer_id']} ({check['source']}): "
            f"{check['status']}, jobs={check['jobs_found']}"
        )
        if check.get("error"):
            print(f"  Error: {check['error']}")


def _resolve_resume_routing(profile_path: Path, configured: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured_path = Path(configured)
    return configured_path if configured_path.is_absolute() else profile_path.parent / configured_path


def _load_analysis_dependencies(profile_path: Path, routing_override: Path | None, config_path: Path | None):
    profile = load_candidate_profile(profile_path)
    config = load_config(config_path)
    routing_path = _resolve_resume_routing(
        profile_path, profile.resume_routing_file, routing_override
    )
    routing = load_resume_routing(routing_path)
    router = ResumeRouter(routing, config.normalization.get("skill_aliases", {}))
    return profile, config, router


def _registry_discovery_config_path(args, registry) -> Path:
    configured = args.discovery_config or resolve_registry_discovery_config(registry)
    if configured is None:
        raise DiscoveryConfigError(
            "Registry discovery requires --discovery-config or a registry discovery_config path."
        )
    return Path(configured)


def _registry_targets(args, registry, discovery_config):
    known_tracks = list(discovery_config.career_tracks or discovery_config.job_families)
    requested_sources = getattr(args, "source", None)
    registry_sources = (
        [item for item in requested_sources if item in supported_sources()]
        if requested_sources
        else None
    )
    if requested_sources and not registry_sources:
        return []
    return generate_registry_targets(
        registry,
        known_tracks=known_tracks,
        tracks=getattr(args, "track", None),
        sources=registry_sources,
        minimum_priority=getattr(args, "min_priority", None),
        max_employers=getattr(args, "max_employers", None),
    )


def _live_registry_checks(targets, discovery_config) -> list[dict]:
    client = PoliteJsonClient(
        timeout_seconds=discovery_config.request.timeout_seconds,
        retries=discovery_config.request.retries,
        min_interval_seconds=discovery_config.request.min_interval_seconds,
        user_agent=discovery_config.request.user_agent,
    )
    checks: list[dict] = []
    for target in targets:
        errors: list[SourceFailure] = []
        jobs_found = 0
        try:
            batch = create_adapter(target.source, client).fetch(target, limit=1)
            errors = batch.errors
            jobs_found = len(batch.jobs)
        except Exception as exc:
            errors = [
                SourceFailure(
                    source=target.source,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
            ]
        status, error = target_health_status(errors, jobs_found)
        checks.append(
            {
                "employer_id": target_employer_id(target),
                "employer": target.company or target.board,
                "source": target.source,
                "status": status,
                "jobs_found": jobs_found,
                "error": error,
            }
        )
    return checks


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-profile":
            profile = load_candidate_profile(args.profile)
            result = validate_candidate_profile(profile)
            _print_profile_validation(args.profile, result, args.as_json)
            return 0 if result.valid else 1

        if args.command == "discover":
            profile, config, router = _load_analysis_dependencies(
                args.profile, args.resume_routing, args.scoring_config
            )
            if args.employer_registry is not None:
                registry = load_employer_registry(args.employer_registry)
                discovery_config = load_discovery_config(
                    _registry_discovery_config_path(args, registry)
                )
                discovery_config.targets = _registry_targets(
                    args, registry, discovery_config
                )
            else:
                if args.discovery_config is None:
                    raise DiscoveryConfigError(
                        "discover requires --discovery-config, with optional --employer-registry."
                    )
                if args.track or args.min_priority is not None or args.max_employers is not None:
                    raise DiscoveryConfigError(
                        "--track, --min-priority, and --max-employers require --employer-registry."
                    )
                discovery_config = load_discovery_config(args.discovery_config)
            selected_ats = [
                item for item in (args.source or []) if item in supported_sources()
            ]
            selected_aggregators = [
                item for item in (args.source or []) if item in AGGREGATOR_SOURCES
            ]
            if args.source and not selected_ats:
                discovery_config.targets = []
            if selected_aggregators and args.aggregator_config is None:
                raise DiscoveryConfigError(
                    "--source linkedin/indeed requires --aggregator-config."
                )
            json_client = PoliteJsonClient(
                timeout_seconds=discovery_config.request.timeout_seconds,
                retries=discovery_config.request.retries,
                min_interval_seconds=discovery_config.request.min_interval_seconds,
                user_agent=discovery_config.request.user_agent,
            )
            preparation = None
            if args.aggregator_config is not None and (
                not args.source or selected_aggregators
            ):
                aggregator_config = load_aggregator_config(args.aggregator_config)
                text_client = PoliteTextClient(
                    timeout_seconds=discovery_config.request.timeout_seconds,
                    retries=discovery_config.request.retries,
                    min_interval_seconds=discovery_config.request.min_interval_seconds,
                    user_agent=discovery_config.request.user_agent,
                )
                preparation = AggregatorDiscoveryCoordinator(
                    config=aggregator_config,
                    output_dir=args.output_dir,
                    text_client=text_client,
                    json_client=json_client,
                ).prepare(
                    sources=selected_aggregators if args.source else None,
                    limit=args.limit,
                )
            runner = DiscoveryRunner(
                profile=profile,
                app_config=config,
                resume_router=router,
                discovery_config=discovery_config,
                output_dir=args.output_dir,
                client=json_client,
            )
            summary = runner.run(
                sources=selected_ats if args.source and selected_ats else None,
                limit=args.limit,
                discovery_only=args.discovery_only,
                reanalyze_unchanged=args.reanalyze_unchanged,
                seed_jobs=None if preparation is None else preparation.seeds,
                seed_failures=None if preparation is None else preparation.failures,
            )
            if preparation is not None:
                preparation.apply_to(summary)
            _print_discovery_summary(summary, args.as_json)
            return 0

        if args.command == "validate-registry":
            registry = load_employer_registry(args.employer_registry)
            discovery_path = args.discovery_config or resolve_registry_discovery_config(registry)
            discovery_config = (
                load_discovery_config(discovery_path) if discovery_path is not None else None
            )
            known_tracks = (
                list(discovery_config.career_tracks or discovery_config.job_families)
                if discovery_config is not None
                else None
            )
            report = validate_employer_registry(registry, known_tracks=known_tracks)
            value = report.to_dict()
            if args.live and report.valid:
                if discovery_config is None:
                    raise DiscoveryConfigError(
                        "--live registry validation requires a discovery configuration."
                    )
                targets = _registry_targets(args, registry, discovery_config)
                value["live_checks"] = _live_registry_checks(targets, discovery_config)
                if any(
                    item["status"] in {"temporarily_failed", "invalid_config", "unsupported"}
                    for item in value["live_checks"]
                ):
                    value["valid"] = False
            _print_registry_validation(value, args.as_json)
            return 0 if value["valid"] else 1

        profile, config, router = _load_analysis_dependencies(
            args.profile, args.resume_routing, args.config
        )
        try:
            description = args.description_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise PipelineValidationError(f"Could not read job description {args.description_file}: {exc}") from exc
        raw_job = RawJob(
            title=args.title,
            company=args.company,
            location=args.location,
            source=args.source,
            source_native_id=args.source_native_id,
            source_url=args.source_url,
            apply_url=args.apply_url,
            posted_date=args.posted_date,
            discovered_date=args.discovered_date,
            job_description=description,
        )
        pipeline = JobHuntPipeline(profile=profile, config=config, resume_router=router)
        result = pipeline.analyze(raw_job, args.job_pool if args.save else None)
        _print_pipeline_result(result, args.as_json)
        return 0
    except (
        ConfigError,
        AggregatorConfigError,
        DiscoveryConfigError,
        EmployerRegistryError,
        ProfileLoadError,
        ResumeRoutingError,
        PipelineValidationError,
        SchemaMismatchError,
        StorageError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
