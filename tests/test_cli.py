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
    def test_manual_auth_help_describes_human_controlled_headed_flow(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["inspect-application-form", "--help"])
        self.assertEqual(raised.exception.code, 0)
        text = output.getvalue().lower()
        self.assertIn("--manual-auth", text)
        self.assertIn("headed browser", text)
        self.assertIn("human-", text)
        self.assertIn("controlled sign-in", text)

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
        self.assertIn("Explicit experience-requirement risk:", text)
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

    def test_registry_discovery_generates_targets_without_breaking_direct_mode(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "career_tracks": {
                            "data_bi": {"title_terms": ["data analyst"]}
                        },
                        "relevance_policy": {"enabled": True},
                        "targets": [],
                    }
                ),
                encoding="utf-8",
            )
            registry = root / "employer_registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "discovery_config": "discovery.json",
                        "employers": [],
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
                    "--employer-registry", str(registry),
                    "--track", "data_bi",
                    "--max-employers", "1",
                    "--output-dir", str(private_output),
                    "--json",
                ])
            value = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, errors.getvalue())
        self.assertEqual(value["targets_attempted"], 0)
        self.assertTrue(value["health_path"].endswith("employer_health.json"))

    def test_validate_registry_is_offline_by_default(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "employer_registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "employers": [
                            {
                                "employer_id": "example",
                                "name": "Example",
                                "enabled": True,
                                "source": "greenhouse",
                                "career_tracks": ["data_bi"],
                                "priority": 1,
                                "ats": {"board": "example"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = main([
                    "validate-registry",
                    "--employer-registry", str(registry),
                    "--json",
                ])
            value = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, errors.getvalue())
        self.assertTrue(value["valid"])
        self.assertNotIn("live_checks", value)

    def test_discover_requires_policy_config(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main([
                "discover",
                "--profile", str(FIXTURES / "profile_valid.json"),
                "--resume-routing", str(FIXTURES / "resume_routing_valid.json"),
            ])
        self.assertEqual(exit_code, 2)
        self.assertIn("requires --discovery-config", errors.getvalue())
if __name__ == "__main__":
    unittest.main()
