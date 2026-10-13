import json
import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.rulebook_scraper.store import SCHEMA
from scripts.audit_cross_reference_integrity import audit, main


class CrossReferenceIntegrityAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO node(id,node_type,stable_key,title,text) VALUES ('s','rule','s','Source','See rule 2.')")
        self.conn.execute("INSERT INTO node(id,node_type,stable_key,title) VALUES ('t','provision','t','Target')")
        self.conn.execute("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES ('e','s','t','references','test',1)")
        self.conn.execute('''INSERT INTO reference_occurrence(
            occurrence_id,group_id,source_node_id,target_node_id,edge_id,citation_kind,
            citation_text,group_text,span_start,span_end,status,source_method,confidence)
            VALUES ('o','g','s','t','e','rule','rule 2','rule 2',4,10,'materialized','test',1)''')

    def test_materialized_is_not_assumed_to_be_readable(self):
        result = audit(self.conn)
        self.assertEqual(result['counts']['materialized_target_without_text_or_version'], 1)
        self.conn.execute('INSERT INTO node(id,node_type,stable_key,title,text,metadata_json) VALUES (?,?,?,?,?,?)',
                          ('v','rule','v','Version','Readable text',json.dumps({'canonical_provision_id':'t'})))
        self.assertNotIn('materialized_target_without_text_or_version', audit(self.conn)['counts'])

    def test_broken_edge_and_out_of_bounds_span_are_reported(self):
        self.conn.execute("DELETE FROM edge WHERE id='e'")
        self.conn.execute("UPDATE reference_occurrence SET span_end=100")
        result = audit(self.conn)
        self.assertEqual(result['counts']['materialized_missing_edge'], 1)
        self.assertEqual(result['counts']['invalid_span'], 1)

    def test_audit_works_with_writes_disabled_and_reports_endpoint_disagreement(self):
        self.conn.execute("UPDATE edge SET to_node_id='s' WHERE id='e'")
        self.conn.commit()
        self.conn.execute('PRAGMA query_only=ON')
        result = audit(self.conn)
        self.assertEqual(result['counts']['materialized_endpoint_mismatch'], 1)
        self.assertEqual(result['counts']['occurrences'], 1)

    def test_strict_cli_fails_for_broken_links_and_writes_evidence(self):
        self.conn.execute("DELETE FROM edge WHERE id='e'")
        self.conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with sqlite3.connect(path / 'corpus.sqlite3') as snapshot:
                self.conn.backup(snapshot)
            argv = ['audit', '--db', str(path / 'corpus.sqlite3'), '--output-dir', str(path / 'results'), '--strict']
            with patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            report = json.loads((path / 'results/cross-reference-integrity.json').read_text())
            self.assertEqual(report['counts']['materialized_missing_edge'], 1)


if __name__ == '__main__':
    unittest.main()
