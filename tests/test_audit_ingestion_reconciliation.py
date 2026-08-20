from __future__ import annotations

import sqlite3

from backend.rulebook_scraper.models import Node
from backend.rulebook_scraper.store import SCHEMA, finish_ingestion_run, reconcile_source_output, start_ingestion_run
from scripts.audit_ingestion_reconciliation import audit_ingestion_reconciliation


def test_audit_accepts_current_manifest_and_snapshots() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    url = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
    run_id = start_ingestion_run(conn, command="audit-test")
    reconcile_source_output(
        conn,
        run_id=run_id,
        source_url=url,
        source_type="part",
        raw_html="<html>test</html>",
        nodes=[Node("page", "part", "part:test", "Test", url=url)],
    )
    finish_ingestion_run(conn, run_id=run_id)

    report = audit_ingestion_reconciliation(conn)
    assert report["ok"] is True
    assert report["metrics"] == {
        "missing_manifest_nodes": 0,
        "missing_manifest_edges": 0,
        "stale_live_edges": 0,
        "orphan_run_memberships": 0,
    }
    assert report["snapshots"]["rows"] == 1


def test_audit_detects_manifest_endpoint_drift() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO ingestion_run(run_id,started_at,status,command) VALUES ('run','now','completed','test')"
    )
    conn.execute(
        "INSERT INTO ingestion_output(scope_key,object_type,object_id,run_id,updated_at) VALUES ('scope','node','missing','run','now')"
    )
    report = audit_ingestion_reconciliation(conn)
    assert report["ok"] is False
    assert report["metrics"]["missing_manifest_nodes"] == 1
