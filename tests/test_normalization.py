from __future__ import annotations

import unittest

from jobhuntbot.models import NormalizedJob
from jobhuntbot.normalization import (
    build_job_id,
    canonicalize_url,
    normalize_skills,
    validate_normalized_job,
)


class NormalizationTests(unittest.TestCase):
    def test_url_normalization_removes_tracking_and_sorts_query(self) -> None:
        first = "HTTPS://Jobs.Example.com/opening/123/?utm_source=x&b=2&a=1#apply"
        second = "https://jobs.example.com/opening/123?a=1&b=2"
        self.assertEqual(canonicalize_url(first), canonicalize_url(second))

    def test_native_job_id_has_highest_precedence(self) -> None:
        common = {
            "source": "Example ATS",
            "company": "Example Co",
            "title": "Data Analyst",
            "location": "Boston, MA",
        }
        with_native_a = build_job_id(**common, source_native_id="ABC-123", apply_url="https://a.example/job")
        with_native_b = build_job_id(**common, source_native_id="ABC-123", apply_url="https://b.example/job")
        self.assertEqual(with_native_a, with_native_b)
        self.assertTrue(with_native_a.startswith("native_example-ats_"))

    def test_canonical_url_id_is_stable_but_real_url_change_changes_id(self) -> None:
        kwargs = {
            "source": "Company",
            "company": "Example Co",
            "title": "Analyst",
            "location": "Boston",
        }
        first = build_job_id(**kwargs, apply_url="https://example.com/jobs/1?utm_source=email")
        tracking_variant = build_job_id(**kwargs, apply_url="https://example.com/jobs/1")
        changed = build_job_id(**kwargs, apply_url="https://example.com/jobs/2")
        self.assertEqual(first, tracking_variant)
        self.assertNotEqual(first, changed)

    def test_fallback_fingerprint_is_repeatable(self) -> None:
        first = build_job_id(source="Manual", company=" Example Co ", title="DATA Analyst", location="Boston")
        second = build_job_id(source="manual", company="example co", title="data analyst", location=" Boston ")
        self.assertEqual(first, second)

    def test_skill_aliases_are_deduplicated(self) -> None:
        values = normalize_skills(["NodeJS", "node js", "Python"], {"nodejs": "node.js", "node js": "node.js"})
        self.assertEqual(values, ["node.js", "python"])

    def test_normalized_job_validation_flags_missing_contract_fields(self) -> None:
        job = NormalizedJob(
            job_id="",
            title="",
            company="",
            location="",
            source="",
            source_url="",
            apply_url="",
            job_description="",
            discovered_date="",
        )
        result = validate_normalized_job(job)
        codes = {issue.code for issue in result.errors}
        self.assertFalse(result.valid)
        self.assertIn("missing_job_id", codes)
        self.assertIn("missing_title", codes)
        self.assertIn("missing_job_description", codes)


if __name__ == "__main__":
    unittest.main()
