from __future__ import annotations

import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.job_parser import DeterministicJobParser
from jobhuntbot.models import RawJob


FIXTURES = Path(__file__).parent / "fixtures"


class JobParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = DeterministicJobParser(load_config())

    def test_extracts_normalized_job_information(self) -> None:
        description = (FIXTURES / "job_high_fit.txt").read_text(encoding="utf-8")
        raw = RawJob(
            title="GIS Data Analyst",
            company="MapWorks",
            location="Boston, MA",
            source="greenhouse",
            source_native_id="99881",
            source_url="https://boards.example.com/jobs/99881?utm_source=test",
            apply_url="https://boards.example.com/jobs/99881/apply",
            posted_date="2026-08-09",
            discovered_date="2026-08-10",
            job_description=description,
        )

        job = self.parser.parse(raw)

        self.assertTrue(job.job_id.startswith("native_greenhouse_"))
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.remote_policy, "hybrid")
        self.assertEqual(job.salary.minimum, 90000)
        self.assertEqual(job.salary.maximum, 110000)
        self.assertEqual(job.experience_required.minimum_years, 2)
        self.assertIn("bachelor", job.education_required)
        self.assertIn("python", job.skills_required)
        self.assertIn("sql", job.skills_required)
        self.assertIn("arcgis pro", job.preferred_skills)
        self.assertIn("tableau", job.preferred_skills)
        self.assertIn("geospatial", job.industries)
        self.assertEqual(job.job_family, "geospatial")
        self.assertIn("GISP certification", job.licenses_required)
        self.assertTrue(job.work_authorization_requirements)

    def test_absent_fields_remain_unknown_not_synthesized(self) -> None:
        raw = RawJob(
            title="Analyst",
            company="Example Co",
            location="",
            source="manual",
            job_description="Analyze operational reports and communicate findings.",
        )
        job = self.parser.parse(raw)

        self.assertIsNone(job.salary)
        self.assertIsNone(job.experience_required)
        self.assertEqual(job.education_required, [])
        self.assertEqual(job.skills_required, [])
        self.assertEqual(job.remote_policy, "")
        self.assertTrue(job.discovered_date)


if __name__ == "__main__":
    unittest.main()
