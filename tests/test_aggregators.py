from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from jobhuntbot.aggregator_discovery import AggregatorDiscoveryCoordinator
from jobhuntbot.aggregators import (
    AggregatorAdapter,
    AggregatorBatch,
    AggregatorConfig,
    AggregatorFailure,
    AggregatorTarget,
    ExternalJobCandidate,
    load_aggregator_config,
    normalize_candidate,
    parse_public_candidates,
)
from jobhuntbot.cli import main
from jobhuntbot.config import load_config
from jobhuntbot.deduplication import deduplicate_jobs
from jobhuntbot.discovery import (
    DiscoveryConfig,
    DiscoveryRunner,
    DiscoverySeed,
    IncrementalPolicy,
)
from jobhuntbot.profile import load_candidate_profile
from jobhuntbot.relevance import CareerTrack, LocationPolicy, RelevancePolicy
from jobhuntbot.resume_router import ResumeRouter, load_resume_routing
from jobhuntbot.source_resolution import (
    AuthoritativeHandoff,
    HandoffResult,
    append_candidate_provenance,
    canonicalize_candidate_url,
    resolve_candidate,
    resolve_supported_ats_url,
)
from jobhuntbot.sources import DiscoveryTarget, SourceBatch, SourceFailure, SourceJob


FIXTURES = Path(__file__).parent / "fixtures"
AGGREGATOR_FIXTURES = FIXTURES / "aggregators"


class NoNetworkTextClient:
    def get_text(self, url, *, headers=None):
        raise AssertionError("Network access is forbidden in offline tests.")


class NoNetworkJsonClient:
    def get_json(self, url, *, headers=None):
        raise AssertionError("Network access is forbidden in offline tests.")

    def post_json(self, url, payload, *, headers=None):
        raise AssertionError("Network access is forbidden in offline tests.")


class FixedSourceAdapter:
    def __init__(self, jobs, errors=None):
        self.jobs = jobs
        self.errors = errors or []

    def fetch(self, target, *, limit=None):
        jobs = self.jobs[:limit] if limit is not None else list(self.jobs)
        return SourceBatch(jobs=jobs, errors=list(self.errors), pages_fetched=1)


class FixedHandoff:
    def __init__(self, job: SourceJob | None, errors=None):
        self.job = job
        self.errors = errors or []
        self.calls = 0

    def fetch(self, candidate, resolution):
        self.calls += 1
        return HandoffResult(
            job=self.job,
            errors=list(self.errors),
            pages_fetched=1,
            reason=(
                "Authoritative full job description retrieved through existing ATS adapter."
                if self.job is not None
                else "Authoritative endpoint unavailable."
            ),
        )


class AggregatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        self.app_config = load_config()
        routing = load_resume_routing(FIXTURES / "resume_routing_valid.json")
        self.router = ResumeRouter(
            routing, self.app_config.normalization.get("skill_aliases", {})
        )
        self.description = (FIXTURES / "job_high_fit.txt").read_text(encoding="utf-8")

    def _fixture_value(self, name: str) -> dict:
        return json.loads((AGGREGATOR_FIXTURES / name).read_text(encoding="utf-8"))[0]

    def _authoritative_job(
        self,
        *,
        source="greenhouse",
        job_id="12345",
        location="New York, NY",
        posted_date="2026-08-09",
        metadata=None,
    ) -> SourceJob:
        return SourceJob(
            source=source,
            source_job_id=job_id,
            company="Example Analytics",
            title="GIS Data Analyst",
            description=self.description,
            location=location,
            posted_date=posted_date,
            source_url=f"https://boards.greenhouse.io/example/jobs/{job_id}",
            apply_url=f"https://boards.greenhouse.io/example/jobs/{job_id}",
            metadata=dict(metadata or {}),
        )

    def _relevant_config(self, targets=None, *, incremental=False, location=False):
        return DiscoveryConfig(
            targets=list(targets or []),
            career_tracks={
                "data_bi": CareerTrack(
                    name="data_bi",
                    title_terms=["data analyst"],
                    description_terms=["sql", "data visualization"],
                    minimum_description_matches=1,
                )
            },
            relevance_policy=RelevancePolicy(enabled=True),
            location_policy=LocationPolicy(
                enabled=location,
                primary_markets=["New York, NY"],
                allow_other_locations=True,
                allowed_countries=["US"],
            ),
            incremental=IncrementalPolicy(enabled=incremental),
        )

    def test_linkedin_style_candidate_normalization(self) -> None:
        candidate = normalize_candidate(
            "linkedin", self._fixture_value("linkedin_candidates.json"), ["data_bi"]
        )
        self.assertEqual(candidate.aggregator_job_id, "urn:li:jobPosting:10001")
        self.assertEqual(candidate.company, "Example Analytics")
        self.assertEqual(candidate.location, "New York, NY")
        self.assertEqual(candidate.job_families, ["data_bi"])
        self.assertIn("greenhouse", candidate.external_url)

    def test_indeed_style_candidate_normalization(self) -> None:
        candidate = normalize_candidate(
            "indeed", self._fixture_value("indeed_candidates.json"), ["urban_transport_gis"]
        )
        self.assertEqual(candidate.aggregator_job_id, "indeed-20002")
        self.assertEqual(candidate.title, "GIS Analyst")
        self.assertEqual(candidate.posted_date, "2026-08-09")
        self.assertIn("lever", candidate.external_url)

    def test_public_linkedin_card_parsing_keeps_only_rendered_candidate_facts(self) -> None:
        html = """
        <div class="base-search-card" data-entity-urn="urn:li:jobPosting:42">
          <a class="base-card__full-link" href="/jobs/view/42?trk=test"></a>
          <h3 class="base-search-card__title">Data Analyst</h3>
          <h4 class="base-search-card__subtitle"><a>Example</a></h4>
          <span class="job-search-card__location">New York, NY</span>
          <time datetime="2026-08-10"></time>
        </div>
        """
        values = parse_public_candidates(
            "linkedin", html, "https://www.linkedin.com/jobs/search", ["data_bi"]
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].aggregator_job_id, "42")
        self.assertEqual(values[0].title, "Data Analyst")
        self.assertIsNone(values[0].external_url)

    def test_tracking_redirect_is_unwrapped_and_cleaned(self) -> None:
        wrapped = (
            "https://www.linkedin.com/redir/redirect?url="
            "https%3A%2F%2Fboards.greenhouse.io%2Fexample%2Fjobs%2F12345%3Futm_source%3Dlinkedin"
            "&trk=public_jobs"
        )
        self.assertEqual(
            canonicalize_candidate_url(wrapped),
            "https://boards.greenhouse.io/example/jobs/12345",
        )

    def test_supported_ats_url_resolution_patterns(self) -> None:
        cases = {
            "https://jobs.smartrecruiters.com/Example/744000123456789-data-analyst": (
                "smartrecruiters", "Example", "744000123456789"
            ),
            "https://job-boards.greenhouse.io/example/jobs/12345?utm_source=x": (
                "greenhouse", "example", "12345"
            ),
            "https://jobs.lever.co/example/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": (
                "lever", "example", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            ),
            "https://jobs.ashbyhq.com/example/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": (
                "ashby", "example", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            ),
            "https://example.wd5.myworkdayjobs.com/en-US/External/job/New-York/Data-Analyst_R123": (
                "workday", "example", "R123"
            ),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                result = resolve_supported_ats_url(url, company="Example", title="Data Analyst")
                self.assertIsNotNone(result)
                self.assertEqual((result.source, result.target.board, result.native_job_id), expected)

    def test_supported_host_without_required_identifiers_is_not_misclassified(self) -> None:
        self.assertIsNone(resolve_supported_ats_url("https://boards.greenhouse.io/example"))
        self.assertIsNone(resolve_supported_ats_url("https://example.wd5.myworkdayjobs.com/en-US/External"))

    def test_unresolved_company_page_and_insufficient_content_are_explainable(self) -> None:
        company = resolve_candidate(
            ExternalJobCandidate("indeed", external_url="https://careers.example.com/jobs/1")
        )
        excerpt = resolve_candidate(
            ExternalJobCandidate("linkedin", excerpt="Short aggregator excerpt")
        )
        self.assertEqual(company.status, "resolved_company_page")
        self.assertEqual(excerpt.status, "insufficient_content")

    def test_aggregator_to_authoritative_handoff_preserves_provenance(self) -> None:
        candidate = normalize_candidate(
            "linkedin", self._fixture_value("linkedin_candidates.json"), ["data_bi"]
        )
        resolution = resolve_candidate(candidate)
        job = self._authoritative_job()
        handoff = AuthoritativeHandoff(
            NoNetworkJsonClient(),
            adapter_factory=lambda source, client: FixedSourceAdapter([job]),
        ).fetch(candidate, resolution)
        self.assertIsNotNone(handoff.job)
        self.assertEqual(handoff.job.metadata["authoritative_source"], "greenhouse")
        self.assertEqual(handoff.job.metadata["discovered_via"], ["linkedin"])
        self.assertEqual(
            handoff.job.metadata["_discovery_provenance"][0]["aggregator_job_id"],
            "urn:li:jobPosting:10001",
        )

    def test_cross_aggregator_provenance_merges_without_second_effective_job(self) -> None:
        job = self._authoritative_job(
            metadata={
                "authoritative_source": "greenhouse",
                "discovered_via": ["linkedin"],
                "_discovery_provenance": [{"discovered_via": "linkedin", "aggregator_job_id": "1"}],
            }
        )
        candidate = ExternalJobCandidate(
            "indeed",
            aggregator_job_id="2",
            external_url="https://boards.greenhouse.io/example/jobs/12345",
        )
        append_candidate_provenance(job, candidate, resolve_candidate(candidate))
        duplicate = self._authoritative_job(
            metadata={"discovered_via": ["indeed"], "_discovery_provenance": job.metadata["_discovery_provenance"]}
        )
        result = deduplicate_jobs([job, duplicate])
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].metadata["discovered_via"], ["linkedin", "indeed"])
        self.assertEqual(len(result.jobs[0].metadata["_discovery_provenance"]), 2)

    def test_coordinator_deduplicates_two_aggregators_before_second_handoff(self) -> None:
        candidate_one = normalize_candidate(
            "linkedin", self._fixture_value("linkedin_candidates.json"), ["data_bi"]
        )
        candidate_two = ExternalJobCandidate(
            "indeed",
            aggregator_job_id="same-ats",
            title="Data Analyst",
            company="Example Analytics",
            location="New York, NY",
            external_url="https://boards.greenhouse.io/example/jobs/12345",
            job_families=["data_bi"],
        )

        class TwoSourceAdapter:
            def fetch(self, target, *, limit=None):
                value = candidate_one if target.source == "linkedin" else candidate_two
                return AggregatorBatch(target.source, candidates=[value])

        handoff = FixedHandoff(self._authoritative_job())
        config = AggregatorConfig(
            targets=[
                AggregatorTarget("linkedin", input_file=Path("unused")),
                AggregatorTarget("indeed", input_file=Path("unused")),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = AggregatorDiscoveryCoordinator(
                config=config,
                output_dir=temp_dir,
                text_client=NoNetworkTextClient(),
                json_client=NoNetworkJsonClient(),
                aggregator_adapter=TwoSourceAdapter(),
                handoff=handoff,
            ).prepare()
        self.assertEqual(result.resolved, 2)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(len(result.seeds), 1)
        self.assertEqual(handoff.calls, 1)
        self.assertEqual(result.seeds[0].job.metadata["discovered_via"], ["linkedin", "indeed"])

    def test_source_failure_isolated_from_other_aggregator(self) -> None:
        candidate = ExternalJobCandidate("indeed", aggregator_job_id="2", excerpt="Short")

        class IsolatedAdapter:
            def fetch(self, target, *, limit=None):
                if target.source == "linkedin":
                    failure = AggregatorFailure(
                        "linkedin", "blocked_or_unavailable", error_type="PublicAccessBlocked", reason="HTTP 429"
                    )
                    return AggregatorBatch(
                        "linkedin", status="blocked_or_unavailable", errors=[failure], requests_made=1
                    )
                return AggregatorBatch("indeed", candidates=[candidate])

        config = AggregatorConfig(
            targets=[
                AggregatorTarget("linkedin", search_url="https://linkedin.example"),
                AggregatorTarget("indeed", search_url="https://indeed.example"),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = AggregatorDiscoveryCoordinator(
                config=config,
                output_dir=temp_dir,
                text_client=NoNetworkTextClient(),
                json_client=NoNetworkJsonClient(),
                aggregator_adapter=IsolatedAdapter(),
            ).prepare()
        self.assertEqual(result.source_status["linkedin"]["status"], "blocked_or_unavailable")
        self.assertEqual(result.source_status["indeed"]["status"], "success")
        self.assertEqual(result.candidates, 1)

    def test_private_output_contains_audit_resolution_and_provenance(self) -> None:
        candidate = normalize_candidate(
            "linkedin", self._fixture_value("linkedin_candidates.json"), ["data_bi"]
        )

        class OneAdapter:
            def fetch(self, target, *, limit=None):
                return AggregatorBatch("linkedin", candidates=[candidate])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "my-materials" / "discovery" / "phase2.3"
            result = AggregatorDiscoveryCoordinator(
                config=AggregatorConfig(
                    targets=[AggregatorTarget("linkedin", input_file=Path("unused"))]
                ),
                output_dir=root,
                text_client=NoNetworkTextClient(),
                json_client=NoNetworkJsonClient(),
                aggregator_adapter=OneAdapter(),
                handoff=FixedHandoff(self._authoritative_job()),
            ).prepare()
            self.assertEqual(result.resolved, 1)
            for name in (
                "raw_candidates", "resolved", "unresolved", "recommended", "irrelevant",
                "errors", "state", "provenance",
            ):
                self.assertTrue((root / name).is_dir(), name)
            provenance = json.loads((root / "provenance" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(provenance[0]["discovered_via"], "linkedin")

    def test_international_authoritative_job_uses_existing_us_location_policy(self) -> None:
        job = self._authoritative_job(location="Toronto, Canada")
        target = DiscoveryTarget(source="greenhouse", board="example", job_families=["data_bi"])
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=self._relevant_config(location=True),
                output_dir=temp_dir,
            ).run(seed_jobs=[DiscoverySeed(job, target)], discovery_only=True)
            queue = json.loads(
                (Path(temp_dir) / "irrelevant" / "current_queue.json").read_text(encoding="utf-8")
            )
        self.assertEqual(summary.jobs_relevant, 0)
        self.assertEqual(queue[0]["relevance"]["location_tier"], "international")

    def test_seed_freshness_uses_existing_target_filter(self) -> None:
        job = self._authoritative_job(posted_date="2000-01-01")
        target = DiscoveryTarget(
            source="greenhouse", board="example", job_families=["data_bi"], posted_within_days=30
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=self._relevant_config(),
                output_dir=temp_dir,
            ).run(seed_jobs=[DiscoverySeed(job, target)], discovery_only=True)
        self.assertEqual(summary.jobs_after_filters, 0)
        self.assertEqual(summary.jobs_normalized, 0)

    def test_provenance_only_change_does_not_trigger_incremental_reanalysis(self) -> None:
        target = DiscoveryTarget(source="greenhouse", board="example", job_families=["data_bi"])
        config = self._relevant_config(incremental=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            first_job = self._authoritative_job(metadata={"discovered_via": ["linkedin"]})
            first = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=config,
                output_dir=temp_dir,
            ).run(seed_jobs=[DiscoverySeed(first_job, target)])
            second_job = self._authoritative_job(metadata={"discovered_via": ["linkedin", "indeed"]})
            second = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=config,
                output_dir=temp_dir,
            ).run(seed_jobs=[DiscoverySeed(second_job, target)])
        self.assertEqual(first.jobs_analyzed, 1)
        self.assertEqual(second.jobs_analyzed, 0)
        self.assertEqual(second.jobs_skipped_unchanged, 1)

    def test_registry_and_aggregator_same_native_job_are_one_effective_job(self) -> None:
        registry_target = DiscoveryTarget(
            source="greenhouse", board="example", job_families=["data_bi"]
        )
        registry_job = self._authoritative_job()
        aggregator_job = self._authoritative_job(
            metadata={
                "discovered_via": ["linkedin"],
                "_discovery_provenance": [{"discovered_via": "linkedin", "aggregator_job_id": "1"}],
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=self._relevant_config([registry_target]),
                output_dir=temp_dir,
                adapter_factory=lambda source, client: FixedSourceAdapter([registry_job]),
            ).run(seed_jobs=[DiscoverySeed(aggregator_job, registry_target)], discovery_only=True)
        self.assertEqual(summary.jobs_after_deduplication, 1)
        self.assertEqual(summary.duplicates_removed, 1)

    def test_cli_keeps_old_mode_and_accepts_aggregator_source_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            discovery_config = root / "discovery.json"
            discovery_config.write_text(
                json.dumps({"schema_version": 1, "targets": []}), encoding="utf-8"
            )
            candidates = root / "linkedin.json"
            candidates.write_text("[]", encoding="utf-8")
            aggregator_config = root / "aggregators.json"
            aggregator_config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "targets": [
                            {"source": "linkedin", "input_file": "linkedin.json", "max_results": 2}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = main(
                    [
                        "discover",
                        "--profile", str(FIXTURES / "profile_valid.json"),
                        "--resume-routing", str(FIXTURES / "resume_routing_valid.json"),
                        "--discovery-config", str(discovery_config),
                        "--aggregator-config", str(aggregator_config),
                        "--source", "linkedin",
                        "--output-dir", str(root / "my-materials" / "discovery" / "phase2.3"),
                        "--json",
                    ]
                )
            value = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, errors.getvalue())
        self.assertEqual(value["aggregator_source_status"]["linkedin"]["status"], "empty")
        self.assertEqual(value["jobs_analyzed"], 0)

    def test_config_keeps_search_terms_private_and_resolves_relative_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "input.json").write_text("[]", encoding="utf-8")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "targets": [
                            {
                                "source": "indeed",
                                "input_file": "input.json",
                                "job_families": ["applied_ai"],
                                "search_keywords": ["private term"],
                                "location_filter": ["private location"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = load_aggregator_config(path)
        self.assertTrue(config.targets[0].input_file.is_absolute())
        self.assertEqual(config.targets[0].search_keywords, ["private term"])

    def test_tracked_template_covers_both_sources_and_all_four_tracks(self) -> None:
        config = load_aggregator_config(
            Path(__file__).parents[1] / "templates" / "aggregator_discovery.template.json"
        )
        self.assertEqual({target.source for target in config.targets}, {"linkedin", "indeed"})
        self.assertEqual(
            {family for target in config.targets for family in target.job_families},
            {"higher_education", "data_bi", "urban_transport_gis", "applied_ai"},
        )


if __name__ == "__main__":
    unittest.main()
