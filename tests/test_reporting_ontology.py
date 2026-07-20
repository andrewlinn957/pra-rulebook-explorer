import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.app.db import configure_connection
from backend.app.migrations import apply_migrations
from scripts.project_reporting_ontology import rebuild


ROOT = Path(__file__).resolve().parents[1]


class ReportingOntologyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "ontology.sqlite3"
        raw = sqlite3.connect(path)
        raw.executescript((ROOT / "schema.sql").read_text())
        raw.close()
        self.conn = configure_connection(sqlite3.connect(path), wal=False)
        apply_migrations(self.conn)
        self.conn.execute(
            """INSERT INTO reporting_return_catalog(
                 return_id,return_code,name,description,estate,family,effective_from,
                 effective_text,source_page_url,status
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "reporting_return:pra115", "PRA115", "Step-in risk", "Step-in risk reporting.",
                "supervisory_reporting", "PRA data items", "2026-01-01", "1 January 2026",
                "https://example.test/reporting", "current",
            ),
        )
        self.conn.execute(
            """INSERT INTO reporting_artifact(
                 artifact_id,url,display_title,artifact_role,estate,file_type,sheet_names_json,
                 description,classification_method,classification_confidence
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "reporting_artifact:pra115", "https://example.test/pra115.xlsx", "PRA115 workbook",
                "template", "supervisory_reporting", "xlsx", '["Cover","SI 700.00"]',
                "PRA115 template.", "official_table", 1,
            ),
        )
        self.conn.execute(
            "INSERT INTO reporting_return_artifact(return_id,artifact_id,relationship) VALUES (?,?,?)",
            ("reporting_return:pra115", "reporting_artifact:pra115", "template"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_projection_builds_hierarchy_resources_and_components(self):
        counts = rebuild(self.conn)

        self.assertEqual(counts["regimes"], 3)
        self.assertEqual(counts["requirements"], 1)
        self.assertEqual(counts["editions"], 1)
        self.assertEqual(counts["resources"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM reporting_resource_component WHERE component_type='worksheet'").fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM reporting_resource_component WHERE component_type='logical_template'").fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM graph_node WHERE node_type='RequirementEdition'").fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM graph_edge WHERE edge_type='HAS_TEMPLATE_RESOURCE'").fetchone()
        )

    def test_resource_display_name_inherits_and_supports_overrides(self):
        rebuild(self.conn)
        inherited = self.conn.execute(
            "SELECT resolved_display_name,display_name_source FROM reporting_edition_resource_names"
        ).fetchone()
        self.assertEqual(inherited[0], "PRA115 — Step-in risk — Reporting template")
        self.assertEqual(inherited[1], "inherited_from_requirement")

        self.conn.execute(
            "UPDATE reporting_requirement SET display_name='PRA115 — Step-in risk assessment' WHERE code='PRA115'"
        )
        self.assertEqual(
            self.conn.execute("SELECT resolved_display_name FROM reporting_edition_resource_names").fetchone()[0],
            "PRA115 — Step-in risk assessment — Reporting template",
        )

        self.conn.execute("UPDATE reporting_resource SET display_name='PRA115 submission workbook'")
        overridden = self.conn.execute(
            "SELECT resolved_display_name,display_name_source FROM reporting_edition_resource_names"
        ).fetchone()
        self.assertEqual(tuple(overridden), ("PRA115 submission workbook", "resource_override"))

    def test_display_name_override_survives_projection_rebuild(self):
        rebuild(self.conn)
        self.conn.execute(
            "INSERT INTO reporting_display_name_override VALUES (?,?,?,CURRENT_TIMESTAMP)",
            ("requirement", "requirement:pra115", "PRA115 — Step-in risk assessment"),
        )
        self.conn.commit()

        rebuild(self.conn)

        self.assertEqual(
            self.conn.execute("SELECT resolved_display_name FROM reporting_requirement_names").fetchone()[0],
            "PRA115 — Step-in risk assessment",
        )
        self.assertEqual(
            self.conn.execute("SELECT resolved_display_name FROM reporting_edition_resource_names").fetchone()[0],
            "PRA115 — Step-in risk assessment — Reporting template",
        )


if __name__ == "__main__":
    unittest.main()
