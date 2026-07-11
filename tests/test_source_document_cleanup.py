import sqlite3
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
