from __future__ import annotations

import json
import sqlite3

from backend.rulebook_scraper.enrich import repair_structured_definition_text
from backend.rulebook_scraper.parse import extract_part


PART_URL = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
PART_HTML = """
<html>
  <body>
    <h1>Test Part</h1>
    <div class="rulebook-content">
      <div class="row-block" id="rule-1-2">
        <span class="rule-number">1.2</span>
        <div class="div-row__col-2">
          <p><a href="#glossary-term-group123">group of connected clients</a></p>
          <p>means any of the following:</p>
          <ol>
            <li>(1) persons under common control; or</li>
            <li>(2) persons that are economically interconnected.</li>
          </ol>
          <p><a class="glossary-link" title="large exposure" href="#glossary-term-large456">large exposure</a></p>
          <p>means an exposure above the applicable threshold.</p>
        </div>
      </div>
    </div>
  </body>
</html>
"""


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE document_source (
          id TEXT PRIMARY KEY,
          source_type TEXT NOT NULL,
          url TEXT NOT NULL UNIQUE,
          fetched_at TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          raw_html TEXT NOT NULL,
          raw_text TEXT DEFAULT ''
        );
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


def test_inline_definition_includes_following_list_until_next_term() -> None:
    nodes, _ = extract_part(PART_HTML, PART_URL)
    definitions = {node.title: node.text for node in nodes if node.node_type == "defined_term"}

    assert definitions["group of connected clients"] == (
        "means any of the following: (1) persons under common control; or "
        "(2) persons that are economically interconnected."
    )
    assert definitions["large exposure"] == "means an exposure above the applicable threshold."
    assert "large exposure" not in definitions["group of connected clients"]


def test_part_parser_preserves_firm_category_tags() -> None:
    html = """
    <html>
      <body>
        <h1>Capital Buffers</h1>
        <a href="/pra-rules/crr-firms">CRR Firms</a>
        <a href="/pra-rules/non-crr-firms">Non-CRR Firms</a>
        <a href="/pra-rules/sii-firms">SII Firms</a>
        <a href="/pra-rules/non-sii-firms">Non-SII Firms</a>
        <a href="/pra-rules/non-authorised-persons">Non-authorised Persons</a>
        <div class="main-content">
          <ul>
            <li>CRR Firms</li>
            <li>Non-CRR Firms</li>
          </ul>
        </div>
        <div class="rulebook-content"></div>
      </body>
    </html>
    """

    part = next(node for node in extract_part(html, PART_URL)[0] if node.node_type == "part")

    assert part.metadata["firm_categories"] == ["CRR Firms", "Non-CRR Firms"]


def test_unnumbered_heading_contains_following_rules() -> None:
    html = """
    <html>
      <body>
        <h1>Liquidity (CRR)</h1>
        <div class="rulebook-content">
          <div class="chapter-section" id="chapter-2">
            <span class="rule-number chapter-number">2</span>
            <h2 class="chapter-heading">Level of Application</h2>
          </div>
          <div class="row-block row-block--border" id="rule-2-1">
            <span class="rule-number">2.1</span>
            <div class="div-row__col-2"><p>Rule 2.1 text.</p></div>
          </div>
          <div class="row-block" id="domestic-heading">
            <span class="rule-number"></span>
            <div class="div-row__col-2"><h3>Domestic liquidity sub-groups</h3></div>
          </div>
          <div class="row-block row-block--border" id="rule-2-2">
            <span class="rule-number">2.2</span>
            <div class="div-row__col-2"><p>Rule 2.2 text.</p></div>
          </div>
          <div class="row-block row-block--border" id="rule-2-3">
            <span class="rule-number">2.3</span>
            <div class="div-row__col-2"><p>Rule 2.3 text.</p></div>
          </div>
        </div>
      </body>
    </html>
    """

    nodes, edges = extract_part(html, PART_URL)
    by_title = {node.title: node for node in nodes}
    domestic = by_title["Domestic liquidity sub-groups"]
    rule_22 = next(node for node in nodes if node.metadata.get("rule_number") == "2.2")
    rule_23 = next(node for node in nodes if node.metadata.get("rule_number") == "2.3")

    assert any(edge.from_node_id == domestic.id and edge.to_node_id == rule_22.id for edge in edges)
    assert any(edge.from_node_id == domestic.id and edge.to_node_id == rule_23.id for edge in edges)


