import sqlite3
import unittest

from backend.app.reporting import reporting_overview_graph


class ReportingOverviewGraphTests(unittest.TestCase):
    def make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE graph_node (
              node_id TEXT PRIMARY KEY,
              node_type TEXT,
              label TEXT,
              source_table TEXT,
              source_pk TEXT,
              properties_json TEXT DEFAULT '{}',
              effective_from TEXT,
              effective_to TEXT,
              review_status TEXT
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
              review_status TEXT,
              effective_from TEXT,
              effective_to TEXT
            );
            CREATE TABLE template (
              template_id TEXT PRIMARY KEY,
              template_code TEXT NOT NULL,
              title TEXT,
              annex TEXT,
              source_id TEXT,
              effective_from TEXT,
              effective_to TEXT
            );
            CREATE TABLE source_document (
              source_id TEXT PRIMARY KEY,
              title TEXT,
              url TEXT NOT NULL,
              local_path TEXT,
              file_type TEXT,
              checksum_sha256 TEXT,
              downloaded_at TEXT,
              publication_date TEXT,
              effective_from TEXT,
              effective_to TEXT,
              parent_url TEXT,
              source_status TEXT DEFAULT 'downloaded',
              notes TEXT
            );
            """
        )
        return conn

    def add_node(self, conn, node_id, node_type, label, props="{}"):
        conn.execute(
            "INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json) VALUES (?,?,?,?,?,?)",
            (node_id, node_type, label, node_type.lower(), node_id, props),
        )

    def add_edge(self, conn, edge_id, src, tgt, edge_type, method="manifest", confidence=1):
        conn.execute(
            "INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES (?,?,?,?,?,?)",
            (edge_id, src, tgt, edge_type, confidence, method),
        )

    def test_landing_overview_shows_only_data_item_return_nodes(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:COR001", "DataItem", "COR001", '{"reporting_domain":"capital"}')
        self.add_node(conn, "template:COR001", "Template", "COR001 template")
        self.add_node(conn, "instructions:COR001", "InstructionSet", "COR001 instructions")
        self.add_node(conn, "source:COR001", "SourceDocument", "COR001 source PDF")
        self.add_node(conn, "provision:rr-1", "Provision", "Regulatory Reporting 1.1")
        self.add_edge(conn, "e1", "data_item:COR001", "template:COR001", "USES_TEMPLATE")
        self.add_edge(conn, "e2", "data_item:COR001", "instructions:COR001", "USES_INSTRUCTIONS")
        self.add_edge(conn, "e3", "data_item:COR001", "source:COR001", "EVIDENCED_BY")
        self.add_edge(conn, "e4", "source:COR001", "provision:rr-1", "REFERENCES_RULE", "reporting_llm_reference", 0.8)

        graph = reporting_overview_graph(conn)

        self.assertEqual([n["id"] for n in graph["nodes"]], ["data_item:COR001"])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["root_count"], 1)
        self.assertEqual(graph["available_edge_types"], {})

    def test_selected_return_includes_only_that_returns_children_and_references(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:COR001", "DataItem", "COR001", '{"reporting_domain":"capital"}')
        self.add_node(conn, "data_item:PRA110", "DataItem", "PRA110", '{"reporting_domain":"liquidity"}')
        self.add_node(conn, "template:COR001", "Template", "COR001 template")
        self.add_node(conn, "template:PRA110", "Template", "PRA110 template")
        self.add_node(conn, "instructions:COR001", "InstructionSet", "COR001 instructions")
        self.add_node(conn, "source:COR001", "SourceDocument", "COR001 source PDF")
        self.add_node(conn, "provision:rr-1", "Provision", "Regulatory Reporting 1.1")
        self.add_edge(conn, "e1", "data_item:COR001", "template:COR001", "USES_TEMPLATE")
        self.add_edge(conn, "e2", "data_item:PRA110", "template:PRA110", "USES_TEMPLATE")
        self.add_edge(conn, "e3", "data_item:COR001", "instructions:COR001", "USES_INSTRUCTIONS")
        self.add_edge(conn, "e4", "data_item:COR001", "source:COR001", "EVIDENCED_BY")
        self.add_edge(conn, "e5", "source:COR001", "provision:rr-1", "REFERENCES_RULE", "reporting_llm_reference", 0.8)

        graph = reporting_overview_graph(conn, selected_return="COR001")

        ids = {n["id"] for n in graph["nodes"]}
        self.assertIn("data_item:COR001", ids)
        self.assertIn("template:COR001", ids)
        self.assertIn("instructions:COR001", ids)
        self.assertIn("source:COR001", ids)
        self.assertIn("provision:rr-1", ids)
        self.assertNotIn("data_item:PRA110", ids)
        self.assertNotIn("template:PRA110", ids)
        self.assertEqual(graph["root_count"], 1)
        self.assertEqual(graph["available_edge_types"]["USES_TEMPLATE"], 1)
        self.assertEqual(graph["available_edge_types"]["REFERENCES_RULE"], 1)

    def test_selected_return_suppresses_source_document_duplicate_of_instruction_set(self):
        conn = self.make_conn()
        url = "https://www.bankofengland.co.uk/example/corep-ccr-instructions.pdf"
        self.add_node(conn, "data_item:COREP-CCR", "DataItem", "COREP-CCR")
        self.add_node(
            conn,
            "instruction_set:COREP-CCR",
            "InstructionSet",
            "COREP-CCR instruction set",
            '{"source_document_ids":["annex-xxvi"]}',
        )
        self.add_node(conn, "source_document:annex-xxvi", "SourceDocument", "Annex XXVI (PDF)")
        conn.execute("UPDATE graph_node SET source_pk=? WHERE node_id=?", ("annex-xxvi", "source_document:annex-xxvi"))
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,file_type) VALUES (?,?,?,?)",
            ("annex-xxvi", "Annex XXVI", url, "pdf"),
        )
        self.add_edge(conn, "e1", "data_item:COREP-CCR", "instruction_set:COREP-CCR", "USES_INSTRUCTIONS")
        self.add_edge(conn, "e2", "data_item:COREP-CCR", "source_document:annex-xxvi", "EVIDENCED_BY")

        graph = reporting_overview_graph(conn, selected_return="COREP-CCR")

        ids = {n["id"] for n in graph["nodes"]}
        self.assertIn("instruction_set:COREP-CCR", ids)
        self.assertNotIn("source_document:annex-xxvi", ids)
        self.assertNotIn("EVIDENCED_BY", graph["available_edge_types"])

    def test_overview_excludes_datapoints_by_default_but_can_summarise_them(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:PRA110", "DataItem", "PRA110")
        self.add_node(conn, "template:PRA110", "Template", "PRA110 template")
        self.add_node(conn, "datapoint:1", "DataPoint", "Row 1 column 1")
        self.add_edge(conn, "e1", "data_item:PRA110", "template:PRA110", "USES_TEMPLATE")
        self.add_edge(conn, "e2", "template:PRA110", "datapoint:1", "HAS_DATAPOINT")

        without = reporting_overview_graph(conn, selected_return="PRA110")
        with_dp = reporting_overview_graph(conn, selected_return="PRA110", include_datapoints=True)

        self.assertNotIn("datapoint:1", {n["id"] for n in without["nodes"]})
        self.assertIn("datapoint_group:template:PRA110", {n["id"] for n in with_dp["nodes"]})
        self.assertNotIn("datapoint:1", {n["id"] for n in with_dp["nodes"]})

    def test_selected_return_keeps_only_current_taxonomy_package_source_documents(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:PRA110", "DataItem", "PRA110")
        packages = [
            ("source:pra110-v36", "pra110.xsd", "https://www.bankofengland.co.uk/example/boe-banking-v360.zip#Banking_3.6.0/path/pra110.xsd"),
            ("source:pra110-v37", "pra110.xsd", "https://www.bankofengland.co.uk/example/boe-banking-370-hotfix.zip#boe-banking-370-hotfix/path/pra110.xsd"),
            ("source:pra110-v40", "pra110.xsd", "https://www.bankofengland.co.uk/example/boebanking400.zip#Banking_4.0.0/path/pra110.xsd"),
            ("source:pra110-label-v40", "pra110-lab-en.xml", "https://www.bankofengland.co.uk/example/boebanking400.zip#Banking_4.0.0/path/pra110-lab-en.xml"),
        ]
        for node_id, label, url in packages:
            self.add_node(conn, node_id, "SourceDocument", label)
            conn.execute(
                "INSERT INTO source_document(source_id,title,url,file_type) VALUES (?,?,?,?)",
                (node_id, label, url, "xsd" if label.endswith(".xsd") else "xml"),
            )
            self.add_edge(conn, f"edge:{node_id}", "data_item:PRA110", node_id, "EVIDENCED_BY")

        graph = reporting_overview_graph(conn, selected_return="PRA110")

        ids = {n["id"] for n in graph["nodes"]}
        self.assertIn("source:pra110-v40", ids)
        self.assertIn("source:pra110-label-v40", ids)
        self.assertNotIn("source:pra110-v36", ids)
        self.assertNotIn("source:pra110-v37", ids)

    def test_selected_return_keeps_only_current_pra110_q_and_a_source_document(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:PRA110", "DataItem", "PRA110")
        versions = [
            ("source:qna-unversioned", "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/pra110-reporting-templates-and-instructions-q-and-as.pdf"),
            ("source:qna-v4", "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/pra110-reporting-templates-and-instructions-q-and-as-v4.pdf"),
            ("source:qna-v6", "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/pra110-reporting-templates-and-instructions-q-and-as-v6.pdf"),
            ("source:qna-v7", "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/pra110-reporting-templates-and-instructions-q-and-as-v7.pdf"),
        ]
        for node_id, url in versions:
            self.add_node(conn, node_id, "SourceDocument", "PRA110 reporting template and instructions: Q&As")
            conn.execute(
                "INSERT INTO source_document(source_id,title,url,file_type) VALUES (?,?,?,?)",
                (node_id, "PRA110 reporting template and instructions: Q&As", url, "pdf"),
            )
            self.add_edge(conn, f"edge:{node_id}", "data_item:PRA110", node_id, "EVIDENCED_BY")

        graph = reporting_overview_graph(conn, selected_return="PRA110")

        ids = {n["id"] for n in graph["nodes"]}
        self.assertIn("source:qna-v7", ids)
        self.assertNotIn("source:qna-unversioned", ids)
        self.assertNotIn("source:qna-v4", ids)
        self.assertNotIn("source:qna-v6", ids)
        self.assertEqual([e["to_node_id"] for e in graph["edges"] if e["edge_type"] == "EVIDENCED_BY"], ["source:qna-v7"])

    def test_selected_return_template_nodes_include_source_template_links(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:COR011", "DataItem", "COR011")
        self.add_node(conn, "template:C75.01", "Template", "C75.01 Collateral swaps")
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,local_path,file_type,parent_url) VALUES (?,?,?,?,?,?)",
            (
                "source:corep-liquidity",
                "Annex XXIV",
                "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/corep-liquidity.xlsx",
                "backend/data/raw/reporting-sources/cor011-lcr-final/files/corep-liquidity.xlsx",
                "xlsx",
                "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/regulatory-reporting-banking-sector/banks-building-societies-and-investment-firms",
            ),
        )
        conn.execute(
            "INSERT INTO template(template_id,template_code,title,annex,source_id) VALUES (?,?,?,?,?)",
            ("template:C75.01", "C75.01", "Collateral swaps", "Annex XXIV", "source:corep-liquidity"),
        )
        self.add_edge(conn, "e1", "data_item:COR011", "template:C75.01", "USES_TEMPLATE")

        graph = reporting_overview_graph(conn, selected_return="COR011")

        template = next(n for n in graph["nodes"] if n["id"] == "template:C75.01")
        self.assertEqual(template["url"], "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/corep-liquidity.xlsx")
        self.assertEqual(template["metadata"]["source_url"], template["url"])
        self.assertEqual(template["metadata"]["source_file_type"], "xlsx")
        self.assertEqual(template["metadata"]["source_local_path"], "backend/data/raw/reporting-sources/cor011-lcr-final/files/corep-liquidity.xlsx")

    def test_template_nodes_fall_back_to_properties_source_id_for_source_metadata(self):
        conn = self.make_conn()
        self.add_node(conn, "data_item:FINREP", "DataItem", "FINREP")
        self.add_node(conn, "template:FINREP:FINREP_42", "Template", "FINREP 42", '{"source_id":"source:finrep-national"}')
        conn.execute(
            "INSERT INTO source_document(source_id,title,url,local_path,file_type,parent_url) VALUES (?,?,?,?,?,?)",
            (
                "source:finrep-national",
                "Annex IV",
                "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/finrep-national-accounting-framework.xlsx",
                "backend/data/raw/reporting-sources/banking-reporting-all/files/finrep-national-accounting-framework.xlsx",
                "xlsx",
                "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/regulatory-reporting-banking-sector/banks-building-societies-and-investment-firms",
            ),
        )
        self.add_edge(conn, "e1", "data_item:FINREP", "template:FINREP:FINREP_42", "USES_TEMPLATE")

        graph = reporting_overview_graph(conn, selected_return="FINREP")

        template = next(n for n in graph["nodes"] if n["id"] == "template:FINREP:FINREP_42")
        self.assertEqual(template["metadata"]["source_title"], "Annex IV")
        self.assertEqual(template["metadata"]["source_url"], "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/regulatory-reporting/banking/finrep-national-accounting-framework.xlsx")
        self.assertEqual(template["metadata"]["source_file_type"], "xlsx")

    def test_template_nodes_include_template_enrichment_when_available(self):
        conn = self.make_conn()
        conn.execute(
            """
            CREATE TABLE reporting_template_enrichment (
              template_id TEXT PRIMARY KEY,
              model TEXT,
              prompt_version TEXT,
              input_hash TEXT,
              status TEXT,
              purpose TEXT,
              contents TEXT,
              summary TEXT,
              key_rows_json TEXT,
              quality_notes TEXT,
              response_json TEXT,
              updated_at TEXT
            )
            """
        )
        self.add_node(conn, "data_item:FINREP", "DataItem", "FINREP")
        self.add_node(conn, "template:FINREP:FINREP_1.1", "Template", "FINREP 1.1")
        conn.execute(
            """
            INSERT INTO reporting_template_enrichment(template_id,model,prompt_version,input_hash,status,purpose,contents,summary,key_rows_json,quality_notes,response_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "template:FINREP:FINREP_1.1",
                "gpt-4.1-nano",
                "reporting-template-enrichment-v1",
                "abc123",
                "ok",
                "Captures balance sheet financial assets by accounting portfolio.",
                "Breaks assets into cash, loans, debt securities and equity instruments.",
                "FINREP 1.1 explains the asset side of the statement of financial position.",
                '["Cash and cash balances", "Financial assets held for trading"]',
                "Uses workbook row labels only.",
                "{}",
                "2026-07-10T10:00:00Z",
            ),
        )
        self.add_edge(conn, "e1", "data_item:FINREP", "template:FINREP:FINREP_1.1", "USES_TEMPLATE")

        graph = reporting_overview_graph(conn, selected_return="FINREP")

        template = next(n for n in graph["nodes"] if n["id"] == "template:FINREP:FINREP_1.1")
        self.assertEqual(template["metadata"]["template_contents"], "Breaks assets into cash, loans, debt securities and equity instruments.")
        self.assertEqual(template["metadata"]["template_purpose"], "Captures balance sheet financial assets by accounting portfolio.")
        self.assertEqual(template["metadata"]["template_summary"], "FINREP 1.1 explains the asset side of the statement of financial position.")
        self.assertEqual(template["metadata"]["template_key_rows"], ["Cash and cash balances", "Financial assets held for trading"])


if __name__ == "__main__":
    unittest.main()
