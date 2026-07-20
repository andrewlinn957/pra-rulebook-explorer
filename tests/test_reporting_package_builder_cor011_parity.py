import sqlite3
import tempfile
import unittest
from pathlib import Path

import scripts.build_reporting_graph_packages as packages
import scripts.semantic_reporting_extraction as semantic


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
CREATE TABLE source_span (
  span_id TEXT PRIMARY KEY,
  source_id TEXT,
  span_type TEXT,
  page_number INTEGER,
  sheet_name TEXT,
  row_number INTEGER,
  column_number INTEGER,
  heading_path TEXT,
  anchor TEXT,
  raw_text TEXT,
  normalised_text TEXT
);
CREATE TABLE reporting_obligation (
  obligation_id TEXT PRIMARY KEY,
  data_item_code TEXT,
  title TEXT,
  domain TEXT,
  source_span_id TEXT
);
CREATE TABLE graph_node (
  node_id TEXT PRIMARY KEY,
  node_type TEXT,
  label TEXT,
  source_table TEXT,
  source_pk TEXT,
  properties_json TEXT DEFAULT '{}',
  effective_from TEXT,
  effective_to TEXT,
  review_status TEXT DEFAULT 'candidate'
);
CREATE TABLE graph_edge (
  edge_id TEXT PRIMARY KEY,
  source_node_id TEXT,
  target_node_id TEXT,
  edge_type TEXT,
  properties_json TEXT DEFAULT '{}',
  evidence_span_id TEXT,
  confidence REAL,
  extraction_method TEXT,
  review_status TEXT DEFAULT 'candidate',
  effective_from TEXT,
  effective_to TEXT
);
CREATE TABLE template (
  template_id TEXT PRIMARY KEY,
  template_code TEXT,
  title TEXT,
  annex TEXT,
  source_id TEXT
);
CREATE TABLE template_row (
  row_id TEXT PRIMARY KEY,
  template_id TEXT,
  row_code TEXT,
  row_order INTEGER,
  label TEXT,
  source_span_id TEXT
);
CREATE TABLE template_column (
  column_id TEXT PRIMARY KEY,
  template_id TEXT,
  column_code TEXT,
  column_order INTEGER,
  label TEXT,
  source_span_id TEXT
);
CREATE TABLE datapoint (
  datapoint_id TEXT PRIMARY KEY,
  template_id TEXT,
  row_id TEXT,
  column_id TEXT,
  data_type TEXT,
  concept_label TEXT,
  source_span_id TEXT
);
CREATE TABLE concept (
  concept_id TEXT PRIMARY KEY,
  concept_type TEXT,
  label TEXT,
  description TEXT
);
CREATE TABLE validation_rule (
  validation_id TEXT PRIMARY KEY,
  label TEXT,
  expression_text TEXT,
  source_id TEXT,
  source_span_id TEXT
);
CREATE TABLE provision (
  provision_id TEXT PRIMARY KEY,
  provision_label TEXT,
  text TEXT
);
"""


class Cor011PackageBuilderParityTests(unittest.TestCase):
    def setUp(self):
        (packages.ROOT / "outputs").mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=packages.ROOT / "outputs")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "rulebook.sqlite3"
        self.out = self.root / "packages"
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.executemany(
            """
            INSERT INTO source_document(source_id,title,url,local_path,file_type,checksum_sha256,source_status)
            VALUES (?,?,?,?,?,?,?)
            """,
            [
                (
                    "tmpl",
                    "Annex XXIV Reporting on Liquidity Coverage templates C 72.00 C 73.00 C 74.00 C 75.01 C 76.00",
                    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/corep-liquidity-templates.xlsx",
                    "files/corep-liquidity-templates.xlsx",
                    "xlsx",
                    "tmpl-hash",
                    "downloaded",
                ),
                (
                    "instr",
                    "Annex XXV instructions for reporting on liquidity coverage",
                    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/corep-liquidity-instructions.pdf",
                    "files/corep-liquidity-instructions.pdf",
                    "pdf",
                    "instr-hash",
                    "downloaded",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO source_span(span_id,source_id,span_type,anchor,raw_text,normalised_text)
            VALUES (?,?,?,?,?,?)
            """,
            [
                (
                    "span-tmpl",
                    "tmpl",
                    "xlsx_workbook",
                    "workbook",
                    "Annex XXIV contains templates C 72.00 C 73.00 C 74.00 C 75.01 C 76.00 for COR011",
                    "Annex XXIV contains templates C 72.00 C 73.00 C 74.00 C 75.01 C 76.00 for COR011",
                ),
                (
                    "span-instr",
                    "instr",
                    "pdf_page",
                    "page 1",
                    "Annex XXV instructions for COREP liquidity COR011",
                    "Annex XXV instructions for COREP liquidity COR011",
                ),
            ],
        )
        conn.commit()
        conn.close()
        self.old_db = packages.DB
        self.old_out = packages.OUT
        self.old_semantic_db = semantic.DB
        packages.DB = self.db
        packages.OUT = self.out
        semantic.DB = self.db

    def tearDown(self):
        packages.DB = self.old_db
        packages.OUT = self.old_out
        semantic.DB = self.old_semantic_db

    def test_general_builder_produces_mandatory_cor011_semantic_edges(self):
        builder = packages.Builder()
        builder.build()

        conn = sqlite3.connect(self.db)
        actual_edges = {
            (source, edge_type, target)
            for source, edge_type, target in conn.execute(
                "SELECT source_node_id,edge_type,target_node_id FROM graph_edge"
            )
        }
        actual_nodes = {
            (node_id, node_type)
            for node_id, node_type in conn.execute("SELECT node_id,node_type FROM graph_node")
        }

        self.assertIn(("template_set:AnnexXXIV", "TemplateSet"), actual_nodes)
        self.assertIn(("instruction_set:AnnexXXV", "InstructionSet"), actual_nodes)
        self.assertIn(("template:C75.01", "Template"), actual_nodes)
        self.assertIn(("data_item:COR011", "USES_TEMPLATE", "template_set:AnnexXXIV"), actual_edges)
        self.assertIn(("data_item:COR011", "USES_INSTRUCTIONS", "instruction_set:AnnexXXV"), actual_edges)
        self.assertIn(("template_set:AnnexXXIV", "CONTAINS", "template:C75.01"), actual_edges)
        self.assertIn(("template:C75.01", "USES_INSTRUCTIONS", "instruction_set:AnnexXXV"), actual_edges)
        self.assertTrue(
            any(
                source == "instruction_set:AnnexXXV"
                and edge_type == "EVIDENCED_BY"
                and target.startswith("source_document:")
                for source, edge_type, target in actual_edges
            )
        )
        self.assertEqual(set(packages.mandatory_cor011_edge_specs()) - actual_edges, set())

    def test_legacy_semantic_script_can_check_general_builder_parity(self):
        builder = packages.Builder()
        builder.build()

        result = semantic.check_cor011_parity()

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["missing_mandatory_edges"], [])


if __name__ == "__main__":
    unittest.main()
