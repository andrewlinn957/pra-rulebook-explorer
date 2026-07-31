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
