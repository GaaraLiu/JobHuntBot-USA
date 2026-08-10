from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.job_parser import DeterministicJobParser
from jobhuntbot.models import RawJob, ResumeRoutingConfig
from jobhuntbot.profile import load_candidate_profile
from jobhuntbot.resume_router import ResumeRouter, ResumeRoutingError, load_resume_routing


FIXTURES = Path(__file__).parent / "fixtures"


class ResumeRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        self.routing = load_resume_routing(FIXTURES / "resume_routing_valid.json")
        self.router = ResumeRouter(self.routing, self.config.normalization.get("skill_aliases", {}))
        self.parser = DeterministicJobParser(self.config)

    def _job(self, fixture: str, title: str, location: str):
        return self.parser.parse(
            RawJob(
                title=title,
                company="Synthetic Employer",
                location=location,
                source="manual",
                job_description=(FIXTURES / fixture).read_text(encoding="utf-8"),
            )
        )

    def test_routes_geospatial_job_to_gis_resume(self) -> None:
        recommendation = self.router.route(
            self.profile,
            self._job("job_high_fit.txt", "GIS Data Analyst", "Boston, MA"),
        )
        self.assertEqual(recommendation.resume_id, "gis-data")
        self.assertEqual(recommendation.file_path, "resumes/gis-data.pdf")
        self.assertTrue(any("Job family" in item for item in recommendation.evidence))
        self.assertTrue(any("Candidate-confirmed" in item for item in recommendation.evidence))

    def test_routes_general_analytics_job_to_general_resume(self) -> None:
        recommendation = self.router.route(
            self.profile,
            self._job("job_review.txt", "Senior Data Platform Analyst", "Seattle, WA"),
        )
        self.assertEqual(recommendation.resume_id, "general-data")
        self.assertIn("structured", recommendation.reason)

    def test_no_match_and_no_default_returns_no_selection(self) -> None:
        router = ResumeRouter(ResumeRoutingConfig(resumes=[]))
        recommendation = router.route(
            self.profile,
            self._job("job_high_fit.txt", "GIS Data Analyst", "Boston, MA"),
        )
        self.assertFalse(recommendation.selected)
        self.assertIn("No structured resume route", recommendation.reason)

    def test_duplicate_resume_ids_are_rejected(self) -> None:
        value = {
            "schema_version": 1,
            "resumes": [
                {"resume_id": "same", "file_path": "one.pdf"},
                {"resume_id": "same", "file_path": "two.pdf"}
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "routes.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ResumeRoutingError):
                load_resume_routing(path)


if __name__ == "__main__":
    unittest.main()
