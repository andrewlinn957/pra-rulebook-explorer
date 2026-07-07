import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.load_reporting_instruction_edges import load_reporting_instruction_edges


class ReportingInstructionEdgeLoaderTests(unittest.TestCase):
    def make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE graph_node(node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, label TEXT)")
        conn.execute("""
            CREATE TABLE graph_edge(
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
            )
        """)
        for node_id, node_type, label in [
            ("data_item:COR011", "DataItem", "COR011"),
            ("template:C75.01", "Template", "C75.01"),
            ("instruction_set:AnnexXXV", "InstructionSet", "Annex XXV instructions"),
        ]:
            conn.execute("INSERT INTO graph_node(node_id,node_type,label) VALUES (?,?,?)", (node_id, node_type, label))
        return conn

    def write_candidates(self, root: Path):
        path = root / "pkg" / "semantic-extraction"
        path.mkdir(parents=True)
        with (path / "graph_edges_candidate.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "source_node_id", "edge_type", "target_node_id", "evidence_span_id",
                "confidence", "extraction_method", "review_status", "explanation",
            ])
            writer.writeheader()
            writer.writerow({
                "source_node_id": "data_item:COR011",
                "edge_type": "USES_INSTRUCTIONS",
                "target_node_id": "instruction_set:AnnexXXV",
                "evidence_span_id": "span:abc",
                "confidence": "0.98",
                "extraction_method": "deterministic",
                "review_status": "accepted_candidate",
                "explanation": "Annex XXV provides instructions for liquidity reporting.",
            })
            writer.writerow({
                "source_node_id": "template:C75.01",
                "edge_type": "USES_INSTRUCTIONS",
                "target_node_id": "instruction_set:AnnexXXV",
                "evidence_span_id": "span:abc",
                "confidence": "0.93",
                "extraction_method": "deterministic",
                "review_status": "accepted_candidate",
                "explanation": "Template C75.01 uses Annex XXV instructions.",
            })
            writer.writerow({
                "source_node_id": "template:C75.01",
                "edge_type": "REPORTS_CONCEPT",
                "target_node_id": "concept:CollateralSwaps",
                "review_status": "accepted_candidate",
            })

    def test_loads_all_accepted_instruction_edges_for_returns_and_templates(self):
        conn = self.make_db()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_candidates(root)

            result = load_reporting_instruction_edges(conn, root)

        self.assertEqual(result.inserted, 2)
        rows = conn.execute(
            "SELECT source_node_id,target_node_id,edge_type,review_status FROM graph_edge ORDER BY source_node_id"
        ).fetchall()
        self.assertEqual(rows, [
            ("data_item:COR011", "instruction_set:AnnexXXV", "USES_INSTRUCTIONS", "accepted_candidate"),
            ("template:C75.01", "instruction_set:AnnexXXV", "USES_INSTRUCTIONS", "accepted_candidate"),
        ])

    def test_loader_is_idempotent(self):
        conn = self.make_db()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_candidates(root)
            first = load_reporting_instruction_edges(conn, root)
            second = load_reporting_instruction_edges(conn, root)

        self.assertEqual(first.inserted, 2)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM graph_edge").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
