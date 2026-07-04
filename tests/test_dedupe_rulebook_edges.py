import sqlite3

from scripts.dedupe_rulebook_edges import (
    count_exact_duplicates,
    count_html_regex_reference_duplicates,
    delete_exact_duplicates,
    delete_html_regex_reference_duplicates,
)


def make_conn():
    conn = sqlite3.connect(':memory:')
    conn.execute('''
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
      )
    ''')
    return conn


def add_edge(conn, edge_id, edge_type='references', method='regex_reference', evidence='3.4', meta='{}'):
    conn.execute(
        'INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)',
        (edge_id, 'a', 'b', edge_type, method, 0.9, evidence, '', meta),
    )


def test_deletes_exact_duplicate_edges_only():
    conn = make_conn()
    add_edge(conn, 'one', edge_type='has_obligation_pattern', method='regex_obligation', evidence='must notify PRA')
    add_edge(conn, 'two', edge_type='has_obligation_pattern', method='regex_obligation', evidence='must notify PRA')
    add_edge(conn, 'three', edge_type='has_obligation_pattern', method='regex_obligation', evidence='must submit return')

    assert count_exact_duplicates(conn) == 1
    assert delete_exact_duplicates(conn) == 1
    assert conn.execute('SELECT COUNT(*) FROM edge').fetchone()[0] == 2
    assert count_exact_duplicates(conn) == 0


def test_deletes_regex_reference_when_html_anchor_resolved_same_reference():
    conn = make_conn()
    add_edge(conn, 'html', method='html_anchor_resolved', evidence='3.4')
    add_edge(conn, 'regex', method='regex_reference', evidence='context around 3.4', meta='{"reference":"3.4"}')
    add_edge(conn, 'other', method='regex_reference', evidence='context around 3.5', meta='{"reference":"3.5"}')

    assert count_html_regex_reference_duplicates(conn) == 1
    assert delete_html_regex_reference_duplicates(conn) == 1
    remaining = [row[0] for row in conn.execute('SELECT id FROM edge ORDER BY id')]
    assert remaining == ['html', 'other']
