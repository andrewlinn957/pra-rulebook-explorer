from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.backfill_reader_reference_text import (
    Resolution,
    aggregate_descendant_text,
    apply_resolutions,
    extract_html,
    reader_targets,
    source_text_issue,
)
from backend.app.graph import neighbourhood


def make_db() -> sqlite3.Connection:
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
        );
        """
    )
    return conn


def test_reader_targets_are_exactly_empty_outgoing_reference_targets() -> None:
    conn = make_db()
    conn.executemany(
        "INSERT INTO node(id,node_type,stable_key,title,text) VALUES(?,?,?,?,?)",
        [
            ("root", "rule", "root", "1.1", "Main provision"),
            ("missing", "defined_term", "missing", "firm", ""),
            ("present", "rule", "present", "2.1", "Referenced provision"),
            ("structure", "chapter", "structure", "Chapter 2", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES(?,?,?,?,?,?)",
        [
            ("a", "root", "missing", "uses_defined_term", "html_glossary_link", 1.0),
            ("b", "root", "present", "references", "html_link", 1.0),
            ("c", "root", "structure", "contains", "site_structure", 1.0),
        ],
    )
    assert [row["id"] for row in reader_targets(conn)] == ["missing"]


def test_structural_reference_aggregates_nested_provisions_in_natural_order() -> None:
    conn = make_db()
    conn.executemany(
        "INSERT INTO node(id,node_type,stable_key,title,text) VALUES(?,?,?,?,?)",
        [
            ("chapter", "chapter", "chapter", "Chapter 1", ""),
            ("ten", "rule", "ten", "1.10", "Tenth provision."),
            ("two", "rule", "two", "1.2", "Second provision."),
        ],
    )
    conn.executemany(
        "INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES(?,?,?,?,?,?)",
        [
            ("a", "chapter", "ten", "contains", "site_structure", 1.0),
            ("b", "chapter", "two", "contains", "site_structure", 1.0),
        ],
    )
    nodes = {row["id"]: row for row in conn.execute("SELECT * FROM node")}
    result = aggregate_descendant_text(
        nodes["chapter"],
        {"chapter": ["ten", "two"]},
        nodes,
    )
    assert result.resolved
    assert result.text == "1.2\nSecond provision.\n\n1.10\nTenth provision."
    assert result.source_node_ids == ("two", "ten")


def test_apply_records_provenance_and_replaces_generic_external_title() -> None:
    conn = make_db()
    conn.executemany(
        "INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES(?,?,?,?,?,?,?)",
        [
            ("root", "rule", "root", "1.1", "Main provision", "", "{}"),
            (
                "target",
                "external_reference",
                "target",
                "Bank of England",
                "",
                "https://example.test/source.pdf",
                '{"placeholder":true}',
            ),
        ],
    )
    conn.execute(
        "INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence) VALUES(?,?,?,?,?,?)",
        ("a", "root", "target", "references", "html_link", 1.0),
    )
    targets = reader_targets(conn)
    updated = apply_resolutions(
        conn,
        targets,
        [
            Resolution(
                "target",
                "authoritative_url",
                text="Official source text.",
                source_url="https://example.test/source.pdf",
                source_title="Liquidity reporting instructions",
                rationale="Retained successor document.",
                content_type="application/pdf",
                content_hash="abc123",
            )
        ],
    )
    row = conn.execute("SELECT * FROM node WHERE id='target'").fetchone()
    metadata = json.loads(row["metadata_json"])
    assert updated == 1
    assert row["text"] == "Official source text."
    assert row["title"] == "Liquidity reporting instructions"
    assert metadata["placeholder"] is True
    assert metadata["reader_reference_text"]["method"] == "authoritative_url"
    assert metadata["reader_reference_text"]["original_title"] == "Bank of England"
    assert metadata["reader_reference_text"]["rationale"] == "Retained successor document."
    assert reader_targets(conn) == []
    graph = neighbourhood(conn, "root", depth=1, edge_types=["references"])
    target = next(node for node in graph["nodes"] if node["id"] == "target")
    assert target["text"] == "Official source text."
    assert target["metadata"]["reader_reference_text"]["source_url"] == "https://example.test/source.pdf"


def test_html_extraction_prefers_complete_main_over_nested_page_metadata() -> None:
    body, title = extract_html(
        b"""
        <html>
          <head><meta property="og:title" content="Supervisory statement 4/18"></head>
          <body>
            <main>
              <div class="page-content">First published 17 May 2018</div>
              <section>
                This supervisory statement sets out the PRA's expectations for
                effective financial management and planning by insurers.
              </section>
            </main>
          </body>
        </html>
        """
    )
    assert title == "Supervisory statement 4/18"
    assert "First published 17 May 2018" in body
    assert "effective financial management and planning" in body


def test_external_source_quality_rejects_navigation_scraps_and_error_pages() -> None:
    assert "only 15 characters" in source_text_issue("In this section", "Resolution")
    assert source_text_issue("This is substantive official source text. " * 8) == ""
    assert source_text_issue("A" * 500, "404 - Page Not Found") == "source resolved to an error page"
    assert source_text_issue("A" * 500, "Rule not found") == "source resolved to an error page"
    assert source_text_issue("A" * 500, "We can't sign you in") == "source resolved to an error page"
