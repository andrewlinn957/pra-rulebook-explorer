import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.inspect_reporting_sources import inspect_reporting_sources
from scripts.source_document_cleanup import classify_source_document, run_source_cleanup


SCHEMA = """
CREATE TABLE source_document (
  source_id TEXT PRIMARY KEY,
  title TEXT,
  url TEXT,
  local_path TEXT,
  file_type TEXT,
  checksum_sha256 TEXT,
  downloaded_at TEXT,
  publication_date TEXT,
  effective_from TEXT,
  effective_to TEXT,
  parent_url TEXT,
  source_status TEXT,
  notes TEXT
);
CREATE TABLE graph_node (
  node_id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  label TEXT,
  source_table TEXT,
  source_pk TEXT,
  properties_json TEXT,
  effective_from TEXT,
  effective_to TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE TABLE graph_edge (
  edge_id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  properties_json TEXT,
  evidence_span_id TEXT,
  confidence REAL,
  extraction_method TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  effective_from TEXT,
  effective_to TEXT
);
"""


class SourceDocumentCleanupTests(unittest.TestCase):
    def make_db(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "rulebook.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        return db

    def add_source(self, conn, source_id, title, url, file_type, checksum="", local_path=""):
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,checksum_sha256,local_path) VALUES (?,?,?,?,?,?)",
            (source_id, title, url, file_type, checksum, local_path),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            (f"source_document:{source_id}", "SourceDocument", title, "source_document", source_id, "{}"),
        )

    def test_classifier_distinguishes_instructions_templates_and_taxonomy(self):
        self.assertEqual(
            classify_source_document({"title": "Annex XXV", "url": "https://example.test/corep-liquidity-instructions.pdf", "file_type": "pdf", "local_path": ""})[0],
            "instruction_pdf",
        )
        self.assertEqual(
            classify_source_document({"title": "Appendix 3: PRA101 template", "url": "https://example.test/pra101-template.pdf", "file_type": "pdf", "local_path": ""})[0],
            "template_pdf",
        )
        self.assertEqual(
            classify_source_document({"title": "Statement of Policy - Pillar 2 liquidity", "url": "https://example.test/pillar-2-liquidity-sop-update.pdf", "file_type": "pdf", "local_path": ""})[0],
            "policy_pdf",
        )
        self.assertEqual(
            classify_source_document({"title": "PRA101", "url": "https://example.test/pra101.xlsx", "file_type": "xlsx", "local_path": ""})[0],
            "template_workbook",
        )
        self.assertEqual(
            classify_source_document({"title": "pra101-lab-en.xml", "url": "https://example.test/pra101-lab-en.xml", "file_type": "xml", "local_path": ""})[0],
            "taxonomy_xml",
        )

    def test_exact_url_duplicate_rewires_graph_edges_to_canonical_source_node(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        self.add_source(conn, "canonical", "Regulatory Reporting", "https://www.prarulebook.co.uk/pra-rules/regulatory-reporting", "html")
        self.add_source(conn, "variant", "Regulatory Reporting", "https://www.prarulebook.co.uk/pra-rules/regulatory-reporting?download=1", "html")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)", ("data_item:PRA001", "DataItem", "PRA001", "{}"))
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)", ("e1", "data_item:PRA001", "source_document:variant", "EVIDENCED_BY"))
        conn.commit(); conn.close()

        result = run_source_cleanup(db, apply=True)

        self.assertEqual(result["duplicates_rewired"], 1)
        conn = sqlite3.connect(db)
        self.assertTrue(conn.execute("SELECT 1 FROM graph_edge WHERE source_node_id='data_item:PRA001' AND target_node_id='source_document:canonical'").fetchone())
        self.assertFalse(conn.execute("SELECT 1 FROM graph_edge WHERE source_node_id='data_item:PRA001' AND target_node_id='source_document:variant'").fetchone())
        self.assertFalse(conn.execute("SELECT 1 FROM graph_node WHERE node_id='source_document:variant'").fetchone())
        self.assertEqual(conn.execute("SELECT decision FROM source_document_cleanup WHERE source_id='variant'").fetchone()[0], "duplicate_rewired")

        second = run_source_cleanup(db, apply=True)
        self.assertEqual(second["duplicates_rewired"], 0)
        conn.close()
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT graph_edges_rewired FROM source_document_cleanup WHERE source_id='variant'").fetchone()[0], 1)

    def test_dry_run_reports_candidates_without_mutating_cleanup_or_graph(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        self.add_source(conn, "canonical", "Instructions", "https://example.test/instructions.pdf", "pdf")
        self.add_source(conn, "variant", "Instructions", "https://example.test/instructions.pdf?download=1", "pdf")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)", ("data_item:PRA001", "DataItem", "PRA001", "{}"))
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)", ("e1", "data_item:PRA001", "source_document:variant", "EVIDENCED_BY"))
        conn.commit()
        conn.close()

        result = run_source_cleanup(db, apply=False)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["duplicate_candidates"], 1)
        self.assertEqual(result["by_decision"]["duplicate_candidate"], 1)
        conn = sqlite3.connect(db)
        self.assertFalse(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_document_cleanup'"
            ).fetchone()
        )
        self.assertTrue(
            conn.execute(
                "SELECT 1 FROM graph_edge WHERE target_node_id='source_document:variant'"
            ).fetchone()
        )
        conn.close()

    def test_apply_repairs_edges_reintroduced_after_an_earlier_cleanup(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        self.add_source(conn, "canonical", "Instructions", "https://example.test/instructions.pdf", "pdf")
        self.add_source(conn, "variant", "Instructions", "https://example.test/instructions.pdf?download=1", "pdf")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)", ("data_item:PRA001", "DataItem", "PRA001", "{}"))
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)", ("e1", "data_item:PRA001", "source_document:variant", "EVIDENCED_BY"))
        conn.commit()
        conn.close()
        run_source_cleanup(db, apply=True)

        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            ("source_document:variant", "SourceDocument", "Instructions", "source_document", "variant", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)",
            ("late-edge", "data_item:PRA001", "source_document:variant", "EVIDENCED_BY"),
        )
        conn.commit()
        conn.close()

        result = run_source_cleanup(db, apply=True)

        self.assertEqual(result["duplicates_rewired"], 1)
        conn = sqlite3.connect(db)
        self.assertFalse(
            conn.execute(
                "SELECT 1 FROM graph_edge WHERE target_node_id='source_document:variant'"
            ).fetchone()
        )
        self.assertTrue(
            conn.execute(
                "SELECT 1 FROM graph_edge WHERE edge_id='late-edge' AND target_node_id='source_document:canonical'"
            ).fetchone()
        )
        conn.close()

    def test_rewire_preserves_parallel_edges_with_distinct_evidence(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        self.add_source(conn, "canonical", "Instructions", "https://example.test/instructions.pdf", "pdf")
        self.add_source(conn, "variant", "Instructions", "https://example.test/instructions.pdf?download=1", "pdf")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)", ("data_item:PRA001", "DataItem", "PRA001", "{}"))
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,evidence_span_id) VALUES (?,?,?,?,?)",
            ("canonical-evidence", "data_item:PRA001", "source_document:canonical", "EVIDENCED_BY", "span:old"),
        )
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,evidence_span_id) VALUES (?,?,?,?,?)",
            ("variant-evidence", "data_item:PRA001", "source_document:variant", "EVIDENCED_BY", "span:new"),
        )
        conn.commit()
        conn.close()

        run_source_cleanup(db, apply=True)

        conn = sqlite3.connect(db)
        rows = conn.execute(
            """
            SELECT edge_id,evidence_span_id FROM graph_edge
            WHERE source_node_id='data_item:PRA001'
              AND target_node_id='source_document:canonical'
              AND edge_type='EVIDENCED_BY'
            ORDER BY edge_id
            """
        ).fetchall()
        self.assertEqual(rows, [("canonical-evidence", "span:old"), ("variant-evidence", "span:new")])
        conn.close()

    def test_checksum_dedupe_ignores_taxonomy_xml_even_when_hashes_match(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        self.add_source(conn, "a", "schema a", "https://example.test/a.xml", "xml", checksum="same")
        self.add_source(conn, "b", "schema b", "https://example.test/b.xml", "xml", checksum="same")
        conn.commit(); conn.close()

        result = run_source_cleanup(db, apply=True)

        self.assertEqual(result["duplicates_rewired"], 0)
        conn = sqlite3.connect(db)
        decisions = dict(conn.execute("SELECT source_id,decision FROM source_document_cleanup").fetchall())
        self.assertEqual(decisions["a"], "canonical")
        self.assertEqual(decisions["b"], "canonical")

    def test_source_inspection_records_workbook_sheet_names_as_internal_hints(self):
        db = self.make_db()
        root = db.parent
        workbook = root / "files" / "pra101.xlsx"
        workbook.parent.mkdir()
        with zipfile.ZipFile(workbook, "w") as zf:
            zf.writestr(
                "xl/workbook.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
                <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <sheets>
                    <sheet name="PRA101" sheetId="1" r:id="rId1"/>
                    <sheet name="Instructions" sheetId="2" r:id="rId2"/>
                  </sheets>
                </workbook>
                """,
            )
        conn = sqlite3.connect(db)
        self.add_source(conn, "workbook", "PRA101 template", "https://example.test/pra101.xlsx", "xlsx", local_path="files/pra101.xlsx")
        conn.commit()
        conn.close()

        result = inspect_reporting_sources(db, root=root, apply=True)

        self.assertEqual(result["inspected"], 1)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT workbook_sheets_json,classification_hint FROM source_document_inspection WHERE source_id='workbook'"
        ).fetchone()
        self.assertEqual(json.loads(row[0]), ["PRA101", "Instructions"])
        self.assertEqual(row[1], "template_workbook")

    def test_source_inspection_records_taxonomy_zip_lineage_without_dedupe(self):
        db = self.make_db()
        root = db.parent
        package = root / "files" / "taxonomy.zip"
        package.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr("META-INF/taxonomyPackage.xml", "<taxonomyPackage><name>BoE taxonomy</name></taxonomyPackage>")
            zf.writestr("taxonomy/pra101.xsd", "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'/>")
        conn = sqlite3.connect(db)
        self.add_source(conn, "zip", "BoE taxonomy", "https://example.test/taxonomy.zip", "zip", local_path="files/taxonomy.zip")
        conn.commit()
        conn.close()

        result = inspect_reporting_sources(db, root=root, apply=True)

        self.assertEqual(result["inspected"], 1)
        conn = sqlite3.connect(db)
        manifest, lineage, hint = conn.execute(
            "SELECT taxonomy_manifest_json,lineage_json,classification_hint FROM source_document_inspection WHERE source_id='zip'"
        ).fetchone()
        self.assertIn("META-INF/taxonomyPackage.xml", json.loads(manifest)["files"])
        self.assertEqual(json.loads(lineage)["source_id"], "zip")
        self.assertEqual(hint, "taxonomy_package")

    def test_source_cleanup_uses_inspection_hint_when_metadata_is_weak(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        self.add_source(conn, "ambiguous-pdf", "Annex XXV", "https://example.test/annex-xxv.pdf", "pdf")
        conn.execute(
            """
            CREATE TABLE source_document_inspection (
              source_id TEXT PRIMARY KEY,
              inspection_method TEXT NOT NULL,
              extracted_title TEXT,
              extracted_summary TEXT,
              first_page_text TEXT,
              workbook_sheets_json TEXT,
              taxonomy_manifest_json TEXT,
              lineage_json TEXT,
              classification_hint TEXT,
              confidence REAL NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO source_document_inspection(source_id,inspection_method,classification_hint,confidence) VALUES (?,?,?,?)",
            ("ambiguous-pdf", "pypdf_first_3_pages", "instruction_pdf", 0.84),
        )
        conn.commit()
        conn.close()

        run_source_cleanup(db, apply=True)

        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT source_kind FROM source_document_cleanup WHERE source_id='ambiguous-pdf'").fetchone()[0],
            "instruction_pdf",
        )


if __name__ == "__main__":
    unittest.main()
