from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

from jobhuntbot.storage import load_dashboard_schema


REPO = Path(__file__).resolve().parents[1]


class SchemaContractTests(unittest.TestCase):
    def test_schema_has_explicit_version(self) -> None:
        schema = load_dashboard_schema()
        self.assertEqual(schema.schema_version, 1)

    def test_live_and_template_csv_headers_match_canonical_schema(self) -> None:
        schema = load_dashboard_schema()
        for table in schema.tables:
            filename = f"{table}.csv"
            for directory in (REPO / "dashboard", REPO / "templates" / "dashboard-template"):
                path = directory / filename
                with self.subTest(table=table, directory=directory.name):
                    self.assertTrue(path.exists(), path)
                    with path.open("r", encoding="utf-8-sig", newline="") as handle:
                        header = next(csv.reader(handle))
                    self.assertEqual(header, schema.columns(table))

    def test_job_description_has_one_owner(self) -> None:
        schema = load_dashboard_schema()
        self.assertIn("job_description", schema.columns("job_pool"))
        self.assertNotIn("job_description", schema.columns("application_log"))
        self.assertIn("job_id", schema.columns("application_log"))

    def test_structured_encoding_contract_is_documented(self) -> None:
        path = REPO / "schemas" / "dashboard.schema.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("sorted keys", value["format"]["structured_cell_encoding"])


if __name__ == "__main__":
    unittest.main()

