import sqlite3
import unittest

from backend.app.migrations import apply_migrations, schema_version


class MigrationTests(unittest.TestCase):
    def test_v1_preserves_enrichment_and_repairs_invalid_evidence(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE node(id TEXT PRIMARY KEY);
            CREATE TABLE graph_node(node_id TEXT PRIMARY KEY,node_type TEXT,label TEXT,source_table TEXT,source_pk TEXT);
            CREATE TABLE source_span(span_id TEXT PRIMARY KEY);
            CREATE TABLE graph_edge(
              edge_id TEXT PRIMARY KEY,source_node_id TEXT,target_node_id TEXT,evidence_span_id TEXT,
              FOREIGN KEY(source_node_id) REFERENCES graph_node(node_id),
              FOREIGN KEY(target_node_id) REFERENCES graph_node(node_id),
              FOREIGN KEY(evidence_span_id) REFERENCES source_span(span_id) ON DELETE SET NULL
            );
            CREATE TABLE template(template_id TEXT PRIMARY KEY);
            CREATE TABLE reporting_template_enrichment(
              template_id TEXT PRIMARY KEY,model TEXT NOT NULL,prompt_version TEXT NOT NULL,
              input_hash TEXT NOT NULL,status TEXT NOT NULL,purpose TEXT,contents TEXT,summary TEXT,
              key_rows_json TEXT NOT NULL DEFAULT '[]',quality_notes TEXT,response_json TEXT,error TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(template_id) REFERENCES template(template_id) ON DELETE CASCADE
            );
            INSERT INTO graph_node VALUES ('template:one','Template','One','template','source:one');
            INSERT INTO graph_node VALUES ('source:doc','SourceDocument','Doc','source_document','doc');
            INSERT INTO reporting_template_enrichment(template_id,model,prompt_version,input_hash,status)
              VALUES ('source:one','m','p','h','ok');
            INSERT INTO graph_edge VALUES ('e','template:one','source:doc','missing-span');
            """
        )

        self.assertEqual(apply_migrations(conn), [1, 2, 3, 4, 5, 6, 7, 8, 9])

        self.assertEqual(schema_version(conn), 9)
        self.assertEqual(
            tuple(conn.execute("SELECT template_id,graph_node_id FROM reporting_template_enrichment").fetchone()),
            ("source:one", "template:one"),
        )
        self.assertIsNone(conn.execute("SELECT evidence_span_id FROM graph_edge").fetchone()[0])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reporting_return_catalog'").fetchone()
        )
        self.assertIsNotNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_occurrence'"
            ).fetchone()
        )
        self.assertIsNotNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_snapshot'"
            ).fetchone()
        )
        self.assertIsNotNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_alias'"
            ).fetchone()
        )
        for table in ("ingestion_run", "ingestion_run_scope", "ingestion_output"):
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
            )
        self.assertEqual(apply_migrations(conn), [])
        self.assertEqual(schema_version(conn), 9)

    def test_v1_marks_search_projection_dirty_after_node_changes(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE node(id TEXT PRIMARY KEY)")
        apply_migrations(conn)
        conn.execute("UPDATE search_projection_state SET dirty=0 WHERE singleton=1")

        conn.execute("INSERT INTO node VALUES ('n')")

        self.assertEqual(conn.execute("SELECT dirty FROM search_projection_state").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
