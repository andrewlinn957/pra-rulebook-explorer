import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.graph import neighbourhood


def make_conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        '''
        CREATE TABLE node (
          id TEXT PRIMARY KEY,
          node_type TEXT,
          stable_key TEXT,
          title TEXT,
          text TEXT,
          url TEXT,
          metadata_json TEXT
        );
        CREATE TABLE edge (
          id TEXT PRIMARY KEY,
          from_node_id TEXT,
          to_node_id TEXT,
          edge_type TEXT,
          source_method TEXT,
          confidence REAL,
          evidence_text TEXT,
          source_url TEXT,
          metadata_json TEXT
        );
        '''
    )
    return conn


def add_node(conn, node_id, node_type, title, url='', metadata='{}'):
    conn.execute(
        'INSERT INTO node VALUES (?,?,?,?,?,?,?)',
        (node_id, node_type, node_id, title, '', url, metadata),
    )


def add_edge(conn, edge_id, source, target, edge_type='references'):
    conn.execute(
        'INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)',
        (edge_id, source, target, edge_type, 'html_link', 0.9, '', '', '{}'),
    )


def test_neighbourhood_rolls_guidance_paragraph_reference_up_to_ss_document():
    conn = make_conn()
    add_node(conn, 'rule-1', 'rule', 'Liquidity rule')
    add_node(
        conn,
        'ss-doc',
        'guidance_document',
        'SS31/15 – The Internal Capital Adequacy Assessment Process (ICAAP) and the Supervisory Review and Evaluation Process (SREP)',
        '/guidance/supervisory-statements/ss31-15/',
        '{"document_type":"supervisory_statement"}',
    )
    add_node(conn, 'ss-section', 'guidance_section', 'Introduction')
    add_node(conn, 'ss-para', 'guidance_paragraph', '1.2')
    add_edge(conn, 'contains-1', 'ss-doc', 'ss-section', 'contains')
    add_edge(conn, 'contains-2', 'ss-section', 'ss-para', 'contains')
    add_edge(conn, 'ref-1', 'rule-1', 'ss-para')

    graph = neighbourhood(conn, 'rule-1', depth=1, edge_types=['references'])

    assert {node['id'] for node in graph['nodes']} == {'rule-1', 'ss-doc'}
    ss_node = next(node for node in graph['nodes'] if node['id'] == 'ss-doc')
    assert ss_node['title'].startswith('SS31/15')
    assert graph['edges'][0]['from_node_id'] == 'rule-1'
    assert graph['edges'][0]['to_node_id'] == 'ss-doc'
    assert graph['edges'][0]['metadata']['rolled_up_from_node_ids'] == ['ss-para']


def test_neighbourhood_deduplicates_multiple_references_to_same_ss_document():
    conn = make_conn()
    add_node(conn, 'rule-1', 'rule', 'Liquidity rule')
    add_node(
        conn,
        'sop-doc',
        'guidance_document',
        'Statement of Policy – The PRA’s methodologies for setting Pillar 2 capital',
        '/guidance/statements-of-policy/pillar-2/',
        '{"document_type":"statement_of_policy"}',
    )
    add_node(conn, 'sop-para-1', 'guidance_paragraph', '2.1')
    add_node(conn, 'sop-para-2', 'guidance_paragraph', '2.2')
    add_edge(conn, 'contains-1', 'sop-doc', 'sop-para-1', 'contains')
    add_edge(conn, 'contains-2', 'sop-doc', 'sop-para-2', 'contains')
    add_edge(conn, 'ref-1', 'rule-1', 'sop-para-1')
    add_edge(conn, 'ref-2', 'rule-1', 'sop-para-2')

    graph = neighbourhood(conn, 'rule-1', depth=1, edge_types=['references'])

    assert {node['id'] for node in graph['nodes']} == {'rule-1', 'sop-doc'}
    assert len(graph['edges']) == 1
    assert graph['edges'][0]['to_node_id'] == 'sop-doc'
    assert graph['edges'][0]['metadata']['rolled_up_from_node_ids'] == ['sop-para-1', 'sop-para-2']


if __name__ == '__main__':
    test_neighbourhood_rolls_guidance_paragraph_reference_up_to_ss_document()
    test_neighbourhood_deduplicates_multiple_references_to_same_ss_document()
