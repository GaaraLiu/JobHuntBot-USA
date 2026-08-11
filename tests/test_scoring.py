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

    def score_strong_match_with_unknown_experience(self, *, title: str, requirement: str):
        self.profile.years_of_experience = None
        self.profile.preferred_roles = ["Machine Learning Engineer", "Data Analyst"]
        self.profile.secondary_roles = []
        self.profile.skills = [
            "Python",
            "SQL",
            "Excel",
            "Power BI",
            "TensorFlow",
            "PyTorch",
            "GCP",
        ]
        self.profile.preferred_locations = ["Boston, MA"]
        self.profile.remote_preferences = ["remote"]
        self.profile.minimum_salary = 80000
        self.profile.industries = ["technology"]
        parser = DeterministicJobParser(self.config)
        job = parser.parse(
            RawJob(
                title=title,
                company="Synthetic Technology Employer",
                location="Boston, MA",
                source="manual",
                discovered_date="2026-08-10",
                job_description=(
                    "Technology company.\n"
                    "Full-time\n"
                    "Remote\n"
                    "Requirements\n"
                    "- Bachelor's degree in computer science.\n"
                    f"- {requirement} of relevant experience.\n"
                    "- Python, SQL, Excel, Power BI, TensorFlow, PyTorch, and GCP.\n"
                    "Salary: $100,000 - $120,000 per year.\n"
                ),
            )
        )
        return job, self.scorer.score(self.profile, job)

    def test_early_career_unknown_experience_cannot_auto_apply_to_senior_mle(self) -> None:
        job, result = self.score_strong_match_with_unknown_experience(
            title="Senior Machine Learning Engineer",
            requirement="5+ years",
        )

        experience = next(item for item in result.components if item.component == "experience")
        self.assertEqual(job.seniority.level, "senior")
        self.assertEqual(job.experience_required.minimum_years, 5)
        self.assertEqual(experience.status, "unknown")
        self.assertIsNone(experience.awarded_points)
        self.assertGreaterEqual(result.overall_score, self.config.thresholds.apply_min)
        self.assertTrue(result.seniority_experience_risk.triggered)
        self.assertTrue(result.to_dict()["seniority_experience_risk"]["triggered"])
        self.assertEqual(result.recommendation, Recommendation.REVIEW)

    def test_strong_skill_match_does_not_override_senior_five_year_risk(self) -> None:
        _, result = self.score_strong_match_with_unknown_experience(
            title="Senior Data Analyst",
            requirement="5+ years",
        )

        self.assertTrue(result.seniority_experience_risk.triggered)
        self.assertNotEqual(result.recommendation, Recommendation.APPLY)
        self.assertIn("caps APPLY at REVIEW", " ".join(result.reasoning))

    def test_ordinary_job_preserves_unknown_experience_behavior(self) -> None:
        job, result = self.score_strong_match_with_unknown_experience(
            title="Data Analyst",
            requirement="1-3 years",
        )

        experience = next(item for item in result.components if item.component == "experience")
        self.assertIsNone(job.seniority)
        self.assertEqual(experience.status, "unknown")
        self.assertIsNone(experience.awarded_points)
        self.assertFalse(result.seniority_experience_risk.triggered)
        self.assertEqual(result.recommendation, Recommendation.APPLY)

    def test_senior_title_with_two_year_requirement_does_not_trigger_safeguard(self) -> None:
        job, result = self.score_strong_match_with_unknown_experience(
            title="Senior Analyst",
            requirement="2 years",
        )

        self.assertEqual(job.seniority.level, "senior")
        self.assertFalse(result.seniority_experience_risk.triggered)
        self.assertNotEqual(result.recommendation, Recommendation.SKIP)

    def test_staff_seven_year_role_triggers_advanced_seniority_safeguard(self) -> None:
        job, result = self.score_strong_match_with_unknown_experience(
            title="Staff Machine Learning Engineer",
            requirement="7+ years",
        )

        self.assertEqual(job.seniority.level, "staff")
        self.assertTrue(result.seniority_experience_risk.triggered)
        self.assertNotEqual(result.recommendation, Recommendation.APPLY)

    def test_five_year_requirement_without_explicit_seniority_does_not_trigger_safeguard(self) -> None:
        job, result = self.score_strong_match_with_unknown_experience(
            title="Machine Learning Engineer",
            requirement="5+ years",
        )

        experience = next(item for item in result.components if item.component == "experience")
        self.assertIsNone(job.seniority)
        self.assertEqual(experience.status, "unknown")
        self.assertFalse(result.seniority_experience_risk.triggered)
        self.assertEqual(result.recommendation, Recommendation.APPLY)
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

    def test_unknown_candidate_experience_is_unscored_and_not_a_blocker(self) -> None:
        self.profile.years_of_experience = None
        job = parsed_job("job_review.txt", title="Senior Data Platform Analyst", location="Seattle, WA")

        result = self.scorer.score(self.profile, job)

        experience = next(item for item in result.components if item.component == "experience")
        self.assertEqual(experience.status, "unknown")
        self.assertIsNone(experience.awarded_points)
        self.assertTrue(any("candidate years of experience" in item.casefold() for item in result.unknown_requirements))
        self.assertFalse(any("experience" in item.casefold() for item in result.hard_blockers))

    def test_jersey_city_hybrid_matches_nyc_metro_candidate_policy(self) -> None:
        self.profile.preferred_locations = [
            "New York City",
            "Long Island",
            "Jersey City",
            "Northern New Jersey",
            "NYC metropolitan area",
        ]
        self.profile.remote_preferences = ["remote"]
        parser = DeterministicJobParser(self.config)
        job = parser.parse(
            RawJob(
                title="Forecasting Analyst",
                company="Example Co",
                location="Jersey City, NJ, USA",
                source="manual",
                discovered_date="2026-08-10",
                job_description="Employees work in a hybrid mode.\nFull-time.",
            )
        )

        result = self.scorer.score(self.profile, job)

        location = next(item for item in result.components if item.component == "location")
        self.assertEqual(location.awarded_points, location.maximum_points)
        self.assertIn("matches", location.reason.casefold())

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
