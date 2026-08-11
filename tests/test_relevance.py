from __future__ import annotations

import unittest

from jobhuntbot.models import NormalizedJob, Seniority
from jobhuntbot.relevance import (
    CareerTrack,
    JobRelevanceEvaluator,
    LocationPolicy,
    RelevancePolicy,
)
from jobhuntbot.sources import SourceJob


def _tracks() -> dict[str, CareerTrack]:
    return {
        "higher_education": CareerTrack(
            name="higher_education",
            title_terms=["academic advisor", "student success coordinator"],
            category_terms=["higher education"],
            description_terms=["student advising", "academic programs"],
        ),
        "data_bi": CareerTrack(
            name="data_bi",
            title_terms=["data analyst", "business intelligence analyst"],
            category_terms=["analytics", "business intelligence"],
            description_terms=["power bi", "sql", "dashboard development"],
        ),
        "urban_transport_gis": CareerTrack(
            name="urban_transport_gis",
            title_terms=["transportation planner", "gis analyst"],
            category_terms=["transportation", "urban planning"],
            description_terms=["arcgis", "spatial analysis", "transit planning"],
        ),
        "applied_ai": CareerTrack(
            name="applied_ai",
            title_terms=["machine learning analyst", "junior machine learning engineer"],
            category_terms=["machine learning", "artificial intelligence"],
            description_terms=["python", "model development", "computer vision"],
        ),
    }


def _source(title: str, description: str, *, category: str = "") -> SourceJob:
    return SourceJob(
        source="greenhouse",
        source_job_id=title.casefold().replace(" ", "-"),
        company="Example",
        title=title,
        description=description,
        location="New York, NY",
        source_url="https://example.test/job",
        metadata={"departments": [{"name": category}]} if category else {},
    )


def _normalized(source: SourceJob, seniority: str = "") -> NormalizedJob:
    return NormalizedJob(
        job_id="test-job",
        title=source.title,
        company=source.company,
        location=source.location,
        source=source.source,
        source_url=source.source_url,
        apply_url=source.apply_url,
        job_description=source.description,
        seniority=Seniority(level=seniority, evidence=source.title) if seniority else None,
    )


class RelevanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = JobRelevanceEvaluator(
            _tracks(),
            RelevancePolicy(
                enabled=True,
                global_exclusion_terms=[
                    "cnc machine operator",
                    "facilities electrical engineer",
                ],
                excluded_seniority_levels=[
                    "staff",
                    "principal",
                    "director",
                    "vp",
                    "executive",
                ],
            ),
        )

    def _match(self, title: str, description: str, category: str = ""):
        source = _source(title, description, category=category)
        return self.evaluator.evaluate(source, _normalized(source))

    def test_each_configured_career_track_has_a_realistic_relevant_title(self) -> None:
        cases = [
            ("Academic Advisor", "Provide student advising.", "higher_education"),
            ("Data Analyst", "Build Power BI dashboards using SQL.", "data_bi"),
            ("Transportation Planner", "Use ArcGIS for transit planning.", "urban_transport_gis"),
            ("Junior Machine Learning Engineer", "Develop Python models.", "applied_ai"),
        ]
        for title, description, expected in cases:
            with self.subTest(title=title):
                result = self._match(title, description)
                self.assertTrue(result.relevant)
                self.assertEqual(result.matched_track, expected)

    def test_irrelevant_operator_and_facilities_roles_are_explicitly_excluded(self) -> None:
        for title in ("CNC Machine Operator", "Facilities Electrical Engineer"):
            with self.subTest(title=title):
                result = self._match(title, "Analyze engineering data and technology.")
                self.assertFalse(result.relevant)
                self.assertEqual(result.exclusion_type, "explicit_exclusion")

    def test_generic_data_word_does_not_create_false_relevance(self) -> None:
        result = self._match(
            "Business Operations Coordinator",
            "Use data, analysis, technology, research, and engineering information.",
        )
        self.assertFalse(result.relevant)
        self.assertEqual(result.exclusion_type, "no_track_match")

    def test_category_and_description_can_form_credible_non_title_match(self) -> None:
        result = self._match(
            "Reporting Specialist",
            "Create Power BI reports using SQL.",
            category="Business Intelligence",
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.matched_track, "data_bi")
        self.assertTrue(any(item.startswith("category:") for item in result.matched_terms))

    def test_advanced_seniority_is_configurable_but_senior_is_not_globally_excluded(self) -> None:
        principal = _source("Principal Data Analyst", "Power BI and SQL.")
        result = self.evaluator.evaluate(principal, _normalized(principal, "principal"))
        self.assertFalse(result.relevant)
        self.assertEqual(result.exclusion_type, "advanced_seniority")

        senior = _source("Senior Data Analyst", "Power BI and SQL.")
        result = self.evaluator.evaluate(senior, _normalized(senior, "senior"))
        self.assertTrue(result.relevant)
        self.assertEqual(result.matched_track, "data_bi")

    def test_location_policy_is_configurable_and_explainable(self) -> None:
        evaluator = JobRelevanceEvaluator(
            _tracks(),
            RelevancePolicy(enabled=True),
            LocationPolicy(
                enabled=True,
                primary_markets=["new york"],
                remote_terms=["remote us"],
                relocation_markets=["california"],
                allow_other_locations=False,
            ),
        )
        remote = _source("Data Analyst", "Power BI and SQL.")
        remote.location = "Remote - US"
        result = evaluator.evaluate(remote, _normalized(remote))
        self.assertTrue(result.relevant)
        self.assertEqual(result.location_tier, "remote_us")

        outside = _source("Data Analyst", "Power BI and SQL.")
        outside.location = "Boston, MA"
        result = evaluator.evaluate(outside, _normalized(outside))
        self.assertFalse(result.relevant)
        self.assertEqual(result.exclusion_type, "location")

    def _us_location_evaluator(self, *, allowed_countries=None):
        return JobRelevanceEvaluator(
            _tracks(),
            RelevancePolicy(enabled=True),
            LocationPolicy(
                enabled=True,
                primary_markets=["new york", "jersey city"],
                remote_terms=["remote us", "us remote", "remote united states"],
                relocation_markets=["california"],
                allow_other_locations=True,
                allowed_countries=allowed_countries or ["US"],
            ),
        )

    def test_toronto_canada_is_international_and_excluded_from_us_queue(self) -> None:
        evaluator = self._us_location_evaluator()
        source = _source("Data Analyst", "Power BI and SQL.")
        source.location = "Toronto, Canada"
        result = evaluator.evaluate(source, _normalized(source))
        self.assertFalse(result.relevant)
        self.assertTrue(result.excluded)
        self.assertEqual(result.location_tier, "international")
        self.assertEqual(result.exclusion_type, "international")
        self.assertIn("CA", result.location_reason)

        source.location = "New York, NY"
        source.metadata = {"country": "Canada"}
        result = evaluator.evaluate(source, _normalized(source))
        self.assertEqual(result.location_tier, "international")
        self.assertTrue(result.excluded)
        self.assertEqual(result.exclusion_type, "international")
        self.assertIn("explicit ATS country metadata", result.location_reason)

    def test_san_francisco_is_an_allowed_us_relocation_market(self) -> None:
        evaluator = self._us_location_evaluator()
        source = _source("Data Analyst", "Power BI and SQL.")
        source.location = "San Francisco, CA"
        result = evaluator.evaluate(source, _normalized(source))
        self.assertTrue(result.relevant)
        self.assertIn(
            result.location_tier,
            {"other_us_reviewable", "relocation_allowed"},
        )

    def test_new_york_is_local_acceptable(self) -> None:
        evaluator = self._us_location_evaluator()
        source = _source("Data Analyst", "Power BI and SQL.")
        result = evaluator.evaluate(source, _normalized(source))
        self.assertTrue(result.relevant)
        self.assertEqual(result.location_tier, "local_acceptable")

    def test_remote_us_is_classified_as_remote_us(self) -> None:
        evaluator = self._us_location_evaluator()
        source = _source("Data Analyst", "Power BI and SQL.")
        source.location = "Remote - US"
        result = evaluator.evaluate(source, _normalized(source))
        self.assertTrue(result.relevant)
        self.assertEqual(result.location_tier, "remote_us")

    def test_london_uk_is_international_and_excluded(self) -> None:
        evaluator = self._us_location_evaluator()
        source = _source("Data Analyst", "Power BI and SQL.")
        source.location = "London, UK"
        result = evaluator.evaluate(source, _normalized(source))
        self.assertFalse(result.relevant)
        self.assertEqual(result.location_tier, "international")
        self.assertTrue(result.excluded)
        self.assertEqual(result.exclusion_type, "international")

    def test_ambiguous_location_remains_unknown(self) -> None:
        evaluator = self._us_location_evaluator()
        source = _source("Data Analyst", "Power BI and SQL.")
        source.location = "Springfield"
        result = evaluator.evaluate(source, _normalized(source))
        self.assertTrue(result.relevant)
        self.assertEqual(result.location_tier, "unknown")
        self.assertFalse(result.excluded)
        self.assertIn("UNKNOWN", result.location_reason)

    def test_explicitly_allowed_canada_may_be_reviewable(self) -> None:
        evaluator = self._us_location_evaluator(allowed_countries=["US", "CA"])
        source = _source("Data Analyst", "Power BI and SQL.")
        source.location = "Toronto, Canada"
        result = evaluator.evaluate(source, _normalized(source))
        self.assertTrue(result.relevant)
        self.assertEqual(result.location_tier, "international")
        self.assertFalse(result.excluded)
        self.assertIn("explicitly allowed", result.location_reason)


if __name__ == "__main__":
    unittest.main()
