import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.cleanup_reporting_node_audit import run_cleanup


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
CREATE TABLE reporting_node_audit (
  node_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  expected_category TEXT,
  source_category TEXT,
  issue_type TEXT,
  severity TEXT,
  confidence REAL,
  finding TEXT,
  recommended_action TEXT,
  duplicate_of TEXT,
  response_json TEXT,
  error TEXT
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


class ReportingNodeAuditCleanupTests(unittest.TestCase):
    def make_db(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "rulebook.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("external_reference:1", "ExternalReference", "Regulation X", json.dumps({"existing": True})),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("source_document:dup", "SourceDocument", "Duplicate", "{}"),
        )
        conn.execute(
            """
            INSERT INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,expected_category,source_category,issue_type,severity,confidence,finding,recommended_action,duplicate_of)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("external_reference:1", "gpt-4.1-nano", "v1", "abc", "ok", "legal_reference", "source_document", "wrong_node_type", "high", 0.94, "Should be legal reference", "Reclassify", None),
        )
        conn.execute(
            """
            INSERT INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,expected_category,source_category,issue_type,severity,confidence,finding,recommended_action,duplicate_of)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("source_document:dup", "gpt-4.1-nano", "v1", "def", "ok", "source_document", "source_document", "duplicate_source", "high", 0.91, "Duplicate source", "Consolidate duplicate", "source_document:canonical"),
        )
        conn.commit()
        conn.close()
        return db

    def test_dry_run_records_proposals_without_touching_nodes(self):
        db = self.make_db()

        result = run_cleanup(db, apply=False)

        self.assertEqual(result["findings"], 2)
        self.assertEqual(result["would_mark_nodes"], 2)
        self.assertEqual(result["would_reclassify"], 1)

        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT review_status FROM graph_node WHERE node_id='external_reference:1'").fetchone()[0], "unreviewed")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reporting_node_cleanup").fetchone()[0], 2)

    def test_apply_records_decisions_without_adding_audit_metadata_to_nodes(self):
        db = self.make_db()

        result = run_cleanup(db, apply=True)

        self.assertEqual(result["decided"], 2)
        self.assertEqual(result["implemented"], 0)
        conn = sqlite3.connect(db)
        status, props = conn.execute("SELECT review_status, properties_json FROM graph_node WHERE node_id='external_reference:1'").fetchone()
        self.assertEqual(status, "unreviewed")
        parsed = json.loads(props)
        self.assertTrue(parsed["existing"])
        self.assertNotIn("audit_cleanup", parsed)
        decisions = dict(conn.execute("SELECT node_id, decision FROM reporting_node_cleanup").fetchall())
        self.assertEqual(decisions["external_reference:1"], "pending_apply")
        self.assertEqual(decisions["source_document:dup"], "discarded")

    def test_apply_reclassifications_requires_explicit_flag(self):
        db = self.make_db()

        result = run_cleanup(db, apply=True, apply_reclassifications=True)

        self.assertEqual(result["reclassified"], 1)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT node_type FROM graph_node WHERE node_id='external_reference:1'").fetchone()[0], "LegalInstrument")

    def test_apply_reclassifications_does_not_rewrite_core_reporting_nodes(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("data_item:PRA101", "DataItem", "PRA101", "{}"),
        )
        conn.execute(
            """
            INSERT INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,expected_category,source_category,issue_type,severity,confidence,finding,recommended_action,duplicate_of)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("data_item:PRA101", "gpt-4.1-nano", "v1", "ghi", "ok", "instructions_guidance_pdf", "source_document", "wrong_node_type", "high", 0.95, "Looks like instructions", "Reclassify", None),
        )
        conn.commit()
        conn.close()

        result = run_cleanup(db, apply=True, apply_reclassifications=True)

        self.assertEqual(result["reclassified"], 1)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT node_type FROM graph_node WHERE node_id='data_item:PRA101'").fetchone()[0], "DataItem")
        props = json.loads(conn.execute("SELECT properties_json FROM graph_node WHERE node_id='data_item:PRA101'").fetchone()[0])
        self.assertNotIn("audit_cleanup", props)
        self.assertEqual(conn.execute("SELECT decision FROM reporting_node_cleanup WHERE node_id='data_item:PRA101'").fetchone()[0], "discarded")

    def test_source_document_instruction_finding_repairs_missing_instruction_evidence_edge(self):
        db = self.make_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json,source_table,source_pk) VALUES (?,?,?,?,?,?)",
            ("source_document:annex-xxv", "SourceDocument", "Annex XXV", "{}", "source_document", "annex-xxv"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json) VALUES (?,?,?,?)",
            ("instruction_set:AnnexXXV", "InstructionSet", "Annex XXV instructions", "{}"),
        )
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type) VALUES (?,?,?,?)",
            ("annex-xxv", "Annex XXV", "https://example.test/corep-liquidity-instructions.pdf", "pdf"),
        )
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,properties_json,source_table,source_pk) VALUES (?,?,?,?,?,?)",
            ("source_document:annex-xxviii", "SourceDocument", "Annex XXVIII", "{}", "source_document", "annex-xxviii"),
        )
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type) VALUES (?,?,?,?)",
            ("annex-xxviii", "Annex XXVIII", "https://example.test/pillar3-securitisation-positions-instructions.pdf", "pdf"),
        )
        conn.execute(
            """
            INSERT INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,expected_category,source_category,issue_type,severity,confidence,finding,recommended_action,duplicate_of)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "source_document:annex-xxv",
                "gpt-4.1-nano",
                "v1",
                "jkl",
                "ok",
                "instructions_guidance_pdf",
                "source_document",
                "wrong_node_type",
                "high",
                0.95,
                "Annex XXV is an instructions PDF",
                "Represent as instructions guidance",
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,expected_category,source_category,issue_type,severity,confidence,finding,recommended_action,duplicate_of)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "source_document:annex-xxviii",
                "gpt-4.1-nano",
                "v1",
                "mno",
                "ok",
                "instructions_guidance_pdf",
                "source_document",
                "wrong_node_type",
                "high",
                0.95,
                "Annex XXVIII is an instructions PDF",
                "Represent as instructions guidance",
                None,
            ),
        )
        conn.commit()
        conn.close()

        result = run_cleanup(db, apply=True, apply_reclassifications=True)

        self.assertEqual(result["source_edges_repaired"], 1)
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT node_type FROM graph_node WHERE node_id='source_document:annex-xxv'").fetchone()[0],
            "SourceDocument",
        )
        self.assertTrue(
            conn.execute(
                """
                SELECT 1 FROM graph_edge
                WHERE source_node_id='instruction_set:AnnexXXV'
                  AND edge_type='EVIDENCED_BY'
                  AND target_node_id='source_document:annex-xxv'
                """
            ).fetchone()
        )
        self.assertFalse(
            conn.execute(
                """
                SELECT 1 FROM graph_edge
                WHERE source_node_id='instruction_set:AnnexXXV'
                  AND edge_type='EVIDENCED_BY'
                  AND target_node_id='source_document:annex-xxviii'
                """
            ).fetchone()
        )
        decision, reason = conn.execute(
            "SELECT decision,decision_reason FROM reporting_node_cleanup WHERE node_id='source_document:annex-xxv'"
        ).fetchone()
        self.assertEqual(decision, "implemented")
        self.assertIn("added missing EVIDENCED_BY", reason)


if __name__ == "__main__":
    unittest.main()
