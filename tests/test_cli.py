from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from jobhuntbot.cli import main


FIXTURES = Path(__file__).parent / "fixtures"


class CliTests(unittest.TestCase):
    def test_validate_profile_json_output(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main([
                "validate-profile",
                "--profile", str(FIXTURES / "profile_valid.json"),
                "--json",
            ])
        value = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(value["valid"])

    def test_analyze_job_human_output_contains_required_sections(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main([
                "analyze-job",
                "--profile", str(FIXTURES / "profile_valid.json"),
                "--resume-routing", str(FIXTURES / "resume_routing_valid.json"),
                "--description-file", str(FIXTURES / "job_high_fit.txt"),
                "--title", "GIS Data Analyst",
                "--company", "MapWorks",
                "--location", "Boston, MA",
            ])
        text = output.getvalue()
        self.assertEqual(exit_code, 0, errors.getvalue())
        self.assertIn("Recommendation: APPLY", text)
        self.assertIn("Score breakdown:", text)
        self.assertIn("Matched requirements:", text)
        self.assertIn("Missing requirements:", text)
        self.assertIn("Unknown requirements:", text)
        self.assertIn("Hard blockers:", text)
        self.assertIn("Seniority/experience risk:", text)
        self.assertIn("Resume recommendation:", text)


    def test_discover_command_supports_private_output_and_json_summary(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_families": {
                            "data_bi": {"keywords": ["data analyst"]}
                        },
                        "targets": [],
                    }
                ),
                encoding="utf-8",
            )
            private_output = root / "my-materials" / "discovery"
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = main([
                    "discover",
                    "--profile", str(FIXTURES / "profile_valid.json"),
                    "--resume-routing", str(FIXTURES / "resume_routing_valid.json"),
                    "--discovery-config", str(config),
                    "--output-dir", str(private_output),
                    "--json",
                ])
            value = json.loads(output.getvalue())
            self.assertTrue((private_output / "job_pool.csv").exists())
        self.assertEqual(exit_code, 0, errors.getvalue())
        self.assertEqual(value["jobs_fetched"], 0)
        self.assertEqual(value["jobs_analyzed"], 0)
if __name__ == "__main__":
    unittest.main()
