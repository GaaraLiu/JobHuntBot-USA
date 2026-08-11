from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobhuntbot.discovery_state import DiscoveryStateStore, content_fingerprint
from jobhuntbot.sources import SourceJob


def _job(description: str = "Power BI and SQL reporting.") -> SourceJob:
    return SourceJob(
        source="greenhouse",
        source_job_id="job-1",
        company="Example",
        title="Data Analyst",
        description=description,
        location="New York, NY",
        source_url="https://example.test/job-1",
    )


class DiscoveryStateTests(unittest.TestCase):
    def test_first_and_last_seen_are_persisted_without_replacing_first_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state" / "discovery_state.json"
            state = DiscoveryStateStore(path)
            job = _job()
            first = state.observe(
                job_id="job-id",
                source_job=job,
                content_fingerprint=content_fingerprint(job),
                seen_at="2026-08-10T10:00:00+00:00",
                result_exists=False,
            )
            self.assertEqual(first.status, "new")
            state.mark_analyzed(
                job_id="job-id",
                analyzed_at="2026-08-10T10:00:00+00:00",
                score=80.0,
                decision="APPLY",
                resume="data_bi",
            )
            state.save()

            state = DiscoveryStateStore(path)
            second = state.observe(
                job_id="job-id",
                source_job=job,
                content_fingerprint=content_fingerprint(job),
                seen_at="2026-08-11T10:00:00+00:00",
                result_exists=True,
            )
            self.assertEqual(second.status, "unchanged")
            self.assertFalse(second.should_analyze)
            state.save()
            record = json.loads(path.read_text(encoding="utf-8"))["jobs"]["job-id"]
            self.assertEqual(record["first_seen_at"], "2026-08-10T10:00:00+00:00")
            self.assertEqual(record["last_seen_at"], "2026-08-11T10:00:00+00:00")

    def test_changed_fingerprint_requests_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = DiscoveryStateStore(Path(temp_dir) / "state.json")
            original = _job()
            state.observe(
                job_id="job-id",
                source_job=original,
                content_fingerprint=content_fingerprint(original),
                seen_at="2026-08-10T10:00:00+00:00",
                result_exists=False,
            )
            state.mark_analyzed(
                job_id="job-id",
                analyzed_at="2026-08-10T10:00:00+00:00",
                score=70.0,
                decision="REVIEW",
                resume="data_bi",
            )
            changed = _job("Power BI, SQL, and Tableau reporting.")
            decision = state.observe(
                job_id="job-id",
                source_job=changed,
                content_fingerprint=content_fingerprint(changed),
                seen_at="2026-08-11T10:00:00+00:00",
                result_exists=True,
            )
            self.assertEqual(decision.status, "changed")
            self.assertTrue(decision.should_analyze)


if __name__ == "__main__":
    unittest.main()
