from __future__ import annotations

import unittest
from pathlib import Path

from jobhuntbot.config import load_config
from jobhuntbot.job_parser import DeterministicJobParser
from jobhuntbot.models import RawJob


FIXTURES = Path(__file__).parent / "fixtures"


class JobParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = DeterministicJobParser(load_config())

    def test_extracts_normalized_job_information(self) -> None:
        description = (FIXTURES / "job_high_fit.txt").read_text(encoding="utf-8")
        raw = RawJob(
            title="GIS Data Analyst",
            company="MapWorks",
            location="Boston, MA",
            source="greenhouse",
            source_native_id="99881",
            source_url="https://boards.example.com/jobs/99881?utm_source=test",
            apply_url="https://boards.example.com/jobs/99881/apply",
            posted_date="2026-08-09",
            discovered_date="2026-08-10",
            job_description=description,
        )

        job = self.parser.parse(raw)

        self.assertTrue(job.job_id.startswith("native_greenhouse_"))
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.remote_policy, "hybrid")
        self.assertEqual(job.salary.minimum, 90000)
        self.assertEqual(job.salary.maximum, 110000)
        self.assertEqual(job.experience_required.minimum_years, 2)
        self.assertIn("bachelor", job.education_required)
        self.assertIn("python", job.skills_required)
        self.assertIn("sql", job.skills_required)
        self.assertIn("arcgis pro", job.preferred_skills)
        self.assertIn("tableau", job.preferred_skills)
        self.assertIn("geospatial", job.industries)
        self.assertEqual(job.job_family, "geospatial")
        self.assertIn("GISP certification", job.licenses_required)
        self.assertTrue(job.work_authorization_requirements)

    def test_absent_fields_remain_unknown_not_synthesized(self) -> None:
        raw = RawJob(
            title="Analyst",
            company="Example Co",
            location="",
            source="manual",
            job_description="Analyze operational reports and communicate findings.",
        )
        job = self.parser.parse(raw)

        self.assertIsNone(job.salary)
        self.assertIsNone(job.experience_required)
        self.assertEqual(job.education_required, [])
        self.assertEqual(job.skills_required, [])
        self.assertEqual(job.remote_policy, "")
        self.assertTrue(job.discovered_date)

    def test_word_parenthesized_experience_formats(self) -> None:
        cases = [
            ("three (3) years of experience", 3),
            ("minimum of five (5) years of experience", 5),
            ("at least two (2) years of relevant experience", 2),
            ("one (1) year of experience", 1),
            ("three (3)+ years of experience", 3),
        ]

        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                job = self.parse_description(f"Qualifications\n- {phrase}.")
                self.assertIsNotNone(job.experience_required)
                self.assertEqual(job.experience_required.minimum_years, expected)

    def test_normal_numeric_plus_experience_still_works(self) -> None:
        job = self.parse_description("Qualifications\n- 3+ years of experience.")

        self.assertIsNotNone(job.experience_required)
        self.assertEqual(job.experience_required.minimum_years, 3)

    def test_inconsistent_word_parenthesized_experience_is_unknown(self) -> None:
        job = self.parse_description("Qualifications\n- three (4) years of experience.")

        self.assertIsNone(job.experience_required)

    def test_what_we_need_to_see_marks_required_skills(self) -> None:
        job = self.parse_description(
            """What we need to see:
- Strong Python programming
- Experience with Java, a statically typed language
- Experience deploying machine learning systems in production
"""
        )

        for skill in ("python", "java", "machine learning"):
            self.assertIn(skill, job.skills_required)
        self.assertEqual(job.job_family, "")

    def test_what_we_need_to_see_heading_is_case_insensitive(self) -> None:
        job = self.parse_description("wHaT We NeEd To SeE\n- SQL proficiency")

        self.assertIn("sql", job.skills_required)

    def test_preferred_heading_ends_what_we_need_to_see_section(self) -> None:
        for heading in ("Ways to stand out:", "Ways to stand out from the crowd:"):
            with self.subTest(heading=heading):
                job = self.parse_description(
                    f"""What we need to see:
- Python

{heading}
- Kubernetes

Preferred qualifications:
- Spark

Nice to have:
- Docker
"""
                )

                self.assertIn("python", job.skills_required)
                for skill in ("kubernetes", "spark", "docker"):
                    self.assertIn(skill, job.preferred_skills)
                    self.assertNotIn(skill, job.skills_required)

    def test_what_we_need_to_see_in_prose_is_not_a_heading(self) -> None:
        job = self.parse_description(
            """Job Description
What we need to see in successful candidates is broad Python exposure.
- Kubernetes supports the platform.
"""
        )

        self.assertEqual(job.skills_required, [])

    def parse_description(self, description: str, *, title: str = "Analyst"):
        return self.parser.parse(
            RawJob(
                title=title,
                company="Example Co",
                location="New York, NY",
                source="manual",
                discovered_date="2026-08-10",
                job_description=description,
            )
        )

    def test_master_data_terms_do_not_create_masters_degree(self) -> None:
        job = self.parse_description(
            """Qualifications
- Bachelor's degree in a quantitative field.
- Maintain master data, Masterdata, and master-data records.
"""
        )

        self.assertEqual(job.education_required, ["bachelor"])

        degree_job = self.parse_description(
            "Qualifications\n- A Master of Science or M.S. degree is required."
        )
        self.assertEqual(degree_job.education_required, ["master"])

    def test_plus_classifies_bi_tools_as_preferred_not_required(self) -> None:
        job = self.parse_description(
            """Qualifications
- Power BI, Tableau, or QlikView is a plus.
"""
        )

        for skill in ("powerbi", "tableau", "qlikview"):
            self.assertIn(skill, job.preferred_skills)
            self.assertNotIn(skill, job.skills_required)

    def test_preferred_marker_overrides_required_section_context(self) -> None:
        job = self.parse_description(
            """Requirements
- SQL is required.
- Tableau preferred.
"""
        )

        self.assertIn("sql", job.skills_required)
        self.assertIn("tableau", job.preferred_skills)
        self.assertNotIn("tableau", job.skills_required)

    def test_advanced_excel_required_preserves_advanced_requirement(self) -> None:
        job = self.parse_description(
            """Qualifications
- Advanced Excel skills required, including pivot tables and complex formulas.
"""
        )

        self.assertIn("advanced excel", job.skills_required)
        self.assertNotIn("excel", job.skills_required)

    def test_advanced_excel_context_variants_preserve_proficiency(self) -> None:
        phrases = [
            "Advanced Excel skills required",
            "Microsoft Excel (advanced skills required)",
            "Excel (advanced)",
            "Advanced proficiency in Excel is required",
            "Advanced proficiency in Microsoft Excel is required",
            "Advanced Microsoft Excel proficiency required",
            "Excel - advanced proficiency required",
        ]

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                job = self.parse_description(f"Qualifications\n- {phrase}.")
                self.assertIn("advanced excel", job.skills_required)
                self.assertNotIn("excel", job.skills_required)

    def test_collaboration_counterparties_are_not_required_skills(self) -> None:
        job = self.parse_description(
            """Job Responsibilities
- Work with Supply Chain, Marketing, and Sales teams.
- Partner with Finance and Operations.
- Collaborate with HR and Legal.
- Work closely with Engineering.
- Support Sales and Marketing teams.
"""
        )

        for function in ("supply chain", "marketing", "operations"):
            self.assertNotIn(function, job.skills_required)

    def test_finance_and_legal_collaboration_does_not_create_requirements(self) -> None:
        job = self.parse_description(
            "Job Responsibilities\n- Collaborate closely with Finance and Legal."
        )

        self.assertEqual(job.skills_required, [])

    def test_supply_chain_experience_remains_a_legitimate_requirement(self) -> None:
        job = self.parse_description(
            "Qualifications\n- 3+ years of supply chain planning experience required."
        )

        self.assertIn("supply chain", job.skills_required)

    def test_marketing_analytics_experience_remains_a_legitimate_requirement(self) -> None:
        job = self.parse_description(
            "Qualifications\n- Marketing analytics experience required."
        )

        self.assertIn("marketing", job.skills_required)

    def test_forecasting_domain_requirements_are_retained(self) -> None:
        job = self.parse_description(
            """Job Responsibilities
- Develop forecasting reports and maintain demand planning master data across the supply chain.
""",
            title="Forecasting Analyst",
        )

        for requirement in ("forecasting", "demand planning", "master data", "supply chain"):
            self.assertIn(requirement, job.skills_required)

    def test_analytical_forecasting_role_is_not_classified_as_marketing(self) -> None:
        job = self.parse_description(
            """Company Description
We are a consumer brand supported by marketing and sales teams.

Main Job Objective
Provide forecasting reports and data analysis for demand planning decisions.

Job Responsibilities
- Build business intelligence reporting and forecasting dashboards.
- Share demand planning results with Supply Chain, Marketing, and Sales.
""",
            title="Analyst, Global Forecasting",
        )

        self.assertEqual(job.job_family, "data analytics")

    def test_incidental_qualification_and_benefit_terms_do_not_create_industries(self) -> None:
        job = self.parse_description(
            """Company Description
We are a global consumer products organization.

Qualifications
- Bachelor's degree in Finance, Statistics, or Supply Chain Management.
- Experience with forecasting software and retailer-level reporting.

Benefits
- Medical coverage and technology discounts.
""",
            title="Forecasting Analyst",
        )

        self.assertEqual(job.industries, [])

    def test_recognizes_explicit_title_seniority_signals(self) -> None:
        cases = [
            ("Software Engineering Intern", "intern"),
            ("Entry-Level Data Analyst", "entry"),
            ("Junior Data Analyst", "junior"),
            ("Associate Data Analyst", "associate"),
            ("Mid-Level Data Analyst", "mid"),
            ("Senior Data Analyst", "senior"),
            ("Sr Machine Learning Engineer - Ads Personalization", "senior"),
            ("Lead Data Scientist", "lead"),
            ("Staff Machine Learning Engineer", "staff"),
            ("Principal Data Scientist", "principal"),
            ("Analytics Manager", "manager"),
            ("Director of Analytics", "director"),
            ("VP of Data", "vp"),
            ("Vice President of Analytics", "vp"),
            ("Executive Data Leader", "executive"),
        ]

        for title, expected_level in cases:
            with self.subTest(title=title):
                job = self.parse_description("Requirements\n- 2 years experience.", title=title)
                self.assertIsNotNone(job.seniority)
                self.assertEqual(job.seniority.level, expected_level)
                self.assertEqual(job.seniority.evidence, title)
                self.assertEqual(job.parser_evidence["seniority"], [title])
                self.assertEqual(job.to_dict()["seniority"]["level"], expected_level)

    def test_title_without_explicit_seniority_remains_unspecified(self) -> None:
        job = self.parse_description(
            "Requirements\n- 5+ years of experience.",
            title="Machine Learning Engineer",
        )

        self.assertIsNone(job.seniority)
        self.assertNotIn("seniority", job.parser_evidence)

if __name__ == "__main__":
    unittest.main()
