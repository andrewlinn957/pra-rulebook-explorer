#!/usr/bin/env python3
"""Reconcile duplicate policy occurrences with canonical lexical occurrences.

The deterministic legal-reference pass owns the lexical identity of a citation
at an exact source span.  A policy pass may independently materialise the same
span, sometimes against a legacy alias of the same target node.  Keeping both
rows makes the reader show duplicate references, so this repair removes the
policy duplicate, updates the audit ledger to the deterministic target, and
leaves policy-only occurrences untouched.

Dry-run is the default.  ``--apply`` takes a SQLite backup and performs the
repair in one transaction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DETERMINISTIC_METHOD = "legal_reference_occurrence_v1"
POLICY_METHOD = "resolution_policy_v1"


def parse_metadata(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def occurrence_key(row: sqlite3.Row | dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["source_node_id"],
        int(row["span_start"]),
        int(row["span_end"]),
        str(row["citation_kind"] or "").casefold(),
        str(row["instrument_id"] or "").casefold(),
        str(row["provision_path"] or "").casefold(),
    )


def preferred_target(rows: list[sqlite3.Row]) -> sqlite3.Row:
    """Choose a stable target when old deterministic aliases coexist."""

    return sorted(
        rows,
        key=lambda row: (
            not str(row["target_node_id"] or "").startswith("external:uk-crr:"),
            not str(row["target_node_id"] or "").startswith("external:legislation:"),
            str(row["target_node_id"] or ""),
        ),
    )[0]


def collect_reconciliation(conn: sqlite3.Connection) -> dict[str, Any]:
    canonical: dict[tuple[Any, ...], list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT occurrence_id,source_node_id,target_node_id,edge_id,
               citation_kind,instrument_id,provision_path,span_start,span_end
        FROM reference_occurrence
        WHERE source_method=? AND status='materialized'
          AND target_node_id IS NOT NULL
        """,
        (DETERMINISTIC_METHOD,),
    ):
        canonical[occurrence_key(row)].append(row)

    duplicate_rows: list[dict[str, Any]] = []
    resolution_targets: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT occurrence_id,source_node_id,target_node_id,edge_id,
               citation_kind,instrument_id,provision_path,span_start,span_end,
               metadata_json
        FROM reference_occurrence
        WHERE source_method=? AND status='materialized'
        ORDER BY source_node_id,span_start,occurrence_id
        """,
        (POLICY_METHOD,),
    ):
        candidates = canonical.get(occurrence_key(row), [])
        if not candidates:
            continue
        target = preferred_target(candidates)
        item = {
            "occurrence_id": row["occurrence_id"],
            "source_node_id": row["source_node_id"],
            "old_target_id": row["target_node_id"],
            "new_target_id": target["target_node_id"],
            "old_edge_id": row["edge_id"],
            "canonical_edge_id": target["edge_id"],
            "span_start": row["span_start"],
            "span_end": row["span_end"],
            "citation_kind": row["citation_kind"],
            "instrument_id": row["instrument_id"],
            "provision_path": row["provision_path"],
        }
        duplicate_rows.append(item)
        resolution_id = parse_metadata(row["metadata_json"]).get("resolution_id")
        if resolution_id:
            resolution_targets.setdefault(
                str(resolution_id),
                {
                    "target_id": target["target_node_id"],
                    "source_node_id": row["source_node_id"],
                    "old_target_ids": set(),
                    "occurrence_ids": [],
                },
            )
            resolution_targets[str(resolution_id)]["old_target_ids"].add(
                row["target_node_id"]
            )
            resolution_targets[str(resolution_id)]["occurrence_ids"].append(
                row["occurrence_id"]
            )

    return {
        "duplicate_rows": duplicate_rows,
        "resolution_targets": resolution_targets,
    }


def backup_database(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(
        f"{db_path.name}.pre-target-reconciliation-{stamp}"
    )
    source = connect(db_path)
    destination = connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def apply_reconciliation(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
) -> dict[str, int]:
    duplicate_rows = plan["duplicate_rows"]
    resolution_targets = plan["resolution_targets"]
    deleted_occurrences = 0
    updated_resolutions = 0
    deleted_edges = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in duplicate_rows:
            cursor = conn.execute(
                "DELETE FROM reference_occurrence WHERE occurrence_id=? AND source_method=?",
                (item["occurrence_id"], POLICY_METHOD),
            )
            deleted_occurrences += cursor.rowcount

        for resolution_id, item in resolution_targets.items():
            target = conn.execute(
                "SELECT id,node_type,title,url FROM node WHERE id=?",
                (item["target_id"],),
            ).fetchone()
            if target is None:
                continue
            row = conn.execute(
                "SELECT metadata_json,resolver_method FROM llm_reference_resolution WHERE id=?",
                (resolution_id,),
            ).fetchone()
            if row is None:
                continue
            resolution_metadata = parse_metadata(row["metadata_json"])
            resolution_metadata["canonical_target_reconciliation"] = {
                "method": DETERMINISTIC_METHOD,
                "duplicate_occurrences_removed": item["occurrence_ids"],
                "previous_target_ids": sorted(item["old_target_ids"]),
                "target_id": target["id"],
            }
            conn.execute(
                """
                UPDATE llm_reference_resolution
                SET target_node_id=?,target_node_type=?,target_title=?,
                    resolver_method=?,metadata_json=?
                WHERE id=?
                """,
                (
                    target["id"],
                    target["node_type"],
                    target["title"],
                    "policy_canonical_occurrence_target",
                    json.dumps(resolution_metadata, ensure_ascii=False, sort_keys=True),
                    resolution_id,
                ),
            )
            updated_resolutions += 1

        # A policy edge that lost all materialised occurrences and is no
        # longer referenced by the ledger is an orphan created by the stale
        # target. Build the ledger key set once so this remains set-based for
        # the large corpus rather than doing one full ledger scan per edge.
        conn.execute(
            """
            CREATE TEMP TABLE _resolved_policy_pairs AS
            SELECT DISTINCT source_node_id,target_node_id
            FROM llm_reference_resolution
            WHERE coalesce(resolution_status,'resolved')='resolved'
              AND trim(coalesce(target_node_id,''))<>''
            """
        )
        conn.execute(
            "CREATE INDEX _resolved_policy_pairs_key ON _resolved_policy_pairs(source_node_id,target_node_id)"
        )
        conn.execute(
            """
            CREATE TEMP TABLE _live_materialized_edges AS
            SELECT DISTINCT edge_id
            FROM reference_occurrence
            WHERE status='materialized' AND edge_id IS NOT NULL
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX _live_materialized_edges_key ON _live_materialized_edges(edge_id)"
        )
        cursor = conn.execute(
            """
            DELETE FROM edge
            WHERE source_method=?
              AND NOT EXISTS (
                SELECT 1 FROM _live_materialized_edges live
                WHERE live.edge_id=edge.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM _resolved_policy_pairs pair
                WHERE pair.source_node_id=edge.from_node_id
                  AND pair.target_node_id=edge.to_node_id
              )
            """,
            (POLICY_METHOD,),
        )
        deleted_edges = cursor.rowcount
        conn.execute("DROP TABLE _live_materialized_edges")
        conn.execute("DROP TABLE _resolved_policy_pairs")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "duplicate_policy_occurrences_removed": int(deleted_occurrences),
        "ledger_resolutions_updated": updated_resolutions,
        "orphan_policy_edges_removed": deleted_edges,
    }


def run(db_path: Path, apply: bool) -> dict[str, Any]:
    conn = connect(db_path)
    plan = collect_reconciliation(conn)
    report: dict[str, Any] = {
        "db": str(db_path),
        "apply_requested": apply,
        "duplicate_policy_occurrences": len(plan["duplicate_rows"]),
        "resolutions_with_canonical_matches": len(plan["resolution_targets"]),
        "examples": plan["duplicate_rows"][:20],
    }
    if apply and plan["duplicate_rows"]:
        report["backup"] = str(backup_database(db_path))
        report.update(apply_reconciliation(conn, plan))
    conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.db, args.apply), indent=2, ensure_ascii=False, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())