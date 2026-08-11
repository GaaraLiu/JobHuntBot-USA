"""Command-line interface for local matching and public-ATS discovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .discovery import DiscoveryConfigError, DiscoveryRunner, load_discovery_config
from .models import PipelineResult, RawJob
from .pipeline import JobHuntPipeline, PipelineValidationError
from .profile import ProfileLoadError, load_candidate_profile, validate_candidate_profile
from .resume_router import ResumeRouter, ResumeRoutingError, load_resume_routing
from .sources import supported_sources
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
    discover.add_argument("--discovery-config", required=True, type=Path)
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
        choices=supported_sources(),
        help="Limit the run to one or more configured ATS sources.",
    )
    discover.add_argument("--limit", type=int, help="Maximum unique jobs for this run.")
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
    print(f"Private output: {summary.output_dir}")
    if summary.job_pool:
        print(f"Ranked job pool: {summary.job_pool}")


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
            discovery_config = load_discovery_config(args.discovery_config)
            runner = DiscoveryRunner(
                profile=profile,
                app_config=config,
                resume_router=router,
                discovery_config=discovery_config,
                output_dir=args.output_dir,
            )
            summary = runner.run(
                sources=args.source,
                limit=args.limit,
                discovery_only=args.discovery_only,
                reanalyze_unchanged=args.reanalyze_unchanged,
            )
            _print_discovery_summary(summary, args.as_json)
            return 0

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
        DiscoveryConfigError,
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
