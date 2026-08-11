from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.employer_registry import (
    EmployerHealthStore,
    EmployerRegistryError,
    generate_registry_targets,
    load_employer_registry,
    target_health_status,
    validate_employer_registry,
)
from jobhuntbot.sources import SourceFailure


class EmployerRegistryTests(unittest.TestCase):
    def _value(self) -> dict:
        return {
            "schema_version": 1,
            "discovery_config": "discovery_config.json",
            "defaults": {"max_pages": 2, "max_results": 10},
            "employers": [
                {
                    "employer_id": "example-university",
                    "name": "Example University",
                    "enabled": True,
                    "source": "workday",
                    "career_tracks": ["higher_education", "data_bi"],
                    "priority": 90,
                    "geography": ["US"],
                    "notes": "Private planning note.",
                    "ats": {
                        "base_url": "https://example.wd1.myworkdayjobs.com",
                        "tenant": "example",
                        "career_site": "ExampleCareers",
                        "page_size": 5,
                    },
                },
                {
                    "employer_id": "example-analytics",
                    "name": "Example Analytics",
                    "enabled": True,
                    "source": "greenhouse",
                    "career_tracks": ["data_bi"],
                    "priority": 50,
                    "geography": "US",
                    "ats": {"board": "example"},
                },
                {
                    "employer_id": "disabled-example",
                    "name": "Disabled Example",
                    "enabled": False,
                    "source": "lever",
                    "career_tracks": ["applied_ai"],
                    "priority": 100,
                    "ats": {"board": "disabled"},
                },
            ],
        }

    def _load(self, value: dict):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "employer_registry.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        registry = load_employer_registry(path)
        self.addCleanup(temp.cleanup)
        return registry

    def test_parse_and_validate_multitrack_registry(self) -> None:
        registry = self._load(self._value())
        report = validate_employer_registry(
            registry,
            known_tracks=["higher_education", "data_bi", "applied_ai"],
        )
        self.assertTrue(report.valid, report.to_dict())
        self.assertEqual(report.employer_count, 3)
        self.assertEqual(report.enabled_count, 2)
        self.assertEqual(
            registry.employers[0].career_tracks,
            ["higher_education", "data_bi"],
        )

    def test_invalid_ats_and_missing_board_are_reported(self) -> None:
        value = self._value()
        value["employers"][1]["source"] = "unknown-ats"
        value["employers"][2]["ats"] = {}
        report = validate_employer_registry(self._load(value))
        messages = " ".join(issue.message for issue in report.issues)
        self.assertFalse(report.valid)
        self.assertIn("Unsupported ATS source", messages)
        self.assertIn("requires a board identifier", messages)

    def test_malformed_workday_configuration_is_reported_offline(self) -> None:
        value = self._value()
        del value["employers"][0]["ats"]["career_site"]
        report = validate_employer_registry(self._load(value))
        self.assertFalse(report.valid)
        self.assertIn(
            "base_url, tenant, and career_site",
            " ".join(issue.message for issue in report.issues),
        )

    def test_duplicate_and_malformed_employer_ids_are_reported(self) -> None:
        value = self._value()
        value["employers"][1]["employer_id"] = "Example University"
        value["employers"][2]["employer_id"] = "Example University"
        report = validate_employer_registry(self._load(value))
        messages = " ".join(issue.message for issue in report.issues)
        self.assertIn("must use lowercase", messages)
        self.assertIn("must be unique", messages)

    def test_generation_filters_disabled_and_sorts_by_priority(self) -> None:
        targets = generate_registry_targets(
            self._load(self._value()),
            known_tracks=["higher_education", "data_bi", "applied_ai"],
        )
        self.assertEqual(
            [target.options["employer_id"] for target in targets],
            ["example-university", "example-analytics"],
        )
        self.assertEqual(targets[0].job_families, ["higher_education", "data_bi"])
        self.assertEqual(targets[0].max_pages, 2)
        self.assertEqual(targets[0].max_results, 10)

    def test_generation_supports_track_source_priority_and_max_filters(self) -> None:
        registry = self._load(self._value())
        targets = generate_registry_targets(
            registry,
            known_tracks=["higher_education", "data_bi", "applied_ai"],
            tracks=["data_bi"],
            sources=["greenhouse"],
            minimum_priority=40,
            max_employers=1,
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].source, "greenhouse")
        self.assertEqual(targets[0].company, "Example Analytics")

    def test_unknown_selected_track_is_rejected(self) -> None:
        with self.assertRaises(EmployerRegistryError):
            generate_registry_targets(
                self._load(self._value()),
                known_tracks=["data_bi", "higher_education", "applied_ai"],
                tracks=["unknown"],
            )

    def test_empty_board_is_success_not_failure(self) -> None:
        self.assertEqual(target_health_status([], 0), ("empty", ""))

    def test_failure_statuses_are_explainable(self) -> None:
        temporary = SourceFailure(
            source="workday", error_type="SourceRequestError", reason="HTTP 500"
        )
        unsupported = SourceFailure(
            source="workday", error_type="UnsupportedWorkdayPattern", reason="HTTP 404"
        )
        invalid = SourceFailure(
            source="workday",
            error_type="SourceConfigurationError",
            reason="missing career_site",
        )
        self.assertEqual(target_health_status([temporary], 0)[0], "temporarily_failed")
        self.assertEqual(target_health_status([unsupported], 0)[0], "unsupported")
        self.assertEqual(target_health_status([invalid], 0)[0], "invalid_config")
        self.assertEqual(target_health_status([temporary], 2)[0], "healthy")

    def test_health_state_persists_last_success_and_latest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health.json"
            store = EmployerHealthStore(path)
            store.record(
                employer_id="example",
                employer="Example",
                source="workday",
                status="healthy",
                checked_at="2026-08-10T12:00:00+00:00",
                jobs_found=3,
            )
            store.save()
            reloaded = EmployerHealthStore(path)
            value = reloaded.record(
                employer_id="example",
                employer="Example",
                source="workday",
                status="temporarily_failed",
                checked_at="2026-08-11T12:00:00+00:00",
                error="HTTP 500",
            )
            self.assertEqual(value["last_success"], "2026-08-10T12:00:00+00:00")
            self.assertEqual(value["last_error"], "HTTP 500")


if __name__ == "__main__":
    unittest.main()
