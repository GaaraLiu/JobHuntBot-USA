"""Command-line interface for local matching and public-ATS discovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .aggregator_discovery import AggregatorDiscoveryCoordinator
from .answer_bank import AnswerBankError, AnswerResolver, load_answer_bank, validate_answer_bank
from .application_profile import (
    ApplicationProfileError,
    load_application_profile,
    validate_application_profile,
)
from .application_queue import (
    ApplicationPackageBuilder,
    ApplicationQueueError,
    ApplicationQueueStore,
    load_pipeline_candidates,
    load_questions,
)
from .application_forms import (
    ApplicationFormMapper,
    FormContext,
    SUPPORTED_APPLICATION_ATS,
    create_form_adapter,
    detect_application_ats,
)
from .application_forms.mapping import (
    load_application_form,
    save_private_json,
)
from .browser_forms import (
    BrowserDependencyError,
    BrowserFormExtractionService,
    BrowserSessionPolicy,
    PlaywrightReadOnlyBrowser,
)
from .controlled_fill import (
    ControlledFillPlanner,
    ControlledFillPolicy,
    ControlledFillService,
    FillDependencyError,
    PlaywrightControlledFillBrowser,
    load_mapping_plan,
    load_private_object,
    sanitized_fill_log,
)
from .reviewed_submission import (
    ApprovalService,
    PlaywrightReviewedSubmissionBrowser,
    ReviewPackageBuilder,
    SubmissionContext,
    SubmissionDependencyError,
    SubmissionExecutionPolicy,
    SubmissionExecutionService,
    SubmissionHistoryStore,
    SubmissionQueueStatusManager,
    load_review_package,
    load_submission_approval,
)
from .browser_forms import BrowserRenderedForm
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

    queue_import = subparsers.add_parser(
        "application-queue-import",
        help="Import eligible Phase 1/2 results into the private application queue.",
    )
    queue_import.add_argument("--input", required=True, type=Path)
    queue_import.add_argument(
        "--queue",
        type=Path,
        default=Path("my-materials/application/application_queue.json"),
    )
    queue_import.add_argument(
        "--promote-review-job-id",
        action="append",
        default=[],
        help="Explicitly promote one REVIEW job for preparation; original decision is preserved.",
    )
    queue_import.add_argument("--json", action="store_true", dest="as_json")

    queue_list = subparsers.add_parser(
        "application-queue-list", help="List private application queue records."
    )
    queue_list.add_argument(
        "--queue",
        type=Path,
        default=Path("my-materials/application/application_queue.json"),
    )
    queue_list.add_argument("--status", choices=(
        "ALL", "READY_TO_PREPARE", "PREPARING", "WAITING_FOR_USER", "NEEDS_REVIEW",
        "PACKAGE_READY", "READY_FOR_APPLICATION", "IN_PROGRESS", "FAILED",
        "WITHDRAWN", "SKIPPED", "READY_FOR_USER_REVIEW", "SUBMITTED"
    ), default="ALL")
    queue_list.add_argument("--json", action="store_true", dest="as_json")

    package_build = subparsers.add_parser(
        "application-package-build",
        help="Build a private, reviewable application package without form interaction.",
    )
    package_build.add_argument("--application-id", required=True)
    package_build.add_argument(
        "--application-root", type=Path, default=Path("my-materials/application")
    )
    package_build.add_argument(
        "--queue", type=Path, default=Path("my-materials/application/application_queue.json")
    )
    package_build.add_argument(
        "--application-profile",
        type=Path,
        default=Path("my-materials/application/candidate_application_profile.json"),
    )
    package_build.add_argument(
        "--answer-bank", type=Path, default=Path("my-materials/application/answer_bank.json")
    )
    package_build.add_argument("--questions", required=True, type=Path)
    package_build.add_argument(
        "--cover-letter-status",
        choices=("not_required", "optional", "required", "unknown", "needs_generation"),
        default="unknown",
    )
    package_build.add_argument("--json", action="store_true", dest="as_json")

    validate_bank = subparsers.add_parser(
        "validate-answer-bank", help="Validate a private answer bank and application profile."
    )
    validate_bank.add_argument("--answer-bank", required=True, type=Path)
    validate_bank.add_argument("--application-profile", required=True, type=Path)
    validate_bank.add_argument("--json", action="store_true", dest="as_json")

    unresolved = subparsers.add_parser(
        "answer-bank-unresolved",
        help="Resolve structured questions and report only unresolved items.",
    )
    unresolved.add_argument("--answer-bank", required=True, type=Path)
    unresolved.add_argument("--application-profile", required=True, type=Path)
    unresolved.add_argument("--questions", required=True, type=Path)
    unresolved.add_argument("--job", type=Path)
    unresolved.add_argument("--json", action="store_true", dest="as_json")

    inspect_form = subparsers.add_parser(
        "application-form-inspect",
        help="Read saved/public form structure and emit a canonical form without filling it.",
    )
    inspect_form.add_argument("--input", required=True, type=Path)
    inspect_form.add_argument("--application-url", default="")
    inspect_form.add_argument(
        "--ats", choices=("auto", "unknown", *SUPPORTED_APPLICATION_ATS), default="auto"
    )
    inspect_form.add_argument("--known-source", default="")
    inspect_form.add_argument("--source", default="saved_file")
    inspect_form.add_argument("--job-id", default="")
    inspect_form.add_argument("--company", default="")
    inspect_form.add_argument("--title", default="")
    inspect_form.add_argument("--form-version", default="")
    inspect_form.add_argument("--output", type=Path)
    inspect_form.add_argument("--json", action="store_true", dest="as_json")

    map_form = subparsers.add_parser(
        "application-form-map",
        help="Build a private read-only mapping plan for a canonical application form.",
    )
    map_form.add_argument("--form", required=True, type=Path)
    map_form.add_argument("--application-profile", required=True, type=Path)
    map_form.add_argument("--answer-bank", required=True, type=Path)
    map_form.add_argument("--application-package", type=Path)
    map_form.add_argument("--job-result", type=Path)
    map_form.add_argument("--application-id", default="")
    map_form.add_argument("--output", type=Path)
    map_form.add_argument("--json", action="store_true", dest="as_json")

    browser_form = subparsers.add_parser(
        "inspect-application-form",
        help="READ ONLY: render a public application page and extract structure; never fill or submit.",
    )
    browser_form.add_argument("--url", required=True)
    browser_form.add_argument(
        "--ats", choices=("auto", "unknown", *SUPPORTED_APPLICATION_ATS), default="auto"
    )
    browser_form.add_argument("--job-id", default="")
    browser_form.add_argument("--company", default="")
    browser_form.add_argument("--title", default="")
    browser_form.add_argument("--application-id", default="")
    browser_form.add_argument("--application-profile", type=Path)
    browser_form.add_argument("--answer-bank", type=Path)
    browser_form.add_argument("--application-package", type=Path)
    browser_form.add_argument(
        "--output-dir",
        type=Path,
        default=Path("my-materials/application/browser_forms"),
    )
    browser_form.add_argument("--timeout", type=int, default=20_000, help="Timeout in milliseconds.")
    browser_form.add_argument("--headless", action="store_true", default=True)
    browser_form.add_argument("--headed", action="store_false", dest="headless")
    browser_form.add_argument("--no-follow-apply-link", action="store_false", dest="follow_apply")
    browser_form.add_argument("--manual-auth", action="store_true", help="Pause for user-controlled authentication when required, then continue in the same browser session.")
    browser_form.set_defaults(follow_apply=True)
    browser_form.add_argument("--json", action="store_true", dest="as_json")

    autofill = subparsers.add_parser(
        "autofill-application",
        help="Preview or explicitly execute controlled field population; submission is impossible.",
    )
    autofill.add_argument("--application-package", required=True, type=Path)
    autofill.add_argument("--application-id", default="")
    autofill.add_argument("--mapping-plan", required=True, type=Path)
    autofill.add_argument("--application-form", required=True, type=Path)
    autofill.add_argument("--browser-snapshot", required=True, type=Path)
    autofill.add_argument("--url", default="")
    autofill.add_argument(
        "--output-root", type=Path, default=Path("my-materials/application")
    )
    autofill.add_argument(
        "--execute-fill",
        action="store_true",
        help="Explicitly permit safe field population; never permits submission.",
    )
    autofill.add_argument(
        "--preview",
        action="store_true",
        help="Explicit preview alias; preview is already the default.",
    )
    autofill.add_argument(
        "--allow-partial-fill",
        action="store_true",
        help="Permit safe fields even when other required fields need user input.",
    )
    autofill.add_argument(
        "--allow-file-upload",
        action="store_true",
        help="Preview file-upload intent only; live uploads remain disabled in Phase 3.3.",
    )
    autofill.add_argument("--timeout", type=int, default=20_000)
    autofill.add_argument("--headless", action="store_true", default=True)
    autofill.add_argument("--headed", action="store_false", dest="headless")
    autofill.add_argument("--json", action="store_true", dest="as_json")

    review_application = subparsers.add_parser(
        "review-application",
        help="Generate a private human review; never fills, uploads, or submits.",
    )
    _add_submission_artifact_arguments(review_application)
    review_application.add_argument(
        "--queue", type=Path,
        default=Path("my-materials/application/application_queue.json"),
    )
    review_application.add_argument("--json", action="store_true", dest="as_json")

    approve_application = subparsers.add_parser(
        "approve-application",
        help="Bind explicit approval to one reviewed application state.",
    )
    approve_application.add_argument("--application-id", required=True)
    approve_application.add_argument("--review-package", required=True, type=Path)
    approve_application.add_argument("--application-package", required=True, type=Path)
    approve_application.add_argument("--application-form", required=True, type=Path)
    approval_scope = approve_application.add_mutually_exclusive_group(required=True)
    approval_scope.add_argument("--for-fill", action="store_true")
    approval_scope.add_argument("--for-submission", action="store_true")
    approve_application.add_argument("--confirm-reviewed", action="store_true")
    approve_application.add_argument("--manual-item-resolved", action="append", default=[])
    approve_application.add_argument(
        "--output-root", type=Path, default=Path("my-materials/application")
    )
    approve_application.add_argument("--json", action="store_true", dest="as_json")

    execute_application = subparsers.add_parser(
        "execute-application",
        help="Dry-run by default; --submit requires exact APPROVED_FOR_SUBMISSION approval.",
    )
    _add_submission_artifact_arguments(execute_application)
    execute_application.add_argument("--review-package", required=True, type=Path)
    execute_application.add_argument("--approval", required=True, type=Path)
    execute_application.add_argument(
        "--queue", type=Path,
        default=Path("my-materials/application/application_queue.json"),
    )
    execute_application.add_argument("--submit", action="store_true")
    execute_application.add_argument("--allow-duplicate-submission", action="store_true")
    execute_application.add_argument("--timeout", type=int, default=30_000)
    execute_application.add_argument("--headless", action="store_true", default=True)
    execute_application.add_argument("--headed", action="store_false", dest="headless")
    execute_application.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _add_submission_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--application-package", required=True, type=Path)
    parser.add_argument("--mapping-plan", required=True, type=Path)
    parser.add_argument("--application-form", required=True, type=Path)
    parser.add_argument("--browser-snapshot", required=True, type=Path)
    parser.add_argument(
        "--output-root", type=Path, default=Path("my-materials/application")
    )


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
    experience_risk = score.experience_requirement_risk
    print("\nExplicit experience-requirement risk:")
    print(f"  Triggered: {'yes' if experience_risk.triggered else 'no'}")
    print(f"  Reason: {experience_risk.reason}")
    if experience_risk.evidence:
        print(f"  Evidence: {'; '.join(experience_risk.evidence)}")
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


def _phase34_artifacts(args):
    package = load_private_object(args.application_package)
    if str(package.get("application_id", "")) != args.application_id:
        raise ValueError("--application-id does not match the ApplicationPackage.")
    mapping = load_mapping_plan(args.mapping_plan)
    form = load_application_form(args.application_form)
    rendered = BrowserRenderedForm.from_dict(load_private_object(args.browser_snapshot))
    plan = ControlledFillPlanner().build(package, mapping, form, rendered)
    return package, mapping, form, rendered, plan


def _optional_queue(queue_path: Path, application_id: str):
    if not queue_path.exists():
        return None, {}
    store = ApplicationQueueStore(queue_path)
    try:
        return store, store.find(application_id)
    except ApplicationQueueError:
        return None, {}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "review-application":
            print(
                "REVIEW ONLY - NO BROWSER FILL, FILE UPLOAD, OR SUBMISSION WILL OCCUR.",
                file=sys.stderr,
            )
            package, mapping, form, rendered, plan = _phase34_artifacts(args)
            queue_store, queue_record = _optional_queue(args.queue, args.application_id)
            review = ReviewPackageBuilder().build(
                package, form, mapping, plan, rendered, queue_record=queue_record
            )
            review_path = args.output_root / "reviews" / f"{args.application_id}_review.json"
            save_private_json(review_path, review.to_dict())
            SubmissionQueueStatusManager(queue_store).mark_review_ready(args.application_id)
            history = SubmissionHistoryStore(
                args.output_root / "submissions" / "submission_history.jsonl"
            )
            history.append(
                "review_generated",
                application_id=review.application_id,
                job_id=review.job_id,
                company=str(review.job.get("company", "")),
                title=str(review.job.get("title", "")),
                ats=mapping.ats,
                status=review.review_status,
            )
            value = {"review": review.to_dict(), "private_path": str(review_path)}
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Application: {review.application_id}")
                print(f"Review status: {review.review_status}")
                print(f"Required unresolved: {review.unresolved_required_count}")
                print(f"Manual items: {len(review.manual_items)}")
                print(f"Blockers: {len(review.blockers)}")
                print("Approval granted: no")
            return 0

        if args.command == "approve-application":
            print(
                "APPROVAL RECORDING ONLY - THIS COMMAND DOES NOT OPEN OR SUBMIT A FORM.",
                file=sys.stderr,
            )
            package = load_private_object(args.application_package)
            form = load_application_form(args.application_form)
            review = load_review_package(args.review_package)
            if args.application_id != review.application_id or args.application_id != package.get("application_id"):
                raise ValueError("Application ID does not match review/package state.")
            approval = ApprovalService().create(
                review,
                package,
                form,
                for_submission=args.for_submission,
                confirmed_reviewed=args.confirm_reviewed,
                resolved_manual_item_ids=set(args.manual_item_resolved),
            )
            approval_path = args.output_root / "approvals" / f"{args.application_id}_approval.json"
            save_private_json(approval_path, approval.to_dict())
            history = SubmissionHistoryStore(
                args.output_root / "submissions" / "submission_history.jsonl"
            )
            history.append(
                "approval_recorded",
                application_id=review.application_id,
                job_id=review.job_id,
                company=str(review.job.get("company", "")),
                title=str(review.job.get("title", "")),
                ats=str(review.job.get("ats", "unknown")),
                status=approval.approval_status,
            )
            value = {"approval": approval.to_dict(), "private_path": str(approval_path)}
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Application: {approval.application_id}")
                print(f"Approval: {approval.approval_status}")
                print(f"Form fingerprint: {approval.form_fingerprint}")
                print(f"Package fingerprint: {approval.package_fingerprint}")
                print("Submission performed: no")
            return 0

        if args.command == "execute-application":
            mode = "EXPLICIT SUBMISSION" if args.submit else "DRY RUN"
            print(
                f"{mode} - SUBMISSION REQUIRES EXACT APPROVED_FOR_SUBMISSION STATE.",
                file=sys.stderr,
            )
            package, mapping, form, rendered, plan = _phase34_artifacts(args)
            stored_review = load_review_package(args.review_package)
            approval = load_submission_approval(args.approval)
            if stored_review.application_id != args.application_id or approval.application_id != args.application_id:
                raise ValueError("Application ID does not match review/approval state.")
            if approval.review_generated_at != stored_review.generated_at:
                raise ValueError("Approval is not tied to the supplied ReviewPackage generation.")
            queue_store, queue_record = _optional_queue(args.queue, args.application_id)
            current_review = ReviewPackageBuilder().build(
                package, form, mapping, plan, rendered, queue_record=queue_record
            )
            current_review.generated_at = stored_review.generated_at
            checked_approval = ApprovalService().validate(
                approval, current_review, package, form
            )
            if checked_approval.approval_status == "EXPIRED":
                save_private_json(args.approval, checked_approval.to_dict())
            history = SubmissionHistoryStore(
                args.output_root / "submissions" / "submission_history.jsonl"
            )
            backend = PlaywrightReviewedSubmissionBrowser() if args.submit else None
            context = SubmissionContext(
                package=dict(package),
                form=form,
                mapping=mapping,
                fill_plan=plan,
                rendered=rendered,
                review=current_review,
            )
            result = SubmissionExecutionService(
                backend,
                history=history,
                queue=SubmissionQueueStatusManager(queue_store),
            ).run(
                context,
                checked_approval,
                SubmissionExecutionPolicy(
                    submit=args.submit,
                    allow_duplicate_submission=args.allow_duplicate_submission,
                    timeout_ms=args.timeout,
                    headless=args.headless,
                ),
            )
            result_path = args.output_root / "submissions" / f"{args.application_id}_submission_result.json"
            save_private_json(result_path, result.to_dict())
            value = {"result": result.to_dict(), "private_path": str(result_path)}
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Application: {result.application_id}")
                print(f"Status: {result.status}")
                print(f"Submission attempted: {'yes' if result.submission_attempted else 'no'}")
                print(f"Submission completed: {'yes' if result.submission_completed else 'no'}")
                print(f"Confirmation detected: {'yes' if result.confirmation_detected else 'no'}")
            return 0

        if args.command == "autofill-application":
            if args.preview and args.execute_fill:
                raise ValueError("--preview and --execute-fill are mutually exclusive.")
            mode = "CONTROLLED FILL" if args.execute_fill else "PREVIEW ONLY"
            print(
                f"{mode} - FORM SUBMISSION, LEGAL CONSENT, AND LIVE FILE UPLOAD ARE DISABLED.",
                file=sys.stderr,
            )
            package = load_private_object(args.application_package)
            package_id = str(package.get("application_id", ""))
            if args.application_id and args.application_id != package_id:
                raise ValueError("--application-id does not match the ApplicationPackage.")
            mapping = load_mapping_plan(args.mapping_plan)
            form = load_application_form(args.application_form)
            rendered = BrowserRenderedForm.from_dict(load_private_object(args.browser_snapshot))
            policy = ControlledFillPolicy(
                execute_fill=args.execute_fill,
                allow_partial_fill=args.allow_partial_fill,
                allow_file_upload=args.allow_file_upload,
                timeout_ms=args.timeout,
                headless=args.headless,
            )
            plan = ControlledFillPlanner().build(
                package, mapping, form, rendered, policy=policy
            )
            application_id = plan.application_id or "unidentified_application"
            plan_path = args.output_root / "fill_plans" / f"{application_id}_fill_plan.json"
            result_path = args.output_root / "fill_results" / f"{application_id}_fill_result.json"
            log_path = args.output_root / "fill_logs" / f"{application_id}_fill_log.json"
            save_private_json(plan_path, plan.to_dict())
            backend = PlaywrightControlledFillBrowser() if args.execute_fill else None
            service = ControlledFillService(backend)
            target = args.url or rendered.final_url or form.application_url
            result = service.run(target, plan, form, rendered, policy)
            save_private_json(result_path, result.to_dict())
            save_private_json(log_path, sanitized_fill_log(result))
            value = {
                "mode": mode,
                "plan": plan.to_dict(),
                "result": result.to_dict(),
                "private_paths": {
                    "fill_plan": str(plan_path),
                    "fill_result": str(result_path),
                    "fill_log": str(log_path),
                },
                "submission_possible": False,
            }
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Application: {application_id}")
                print(f"Plan status: {plan.overall_status}")
                print(f"Safe fields planned: {plan.fill_fields}")
                print(f"Required not fillable: {plan.required_not_fillable}")
                print(f"Result: {result.overall_status}")
                print("Submission attempted: no")
                print("Submission completed: no")
            return 0

        if args.command == "inspect-application-form":
            print(
                "READ ONLY - NO FORM DATA WILL BE ENTERED, UPLOADED, OR SUBMITTED.",
                file=sys.stderr,
            )
            if bool(args.application_profile) != bool(args.answer_bank):
                raise ValueError("Provide both --application-profile and --answer-bank, or neither.")
            job: dict = {}
            selected_resume: dict = {}
            application_id = args.application_id
            if args.application_package:
                package = json.loads(args.application_package.read_text(encoding="utf-8"))
                if not isinstance(package, dict):
                    raise ValueError("Application package root must be an object.")
                job = dict(package.get("job_snapshot", {}))
                selected_resume = dict(package.get("selected_resume", {}))
                application_id = application_id or str(package.get("application_id", ""))
            profile = load_application_profile(args.application_profile) if args.application_profile else None
            bank = load_answer_bank(args.answer_bank) if args.answer_bank else None
            known_source = "" if args.ats == "auto" else args.ats
            policy = BrowserSessionPolicy(
                headless=args.headless,
                timeout_ms=args.timeout,
                follow_public_apply_link=args.follow_apply,
            allow_manual_auth_handoff=args.manual_auth,
            )
            outcome = BrowserFormExtractionService(PlaywrightReadOnlyBrowser()).inspect(
                args.url,
                policy=policy,
                known_source=known_source,
                job_id=args.job_id,
                company=args.company,
                title=args.title,
                profile=profile,
                answer_bank=bank,
                application_id=application_id,
                job=job,
                selected_resume=selected_resume,
            )
            stem = args.job_id or outcome.application_form.fingerprint[:16]
            form_path = args.output_dir / f"{stem}_application_form.json"
            root = args.output_dir.parent
            snapshot_path = root / "browser_snapshots" / f"{stem}_browser_snapshot.json"
            log_path = root / "browser_logs" / f"{stem}_network_metadata.json"
            save_private_json(form_path, outcome.application_form.to_dict())
            save_private_json(snapshot_path, outcome.rendered.to_dict())
            save_private_json(
                log_path,
                {
                    "mode": "READ_ONLY",
                    "candidate_data_typed": False,
                    "files_uploaded": 0,
                    "forms_submitted": 0,
                    "blocked_request_count": outcome.rendered.blocked_request_count,
                    "public_network_metadata": outcome.rendered.public_network_metadata,
                },
            )
            mapping_path = None
            if outcome.mapping_plan:
                mapping_path = root / "mappings" / f"{stem}_browser_mapping.json"
                save_private_json(mapping_path, outcome.mapping_plan.to_dict())
            value = outcome.to_dict()
            value["private_paths"] = {
                "application_form": str(form_path),
                "browser_snapshot": str(snapshot_path),
                "browser_log": str(log_path),
                "mapping_plan": str(mapping_path) if mapping_path else None,
            }
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Browser status: {outcome.rendered.status}")
                print(f"ATS: {outcome.application_form.ats}")
                print(f"Fields: {len(outcome.application_form.fields)}")
                print(f"Fingerprint: {outcome.application_form.fingerprint}")
                if outcome.mapping_plan:
                    print(f"Mapping readiness: {outcome.mapping_plan.package_readiness}")
            return 0

        if args.command == "application-form-inspect":
            ats = (
                detect_application_ats(args.application_url, args.known_source)
                if args.ats == "auto"
                else args.ats
            )
            context = FormContext(
                source=args.source,
                ats=ats,
                application_url=args.application_url,
                job_id=args.job_id,
                company=args.company,
                title=args.title,
                form_version=args.form_version,
                metadata={"read_only": True, "input_path": str(args.input)},
            )
            adapter = create_form_adapter(ats)
            if args.input.suffix.casefold() == ".json":
                payload = json.loads(args.input.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Application form metadata root must be an object.")
                form = adapter.parse_json(payload, context)
            else:
                form = adapter.parse_html(args.input.read_text(encoding="utf-8"), context)
            value = form.to_dict()
            if args.output:
                save_private_json(args.output, value)
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"ATS: {form.ats}")
                print(f"Detection: {form.detection_status}")
                print(f"Fields: {len(form.fields)}")
                print(f"Fingerprint: {form.fingerprint}")
            return 0

        if args.command == "application-form-map":
            form = load_application_form(args.form)
            profile = load_application_profile(args.application_profile)
            bank = load_answer_bank(args.answer_bank)
            job: dict = {}
            selected_resume: dict = {}
            application_id = args.application_id
            if args.application_package:
                package = json.loads(args.application_package.read_text(encoding="utf-8"))
                if not isinstance(package, dict):
                    raise ValueError("Application package root must be an object.")
                job = dict(package.get("job_snapshot", {}))
                selected_resume = dict(package.get("selected_resume", {}))
                application_id = application_id or str(package.get("application_id", ""))
            if args.job_result:
                result = json.loads(args.job_result.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Job result root must be an object.")
                job = dict(result.get("job", result))
                selected_resume = dict(result.get("resume", selected_resume))
            plan = ApplicationFormMapper(profile, bank).build_plan(
                form,
                application_id=application_id,
                job=job,
                selected_resume=selected_resume,
            )
            value = plan.to_dict()
            if args.output:
                save_private_json(args.output, value)
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Form mapping: {form.ats} / {form.fingerprint}")
                print(f"Fields: {plan.total_fields}; mapped: {plan.mapped_fields}")
                print(f"Required unresolved: {plan.required_unresolved}")
                print(f"Manual only: {plan.manual_only}")
                print(f"Future autofill eligible: {plan.potential_future_autofill}")
                print(f"Readiness: {plan.package_readiness}")
            return 0

        if args.command == "application-queue-import":
            candidates = load_pipeline_candidates(args.input)
            report = ApplicationQueueStore(args.queue).import_candidates(
                candidates,
                source_path=str(args.input),
                promote_review_job_ids=set(args.promote_review_job_id),
            )
            value = report.to_dict()
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(
                    "Application queue import: "
                    f"added={report.added}, duplicate={report.duplicate}, "
                    f"changed={report.changed}, REVIEW excluded={report.review_excluded}, "
                    f"SKIP excluded={report.skip_excluded}"
                )
            return 0

        if args.command == "application-queue-list":
            applications = ApplicationQueueStore(args.queue).load()["applications"]
            if args.status != "ALL":
                applications = [item for item in applications if item.get("application_status") == args.status]
            if args.as_json:
                print(json.dumps({"applications": applications}, ensure_ascii=True, indent=2))
            else:
                for item in applications:
                    print(
                        f"{item.get('application_id')} | {item.get('application_status')} | "
                        f"{item.get('company')} | {item.get('title')}"
                    )
            return 0

        if args.command == "validate-answer-bank":
            profile = load_application_profile(args.application_profile)
            bank = load_answer_bank(args.answer_bank)
            issues = [
                {"severity": severity, "path": path, "message": message}
                for severity, path, message in (
                    validate_application_profile(profile) + validate_answer_bank(bank)
                )
            ]
            value = {"valid": not any(item["severity"] == "error" for item in issues), "issues": issues}
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Valid: {'yes' if value['valid'] else 'no'}")
                for item in issues:
                    print(f"[{item['severity'].upper()}] {item['path']}: {item['message']}")
            return 0 if value["valid"] else 1

        if args.command == "answer-bank-unresolved":
            profile = load_application_profile(args.application_profile)
            bank = load_answer_bank(args.answer_bank)
            questions = load_questions(args.questions)
            job = {}
            if args.job:
                job_value = json.loads(args.job.read_text(encoding="utf-8"))
                job = job_value.get("job", job_value) if isinstance(job_value, dict) else {}
            result = AnswerResolver(profile, bank).resolve(questions, job)
            value = {
                "prepared_count": len(result.answers),
                "unresolved_count": len(result.unresolved),
                "unresolved": [item.to_dict() for item in result.unresolved],
            }
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Prepared: {len(result.answers)}; unresolved: {len(result.unresolved)}")
                for item in result.unresolved:
                    print(f"{item.question_id}: {item.reason}")
            return 0

        if args.command == "application-package-build":
            profile = load_application_profile(args.application_profile)
            bank = load_answer_bank(args.answer_bank)
            package = ApplicationPackageBuilder(
                queue_store=ApplicationQueueStore(args.queue),
                application_root=args.application_root,
                profile=profile,
                answer_bank=bank,
            ).build(
                args.application_id,
                load_questions(args.questions),
                cover_letter_status=args.cover_letter_status,
            )
            value = package.to_dict()
            if args.as_json:
                print(json.dumps(value, ensure_ascii=True, indent=2))
            else:
                print(f"Application package: {package.application_id}")
                print(f"Readiness: {package.package_readiness}")
                print(f"Unresolved: {len(package.unresolved_questions)}")
            return 0

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
        AnswerBankError,
        ApplicationProfileError,
        ApplicationQueueError,
        BrowserDependencyError,
        FillDependencyError,
        SubmissionDependencyError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