def test_repeated_unnumbered_headings_keep_separate_children() -> None:
    html = """
    <html>
      <body>
        <h1>Solvency Capital Requirement - Undertaking Specific Parameters</h1>
        <div class="rulebook-content">
          <div class="chapter-section" id="premium-risk">
            <span class="rule-number chapter-number">4</span>
            <h2 class="chapter-heading">Premium Risk Method</h2>
          </div>
          <div class="row-block" id="premium-input-data">
            <span class="rule-number"></span>
            <div class="div-row__col-2"><h3>Input data and USP method-specific data requirements</h3></div>
          </div>
          <div class="row-block row-block--border" id="rule-4-1">
            <span class="rule-number">4.1</span>
            <div class="div-row__col-2"><p>Rule 4.1 text.</p></div>
          </div>
          <div class="row-block" id="premium-specification">
            <span class="rule-number"></span>
            <div class="div-row__col-2"><h3>USP method specification</h3></div>
          </div>
          <div class="row-block row-block--border" id="rule-4-4">
            <span class="rule-number">4.4</span>
            <div class="div-row__col-2"><p>Rule 4.4 text.</p></div>
          </div>
          <div class="chapter-section" id="reserve-risk">
            <span class="rule-number chapter-number">5</span>
            <h2 class="chapter-heading">Reserve Risk Method</h2>
          </div>
          <div class="row-block" id="reserve-input-data">
            <span class="rule-number"></span>
            <div class="div-row__col-2"><h3>Input data and USP method-specific data requirements</h3></div>
          </div>
          <div class="row-block row-block--border" id="rule-5-1">
            <span class="rule-number">5.1</span>
            <div class="div-row__col-2"><p>Rule 5.1 text.</p></div>
          </div>
        </div>
      </body>
    </html>
    """

    nodes, edges = extract_part(html, PART_URL)
    by_html_id = {node.metadata.get("html_id"): node for node in nodes}
    premium_input = by_html_id["premium-input-data"]
    premium_spec = by_html_id["premium-specification"]
    reserve_input = by_html_id["reserve-input-data"]
    rule_41 = next(node for node in nodes if node.metadata.get("rule_number") == "4.1")
    rule_44 = next(node for node in nodes if node.metadata.get("rule_number") == "4.4")
    rule_51 = next(node for node in nodes if node.metadata.get("rule_number") == "5.1")

    assert any(edge.from_node_id == premium_input.id and edge.to_node_id == rule_41.id for edge in edges)
    assert any(edge.from_node_id == premium_spec.id and edge.to_node_id == rule_44.id for edge in edges)
    assert any(edge.from_node_id == reserve_input.id and edge.to_node_id == rule_51.id for edge in edges)


def test_heading_row_with_body_text_creates_child_rule() -> None:
    html = """
    <html>
      <body>
        <h1>Depositor Protection</h1>
        <div class="rulebook-content">
          <div class="chapter-section" id="annex-3">
            <span class="rule-number"></span>
            <h2 class="chapter-heading">Annex 3 - Exclusions List (Chapter 16)</h2>
          </div>
          <div class="row-block row-block--border" id="section-a1">
            <span class="rule-number"></span>
            <div class="div-row__col-2">
              <h4>Section A1</h4>
              <p>Deposits held by individuals and businesses will generally be eligible for FSCS protection.</p>
            </div>
          </div>
          <div class="row-block" id="section-a-deleted">
            <span class="rule-number"></span>
            <div class="div-row__col-2">Section A [Deleted]</div>
          </div>
          <div class="row-block row-block--border" id="section-a-deleted-body">
            <span class="rule-number"></span>
            <div class="div-row__col-2"><p>(1) [Deleted] (2) [Deleted]</p></div>
          </div>
        </div>
      </body>
    </html>
    """

    nodes, edges = extract_part(html, PART_URL)
    heading = next(node for node in nodes if node.title == "Section A1" and node.node_type == "chapter")
    body_rule = next(node for node in nodes if node.metadata.get("heading_body"))
    deleted_heading = next(node for node in nodes if node.title == "Section A [Deleted]" and node.node_type == "chapter")
    deleted_body = next(node for node in nodes if node.metadata.get("html_id") == "section-a-deleted-body")

    assert body_rule.text == "Deposits held by individuals and businesses will generally be eligible for FSCS protection."
    assert any(edge.from_node_id == heading.id and edge.to_node_id == body_rule.id for edge in edges)
    assert any(edge.from_node_id == deleted_heading.id and edge.to_node_id == deleted_body.id for edge in edges)
    assert not any(edge.from_node_id == heading.id and edge.to_node_id == deleted_body.id for edge in edges)


def test_repair_expands_inline_definition_and_matching_glossary_alias() -> None:
    parsed_nodes, _ = extract_part(PART_HTML, PART_URL)
    source = next(node for node in parsed_nodes if node.title == "group of connected clients")
    conn = make_db()
    conn.execute(
        "INSERT INTO document_source VALUES(?,?,?,?,?,?,?)",
        ("source", "part", PART_URL, "2026-07-31", "hash", PART_HTML, ""),
    )
    truncated = "means any of the following:"
    conn.executemany(
        "INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES(?,?,?,?,?,?,?)",
        [
            (
                source.id,
                "defined_term",
                source.stable_key,
                source.title,
                truncated,
                PART_URL,
                json.dumps(source.metadata),
            ),
            (
                "glossary-alias",
                "defined_term",
                "defined_term:glossary:group of connected clients",
                source.title,
                truncated,
                "https://www.prarulebook.co.uk/glossary#glossary-term-group123",
                json.dumps({"glossary_hash": "group123", "placeholder": True}),
            ),
        ],
    )

    result = repair_structured_definition_text(conn)

    assert result["inline_definitions_repaired"] == 1
    assert result["glossary_aliases_repaired"] == 1
    texts = {
        row["id"]: row["text"]
        for row in conn.execute("SELECT id,text FROM node")
    }
    assert texts[source.id] == source.text
    assert texts["glossary-alias"] == source.text


def test_repair_does_not_replace_a_different_substantive_definition() -> None:
    parsed_nodes, _ = extract_part(PART_HTML, PART_URL)
    source = next(node for node in parsed_nodes if node.title == "group of connected clients")
    conn = make_db()
    conn.execute(
        "INSERT INTO document_source VALUES(?,?,?,?,?,?,?)",
        ("source", "part", PART_URL, "2026-07-31", "hash", PART_HTML, ""),
    )
    conn.execute(
        "INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES(?,?,?,?,?,?,?)",
        (
            source.id,
            "defined_term",
            source.stable_key,
            source.title,
            "means a deliberately different definition.",
            PART_URL,
            json.dumps(source.metadata),
        ),
    )

    result = repair_structured_definition_text(conn)

    assert result["inline_definitions_repaired"] == 0
    assert conn.execute("SELECT text FROM node WHERE id=?", (source.id,)).fetchone()[0] == (
        "means a deliberately different definition."
    )
