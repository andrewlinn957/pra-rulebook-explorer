import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.validate_reporting_graph_quality import run_quality_gate
from scripts.validate_reporting_source_evidence import validate_source_evidence
from scripts.repair_reporting_instruction_evidence import repair_instruction_evidence


SCHEMA = """
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
CREATE TABLE reporting_node_cleanup (
  node_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL,
  decision_reason TEXT
);
CREATE TABLE source_document_cleanup (
  source_id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  canonical_source_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  decision TEXT NOT NULL,
  decision_reason TEXT,
  graph_edges_rewired INTEGER NOT NULL DEFAULT 0
);
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
CREATE UNIQUE INDEX ux_graph_node_projection
  ON graph_node(source_table, source_pk)
  WHERE source_table IS NOT NULL AND source_pk IS NOT NULL;
"""


class ReportingGraphQualityGateTests(unittest.TestCase):
    def make_db(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "rulebook.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        return db

    def test_gate_fails_when_graph_node_contains_audit_cleanup_metadata(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("data_item:PRA101", "DataItem", "PRA101", json.dumps({"audit_cleanup": {"decision": "discarded"}})),
        )
        conn.commit()
        conn.close()

        result = run_quality_gate(db)

        self.assertEqual(result["status"], "fail")
        failing_ids = {check["check_id"] for check in result["checks"] if check["status"] == "fail"}
        self.assertIn("graph_nodes_no_audit_cleanup", failing_ids)

    def test_gate_fails_when_an_edge_targets_its_source(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)",
            ("self-edge", "source_document:a", "source_document:a", "REFERENCES_SOURCE"),
        )
        conn.commit()
        conn.close()

        result = run_quality_gate(db)

        self.assertEqual(result["status"], "fail")
        by_id = {check["check_id"]: check for check in result["checks"]}
        self.assertEqual(by_id["graph_edges_have_distinct_endpoints"]["row_count"], 1)

    def test_gate_fails_when_edges_still_point_to_duplicate_source_nodes(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("data_item:PRA101", "DataItem", "PRA101", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("source_document:dup", "SourceDocument", "Duplicate", "{}"),
        )
        conn.execute(
            "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
            ("dup", "policy_html", "canonical", "url:https://example.test/a", "duplicate_rewired"),
        )
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)",
            ("e1", "data_item:PRA101", "source_document:dup", "EVIDENCED_BY"),
        )
        conn.commit()
        conn.close()

        result = run_quality_gate(db)

        self.assertEqual(result["status"], "fail")
        by_id = {check["check_id"]: check for check in result["checks"]}
        self.assertEqual(by_id["edges_do_not_point_to_duplicate_sources"]["row_count"], 1)

    def test_gate_fails_when_provision_is_retyped_as_legal_instrument(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,properties_json) VALUES (?,?,?,?,?)",
            ("provision:rr-1", "LegalInstrument", "Regulatory Reporting 1.1", "provision", "{}"),
        )
        conn.commit()
        conn.close()

        result = run_quality_gate(db)

        self.assertEqual(result["status"], "fail")
        failing_ids = {check["check_id"] for check in result["checks"] if check["status"] == "fail"}
        self.assertIn("provisions_not_legal_instruments", failing_ids)

    def test_gate_passes_when_taxonomy_children_remain_canonical(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,parent_url,checksum_sha256) VALUES (?,?,?,?,?,?)",
            ("child-a", "a.xsd", "https://example.test/pkg.zip#taxonomy/a.xsd", "xsd", "https://example.test/pkg.zip", "same"),
        )
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,parent_url,checksum_sha256) VALUES (?,?,?,?,?,?)",
            ("child-b", "b.xsd", "https://example.test/pkg.zip#taxonomy/b.xsd", "xsd", "https://example.test/pkg.zip", "same"),
        )
        for source_id in ("child-a", "child-b"):
            conn.execute(
                "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
                (source_id, "taxonomy_schema", source_id, f"source:{source_id}", "canonical"),
            )
        conn.commit()
        conn.close()

        result = run_quality_gate(db)

        self.assertEqual(result["status"], "pass")

    def test_gate_writes_json_report_when_report_path_is_supplied(self):
        db = self.make_db()
        report = db.parent / "quality-report.json"

        result = run_quality_gate(db, report_path=report)

        self.assertEqual(result["status"], "pass")
        self.assertFalse(
            [
                sample
                for check in result["checks"]
                for sample in check["sample_rows"]
                if sample.get("missing_table") in {"roots", "evidence"}
            ]
        )
        self.assertTrue(report.exists())
        saved = json.loads(report.read_text())
        self.assertEqual(saved["status"], "pass")

    def test_source_evidence_validator_errors_when_instruction_pdf_has_no_instruction_edge(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,local_path) VALUES (?,?,?,?,?)",
            ("annex-xxv", "Annex XXV instructions", "https://example.test/instructions.pdf", "pdf", "files/instructions.pdf"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            ("source_document:annex-xxv", "SourceDocument", "Annex XXV instructions", "source_document", "annex-xxv", "{}"),
        )
        conn.execute(
            "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
            ("annex-xxv", "instruction_pdf", "annex-xxv", "url:https://example.test/instructions.pdf", "canonical"),
        )
        conn.commit()
        conn.close()

        result = validate_source_evidence(db, root=db.parent)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["findings"][0]["source_id"], "annex-xxv")

    def test_source_evidence_validator_accepts_instruction_pdf_with_instruction_edge(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,local_path) VALUES (?,?,?,?,?)",
            ("annex-xxv", "Annex XXV instructions", "https://example.test/instructions.pdf", "pdf", "files/instructions.pdf"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            ("source_document:annex-xxv", "SourceDocument", "Annex XXV instructions", "source_document", "annex-xxv", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("instruction_set:AnnexXXV", "InstructionSet", "Annex XXV", "{}"),
        )
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type) VALUES (?,?,?,?)",
            ("e1", "instruction_set:AnnexXXV", "source_document:annex-xxv", "EVIDENCED_BY"),
        )
        conn.execute(
            "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
            ("annex-xxv", "instruction_pdf", "annex-xxv", "url:https://example.test/instructions.pdf", "canonical"),
        )
        conn.commit()
        conn.close()

        result = validate_source_evidence(db, root=db.parent)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["errors"], 0)

    def test_source_evidence_validator_does_not_warn_for_standalone_taxonomy_child_files(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,parent_url) VALUES (?,?,?,?,?)",
            ("child-xsd", "pra101.xsd", "https://example.test/pkg.zip#taxonomy/pra101.xsd", "xsd", "https://example.test/pkg.zip"),
        )
        conn.execute(
            "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
            ("child-xsd", "taxonomy_schema", "child-xsd", "source:child-xsd", "canonical"),
        )
        conn.commit()
        conn.close()

        result = validate_source_evidence(db, root=db.parent)

        self.assertEqual(result["warnings"], 0)

    def test_quality_gate_with_raw_root_fails_on_source_evidence_errors(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,local_path) VALUES (?,?,?,?,?)",
            ("annex-xxv", "Annex XXV instructions", "https://example.test/instructions.pdf", "pdf", "files/instructions.pdf"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            ("source_document:annex-xxv", "SourceDocument", "Annex XXV instructions", "source_document", "annex-xxv", "{}"),
        )
        conn.execute(
            "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
            ("annex-xxv", "instruction_pdf", "annex-xxv", "url:https://example.test/instructions.pdf", "canonical"),
        )
        conn.commit()
        conn.close()

        result = run_quality_gate(db, raw_root=db.parent, strict=True)

        self.assertEqual(result["status"], "fail")
        failing_ids = {check["check_id"] for check in result["checks"] if check["status"] == "fail"}
        self.assertIn("source_evidence_validation", failing_ids)

    def test_instruction_evidence_repair_creates_semantic_instruction_set_for_instruction_pdf(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type,local_path) VALUES (?,?,?,?,?)",
            ("annex-xxv", "Annex XXV", "https://example.test/corep-liquidity-instructions.pdf", "pdf", "files/instructions.pdf"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            ("source_document:annex-xxv", "SourceDocument", "Annex XXV", "source_document", "annex-xxv", "{}"),
        )
        conn.execute(
            "INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision) VALUES (?,?,?,?,?)",
            ("annex-xxv", "instruction_pdf", "annex-xxv", "url:https://example.test/corep-liquidity-instructions.pdf", "canonical"),
        )
        conn.commit()
        conn.close()

        result = repair_instruction_evidence(db, apply=True)

        self.assertEqual(result["edges_created"], 1)
        after = validate_source_evidence(db, root=db.parent)
        self.assertEqual(after["errors"], 0)
        conn = sqlite3.connect(db)
        self.assertTrue(
            conn.execute(
                """
                SELECT 1
                FROM graph_edge e
                JOIN graph_node i ON i.node_id=e.source_node_id
                WHERE i.node_type='InstructionSet'
                  AND e.edge_type='EVIDENCED_BY'
                  AND e.target_node_id='source_document:annex-xxv'
                """
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
