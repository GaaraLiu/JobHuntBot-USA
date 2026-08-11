from __future__ import annotations

import unittest

from jobhuntbot.deduplication import deduplicate_jobs
from jobhuntbot.normalization import build_job_id, canonicalize_url
from jobhuntbot.sources import SourceJob, SourceSalary


class DeduplicationTests(unittest.TestCase):
    def test_same_native_id_is_removed_and_missing_optional_fields_are_merged(self) -> None:
        first = SourceJob(
            source="greenhouse",
            source_job_id="101",
            company="Example",
            title="Data Analyst",
            description="Description",
            location="New York, NY",
        )
        second = SourceJob(
            source="greenhouse",
            source_job_id="101",
            company="Example",
            title="Data Analyst",
            description="Description",
            location="New York, NY",
            work_mode="hybrid",
            salary=SourceSalary(minimum=80000, maximum=100000, currency="USD", period="year"),
        )
        result = deduplicate_jobs([first, second])
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.jobs[0].work_mode, "hybrid")
        self.assertIsNotNone(result.jobs[0].salary)
        self.assertIn("native job id", result.duplicates[0].reason)

    def test_tracking_parameters_do_not_prevent_url_deduplication(self) -> None:
        first = SourceJob(
            source="greenhouse",
            source_job_id="gh-1",
            company="Example",
            title="Analyst",
            description="Description",
            location="Boston, MA",
            source_url="https://jobs.example.com/123?utm_source=greenhouse&job=123",
        )
        second = SourceJob(
            source="lever",
            source_job_id="lever-9",
            company="Example",
            title="Analyst",
            description="Description",
            location="Boston, MA",
            source_url="https://jobs.example.com/123?job=123&utm_campaign=lever",
        )
        result = deduplicate_jobs([first, second])
        self.assertEqual(len(result.jobs), 1)
        self.assertIn("canonical job/apply URL", result.duplicates[0].reason)
        self.assertEqual(
            canonicalize_url(first.source_url), canonicalize_url(second.source_url)
        )

    def test_company_title_location_fingerprint_is_deterministic_fallback(self) -> None:
        first = SourceJob(
            source="greenhouse",
            source_job_id="",
            company="Example Inc.",
            title="GIS Analyst",
            description="First description",
            location="Queens, NY",
        )
        second = SourceJob(
            source="ashby",
            source_job_id="",
            company=" example inc. ",
            title="GIS  Analyst",
            description="Second description",
            location="QUEENS, NY",
        )
        result = deduplicate_jobs([first, second])
        self.assertEqual(len(result.jobs), 1)
        self.assertIn("fingerprint", result.duplicates[0].reason)

    def test_different_locations_are_not_fingerprint_duplicates(self) -> None:
        jobs = [
            SourceJob("greenhouse", "", "Example", "Analyst", "Description", location="New York, NY"),
            SourceJob("greenhouse", "", "Example", "Analyst", "Description", location="Boston, MA"),
        ]
        result = deduplicate_jobs(jobs)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.duplicates, [])

    def test_phase_1_native_id_policy_remains_stable(self) -> None:
        values = {
            "source": "lever",
            "source_native_id": "abc-123",
            "apply_url": "https://jobs.example.com/abc-123?utm_source=one",
            "source_url": "https://jobs.example.com/abc-123",
            "company": "Example",
            "title": "Data Analyst",
            "location": "New York, NY",
        }
        first = build_job_id(**values)
        values["apply_url"] = "https://jobs.example.com/changed"
        second = build_job_id(**values)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("native_lever_"))


if __name__ == "__main__":
    unittest.main()
