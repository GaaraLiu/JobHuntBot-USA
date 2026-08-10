from __future__ import annotations

import json
import unittest
from pathlib import Path

from jobhuntbot.profile import (
    load_candidate_profile,
    parse_candidate_profile,
    validate_candidate_profile,
)


FIXTURES = Path(__file__).parent / "fixtures"


class CandidateProfileTests(unittest.TestCase):
    def test_valid_profile_loads_and_validates(self) -> None:
        profile = load_candidate_profile(FIXTURES / "profile_valid.json")
        result = validate_candidate_profile(profile)

        self.assertTrue(result.valid, [issue.message for issue in result.issues])
        self.assertEqual(profile.preferred_roles, ["Data Analyst", "GIS Analyst"])
        self.assertEqual(profile.years_of_experience, 3)
        self.assertIn("Tableau", profile.all_skills)
        self.assertFalse(profile.work_authorization.requires_sponsorship_now)

    def test_missing_critical_information_is_flagged_not_invented(self) -> None:
        profile = load_candidate_profile(FIXTURES / "profile_missing_critical.json")
        result = validate_candidate_profile(profile)
        codes = {issue.code for issue in result.errors}

        self.assertFalse(result.valid)
        self.assertIn("missing_preferred_roles", codes)
        self.assertIn("missing_skills", codes)
        self.assertIn("missing_authorization_country", codes)
        self.assertIn("missing_current_authorization", codes)
        self.assertIn("missing_resume_routing_file", codes)
        self.assertIn("invalid_boolean", codes)
        self.assertIn("unknown_years_of_experience", {issue.code for issue in result.warnings})
        self.assertIsNone(profile.work_authorization.requires_sponsorship_now)

    def test_numeric_years_of_experience_is_valid(self) -> None:
        profile = load_candidate_profile(FIXTURES / "profile_valid.json")

        result = validate_candidate_profile(profile)

        self.assertTrue(result.valid)
        self.assertEqual(profile.years_of_experience, 3)
        self.assertNotIn("unknown_years_of_experience", {issue.code for issue in result.issues})

    def test_null_years_of_experience_is_valid_and_unknown(self) -> None:
        with (FIXTURES / "profile_valid.json").open(encoding="utf-8") as handle:
            value = json.load(handle)
        value["years_of_experience"] = None

        profile = parse_candidate_profile(value)
        result = validate_candidate_profile(profile)

        self.assertTrue(result.valid, [issue.message for issue in result.errors])
        self.assertIsNone(profile.years_of_experience)
        self.assertIn("unknown_years_of_experience", {issue.code for issue in result.warnings})

    def test_negative_years_of_experience_is_invalid(self) -> None:
        with (FIXTURES / "profile_valid.json").open(encoding="utf-8") as handle:
            value = json.load(handle)
        value["years_of_experience"] = -1

        result = validate_candidate_profile(parse_candidate_profile(value))

        self.assertFalse(result.valid)
        self.assertIn("invalid_years_of_experience", {issue.code for issue in result.errors})

    def test_malformed_years_of_experience_is_invalid(self) -> None:
        with (FIXTURES / "profile_valid.json").open(encoding="utf-8") as handle:
            value = json.load(handle)
        value["years_of_experience"] = "several"

        profile = parse_candidate_profile(value)
        result = validate_candidate_profile(profile)

        self.assertIsNone(profile.years_of_experience)
        self.assertFalse(result.valid)
        self.assertIn("malformed_years_of_experience", {issue.code for issue in result.errors})

    def test_optional_domain_experience_preserves_context_without_fte_years(self) -> None:
        with (FIXTURES / "profile_valid.json").open(encoding="utf-8") as handle:
            value = json.load(handle)
        value["domain_experience"] = {
            "data_analytics": {
                "first_relevant_year": 2024,
                "current": False,
                "context": "Research, projects, and an internship",
                "evidence_summary": "Dated evidence is stored separately; no FTE total is inferred."
            }
        }

        profile = parse_candidate_profile(value)

        domain = profile.domain_experience["data_analytics"]
        self.assertEqual(domain.first_relevant_year, 2024)
        self.assertFalse(domain.current)
        self.assertIn("no FTE", domain.evidence_summary)

    def test_legacy_target_fields_are_supported(self) -> None:
        legacy = {
            "targets": {
                "primary_role_families": ["Operations"],
                "secondary_role_families": ["Supply Chain"],
                "target_locations": ["Chicago, IL"],
                "target_industries": ["manufacturing"],
                "roles_to_avoid": ["Director"]
            },
            "skills": ["Excel"],
            "years_of_experience": 2,
            "work_authorization": {
                "country": "United States",
                "current_authorization": "Authorized"
            },
            "resume_routing_file": "resume_routing.json"
        }

        profile = parse_candidate_profile(legacy)

        self.assertEqual(profile.preferred_roles, ["Operations"])
        self.assertEqual(profile.preferred_locations, ["Chicago, IL"])
        self.assertEqual(profile.excluded_roles, ["Director"])

    def test_profile_round_trip_output_does_not_add_facts(self) -> None:
        with (FIXTURES / "profile_missing_critical.json").open(encoding="utf-8") as handle:
            original = json.load(handle)
        profile = parse_candidate_profile(original)
        output = profile.to_dict()

        self.assertEqual(output["skills"], [])
        self.assertIsNone(output["years_of_experience"])
        self.assertEqual(output["work_authorization"]["current_authorization"], "")


if __name__ == "__main__":
    unittest.main()
