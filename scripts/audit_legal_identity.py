#!/usr/bin/env python3
"""Read-only audit for canonical provision/version/source-page invariants."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Allow the documented ``python scripts/audit_legal_identity.py`` invocation
# as well as importing the module from the project root during tests.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db import DEFAULT_DB, connect


DATED_LEGACY_KEY_RE = re.compile(r"^rule:.*?/\d{2}-\d{2}-\d{4}(?::|$)")


def audit_legal_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    node_rows = conn.execute("SELECT id,node_type,stable_key,metadata_json FROM node").fetchall()
    nodes = {row["id"]: row for row in node_rows}
    metadata = {row["id"]: _json(row["metadata_json"]) for row in node_rows}
    versions = [row for row in node_rows if metadata[row["id"]].get("identity_type") == "provision_version"]
    canonical = [row for row in node_rows if row["node_type"] == "provision" and metadata[row["id"]].get("identity_type") in {None, "canonical_provision"}]
    source_pages = [row for row in node_rows if row["node_type"] == "part" and metadata[row["id"]].get("identity_type") == "source_page"]
    snapshots = _count_if_exists(conn, "document_snapshot")
    legacy_keys = [row for row in node_rows if row["node_type"] == "rule" and DATED_LEGACY_KEY_RE.match(row["stable_key"] or "")]

    versions_missing_canonical = sum(
        1
        for row in versions
        if not metadata[row["id"]].get("canonical_provision_id")
        or metadata[row["id"]].get("canonical_provision_id") not in nodes
        or nodes[metadata[row["id"]].get("canonical_provision_id")]["node_type"] != "provision"
    )
    sourced_from = {
        row["from_node_id"]: row["to_node_id"]
        for row in conn.execute("SELECT from_node_id,to_node_id FROM edge WHERE edge_type='sourced_from'")
    }
    versions_missing_source_page = sum(
        1
        for row in versions
        if row["id"] not in sourced_from
        or sourced_from[row["id"]] not in nodes
        or nodes[sourced_from[row["id"]]]["node_type"] != "part"
    )
    snapshot_ids = set()
    if _has_table(conn, "document_snapshot"):
        snapshot_ids = {row[0] for row in conn.execute("SELECT snapshot_id FROM document_snapshot")}
    versions_missing_snapshot = sum(1 for row in versions if metadata[row["id"]].get("snapshot_id") not in snapshot_ids)
    source_pages_missing_snapshot = sum(1 for row in source_pages if metadata[row["id"]].get("snapshot_id") not in snapshot_ids)
    versions_missing_identity_edge = conn.execute(
        """
        SELECT COUNT(*)
        FROM node v
        WHERE json_extract(v.metadata_json,'$.identity_type')='provision_version'
          AND NOT EXISTS (
            SELECT 1
            FROM edge e
            WHERE e.from_node_id=json_extract(v.metadata_json,'$.canonical_provision_id')
              AND e.to_node_id=v.id
              AND e.edge_type='has_version'
          )
        """
    ).fetchone()[0]
    semantic_edges_to_versions = conn.execute(
        """
        SELECT COUNT(*)
        FROM edge e JOIN node target ON target.id=e.to_node_id
        WHERE e.edge_type IN ('references','amends')
          AND json_extract(target.metadata_json,'$.identity_type')='provision_version'
        """
    ).fetchone()[0]
    occurrence_targets_to_versions = 0
    if _has_table(conn, "reference_occurrence"):
        occurrence_targets_to_versions = conn.execute(
            """
            SELECT COUNT(*)
            FROM reference_occurrence r JOIN node target ON target.id=r.target_node_id
            WHERE r.target_node_id IS NOT NULL
              AND json_extract(target.metadata_json,'$.identity_type')='provision_version'
            """
        ).fetchone()[0]

    edge_missing_endpoint = conn.execute(
        """
        SELECT COUNT(*) FROM edge e
        WHERE NOT EXISTS (SELECT 1 FROM node n WHERE n.id=e.from_node_id)
           OR NOT EXISTS (SELECT 1 FROM node n WHERE n.id=e.to_node_id)
        """
    ).fetchone()[0]
    occurrence_missing_endpoint = 0
    if _has_table(conn, "reference_occurrence"):
        occurrence_missing_endpoint = conn.execute(
            """
            SELECT COUNT(*) FROM reference_occurrence r
            WHERE NOT EXISTS (SELECT 1 FROM node n WHERE n.id=r.source_node_id)
               OR (r.target_node_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM node n WHERE n.id=r.target_node_id))
               OR (r.edge_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.id=r.edge_id))
            """
        ).fetchone()[0]

    canonical_without_version = conn.execute(
        """
        SELECT COUNT(*) FROM node n
        WHERE n.node_type='provision'
          AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.from_node_id=n.id AND e.edge_type='has_version')
        """
    ).fetchone()[0]
    aliases_missing_node = 0
    if _has_table(conn, "node_alias"):
        aliases_missing_node += conn.execute(
            "SELECT COUNT(*) FROM node_alias a WHERE NOT EXISTS (SELECT 1 FROM node n WHERE n.id=a.node_id)"
        ).fetchone()[0]
    if _has_table(conn, "node_aliases"):
        aliases_missing_node += conn.execute(
            "SELECT COUNT(*) FROM node_aliases a WHERE NOT EXISTS (SELECT 1 FROM node n WHERE n.id=a.node_id)"
        ).fetchone()[0]

    metrics = {
        "canonical_provisions": len(canonical),
        "provision_versions": len(versions),
        "source_pages": len(source_pages),
        "snapshots": snapshots,
        "legacy_dated_rule_keys": len(legacy_keys),
        "versions_missing_canonical": versions_missing_canonical,
        "versions_missing_source_page": versions_missing_source_page,
        "versions_missing_snapshot": versions_missing_snapshot,
        "source_pages_missing_snapshot": source_pages_missing_snapshot,
        "versions_missing_identity_edge": versions_missing_identity_edge,
        "semantic_edges_to_versions": semantic_edges_to_versions,
        "occurrence_targets_to_versions": occurrence_targets_to_versions,
        "canonical_without_version": canonical_without_version,
        "edge_missing_endpoint": edge_missing_endpoint,
        "occurrence_missing_endpoint": occurrence_missing_endpoint,
        "aliases_missing_node": aliases_missing_node,
    }
    failures = {
        "legacy_dated_rule_keys": metrics["legacy_dated_rule_keys"],
        "versions_missing_canonical": metrics["versions_missing_canonical"],
        "versions_missing_source_page": metrics["versions_missing_source_page"],
        "versions_missing_snapshot": metrics["versions_missing_snapshot"],
        "source_pages_missing_snapshot": metrics["source_pages_missing_snapshot"],
        "versions_missing_identity_edge": metrics["versions_missing_identity_edge"],
        "semantic_edges_to_versions": metrics["semantic_edges_to_versions"],
        "occurrence_targets_to_versions": metrics["occurrence_targets_to_versions"],
        "canonical_without_version": metrics["canonical_without_version"],
        "edge_missing_endpoint": metrics["edge_missing_endpoint"],
        "occurrence_missing_endpoint": metrics["occurrence_missing_endpoint"],
        "aliases_missing_node": metrics["aliases_missing_node"],
    }
    return {"ok": all(value == 0 for value in failures.values()), "metrics": metrics, "failures": failures}


def _json(value: object) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _count_if_exists(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if _has_table(conn, table) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit canonical legal identity and provision version invariants")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    conn = connect(args.db)
    report = audit_legal_identity(conn)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
