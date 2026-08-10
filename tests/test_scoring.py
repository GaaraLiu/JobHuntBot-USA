from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.job_parser import DeterministicJobParser
from jobhuntbot.models import RawJob, Recommendation
from jobhuntbot.profile import load_candidate_profile
from jobhuntbot.scoring import JobFitScorer


FIXTURES = Path(__file__).parent / "fixtures"


def parsed_job(filename: str, *, title: str, location: str):
    config = load_config()
    parser = DeterministicJobParser(config)
    return parser.parse(
        RawJob(
            title=title,
            company="Synthetic Employer",
            location=location,
            source="manual",
            job_description=(FIXTURES / filename).read_text(encoding="utf-8"),
            discovered_date="2026-08-10",
        )
    )


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        self.config = load_config()
        self.scorer = JobFitScorer(self.config)

    def test_high_fit_job_is_apply_with_explainable_components(self) -> None:
        job = parsed_job("job_high_fit.txt", title="GIS Data Analyst", location="Boston, MA")
        result = self.scorer.score(self.profile, job)

        self.assertEqual(result.recommendation, Recommendation.APPLY)
        self.assertGreaterEqual(result.overall_score, self.config.thresholds.apply_min)
        self.assertEqual(result.hard_blockers, [])
        self.assertTrue(any("Required skill: python" in item for item in result.matched_requirements))
        self.assertTrue(any("arcgis pro" in item for item in result.missing_requirements))
        self.assertEqual({item.component for item in result.components}, set(self.config.scoring_weights))
        for component in result.components:
            self.assertGreater(component.maximum_points, 0)
            self.assertTrue(component.reason)
            self.assertIsNotNone(component.awarded_points)

    def test_borderline_job_is_review(self) -> None:
        job = parsed_job("job_review.txt", title="Senior Data Platform Analyst", location="Seattle, WA")
        result = self.scorer.score(self.profile, job)

        self.assertEqual(result.recommendation, Recommendation.REVIEW)
        self.assertEqual(result.hard_blockers, [])
        self.assertTrue(any("Experience" in item for item in result.missing_requirements))
        self.assertTrue(any("spark" in item for item in result.missing_requirements))

    def test_explicit_excluded_location_overrides_score(self) -> None:
        job = parsed_job("job_hard_blocker.txt", title="Data Analyst", location="Antarctica Research Station")
        result = self.scorer.score(self.profile, job)

        self.assertEqual(result.recommendation, Recommendation.SKIP)
        self.assertTrue(any("Excluded location" in item for item in result.hard_blockers))
        self.assertGreater(result.overall_score, 0)
        self.assertTrue(any("override" in item for item in result.reasoning))

    def test_unknown_is_separate_and_not_a_hard_blocker(self) -> None:
        parser = DeterministicJobParser(self.config)
        job = parser.parse(
            RawJob(
                title="Data Analyst",
                company="Unknown Details Inc",
                job_description="Analyze reports and communicate findings.",
            )
        )
        result = self.scorer.score(self.profile, job)

        self.assertEqual(result.hard_blockers, [])
        self.assertTrue(result.unknown_requirements)
        unknown_components = [item for item in result.components if item.status == "unknown"]
        self.assertTrue(unknown_components)
        self.assertTrue(all(item.awarded_points is None for item in unknown_components))

    def test_thresholds_are_loaded_from_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "override.json"
            path.write_text(json.dumps({"thresholds": {"apply_min": 99, "review_min": 40}}), encoding="utf-8")
            config = load_config(path)
        job = parsed_job("job_high_fit.txt", title="GIS Data Analyst", location="Boston, MA")
        result = JobFitScorer(config).score(self.profile, job)
        self.assertEqual(result.recommendation, Recommendation.REVIEW)

    def test_unknown_clearance_does_not_become_blocker_without_complete_profile_evidence(self) -> None:
        profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        profile.work_authorization.security_clearances_complete = False
        parser = DeterministicJobParser(self.config)
        job = parser.parse(
            RawJob(
                title="Data Analyst",
                company="Secure Co",
                location="Boston, MA",
                job_description="Requirements\n- Python and SQL.\n- Active Top Secret clearance required.",
            )
        )
        result = self.scorer.score(profile, job)
        self.assertFalse(any("clearance" in item.casefold() for item in result.hard_blockers))
        self.assertTrue(any("clearance" in item.casefold() for item in result.unknown_requirements))


if __name__ == "__main__":
    unittest.main()
