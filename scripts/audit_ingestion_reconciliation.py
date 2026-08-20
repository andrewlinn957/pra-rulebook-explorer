#!/usr/bin/env python3
"""Audit the current ingestion manifests and their live graph endpoints."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def audit_ingestion_reconciliation(conn: sqlite3.Connection) -> dict[str, Any]:
    required = {"ingestion_run", "ingestion_run_scope", "ingestion_output"}
    available = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            tuple(sorted(required)),
        )
    }
    missing_tables = sorted(required - available)
    if missing_tables:
        return {"ok": False, "missing_tables": missing_tables}

    missing_nodes = conn.execute(
        """
        SELECT COUNT(*) FROM ingestion_output o
        WHERE o.object_type='node'
          AND NOT EXISTS (SELECT 1 FROM node n WHERE n.id=o.object_id)
        """
    ).fetchone()[0]
    missing_edges = conn.execute(
        """
        SELECT COUNT(*) FROM ingestion_output o
        WHERE o.object_type='edge'
          AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.id=o.object_id)
        """
    ).fetchone()[0]
    stale_live_edges = conn.execute(
        """
        SELECT COUNT(*) FROM edge e
        WHERE NOT EXISTS (SELECT 1 FROM node n WHERE n.id=e.from_node_id)
           OR NOT EXISTS (SELECT 1 FROM node n WHERE n.id=e.to_node_id)
        """
    ).fetchone()[0]
    orphan_run_memberships = conn.execute(
        """
        SELECT COUNT(*) FROM ingestion_output o
        WHERE NOT EXISTS (SELECT 1 FROM ingestion_run r WHERE r.run_id=o.run_id)
        """
    ).fetchone()[0]
    run_status = {
        row[0]: row[1]
        for row in conn.execute("SELECT status,COUNT(*) FROM ingestion_run GROUP BY status ORDER BY status")
    }
    scope_status = {
        row[0]: row[1]
        for row in conn.execute("SELECT status,COUNT(*) FROM ingestion_run_scope GROUP BY status ORDER BY status")
    }
    output_counts = {
        f"{row[0]}:{row[1]}": row[2]
        for row in conn.execute(
            "SELECT scope_key,object_type,COUNT(*) FROM ingestion_output GROUP BY scope_key,object_type ORDER BY scope_key,object_type"
        )
    }
    snapshots = conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT url),COUNT(DISTINCT content_hash) FROM document_snapshot"
    ).fetchone()
    metrics = {
        "missing_manifest_nodes": missing_nodes,
        "missing_manifest_edges": missing_edges,
        "stale_live_edges": stale_live_edges,
        "orphan_run_memberships": orphan_run_memberships,
    }
    return {
        "ok": all(value == 0 for value in metrics.values()),
        "metrics": metrics,
        "run_status": run_status,
        "scope_status": scope_status,
        "output_counts": output_counts,
        "snapshots": {
            "rows": snapshots[0],
            "urls": snapshots[1],
            "content_hashes": snapshots[2],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        report = audit_ingestion_reconciliation(conn)
    finally:
        conn.close()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not report.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
