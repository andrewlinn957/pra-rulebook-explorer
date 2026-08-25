import sqlite3

from bs4 import BeautifulSoup

from backend.rulebook_scraper.models import Edge, Node
from backend.rulebook_scraper.parse import extract_part, extract_text_blocks
from backend.rulebook_scraper.store import SCHEMA, upsert_edges, upsert_nodes, upsert_source


JUNE_URL = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
JULY_URL = "https://www.prarulebook.co.uk/pra-rules/test-part/01-07-2026"
PART_HTML = """
<html>
  <body>
    <h1>Test Part</h1>
    <div class="rulebook-content">
      <div class="chapter-section" id="article-10">
        <span class="rule-number chapter-number">10</span>
        <h2 class="chapter-heading">Article 10</h2>
      </div>
      <div class="row-block" id="article-10-1">
        <span class="rule-number">1</span>
        <div class="div-row__col-2"><p>Versioned provision text.</p></div>
      </div>
    </div>
  </body>
</html>
"""


def test_parser_emits_one_canonical_provision_and_dated_versions() -> None:
    june_nodes, june_edges = extract_part(PART_HTML, JUNE_URL)
    july_nodes, july_edges = extract_part(PART_HTML.replace("Versioned", "Amended"), JULY_URL)
    nodes = june_nodes + july_nodes
    edges = june_edges + july_edges

    canonical = [node for node in nodes if node.node_type == "provision"]
    versions = [node for node in nodes if node.metadata.get("identity_type") == "provision_version"]

    assert len({node.id for node in canonical}) == 1
    assert len({node.id for node in versions}) == 2
    assert {node.metadata["rulebook_date"] for node in versions} == {"01-06-2026", "01-07-2026"}
    assert all(node.metadata["canonical_provision_id"] == canonical[0].id for node in versions)

    has_version = [edge for edge in edges if edge.edge_type == "has_version"]
    sourced_from = [edge for edge in edges if edge.edge_type == "sourced_from"]
    assert {(edge.from_node_id, edge.to_node_id) for edge in has_version} == {
        (canonical[0].id, version.id) for version in versions
    }
    assert all(edge.to_node_id in {node.id for node in [*june_nodes, *july_nodes] if node.node_type == "part"} for edge in sourced_from)
    assert all(edge.edge_type != "contains" or edge.to_node_id not in {node.id for node in versions} or edge.from_node_id for edge in edges)

    version_texts = {node.metadata["rulebook_date"]: node.text for node in versions}
    assert version_texts["01-06-2026"] == "Versioned provision text."
    assert version_texts["01-07-2026"] == "Amended provision text."


def test_upsert_source_keeps_immutable_snapshots_and_current_page() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    upsert_source(conn, source_type="part", url=JUNE_URL, fetched_at="2026-06-01T00:00:00Z", raw_html="one", raw_text="one")
    upsert_source(conn, source_type="part", url=JUNE_URL, fetched_at="2026-06-02T00:00:00Z", raw_html="one", raw_text="one")
    upsert_source(conn, source_type="part", url=JUNE_URL, fetched_at="2026-06-03T00:00:00Z", raw_html="two", raw_text="two")

    snapshots = conn.execute(
        "SELECT url,content_hash,raw_html FROM document_snapshot ORDER BY fetched_at"
    ).fetchall()
    assert len(snapshots) == 2
    assert [row["raw_html"] for row in snapshots] == ["one", "two"]
    current = conn.execute("SELECT fetched_at,raw_html FROM document_source WHERE url=?", (JUNE_URL,)).fetchone()
    assert tuple(current) == ("2026-06-03T00:00:00Z", "two")


def test_version_keeps_reader_contains_spine() -> None:
    nodes, edges = extract_part(PART_HTML, JUNE_URL)
    version = next(node for node in nodes if node.metadata.get("identity_type") == "provision_version")
    assert any(edge.edge_type == "contains" and edge.to_node_id == version.id for edge in edges)


def test_parser_keeps_sibling_nested_list_blocks_from_pra_markup() -> None:
    issue_one = BeautifulSoup(
        """
        <div class="div-row__col-2">
          <ol class="new-bullet">
            <li>(1) First limb.</li>
            <li>(2) Second limb.</li>
            <ol>
              <li>(a) Nested limb A.</li>
              <li>(b) Nested limb B.</li>
              <li>(c) Nested limb C.</li>
            </ol>
          </ol>
        </div>
        """,
        "lxml",
    ).select_one(".div-row__col-2")
    issue_four = BeautifulSoup(
        """
        <div class="div-row__col-2">
          <p>A firm must:</p>
          <ol class="new-bullet">
            <li>(1) First obligation:</li>
            <ol><li>(a) Notify the PRA.</li><li>(b) Give reasons.</li></ol>
            <li>(2) Second obligation.</li>
            <li>(3) Third obligation.</li>
            <li>(4) Fourth obligation:</li>
            <ol><li>(a) Notify the appointment.</li><li>(b) Advise the PRA.</li></ol>
          </ol>
          <p>using the form referred to in Notifications 10.3.</p>
        </div>
        """,
        "lxml",
    ).select_one(".div-row__col-2")

    first_blocks = extract_text_blocks(issue_one)
    fourth_blocks = extract_text_blocks(issue_four)

    assert [(block["marker"], block["depth"]) for block in first_blocks or []] == [
        ("(1)", 0),
        ("(2)", 0),
        ("(a)", 1),
        ("(b)", 1),
        ("(c)", 1),
    ]
    assert [(block["marker"], block["depth"]) for block in fourth_blocks or []] == [
        ("", 0),
        ("(1)", 0),
        ("(a)", 1),
        ("(b)", 1),
        ("(2)", 0),
        ("(3)", 0),
        ("(4)", 0),
        ("(a)", 1),
        ("(b)", 1),
        ("", 0),
    ]


def test_parser_keeps_repeated_internal_anchor_occurrences_distinct() -> None:
    html = """
    <html><body><h1>Repeated links</h1><div class="rulebook-content">
      <div class="row-block" id="rule-1">
        <span class="rule-number">1</span>
        <div class="div-row__col-2"><p>
          See <a href="/pra-rules/target#anchor">6.2</a> and
          <a href="/pra-rules/target#anchor">6.2</a>.
        </p></div>
      </div>
    </div></body></html>
    """
    nodes, edges = extract_part(html, JUNE_URL)
    source = next(node for node in nodes if node.node_type == "rule")
    links = [
        edge for edge in edges
        if edge.from_node_id == source.id
        and edge.source_method == "html_link"
        and edge.edge_type == "references"
    ]

    assert len(links) == 2
    assert [edge.metadata["anchor_index"] for edge in links] == [0, 1]
    assert len({edge.id for edge in links}) == 2


def test_semantic_edges_target_canonical_identity_when_version_is_supplied() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    canonical = Node("canonical", "provision", "provision:test:1", "1", metadata={"identity_type": "canonical_provision"})
    version = Node("version", "rule", "provision_version:test:1:01-06-2026", "1", "Version", metadata={"identity_type": "provision_version", "canonical_provision_id": "canonical"})
    upsert_nodes(conn, [canonical, version])
    upsert_edges(conn, [Edge("ref", "source", "version", "references", "test")])

    assert conn.execute("SELECT to_node_id FROM edge WHERE id='ref'").fetchone()[0] == "canonical"
