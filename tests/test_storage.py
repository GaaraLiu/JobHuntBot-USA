from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.job_parser import DeterministicJobParser
from jobhuntbot.models import PipelineResult, RawJob
from jobhuntbot.profile import load_candidate_profile
from jobhuntbot.resume_router import ResumeRouter, load_resume_routing
from jobhuntbot.scoring import JobFitScorer
from jobhuntbot.storage import (
    SchemaMismatchError,
    canonical_json,
    load_dashboard_schema,
    read_csv_rows,
    save_job_result,
    write_csv_rows,
)


FIXTURES = Path(__file__).parent / "fixtures"


class StorageTests(unittest.TestCase):
    def _result(self) -> PipelineResult:
        config = load_config()
        profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        job = DeterministicJobParser(config).parse(
            RawJob(
                title="GIS Data Analyst",
                company='MapWorks, "North"',
                location="Boston, MA",
                source="manual",
                source_native_id="storage-test",
                job_description=(FIXTURES / "job_high_fit.txt").read_text(encoding="utf-8")
                + '\nAdditional note: "quoted", comma, and newline.\nSecond line.',
            )
        )
        score = JobFitScorer(config).score(profile, job)
        resume = ResumeRouter(
            load_resume_routing(FIXTURES / "resume_routing_valid.json"),
            config.normalization.get("skill_aliases", {}),
        ).route(profile, job)
        return PipelineResult(job=job, score=score, resume=resume)

    def test_canonical_json_is_deterministic(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": ["x", "y"]}), '{"a":["x","y"],"b":1}')

    def test_csv_round_trip_preserves_commas_quotes_newlines_and_json(self) -> None:
        schema = load_dashboard_schema()
        columns = schema.columns("job_pool")
        result = self._result()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_pool.csv"
            action = save_job_result(path, result, schema)
            _, rows = read_csv_rows(path, columns)
            raw_text = path.read_text(encoding="utf-8")

        self.assertEqual(action, "inserted")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], 'MapWorks, "North"')
        self.assertIn("Second line.", rows[0]["job_description"])
        self.assertIsInstance(json.loads(rows[0]["score_breakdown"]), list)
        self.assertIn('""North""', raw_text)

    def test_repeated_job_id_updates_instead_of_duplicating(self) -> None:
        schema = load_dashboard_schema()
        result = self._result()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_pool.csv"
            self.assertEqual(save_job_result(path, result, schema), "inserted")
            self.assertEqual(save_job_result(path, result, schema), "updated")
            _, rows = read_csv_rows(path, schema.columns("job_pool"))
        self.assertEqual(len(rows), 1)

    def test_schema_mismatch_is_rejected(self) -> None:
        result = self._result()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "job_pool.csv"
            path.write_text("wrong,header\n", encoding="utf-8")
            with self.assertRaises(SchemaMismatchError):
                save_job_result(path, result)


if __name__ == "__main__":
    unittest.main()

