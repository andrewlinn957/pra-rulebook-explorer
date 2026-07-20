import sqlite3
import unittest
from pathlib import Path

from backend.app.migrations import apply_migrations
from backend.app.reporting import reporting_catalog, reporting_catalog_return
from scripts.enrich_reporting_catalog import validate_return_description


ROOT = Path(__file__).resolve().parents[1]


class ReportingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript((ROOT / "schema.sql").read_text())
        apply_migrations(self.conn)
        self.conn.execute(
            """
            INSERT INTO reporting_return_catalog(
              return_id,return_code,name,description,estate,family,source_page_url,status
            ) VALUES ('r1','PRA115','Step-in risk','Step-in risk assessment.',
                      'supervisory_reporting','PRA data items','https://example.test/catalog','current')
            """
        )
        self.conn.execute(
            """
            INSERT INTO reporting_artifact(
              artifact_id,url,display_title,artifact_role,estate,file_type,sheet_names_json,
              classification_method,classification_confidence
            ) VALUES ('a1','https://example.test/pra115.xlsx','PRA115 (XLSX)','template',
                      'supervisory_reporting','xlsx','["PRA115","Definitions"]','official_table',1)
            """
        )
        self.conn.execute(
            "INSERT INTO reporting_return_artifact(return_id,artifact_id,relationship) VALUES ('r1','a1','template')"
        )

    def tearDown(self):
        self.conn.close()

    def test_catalog_returns_human_facing_fields_and_counts(self):
        result = reporting_catalog(self.conn, estate="supervisory_reporting")
        self.assertEqual(result["counts"]["returns"], 1)
        self.assertEqual(result["returns"][0]["name"], "Step-in risk")
        self.assertEqual(result["returns"][0]["template_count"], 1)
        self.assertNotIn("updated_at", result["returns"][0])

    def test_catalog_detail_exposes_workbook_sheets_without_audit_fields(self):
        result = reporting_catalog_return(self.conn, "r1")
        self.assertEqual(result["artifacts"][0]["sheet_names"], ["PRA115", "Definitions"])
        self.assertNotIn("classification_method", result["artifacts"][0])
        self.assertEqual(result["rulebook_references"], [])

    def test_enrichment_cannot_reclassify_supervisory_return_as_pillar_3(self):
        with self.assertRaisesRegex(ValueError, "supervisory return description"):
            validate_return_description(
                "CRR Pillar 3 disclosure for reporting on own funds.",
                expected_estate="supervisory_reporting",
            )

    def test_enrichment_accepts_estate_consistent_descriptions(self):
        self.assertEqual(
            validate_return_description(
                "CRR supervisory return for reporting on own funds.",
                expected_estate="supervisory_reporting",
            ),
            "CRR supervisory return for reporting on own funds.",
        )
        self.assertEqual(
            validate_return_description(
                "Pillar 3 disclosure of own funds.",
                expected_estate="pillar3_disclosure",
            ),
            "Pillar 3 disclosure of own funds.",
        )


if __name__ == "__main__":
    unittest.main()
