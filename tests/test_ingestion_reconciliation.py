from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.rulebook_scraper.cli as scraper_cli
from backend.rulebook_scraper.models import Edge, Node
from backend.rulebook_scraper.store import (
    SCHEMA,
    finish_ingestion_run,
    record_ingestion_scope_failure,
    record_ingestion_scope_started,
    reconcile_derived_output,
    reconcile_source_output,
    start_ingestion_run,
    upsert_edges,
    upsert_nodes,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _page(url: str, node_id: str = "page") -> Node:
    return Node(
        node_id,
        "part",
        f"part:{node_id}",
        "Test Part",
        url=url,
        metadata={"identity_type": "source_page", "source_page_id": node_id},
    )


def _version(url: str, node_id: str, *, metadata: dict | None = None, text: str = "Rule text") -> Node:
    return Node(
        node_id,
        "rule",
        f"version:{node_id}",
        node_id,
        text=text,
        url=f"{url}#{node_id}",
        metadata={"identity_type": "provision_version", "source_page_id": "page", **(metadata or {})},
    )


def _contains(source_id: str, target_id: str, source_url: str, suffix: str = "contains") -> Edge:
    return Edge(
        f"edge-{suffix}-{source_id}-{target_id}",
        source_id,
        target_id,
        "contains",
        "site_structure",
        source_url=source_url,
    )


def test_successful_refresh_removes_stale_output_but_retains_snapshots_and_occurrences() -> None:
    conn = _connection()
    url = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
    page = _page(url)
    old = _version(url, "old-rule")
    old_edge = _contains(page.id, old.id, url)
    run_id = start_ingestion_run(conn, command="test-refresh")
    reconcile_source_output(
        conn,
        run_id=run_id,
        source_url=url,
        source_type="part",
        fetched_at="2026-08-20T18:00:00Z",
        raw_html="<html>old</html>",
        nodes=[page, old],
        edges=[old_edge],
    )
    conn.execute(
        """
        INSERT INTO reference_occurrence(
          occurrence_id,group_id,source_node_id,target_node_id,edge_id,
          citation_kind,citation_text,group_text,span_start,span_end,
          status,source_method,confidence
        ) VALUES ('occ-1','group-1',?,?,?,?,?,?,?,?,?,?,?)
        """,
        (old.id, page.id, old_edge.id, "article", "Article 1", "Article 1", 0, 9, "materialized", "test", 1.0),
    )
    finish_ingestion_run(conn, run_id=run_id)

    new = _version(url, "new-rule")
    run_id = start_ingestion_run(conn, command="test-refresh")
    reconcile_source_output(
        conn,
        run_id=run_id,
        source_url=url,
        source_type="part",
        fetched_at="2026-08-20T18:01:00Z",
        raw_html="<html>new</html>",
        nodes=[page, new],
        edges=[_contains(page.id, new.id, url, "contains-new")],
    )
    finish_ingestion_run(conn, run_id=run_id)

    assert conn.execute("SELECT id FROM node WHERE id=?", (old.id,)).fetchone() is None
    assert conn.execute("SELECT id FROM edge WHERE id=?", (old_edge.id,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM reference_occurrence WHERE occurrence_id='occ-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM document_snapshot WHERE url=?", (url,)).fetchone()[0] == 2
    assert conn.execute("SELECT raw_html FROM document_source WHERE url=?", (url,)).fetchone()[0] == "<html>new</html>"
    assert conn.execute("SELECT COUNT(*) FROM node WHERE id=?", (new.id,)).fetchone()[0] == 1


def test_shared_objects_survive_until_the_last_scope_releases_them() -> None:
    conn = _connection()
    shared = Node("shared", "provision", "provision:shared", "Shared")
    a_url = "https://www.prarulebook.co.uk/pra-rules/a/01-06-2026"
    b_url = "https://www.prarulebook.co.uk/pra-rules/b/01-06-2026"
    a = _page(a_url, "page-a")
    b = _page(b_url, "page-b")

    run_id = start_ingestion_run(conn, command="test-shared")
    reconcile_source_output(conn, run_id=run_id, source_url=a_url, source_type="part", nodes=[a, shared], edges=[])
    reconcile_source_output(conn, run_id=run_id, source_url=b_url, source_type="part", nodes=[b, shared], edges=[])
    finish_ingestion_run(conn, run_id=run_id)

    run_id = start_ingestion_run(conn, command="test-shared")
    reconcile_source_output(conn, run_id=run_id, source_url=a_url, source_type="part", nodes=[a], edges=[])
    finish_ingestion_run(conn, run_id=run_id)
    assert conn.execute("SELECT COUNT(*) FROM node WHERE id='shared'").fetchone()[0] == 1

    run_id = start_ingestion_run(conn, command="test-shared")
    reconcile_source_output(conn, run_id=run_id, source_url=b_url, source_type="part", nodes=[b], edges=[])
    finish_ingestion_run(conn, run_id=run_id)
    assert conn.execute("SELECT COUNT(*) FROM node WHERE id='shared'").fetchone()[0] == 0


def test_changed_source_node_replaces_metadata_and_invalidates_old_occurrences() -> None:
    conn = _connection()
    url = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
    initial = _page(url, "page")
    initial.metadata.update({"removed_field": "stale", "identity_type": "source_page"})
    run_id = start_ingestion_run(conn, command="test-change")
    reconcile_source_output(conn, run_id=run_id, source_url=url, source_type="part", nodes=[initial], edges=[])
    conn.execute(
        """
        INSERT INTO reference_occurrence(
          occurrence_id,group_id,source_node_id,citation_kind,citation_text,
          group_text,span_start,span_end,status,source_method,confidence
        ) VALUES ('occ-change','group-change',?,'article','Article 1','Article 1',0,9,'materialized','test',1.0)
        """,
        (initial.id,),
    )
    finish_ingestion_run(conn, run_id=run_id)

    changed = _page(url, "page")
    changed.metadata["new_field"] = "current"
    changed.text = "Changed source text"
    run_id = start_ingestion_run(conn, command="test-change")
    reconcile_source_output(conn, run_id=run_id, source_url=url, source_type="part", nodes=[changed], edges=[])
    finish_ingestion_run(conn, run_id=run_id)

    row = conn.execute("SELECT text,metadata_json FROM node WHERE id='page'").fetchone()
    assert row[0] == "Changed source text"
    assert "removed_field" not in row[1]
    assert '"new_field": "current"' in row[1]
    assert conn.execute("SELECT COUNT(*) FROM reference_occurrence WHERE occurrence_id='occ-change'").fetchone()[0] == 0


def test_failed_scope_preserves_previous_successful_output() -> None:
    conn = _connection()
    url = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
    page = _page(url)
    rule = _version(url, "rule-1")
    run_id = start_ingestion_run(conn, command="test-failure")
    reconcile_source_output(conn, run_id=run_id, source_url=url, source_type="part", nodes=[page, rule], edges=[])
    finish_ingestion_run(conn, run_id=run_id)

    failed_run = start_ingestion_run(conn, command="test-failure")
    scope_key = "source:part:" + url
    record_ingestion_scope_started(conn, run_id=failed_run, scope_key=scope_key, source_url=url, source_type="part")
    record_ingestion_scope_failure(conn, run_id=failed_run, scope_key=scope_key, error="upstream timeout")
    assert finish_ingestion_run(conn, run_id=failed_run) == "failed"

    assert conn.execute("SELECT COUNT(*) FROM node WHERE id='rule-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ingestion_output WHERE scope_key=?", (scope_key,)).fetchone()[0] == 2
    assert tuple(conn.execute("SELECT status,error FROM ingestion_run_scope WHERE run_id=?", (failed_run,)).fetchone()) == ("failed", "upstream timeout")


def test_legacy_output_is_bootstrapped_and_reconciled_on_first_refresh() -> None:
    conn = _connection()
    url = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
    page = _page(url)
    stale = _version(url, "legacy-rule")
    stale_edge = _contains(page.id, stale.id, url, "legacy")
    upsert_nodes(conn, [page, stale])
    upsert_edges(conn, [stale_edge])

    run_id = start_ingestion_run(conn, command="test-bootstrap")
    reconcile_source_output(conn, run_id=run_id, source_url=url, source_type="part", nodes=[page], edges=[])
    finish_ingestion_run(conn, run_id=run_id)

    assert conn.execute("SELECT COUNT(*) FROM node WHERE id='legacy-rule'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id=?", (stale_edge.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingestion_output WHERE scope_key=?", ("source:part:" + url,)).fetchone()[0] == 1


def test_legacy_index_bootstrap_removes_a_part_no_longer_listed() -> None:
    conn = _connection()
    index_url = "https://www.prarulebook.co.uk/pra-rules"
    root = Node("root", "rulebook", "rulebook:pra-rules", "PRA Rules", url=index_url)
    listed_part = Node("listed", "part", "part:listed", "Listed Part", url="https://www.prarulebook.co.uk/pra-rules/listed/01-06-2026")
    listed_edge = Edge("index-listed", root.id, listed_part.id, "contains", "site_structure", source_url=index_url)
    upsert_nodes(conn, [root, listed_part])
    upsert_edges(conn, [listed_edge])

    run_id = start_ingestion_run(conn, command="test-index-bootstrap")
    reconcile_source_output(
        conn,
        run_id=run_id,
        source_url=index_url,
        source_type="index",
        raw_html="<html><h1>PRA Rules</h1></html>",
        nodes=[root],
        edges=[],
    )
    finish_ingestion_run(conn, run_id=run_id)

    assert conn.execute("SELECT COUNT(*) FROM node WHERE id='listed'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='index-listed'").fetchone()[0] == 0


def test_derived_scope_replaces_only_its_known_edge_layer() -> None:
    conn = _connection()
    source = Node("source", "rule", "rule:source", "Source")
    target = Node("target", "rule", "rule:target", "Target")
    upsert_nodes(conn, [source, target])
    old = Edge("derived-old", source.id, target.id, "shares_defined_term", "derived_term_overlap")
    keep = Edge("explicit", source.id, target.id, "references", "html_link")
    upsert_edges(conn, [old, keep])

    run_id = start_ingestion_run(conn, command="test-derive")
    reconcile_derived_output(
        conn,
        run_id=run_id,
        name="richer-edges",
        edges=[],
        legacy_source_methods=("derived_term_overlap", "title_match"),
    )
    finish_ingestion_run(conn, run_id=run_id)

    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='derived-old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='explicit'").fetchone()[0] == 1


def test_cli_failed_refresh_keeps_previous_source_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
    html = """
    <html><body><h1>Test Part</h1><div class="rulebook-content">
      <div class="chapter-section" id="article-1"><span class="rule-number chapter-number">1</span><h2 class="chapter-heading">Article 1</h2></div>
      <div class="row-block" id="article-1-1"><span class="rule-number">1</span><div class="div-row__col-2"><p>Current text.</p></div></div>
    </div></body></html>
    """
    db = tmp_path / "rulebook.sqlite3"
    raw_dir = tmp_path / "raw"
    out = tmp_path / "graph.json"
    args = SimpleNamespace(
        db=db,
        raw_dir=raw_dir,
        out=out,
        refresh=True,
        include_index=False,
        all_parts=False,
        include_glossary=False,
        full_glossary=False,
        include_crr_terms=False,
        full_crr_terms=False,
        include_guidance=False,
        all_guidance=False,
        guidance=[],
        include_legal_instruments=False,
        derive=True,
        part=[url],
        sleep=0,
    )
    monkeypatch.setattr(scraper_cli, "fetch_url", lambda *args, **kwargs: (url, html, "2026-08-20T18:00:00Z"))
    scraper_cli.command_scrape(args)

    def failed_fetch(*args, **kwargs):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(scraper_cli, "fetch_url", failed_fetch)
    scraper_cli.command_scrape(args)

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM node WHERE node_type='rule'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM ingestion_run ORDER BY started_at DESC LIMIT 1").fetchone()[0] == "failed"
    assert conn.execute(
        "SELECT status FROM ingestion_run_scope WHERE run_id=(SELECT run_id FROM ingestion_run ORDER BY started_at DESC LIMIT 1) AND scope_key='derived:richer-edges'"
    ).fetchone()[0] == "failed"
