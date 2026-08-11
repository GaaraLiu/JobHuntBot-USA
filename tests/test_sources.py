from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from jobhuntbot.sources import DiscoveryTarget, SourceRequestError, create_adapter, supported_sources
from jobhuntbot.sources.ashby import AshbyAdapter
from jobhuntbot.sources.greenhouse import GreenhouseAdapter
from jobhuntbot.sources.lever import LeverAdapter
from jobhuntbot.sources.smartrecruiters import SmartRecruitersAdapter


FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient:
    def __init__(self, *, failure: bool = False, malformed: bool = False):
        self.failure = failure
        self.malformed = malformed
        self.urls: list[str] = []

    def get_json(self, url: str, *, headers=None) -> Any:
        self.urls.append(url)
        if self.failure:
            raise SourceRequestError(url, "controlled network failure")
        if self.malformed:
            return {}
        if "api.smartrecruiters.com" in url and "/postings/sr-101" in url:
            return _fixture("smartrecruiters_detail.json")
        if "api.smartrecruiters.com" in url:
            return _fixture("smartrecruiters_list.json")
        if "greenhouse.io" in url:
            return _fixture("greenhouse_jobs.json")
        if "api.lever.co" in url:
            return _fixture("lever_postings.json")
        if "api.ashbyhq.com" in url:
            return _fixture("ashby_jobs.json")
        raise AssertionError(f"Unexpected URL: {url}")


class SourceAdapterTests(unittest.TestCase):
    def test_registry_contains_only_initial_phase_2_sources(self) -> None:
        self.assertEqual(
            supported_sources(),
            ("ashby", "greenhouse", "lever", "smartrecruiters"),
        )
        adapter = create_adapter("greenhouse", FixtureClient())
        self.assertIsInstance(adapter, GreenhouseAdapter)

    def test_smartrecruiters_parses_list_and_detail(self) -> None:
        client = FixtureClient()
        target = DiscoveryTarget(
            source="smartrecruiters", board="example", company="Example Analytics"
        )
        batch = SmartRecruitersAdapter(client).fetch(target, limit=10)
        self.assertEqual(len(batch.jobs), 1)
        self.assertEqual(batch.errors, [])
        job = batch.jobs[0]
        self.assertEqual(job.source_job_id, "sr-101")
        self.assertEqual(job.company, "Example Analytics")
        self.assertEqual(job.location, "New York, NY, us")
        self.assertEqual(job.employment_type, "full-time")
        self.assertIn("Power BI required", job.description)
        self.assertEqual(len(client.urls), 2)

    def test_greenhouse_preserves_missing_optional_fields_and_isolates_malformed_job(self) -> None:
        target = DiscoveryTarget(source="greenhouse", board="example", company="Example Co")
        batch = GreenhouseAdapter(FixtureClient()).fetch(target)
        self.assertEqual(len(batch.jobs), 1)
        self.assertEqual(len(batch.errors), 1)
        job = batch.jobs[0]
        self.assertEqual(job.company, "Example Co")
        self.assertEqual(job.work_mode, "")
        self.assertEqual(job.employment_type, "")
        self.assertIsNone(job.salary)
        self.assertIn("spatial data", job.description)

    def test_lever_parses_structured_work_and_salary_fields(self) -> None:
        target = DiscoveryTarget(source="lever", board="example", company="Example Co")
        batch = LeverAdapter(FixtureClient()).fetch(target)
        self.assertEqual(len(batch.jobs), 1)
        job = batch.jobs[0]
        self.assertEqual(job.work_mode, "hybrid")
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.salary.minimum, 80000)
        self.assertEqual(job.salary.maximum, 105000)
        self.assertIn("Power BI required", job.description)

    def test_ashby_parses_explicit_fields(self) -> None:
        target = DiscoveryTarget(source="ashby", board="example", company="Example Co")
        batch = AshbyAdapter(FixtureClient()).fetch(target)
        self.assertEqual(len(batch.jobs), 1)
        job = batch.jobs[0]
        self.assertEqual(job.source_job_id, "ashby-401")
        self.assertEqual(job.work_mode, "remote")
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.posted_date, "2026-08-03")
        self.assertEqual(job.salary.currency, "USD")

    def test_malformed_top_level_response_is_reported(self) -> None:
        target = DiscoveryTarget(source="greenhouse", board="example")
        batch = GreenhouseAdapter(FixtureClient(malformed=True)).fetch(target)
        self.assertEqual(batch.jobs, [])
        self.assertEqual(len(batch.errors), 1)
        self.assertEqual(batch.errors[0].error_type, "MalformedSourceResponse")

    def test_source_request_failure_is_returned_not_raised(self) -> None:
        target = DiscoveryTarget(source="ashby", board="example")
        batch = AshbyAdapter(FixtureClient(failure=True)).fetch(target)
        self.assertEqual(batch.jobs, [])
        self.assertEqual(len(batch.errors), 1)
        self.assertIn("controlled network failure", batch.errors[0].reason)


if __name__ == "__main__":
    unittest.main()
