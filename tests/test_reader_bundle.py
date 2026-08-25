import sqlite3

from backend.app.graph import _natural_sort_content, reader_bundle


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
        CREATE TABLE canonical_node (
          id TEXT PRIMARY KEY,
          is_canonical INTEGER NOT NULL
        );
        CREATE TABLE reference_occurrence (
          occurrence_id TEXT PRIMARY KEY,
          group_id TEXT,
          source_node_id TEXT,
          target_node_id TEXT,
          edge_id TEXT,
          relationship_type TEXT,
          citation_kind TEXT,
          citation_text TEXT,
          group_text TEXT,
          instrument_id TEXT,
          provision_path TEXT,
          qualifier TEXT,
          span_start INTEGER,
          span_end INTEGER,
          status TEXT,
          source_method TEXT,
          confidence REAL,
          context_text TEXT,
          metadata_json TEXT
          );
        CREATE TABLE document_source (
          id TEXT PRIMARY KEY,
          source_type TEXT NOT NULL,
          url TEXT NOT NULL UNIQUE,
          fetched_at TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          raw_html TEXT NOT NULL,
          raw_text TEXT DEFAULT ''
        );
        """
    )
    return conn


def add_node(conn, node_id, node_type, title, text=""):
    conn.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        (node_id, node_type, node_id, title, text, "", "{}"),
    )
    conn.execute("INSERT INTO canonical_node VALUES (?,1)", (node_id,))


def add_edge(conn, edge_id, source, target, edge_type="references"):
    conn.execute(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        (
            edge_id,
            source,
            target,
            edge_type,
            "html_link",
            1.0,
            "",
            "",
            "{}",
        ),
    )


def test_reader_bundle_loads_direct_references_from_every_contained_provision():
    conn = make_conn()
    add_node(conn, "chapter", "chapter", "Application")
    add_node(conn, "section", "chapter", "Scope")
    add_node(conn, "rule-1", "rule", "1.1", "See Article 26.")
    add_node(conn, "rule-2", "rule", "1.2", "A firm has the defined term.")
    add_node(conn, "article-26", "external_reference", "Article 26", "Source.")
    add_node(conn, "term-firm", "defined_term", "firm", "Meaning.")
    add_node(conn, "nested", "external_reference", "Nested reference", "Nested.")
    add_edge(conn, "contains-section", "chapter", "section", "contains")
    add_edge(conn, "contains-rule-1", "section", "rule-1", "contains")
    add_edge(conn, "contains-rule-2", "chapter", "rule-2", "contains")
    add_edge(conn, "rule-reference", "rule-1", "article-26")
    add_edge(conn, "rule-definition", "rule-2", "term-firm", "uses_defined_term")
    add_edge(conn, "nested-reference", "article-26", "nested")

    bundle = reader_bundle(conn, "chapter")

    assert bundle["spine_node_count"] == 4
    assert bundle["source_provision_count"] == 2
    assert bundle["reference_level"] == 1
    assert [child["id"] for child in bundle["contents"]["children"]] == [
        "section",
        "rule-2",
    ]
    assert [child["id"] for child in bundle["contents"]["children"][0]["children"]] == [
        "rule-1",
    ]
    assert {
        (edge["from_node_id"], edge["to_node_id"], edge["edge_type"])
        for edge in bundle["graph"]["edges"]
    } == {
        ("rule-1", "article-26", "references"),
        ("rule-2", "term-firm", "uses_defined_term"),
    }
    assert "nested" not in {node["id"] for node in bundle["graph"]["nodes"]}

    depth_two = reader_bundle(conn, "chapter", reference_depth=2)
    assert depth_two["reference_depth"] == 2
    assert (
        "article-26",
        "nested",
        "references",
    ) in {
        (edge["from_node_id"], edge["to_node_id"], edge["edge_type"])
        for edge in depth_two["graph"]["edges"]
    }


def test_content_order_uses_every_numeric_component_of_rule_titles():
    items = [
        {"id": "ten", "node_type": "rule", "title": "7.10", "metadata": {}},
        {"id": "two", "node_type": "rule", "title": "7.2", "metadata": {}},
        {"id": "one", "node_type": "rule", "title": "7.1", "metadata": {}},
    ]

    assert [item["id"] for item in _natural_sort_content(items)] == [
        "one",
        "two",
        "ten",
    ]


def test_reader_bundle_preserves_repeated_html_links_to_one_target():
    conn = make_conn()
    source_url = "https://www.prarulebook.co.uk/pra-rules/source/01-06-2026"
    add_node(conn, "root", "rule", "28.2A", "Apply 6.2 (6)(a) or (b), then 6.2 (6)(b).")
    add_node(conn, "target", "rule", "6.2", "Target text.")
    conn.execute(
        "UPDATE node SET url=?,metadata_json=? WHERE id='root'",
        (
            f"{source_url}#source-row",
            '{"html_id":"source-row","source_page_id":"source-page"}',
        ),
    )
    conn.execute(
        "INSERT INTO document_source VALUES (?,?,?,?,?,?,?)",
        (
            "source-page",
            "part",
            source_url,
            "2026-06-25T00:00:00Z",
            "hash",
            """<div id="source-row"><div class="div-row__col-2"><p>
              Apply <a href="/pra-rules/target#target" title="6.2">6.2</a>(6)(a) or (b),
              then <a href="/pra-rules/target#target" title="6.2">6.2</a>(6)(b).
            </p></div></div>""",
            "",
        ),
    )
    add_edge(conn, "reference", "root", "target")
    conn.execute(
        "UPDATE edge SET source_method='html_anchor_resolved',metadata_json=? WHERE id='reference'",
        ('{"href":"https://www.prarulebook.co.uk/pra-rules/target#target"}',),
    )
    conn.commit()

    bundle = reader_bundle(conn, "root")
    edge = bundle["graph"]["edges"][0]

    assert [
        occurrence["citation_text"]
        for occurrence in edge["metadata"]["reference_occurrences"]
    ] == ["6.2 (6)(a)", "6.2 (6)(b)"]
