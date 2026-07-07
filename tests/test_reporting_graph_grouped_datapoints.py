import sqlite3
import unittest

from backend.app.reporting import reporting_overview_graph


class ReportingGroupedDatapointGraphTests(unittest.TestCase):
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
            """
        )
        return conn

    def test_grouped_datapoint_mode_adds_one_summary_node_per_template(self):
        conn = self.make_conn()
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('data_item:COR011','DataItem','COR011','reporting_obligation','data_item:COR011')")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('template:C72','Template','C72.00 Liquid assets','template','template:C72')")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('datapoint:1','DataPoint','Cash inflows','datapoint','datapoint:1')")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('datapoint:2','DataPoint','Cash outflows','datapoint','datapoint:2')")
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES ('e1','data_item:COR011','template:C72','USES_TEMPLATE',1,'manifest')")
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES ('e2','template:C72','datapoint:1','HAS_DATAPOINT',1,'manifest')")
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES ('e3','template:C72','datapoint:2','HAS_DATAPOINT',1,'manifest')")

        graph = reporting_overview_graph(conn, selected_return="COR011", include_datapoints=True)

        node_types = {n["node_type"] for n in graph["nodes"]}
        grouped = [n for n in graph["nodes"] if n["node_type"] == "DataPointGroup"]
        self.assertIn("DataPointGroup", node_types)
        self.assertNotIn("DataPoint", node_types)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["metadata"]["datapoint_count"], 2)
        self.assertEqual(grouped[0]["metadata"]["sample_datapoints"], ["Cash inflows", "Cash outflows"])
        self.assertEqual(graph["available_edge_types"]["SUMMARISES_DATAPOINTS"], 1)


if __name__ == "__main__":
    unittest.main()
