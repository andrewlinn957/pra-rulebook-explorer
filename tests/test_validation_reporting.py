import csv
import json
import sqlite3
import unittest
from pathlib import Path

from backend.app.validation import validation_dashboard


class ValidationReportingDashboardTests(unittest.TestCase):
    def make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
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
            CREATE TABLE reporting_obligation (
              obligation_id TEXT PRIMARY KEY,
              data_item_code TEXT NOT NULL,
              title TEXT,
              domain TEXT,
              frequency TEXT,
              reporting_horizon_days INTEGER,
              effective_from TEXT,
              effective_to TEXT,
              source_span_id TEXT
            );
            CREATE TABLE template (
              template_id TEXT PRIMARY KEY,
              template_code TEXT,
              title TEXT,
              annex TEXT,
              source_id TEXT,
              effective_from TEXT,
              effective_to TEXT
            );
            CREATE TABLE datapoint (
              datapoint_id TEXT PRIMARY KEY,
              template_id TEXT,
              row_id TEXT,
              column_id TEXT,
              data_type TEXT,
              unit_type TEXT,
              concept_label TEXT,
              source_span_id TEXT
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
            """
        )
        return conn

    def test_dashboard_has_separate_reporting_section(self):
        conn = self.make_conn()
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,metadata_json) VALUES ('n1','rule','Rule','rule:1','{}')")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('DataItem:COR001','DataItem','COR001','data_item','COR001')")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('Template:t1','Template','Template 1','template','t1')")
        conn.execute("INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk) VALUES ('SourceDocument:s1','SourceDocument','Source 1','source_document','s1')")
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES ('e1','DataItem:COR001','Template:t1','USES_TEMPLATE',1,'manifest')")
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES ('e2','DataItem:COR001','SourceDocument:s1','EVIDENCED_BY',1,'manifest')")
        conn.execute("INSERT INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method) VALUES ('e3','SourceDocument:s1','n1','REFERENCES_RULE',0.85,'reporting_llm_reference')")
        conn.execute("INSERT INTO template(template_id,template_code,title) VALUES ('t1','COR001','Template 1')")
        conn.execute("INSERT INTO datapoint(datapoint_id,template_id,row_id,column_id,concept_label) VALUES ('dp1','t1','r1','c1','Capital')")
        conn.execute("INSERT INTO reporting_obligation(obligation_id,data_item_code,title) VALUES ('o1','COR001','Submit COR001')")

        result = validation_dashboard(conn)

        self.assertIn('reporting', result)
        self.assertEqual(result['reporting']['totals']['data_items'], 1)
        self.assertEqual(result['reporting']['totals']['templates'], 1)
        self.assertEqual(result['reporting']['totals']['datapoints'], 1)
        self.assertEqual(result['reporting']['totals']['obligations'], 1)
        self.assertEqual(result['reporting']['totals']['reporting_reference_edges'], 1)
        self.assertIn('checks', result['reporting'])
        self.assertTrue(any(c['check'] == 'reporting coverage' for c in result['reporting']['checks']))


class ValidationNearSelfLoopTests(unittest.TestCase):
    def test_near_self_loop_ignores_derived_similarity_edges(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,url) VALUES ('a','rule','1.1','rule:a','same text','https://example.test/a')")
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,url) VALUES ('b','rule','1.1','rule:b','same text','https://example.test/b')")
        conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES ('derived','a','b','shares_defined_term','derived_term_overlap',0.5)")

        from backend.app.validation import _near_self_loop_samples
        rows = _near_self_loop_samples(conn)

        self.assertEqual(rows, [])

    def test_near_self_loop_ignores_empty_text_title_matches(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,url) VALUES ('a','chapter','Application','chapter:a','','https://example.test/a')")
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,url) VALUES ('b','chapter','Application','chapter:b','','https://example.test/b')")
        conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES ('ref','a','b','references','rollup_child_edge',0.5)")

        from backend.app.validation import _near_self_loop_samples
        rows = _near_self_loop_samples(conn)

        self.assertEqual(rows, [])

    def test_near_self_loop_keeps_identity_like_reference_edges(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,url) VALUES ('a','chapter','Application','chapter:a','same text','https://example.test/a')")
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,url) VALUES ('b','chapter','Application','chapter:b','same text','https://example.test/b')")
        conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES ('ref','a','b','references','rollup_child_edge',0.5)")

        from backend.app.validation import _near_self_loop_samples
        rows = _near_self_loop_samples(conn)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'ref')


class ValidationUnresolvedReferenceMetricTests(unittest.TestCase):
    def test_non_reference_placeholder_is_not_reported_as_orphan(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,metadata_json) VALUES ('instrument','legal_instrument','Instrument','instrument','{}')")
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,metadata_json) VALUES ('future','rule_reference','Future part','future','{\"placeholder\":1}')")
        conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES ('amends','instrument','future','amends','legal_instrument_listing',1)")

        from backend.app.validation import _unresolved_references
        check = _unresolved_references(conn)

        self.assertEqual(check['metrics']['live_placeholder_reference_nodes'], 0)
        self.assertEqual(check['metrics']['orphan_placeholder_reference_nodes'], 0)
        self.assertEqual(check['metrics']['non_reference_placeholder_nodes'], 1)
        self.assertEqual(check['status'], 'pass')


class ValidationUnresolvedReferenceSampleTests(unittest.TestCase):
    def test_unresolved_reference_samples_include_display_id(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,metadata_json) VALUES ('source','rule','Source rule','rule:source','See Target','{}')")
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,metadata_json) VALUES ('target','rule_reference','Target rule','target','{""placeholder"":1}')")
        conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json) VALUES ('edge-123','source','target','references','regex_reference',0.8,'Target','https://example.test','{}')")

        from backend.app.validation import _unresolved_reference_samples
        rows = _unresolved_reference_samples(conn)

        self.assertEqual(rows[0]['sample_id'], 'UR-0001')
        self.assertEqual(rows[0]['edge_id'], 'edge-123')


class ValidationSuspect403Tests(unittest.TestCase):
    def test_suspect_403_samples_have_short_review_ids(self):
        from backend.app.validation import _suspect_403_reference_samples
        out = Path('outputs/broken-reference-check')
        out.mkdir(parents=True, exist_ok=True)
        path = out / 'unresolved-external-link-check.csv'
        original = path.read_text(encoding='utf-8') if path.exists() else None
        try:
            with path.open('w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, ['classification','status','reason','title','url','final_url','live_edges','target_id','stable_key'])
                w.writeheader()
                w.writerow({'classification':'suspect','status':'403','reason':'HTTP 403','title':'Blocked PDF','url':'https://example.test/a.pdf','final_url':'https://example.test/a.pdf','live_edges':'2','target_id':'target-1','stable_key':'external:x'})
            rows = _suspect_403_reference_samples()
            self.assertEqual(rows[0]['review_id'], '403-0001')
            self.assertEqual(rows[0]['target_id'], 'target-1')
        finally:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding='utf-8')

class ValidationUnresolvedReviewTests(unittest.TestCase):
    def test_unresolved_reference_samples_include_saved_review_findings(self):
        from backend.app.validation import _unresolved_reference_review_path, _unresolved_reference_samples
        path = _unresolved_reference_review_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = path.read_text(encoding='utf-8') if path.exists() else None
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT,
              title TEXT,
              stable_key TEXT,
              url TEXT,
              text TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE edge (
              id TEXT PRIMARY KEY,
              from_node_id TEXT,
              to_node_id TEXT,
              edge_type TEXT,
              source_method TEXT,
              confidence REAL,
              evidence_text TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,text,metadata_json) VALUES ('source','rule','Source rule','rule:source','See Target','{}')")
        conn.execute("INSERT INTO node(id,node_type,title,stable_key,url,metadata_json) VALUES ('target','rule_reference','Target rule','target','https://example.test/old','{""placeholder"":1}')")
        conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json) VALUES ('edge-123','source','target','references','regex_reference',0.8,'Target','https://example.test','{}')")
        try:
            path.write_text(json.dumps({'target': {'target_id':'target','edge_id':'edge-123','decision':'outdated','replacement_url':'https://example.test/new','note':'Old PDF still loads'}}), encoding='utf-8')
            rows = _unresolved_reference_samples(conn)
            self.assertEqual(rows[0]['review_decision'], 'outdated')
            self.assertEqual(rows[0]['review_replacement_url'], 'https://example.test/new')
            self.assertEqual(rows[0]['review_note'], 'Old PDF still loads')
        finally:
            conn.close()
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding='utf-8')


if __name__ == "__main__":
    unittest.main()
