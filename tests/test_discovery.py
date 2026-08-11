from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.discovery import (
    DiscoveryConfig,
    DiscoveryConfigError,
    IncrementalPolicy,
    DiscoveryRunner,
    SourceAwareParser,
    load_discovery_config,
)
from jobhuntbot.models import RawJob
from jobhuntbot.profile import load_candidate_profile
from jobhuntbot.resume_router import ResumeRouter, load_resume_routing
from jobhuntbot.relevance import CareerTrack, RelevancePolicy
from jobhuntbot.sources import DiscoveryTarget, SourceBatch, SourceJob, SourceSalary
from jobhuntbot.sources.workday import WorkdayAdapter


FIXTURES = Path(__file__).parent / "fixtures"


class FixedAdapter:
    def __init__(self, jobs: list[SourceJob]):
        self.jobs = jobs

    def fetch(self, target, *, limit=None) -> SourceBatch:
        selected = self.jobs[:limit] if limit is not None else self.jobs
        return SourceBatch(jobs=list(selected))


class FailingAdapter:
    def fetch(self, target, *, limit=None) -> SourceBatch:
        raise RuntimeError("controlled adapter failure")


class WorkdayIntegrationClient:
    def post_json(self, url, payload, *, headers=None):
        return {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Data Reporting Analyst",
                    "externalPath": "/job/New-York-NY/Data-Reporting-Analyst_EX1001",
                    "locationsText": "New York, NY",
                },
                {
                    "title": "Laboratory Technician",
                    "externalPath": "/job/New-York-NY/Laboratory-Technician_EX1002",
                    "locationsText": "New York, NY",
                },
            ],
        }

    def get_json(self, url, *, headers=None):
        if url.endswith("EX1001"):
            return {
                "jobPostingInfo": {
                    "title": "Data Reporting Analyst",
                    "jobReqId": "EX1001",
                    "jobDescription": (
                        "Build Power BI dashboards and write SQL queries. "
                        "Bachelor's degree required."
                    ),
                    "location": "New York, NY",
                    "countryCode": "US",
                    "timeType": "Full time",
                }
            }
        return {
            "jobPostingInfo": {
                "title": "Laboratory Technician",
                "jobReqId": "EX1002",
                "jobDescription": "Prepare chemical samples and maintain laboratory equipment.",
                "location": "New York, NY",
                "countryCode": "US",
                "timeType": "Full time",
            }
        }


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app_config = load_config()
        self.profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        routing = load_resume_routing(FIXTURES / "resume_routing_valid.json")
        self.router = ResumeRouter(
            routing, self.app_config.normalization.get("skill_aliases", {})
        )
        self.high_fit_description = (FIXTURES / "job_high_fit.txt").read_text(
            encoding="utf-8"
        )

    def _job(self, job_id: str = "job-1", *, description: str | None = None) -> SourceJob:
        return SourceJob(
            source="greenhouse",
            source_job_id=job_id,
            company="MapWorks",
            title="GIS Data Analyst",
            description=description if description is not None else self.high_fit_description,
            location="Boston, MA",
            work_mode="hybrid",
            employment_type="full-time",
            source_url=f"https://boards.greenhouse.io/mapworks/jobs/{job_id}",
            apply_url=f"https://boards.greenhouse.io/mapworks/jobs/{job_id}",
        )

    def test_load_config_supports_four_job_families_and_targets(self) -> None:
        value = {
            "schema_version": 1,
            "job_families": {
                "higher_education": {"keywords": ["university"]},
                "data_bi": {"keywords": ["Power BI"]},
                "urban_transport_gis": {"keywords": ["GIS"]},
                "applied_ai": {"keywords": ["machine learning"]},
            },
            "targets": [
                {
                    "source": "greenhouse",
                    "board": "example",
                    "company": "Example",
                    "enabled": True,
                    "job_families": ["data_bi"],
                    "employment_types": ["full-time"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "discovery.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            config = load_discovery_config(path)
        self.assertEqual(len(config.job_families), 4)
        self.assertEqual(config.targets[0].source, "greenhouse")
        self.assertEqual(config.targets[0].job_families, ["data_bi"])

    def test_phase_2_1_template_loads_targeted_incremental_settings(self) -> None:
        template = Path(__file__).parents[1] / "templates" / "discovery_config.template.json"
        config = load_discovery_config(template)
        self.assertEqual(set(config.career_tracks), {
            "higher_education",
            "data_bi",
            "urban_transport_gis",
            "applied_ai",
        })
        self.assertTrue(config.relevance_policy.enabled)
        self.assertTrue(config.incremental.enabled)
        self.assertEqual(config.incremental.state_file, "state/discovery_state.json")
        self.assertEqual(config.targets[0].max_pages, 2)

    def test_config_rejects_unsupported_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "discovery.json"
            path.write_text(
                json.dumps({"targets": [{"source": "indeed", "board": "x"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(DiscoveryConfigError):
                load_discovery_config(path)

    def test_config_validates_workday_identifiers_without_board_alias(self) -> None:
        value = {
            "targets": [
                {
                    "source": "workday",
                    "company": "Example",
                    "base_url": "https://example.wd1.myworkdayjobs.com",
                    "tenant": "example",
                    "career_site": "ExampleCareers",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "discovery.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            config = load_discovery_config(path)
        self.assertEqual(config.targets[0].board, "example")

        del value["targets"][0]["career_site"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "discovery.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(DiscoveryConfigError):
                load_discovery_config(path)

    def test_source_aware_parser_explicit_fields_take_precedence(self) -> None:
        parser = SourceAwareParser(self.app_config)
        raw = self._job().to_raw_job(discovered_date="2026-08-10")
        raw.metadata["discovery_source_fields"] = {
            "work_mode": "remote",
            "employment_type": "full-time",
            "salary": {
                "minimum": 120000,
                "maximum": 140000,
                "currency": "USD",
                "period": "year",
                "raw_text": "$120,000-$140,000",
            },
        }
        job = parser.parse(raw)
        self.assertEqual(job.remote_policy, "remote")
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.salary.minimum, 120000)
        self.assertIn("ATS structured work mode", job.parser_evidence["remote_policy"][-1])

    def test_source_failure_and_malformed_job_are_isolated(self) -> None:
        targets = [
            DiscoveryTarget(source="ashby", board="bad"),
            DiscoveryTarget(source="greenhouse", board="good"),
        ]
        config = DiscoveryConfig(targets=targets)
        jobs = [self._job(), self._job("bad-job", description="")]

        def factory(source, client):
            return FailingAdapter() if source == "ashby" else FixedAdapter(jobs)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "my-materials" / "discovery"
            summary = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=config,
                output_dir=output_dir,
                adapter_factory=factory,
            ).run()
            health = json.loads(
                (output_dir / "health" / "employer_health.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(summary.targets_attempted, 2)
        self.assertEqual(len(summary.source_errors), 1)
        self.assertEqual(len(summary.analysis_errors), 1)
        self.assertEqual(summary.jobs_analyzed, 1)
        self.assertEqual(health["employers"]["ashby:bad"]["status"], "temporarily_failed")
        self.assertEqual(health["employers"]["greenhouse:good"]["status"], "healthy")

    def test_workday_flows_through_relevance_phase1_and_private_outputs(self) -> None:
        target = DiscoveryTarget(
            source="workday",
            board="example",
            company="Example University",
            job_families=["data_bi"],
            max_results=2,
            options={
                "base_url": "https://example.wd1.myworkdayjobs.com",
                "tenant": "example",
                "career_site": "ExampleCareers",
            },
        )
        config = DiscoveryConfig(
            targets=[target],
            career_tracks={
                "data_bi": CareerTrack(
                    name="data_bi",
                    title_terms=["data reporting analyst"],
                    description_terms=["power bi", "sql"],
                )
            },
            relevance_policy=RelevancePolicy(enabled=True),
        )
        dashboard = Path("dashboard/job_pool.csv")
        dashboard_before = dashboard.read_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "my-materials" / "discovery"
            summary = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=config,
                output_dir=output_dir,
                client=WorkdayIntegrationClient(),
                adapter_factory=lambda source, client: WorkdayAdapter(client),
            ).run()
            recommended = json.loads(
                (output_dir / "recommended" / "current_queue.json").read_text(
                    encoding="utf-8"
                )
            )
            irrelevant = json.loads(
                (output_dir / "irrelevant" / "current_queue.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(summary.jobs_fetched, 2)
        self.assertEqual(summary.jobs_relevant, 1)
        self.assertEqual(summary.jobs_irrelevant, 1)
        self.assertEqual(summary.jobs_analyzed, 1)
        self.assertEqual(recommended[0]["source"], "workday")
        self.assertEqual(recommended[0]["title"], "Data Reporting Analyst")
        self.assertEqual(irrelevant[0]["title"], "Laboratory Technician")
        self.assertEqual(dashboard.read_bytes(), dashboard_before)

    def test_full_pipeline_writes_only_requested_private_output(self) -> None:
        config = DiscoveryConfig(
            targets=[DiscoveryTarget(source="greenhouse", board="mapworks")]
        )
        dashboard = Path("dashboard/job_pool.csv")
        dashboard_before = dashboard.read_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "my-materials" / "discovery"
            runner = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=config,
                output_dir=output_dir,
                adapter_factory=lambda source, client: FixedAdapter([self._job()]),
            )
            summary = runner.run()
            raw_files = list((output_dir / "raw").glob("*.json"))
            normalized_files = list((output_dir / "normalized").glob("*.json"))
            result_files = list((output_dir / "results").glob("*.json"))
            with (output_dir / "job_pool.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            log_text = (output_dir / "discovery_log.jsonl").read_text(encoding="utf-8")
        self.assertEqual(summary.jobs_analyzed, 1)
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(len(normalized_files), 1)
        self.assertEqual(len(result_files), 1)
        self.assertEqual(rows[0]["selected_resume"], "gis-data")
        self.assertIn(rows[0]["decision"], {"APPLY", "REVIEW", "SKIP"})
        self.assertIn('"event": "run_completed"', log_text)
        self.assertEqual(dashboard.read_bytes(), dashboard_before)

    def test_discovery_only_normalizes_without_scoring_or_job_pool(self) -> None:
        config = DiscoveryConfig(
            targets=[DiscoveryTarget(source="greenhouse", board="mapworks")]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "my-materials" / "discovery"
            summary = DiscoveryRunner(
                profile=self.profile,
                app_config=self.app_config,
                resume_router=self.router,
                discovery_config=config,
                output_dir=output_dir,
                adapter_factory=lambda source, client: FixedAdapter([self._job()]),
            ).run(discovery_only=True)
            self.assertEqual(len(list((output_dir / "normalized").glob("*.json"))), 1)
            self.assertEqual(len(list((output_dir / "results").glob("*.json"))), 0)
            self.assertFalse((output_dir / "job_pool.csv").exists())
        self.assertEqual(summary.jobs_normalized, 1)
        self.assertEqual(summary.jobs_analyzed, 0)

    def test_freshness_filter_preserves_unknown_and_excludes_stale(self) -> None:
        config = DiscoveryConfig()
        runner = DiscoveryRunner(
            profile=self.profile,
            app_config=self.app_config,
            resume_router=self.router,
            discovery_config=config,
            output_dir="unused",
        )
        target = DiscoveryTarget(
            source="greenhouse",
            board="example",
            posted_within_days=30,
        )
        unknown = self._job()
        matched, _, freshness = runner._matches_target(unknown, target)
        self.assertTrue(matched)
        self.assertEqual(freshness, "unknown")

        stale = self._job("stale")
        stale.posted_date = "2000-01-01"
        matched, reason, freshness = runner._matches_target(stale, target)
        self.assertFalse(matched)
        self.assertEqual(freshness, "stale")
        self.assertIn("posted_within_days=30", reason)

    def test_targeted_incremental_run_separates_queues_and_reuses_unchanged_result(self) -> None:
        relevant = self._job()
        irrelevant = SourceJob(
            source="greenhouse",
            source_job_id="cnc-1",
            company="Factory",
            title="CNC Machine Operator",
            description="Operate CNC machinery and review production data.",
            location="New York, NY",
            source_url="https://example.test/cnc-1",
        )
        config = DiscoveryConfig(
            targets=[DiscoveryTarget(source="greenhouse", board="example")],
            career_tracks={
                "data_bi": CareerTrack(
                    name="data_bi",
                    title_terms=["data analyst"],
                    description_terms=["power bi", "sql"],
                )
            },
            relevance_policy=RelevancePolicy(
                enabled=True,
                global_exclusion_terms=["cnc machine operator"],
            ),
            incremental=IncrementalPolicy(enabled=True),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "my-materials" / "discovery"

            def run_with(jobs):
                return DiscoveryRunner(
                    profile=self.profile,
                    app_config=self.app_config,
                    resume_router=self.router,
                    discovery_config=config,
                    output_dir=output_dir,
                    adapter_factory=lambda source, client: FixedAdapter(jobs),
                ).run()

            first = run_with([relevant, irrelevant])
            self.assertEqual(first.jobs_relevant, 1)
            self.assertEqual(first.jobs_irrelevant, 1)
            self.assertEqual(first.jobs_analyzed, 1)
            self.assertEqual(first.jobs_new, 1)

            recommended = json.loads(
                (output_dir / "recommended" / "current_queue.json").read_text(
                    encoding="utf-8"
                )
            )
            excluded = json.loads(
                (output_dir / "irrelevant" / "current_queue.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([item["title"] for item in recommended], ["GIS Data Analyst"])
            self.assertEqual([item["title"] for item in excluded], ["CNC Machine Operator"])
            with (output_dir / "job_pool.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                pool = list(csv.DictReader(handle))
            self.assertEqual([row["title"] for row in pool], ["GIS Data Analyst"])

            second = run_with([relevant, irrelevant])
            self.assertEqual(second.jobs_analyzed, 0)
            self.assertEqual(second.jobs_skipped_unchanged, 1)

            changed = self._job(description=self.high_fit_description + "\nTableau required.")
            third = run_with([changed, irrelevant])
            self.assertEqual(third.jobs_analyzed, 1)
            self.assertEqual(third.jobs_changed, 1)


if __name__ == "__main__":
    unittest.main()
