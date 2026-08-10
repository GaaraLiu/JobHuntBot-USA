from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.models import RawJob, Recommendation
from jobhuntbot.pipeline import JobHuntPipeline, PipelineValidationError
from jobhuntbot.profile import load_candidate_profile
from jobhuntbot.resume_router import ResumeRouter, load_resume_routing
from jobhuntbot.storage import load_dashboard_schema, read_csv_rows


FIXTURES = Path(__file__).parent / "fixtures"


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        routing = load_resume_routing(FIXTURES / "resume_routing_valid.json")
        self.pipeline = JobHuntPipeline(
            profile=self.profile,
            config=self.config,
            resume_router=ResumeRouter(routing, self.config.normalization.get("skill_aliases", {})),
        )

    def test_end_to_end_analysis_and_optional_save(self) -> None:
        raw = RawJob(
            title="GIS Data Analyst",
            company="MapWorks",
            location="Boston, MA",
            source="manual",
            source_native_id="pipeline-1",
            job_description=(FIXTURES / "job_high_fit.txt").read_text(encoding="utf-8"),
        )
        schema = load_dashboard_schema()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_pool.csv"
            result = self.pipeline.analyze(raw, path)
            _, rows = read_csv_rows(path, schema.columns("job_pool"))

        self.assertEqual(result.score.recommendation, Recommendation.APPLY)
        self.assertEqual(result.resume.resume_id, "gis-data")
        self.assertTrue(result.saved)
        self.assertEqual(result.storage_action, "inserted")
        self.assertEqual(rows[0]["recommendation"], "APPLY")
        self.assertEqual(rows[0]["status"], "Pending")

    def test_invalid_profile_stops_pipeline(self) -> None:
        invalid_profile = load_candidate_profile(FIXTURES / "profile_missing_critical.json")
        pipeline = JobHuntPipeline(
            profile=invalid_profile,
            config=self.config,
            resume_router=self.pipeline.resume_router,
        )
        with self.assertRaises(PipelineValidationError):
            pipeline.analyze(RawJob(title="Analyst", company="Co", job_description="Requirements: SQL"))

    def test_missing_raw_job_fields_are_rejected(self) -> None:
        with self.assertRaises(PipelineValidationError):
            self.pipeline.analyze(RawJob(title="", company="Co", job_description=""))


if __name__ == "__main__":
    unittest.main()
