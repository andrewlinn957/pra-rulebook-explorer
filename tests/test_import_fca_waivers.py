import importlib.util
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import_fca_waivers.py"
spec = importlib.util.spec_from_file_location("import_fca_waivers", SCRIPT_PATH)
import_fca_waivers = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(import_fca_waivers)


class ImportFcaWaiversTests(unittest.TestCase):
    def make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT NOT NULL,
              stable_key TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT NOT NULL,
              to_node_id TEXT NOT NULL,
              edge_type TEXT NOT NULL,
              source_method TEXT NOT NULL,
              confidence REAL NOT NULL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        return conn

    def test_one_waiver_record_linked_to_multiple_rules_creates_one_permission_node(self):
        conn = self.make_conn()
        records = [
            {
                "FRN": "400481",
                "Organisation Name": "Example UK Branch",
                "Waiver Ref": "00009831.pdf",
                "Rule Handbook: Rule Ref": "Reporting - SII",
                "Sub Rule Number": "Ru 2.2(1), 2.5A, 2.5B",
                "Waiver Status": "Completed Approved",
                "Start Date": "31/12/2024",
                "End Date": "",
            }
        ]

        with patch.object(import_fca_waivers, "load_graph_indexes", return_value={}), patch.object(
            import_fca_waivers, "resolve_targets", return_value=(["rule-2-2", "rule-2-5a", "rule-2-5b"], "mapped_to_rule")
        ):
            summary = import_fca_waivers.import_permissions(conn, records)

        self.assertEqual(summary["permission_nodes"], 1)
        self.assertEqual(summary["has_permission_edges"], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM node WHERE node_type='permission'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edge WHERE edge_type='has_permission'").fetchone()[0], 3)

        stable_key = conn.execute("SELECT stable_key FROM node WHERE node_type='permission'").fetchone()[0]
        self.assertNotIn("rule-2-2", stable_key)
        self.assertNotIn("rule-2-5a", stable_key)
        self.assertNotIn("rule-2-5b", stable_key)


if __name__ == "__main__":
    unittest.main()
