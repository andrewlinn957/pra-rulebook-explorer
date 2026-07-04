import importlib.util
import sqlite3
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "llm_reference_pass.py"
spec = importlib.util.spec_from_file_location("llm_reference_pass", SCRIPT_PATH)
llm_reference_pass = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(llm_reference_pass)


def make_conn():
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
        """
    )
    return conn


def add_node(conn, node_id, node_type, title, url='', metadata='{}', stable_key=None):
    conn.execute(
        "INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES (?,?,?,?,?,?,?)",
        (node_id, node_type, stable_key or node_id, title, '', url, metadata),
    )


def test_crr_article_reference_outside_liquidity_parts_resolves_to_uk_crr_external_article():
    conn = make_conn()
    add_node(conn, 'source', 'rule', '2.7', metadata='{"part_title":"Disclosure (CRR)"}')
    add_node(conn, 'lcr-article-11-2', 'rule', 'Article 11(2)', 'https://www.prarulebook.co.uk/pra-rules/liquidity-coverage-ratio-crr/01-06-2026#x', '{"part_title":"Liquidity Coverage Ratio (CRR)"}')

    resolver = llm_reference_pass.Resolver(conn)
    target, method, score = resolver.resolve('source', {
        'reference_text': 'Article 11(2) of the CRR',
        'target_kind': 'article',
        'target_title_or_identifier': 'Article 11(2)',
        'target_part_or_document': 'CRR',
        'confidence': 0.95,
    })

    assert target['node_type'] == 'external_reference'
    assert target['url'] == 'https://www.legislation.gov.uk/eur/2013/575/article/11'
    assert target['title'] == 'UK CRR Article 11(2)'
    assert method == 'uk_crr_external_article'
    assert score >= 0.98
    assert conn.execute("SELECT COUNT(*) FROM node WHERE id=?", (target['id'],)).fetchone()[0] == 1


def test_explicit_liquidity_rulebook_crr_article_reference_stays_internal_when_target_names_liquidity_part():
    conn = make_conn()
    add_node(conn, 'source', 'rule', '2.5', metadata='{"part_title":"Liquidity (CRR)"}')
    add_node(conn, 'lcr-article-11-2', 'rule', 'Article 11(2)', 'https://www.prarulebook.co.uk/pra-rules/liquidity-coverage-ratio-crr/01-06-2026#x', '{"part_title":"Liquidity Coverage Ratio (CRR)"}')

    resolver = llm_reference_pass.Resolver(conn)
    target, method, score = resolver.resolve('source', {
        'reference_text': 'Liquidity Coverage Ratio (CRR) Article 11(2)',
        'target_kind': 'article',
        'target_title_or_identifier': 'Article 11(2)',
        'target_part_or_document': 'Liquidity Coverage Ratio (CRR)',
        'confidence': 0.95,
    })

    assert target['id'] == 'lcr-article-11-2'
    assert method == 'exact_node_title'
    assert score >= 0.9


def test_of_crr_article_reference_inside_liquidity_part_still_resolves_to_uk_crr():
    conn = make_conn()
    add_node(conn, 'source', 'rule', '2.5', metadata='{"part_title":"Liquidity Coverage Ratio (CRR)"}')
    add_node(conn, 'lcr-article-415', 'rule', 'Article 415', 'https://www.prarulebook.co.uk/pra-rules/liquidity-crr/01-06-2026#x', '{"part_title":"Liquidity (CRR)"}')

    resolver = llm_reference_pass.Resolver(conn)
    target, method, score = resolver.resolve('source', {
        'reference_text': 'Article 415(2) of CRR',
        'target_kind': 'article',
        'target_title_or_identifier': 'Article 415(2)',
        'target_part_or_document': 'CRR',
        'confidence': 0.95,
    })

    assert target['node_type'] == 'external_reference'
    assert target['url'] == 'https://www.legislation.gov.uk/eur/2013/575/article/415'
    assert method == 'uk_crr_external_article'


if __name__ == '__main__':
    test_crr_article_reference_outside_liquidity_parts_resolves_to_uk_crr_external_article()
    test_explicit_liquidity_rulebook_crr_article_reference_stays_internal_when_target_names_liquidity_part()
    test_of_crr_article_reference_inside_liquidity_part_still_resolves_to_uk_crr()
