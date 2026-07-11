import sqlite3
import unittest

from scripts.enrich_reporting_templates import (
    batch_request_line,
    build_fallback_enrichment,
    collect_template_context,
    ensure_schema,
    store_enrichment,
)


class ReportingTemplateEnrichmentTests(unittest.TestCase):
    def make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE template (
              template_id TEXT PRIMARY KEY,
              template_code TEXT NOT NULL,
              title TEXT,
              annex TEXT,
              source_id TEXT
            );
            CREATE TABLE source_document (
              source_id TEXT PRIMARY KEY,
              title TEXT,
              url TEXT NOT NULL,
              local_path TEXT,
              file_type TEXT
            );
            CREATE TABLE template_row (
              row_id TEXT PRIMARY KEY,
              template_id TEXT NOT NULL,
              row_code TEXT,
              row_order INTEGER,
              label TEXT
            );
            CREATE TABLE template_column (
              column_id TEXT PRIMARY KEY,
              template_id TEXT NOT NULL,
              column_code TEXT,
              column_order INTEGER,
              label TEXT
            );
            CREATE TABLE datapoint (
              datapoint_id TEXT PRIMARY KEY,
              template_id TEXT NOT NULL,
              concept_label TEXT
            );
            CREATE TABLE graph_node (
              node_id TEXT PRIMARY KEY,
              node_type TEXT,
              label TEXT,
              source_table TEXT,
              source_pk TEXT,
              properties_json TEXT DEFAULT '{}'
            );
            """
        )
        return conn

    def test_collect_template_context_includes_rows_columns_and_source(self):
        conn = self.make_conn()
        conn.execute("INSERT INTO source_document(source_id,title,url,local_path,file_type) VALUES (?,?,?,?,?)", ("source:finrep", "Annex III", "https://example.test/finrep.xlsx", "files/finrep.xlsx", "xlsx"))
        conn.execute("INSERT INTO template(template_id,template_code,title,annex,source_id) VALUES (?,?,?,?,?)", ("template:FINREP:1.1", "FINREP", "FINREP 1.1 Balance sheet assets", "Annex III", "source:finrep"))
        conn.execute("INSERT INTO template_row(row_id,template_id,row_code,row_order,label) VALUES (?,?,?,?,?)", ("row:1", "template:FINREP:1.1", "0010", 10, "Cash, cash balances at central banks and other demand deposits"))
        conn.execute("INSERT INTO template_column(column_id,template_id,column_code,column_order,label) VALUES (?,?,?,?,?)", ("col:1", "template:FINREP:1.1", "010", 10, "Carrying amount"))
        conn.execute("INSERT INTO datapoint(datapoint_id,template_id,concept_label) VALUES (?,?,?)", ("dp:1", "template:FINREP:1.1", "Financial assets held for trading"))

        context = collect_template_context(conn, "template:FINREP:1.1")

        self.assertEqual(context["template_id"], "template:FINREP:1.1")
        self.assertEqual(context["source"]["file_type"], "xlsx")
        self.assertIn("Cash, cash balances", context["rows"][0]["label"])
        self.assertEqual(context["columns"][0]["label"], "Carrying amount")
        self.assertEqual(context["datapoint_labels"], ["Financial assets held for trading"])

    def test_collect_template_context_falls_back_to_graph_template_nodes(self):
        conn = self.make_conn()
        conn.execute("INSERT INTO source_document(source_id,title,url,local_path,file_type) VALUES (?,?,?,?,?)", ("source:finrep", "Annex IV", "https://example.test/finrep.xlsx", "files/finrep.xlsx", "xlsx"))
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            ("template:FINREP:FINREP_1.1", "Template", "FINREP 1.1", "template", "template:FINREP:FINREP_1.1", '{"source_id":"source:finrep","data_item_code":"FINREP"}'),
        )

        context = collect_template_context(conn, "template:FINREP:FINREP_1.1")

        self.assertEqual(context["template_code"], "FINREP")
        self.assertEqual(context["title"], "FINREP 1.1")
        self.assertEqual(context["source"]["title"], "Annex IV")
        self.assertEqual(context["source"]["file_type"], "xlsx")

    def test_fallback_enrichment_produces_user_facing_template_description(self):
        context = {
            "template_id": "template:FINREP:1.1",
            "template_code": "FINREP",
            "title": "FINREP 1.1 Balance sheet assets",
            "annex": "Annex III",
            "rows": [{"code": "0010", "label": "Cash, cash balances at central banks and other demand deposits"}],
            "columns": [{"code": "010", "label": "Carrying amount"}],
            "datapoint_labels": ["Financial assets held for trading"],
            "source": {"title": "Annex III", "url": "https://example.test/finrep.xlsx"},
            "workbook_index": {"number": "1.1", "code": "F 01.01", "name": "Balance Sheet Statement: assets", "group": "Balance Sheet Statement [Statement of Financial Position]"},
        }

        result = build_fallback_enrichment(context)

        self.assertIn("Balance Sheet Statement: assets", result["summary"])
        self.assertIn("Cash, cash balances", result["contents"])
        self.assertEqual(result["key_rows"], ["Cash, cash balances at central banks and other demand deposits"])
        self.assertIn("within FINREP", result["purpose"])

    def test_batch_request_line_uses_openai_batch_chat_completion_shape(self):
        context = {
            "template_id": "template:FINREP:1.1",
            "template_code": "FINREP",
            "title": "FINREP 1.1 Balance sheet assets",
            "annex": "Annex III",
            "source": {"title": "Annex III"},
            "rows": [{"code": "0010", "label": "Cash and balances"}],
            "columns": [],
            "datapoint_labels": [],
        }

        line = batch_request_line("template:FINREP:1.1", context, model="gpt-4.1-nano")

        self.assertEqual(line["custom_id"], "template:FINREP:1.1")
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["url"], "/v1/chat/completions")
        self.assertEqual(line["body"]["model"], "gpt-4.1-nano")
        self.assertEqual(line["body"]["response_format"], {"type": "json_object"})

    def test_store_enrichment_is_idempotent(self):
        conn = self.make_conn()
        ensure_schema(conn)
        store_enrichment(
            conn,
            template_id="template:FINREP:1.1",
            model="fallback",
            prompt_version="test-v1",
            input_hash="abc",
            status="ok",
            enrichment={"purpose": "Purpose", "contents": "Contents", "summary": "Summary", "key_rows": ["Row A"], "quality_notes": "Notes"},
            response={"source": "test"},
            error="",
        )
        store_enrichment(
            conn,
            template_id="template:FINREP:1.1",
            model="fallback",
            prompt_version="test-v1",
            input_hash="def",
            status="ok",
            enrichment={"purpose": "New", "contents": "New contents", "summary": "New summary", "key_rows": [], "quality_notes": "New notes"},
            response={"source": "test2"},
            error="",
        )

        row = conn.execute("SELECT input_hash,purpose,contents FROM reporting_template_enrichment WHERE template_id=?", ("template:FINREP:1.1",)).fetchone()
        self.assertEqual(dict(row), {"input_hash": "def", "purpose": "New", "contents": "New contents"})


if __name__ == "__main__":
    unittest.main()
