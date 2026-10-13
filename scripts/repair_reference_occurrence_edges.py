#!/usr/bin/env python3
"""Repair materialized citation-to-edge links without discarding evidence.

The default mode is a read-only preview. Exact source, target, relationship and
wording matches reuse an existing edge. Every other resolved citation gets its
own provenance-marked edge, avoiding guesses between parallel graph assertions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend/data/rulebook.sqlite3"


def expected_edge_type(relationship_type: str) -> str | None:
    return {
        "REF": "references",
        "DEF": "uses_defined_term",
        "AMEND": "amends",
        "AMENDS": "amends",
    }.get((relationship_type or "").upper())


def occurrence_edge_id(source_id: str, target_id: str, edge_type: str, occurrence_id: str) -> str:
    key = "|".join((source_id, target_id, edge_type, "occurrence", occurrence_id))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def plan_repairs(conn: sqlite3.Connection) -> tuple[list[dict], Counter[str]]:
    rows = conn.execute(
        """
        SELECT o.occurrence_id,o.source_node_id,o.target_node_id,o.edge_id,
               o.relationship_type,o.source_method,o.confidence,o.citation_text,
               o.group_text,o.metadata_json,s.url AS source_url,
               s.id AS source_exists,t.id AS target_exists,
               e.id AS stored_edge_id,e.from_node_id AS stored_from,
               e.to_node_id AS stored_to
        FROM reference_occurrence o
        LEFT JOIN edge e ON e.id=o.edge_id
        LEFT JOIN node s ON s.id=o.source_node_id
        LEFT JOIN node t ON t.id=o.target_node_id
        WHERE o.status='materialized'
          AND (e.id IS NULL OR e.from_node_id<>o.source_node_id
               OR e.to_node_id<>o.target_node_id
               OR e.edge_type<>CASE UPPER(o.relationship_type)
                 WHEN 'REF' THEN 'references'
                 WHEN 'DEF' THEN 'uses_defined_term'
                 WHEN 'AMEND' THEN 'amends'
                 WHEN 'AMENDS' THEN 'amends'
                 ELSE '' END)
        ORDER BY o.occurrence_id
        """
    ).fetchall()
    plan: list[dict] = []
    counts: Counter[str] = Counter()
    for row in rows:
        if not row["source_exists"]:
            counts["missing_source"] += 1
            continue
        if not row["target_exists"]:
            counts["missing_target"] += 1
            continue
        graph_type = expected_edge_type(row["relationship_type"])
        if not graph_type:
            counts[f"unsupported_relationship:{row['relationship_type']}"] += 1
            continue
        evidence = str(row["citation_text"] or "").strip()
        candidates = conn.execute(
            """
            SELECT id FROM edge
            WHERE from_node_id=? AND to_node_id=? AND edge_type=?
              AND lower(trim(COALESCE(evidence_text,'')))=lower(?)
            ORDER BY id
            """,
            (row["source_node_id"], row["target_node_id"], graph_type, evidence),
        ).fetchall()
        if len(candidates) == 1:
            edge_id = str(candidates[0]["id"])
            action = "attach_exact_edge"
        else:
            edge_id = occurrence_edge_id(
                str(row["source_node_id"]), str(row["target_node_id"]),
                graph_type, str(row["occurrence_id"]),
            )
            action = "create_occurrence_edge"
        counts[action] += 1
        plan.append({
            "occurrence_id": str(row["occurrence_id"]),
            "source_node_id": str(row["source_node_id"]),
            "target_node_id": str(row["target_node_id"]),
            "edge_id": edge_id,
            "edge_type": graph_type,
            "source_method": str(row["source_method"] or "reference_recall_stage_v1"),
            "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
            "evidence_text": evidence or str(row["group_text"] or ""),
            "source_url": str(row["source_url"] or ""),
            "metadata": {
                "citation_occurrence_id": str(row["occurrence_id"]),
                "repair": "occurrence_provenance_preserved",
            },
            "action": action,
        })
    return plan, counts


def broken_association_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        """
        SELECT COUNT(*) FROM reference_occurrence o
        LEFT JOIN edge e ON e.id=o.edge_id
        WHERE o.status='materialized'
          AND (e.id IS NULL OR e.from_node_id<>o.source_node_id
               OR e.to_node_id<>o.target_node_id
               OR e.edge_type<>CASE UPPER(o.relationship_type)
                 WHEN 'REF' THEN 'references'
                 WHEN 'DEF' THEN 'uses_defined_term'
                 WHEN 'AMEND' THEN 'amends'
                 WHEN 'AMENDS' THEN 'amends'
                 ELSE '' END)
        """
    ).fetchone()[0])


def consistent_backup(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path), timeout=60)
    target = sqlite3.connect(str(backup_path), timeout=60)
    try:
        source.backup(target, pages=2048, sleep=0.05)
    finally:
        target.close()
        source.close()


def apply_plan(conn: sqlite3.Connection, plan: list[dict]) -> int:
    inserted = 0
    for item in plan:
        if item["action"] == "create_occurrence_edge":
            existing = conn.execute(
                "SELECT from_node_id,to_node_id,edge_type FROM edge WHERE id=?",
                (item["edge_id"],),
            ).fetchone()
            if existing and tuple(existing) != (
                item["source_node_id"], item["target_node_id"], item["edge_type"],
            ):
                raise RuntimeError(f"deterministic edge ID collision for {item['occurrence_id']}")
            if not existing:
                conn.execute(
                    """
                    INSERT INTO edge
                      (id,from_node_id,to_node_id,edge_type,source_method,confidence,
                       evidence_text,source_url,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item["edge_id"], item["source_node_id"], item["target_node_id"],
                        item["edge_type"], item["source_method"], item["confidence"],
                        item["evidence_text"], item["source_url"],
                        json.dumps(item["metadata"], ensure_ascii=False, sort_keys=True),
                    ),
                )
                inserted += 1
        conn.execute(
            "UPDATE reference_occurrence SET edge_id=? WHERE occurrence_id=?",
            (item["edge_id"], item["occurrence_id"]),
        )
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="Back up the database and apply the repair")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()
    db_path = args.db.resolve()
    if not db_path.is_file():
        parser.error(f"database does not exist: {db_path}")
    uri = f"file:{db_path}?mode=rw" if args.apply else f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        plan, counts = plan_repairs(conn)
        print(json.dumps({"mode": "apply" if args.apply else "preview", "planned": dict(counts), "remaining_before": broken_association_count(conn)}, indent=2))
        if not args.apply:
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = (args.backup_dir or db_path.parent / "backups").resolve()
        backup_path = backup_dir / f"{db_path.stem}.pre-occurrence-repair-{stamp}{db_path.suffix}"
        conn.close()
        consistent_backup(db_path, backup_path)

        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN IMMEDIATE")
        plan, counts = plan_repairs(conn)
        inserted = apply_plan(conn, plan)
        remaining = broken_association_count(conn)
        unsupported = {key: value for key, value in counts.items() if key.startswith("unsupported_relationship:")}
        if remaining or unsupported:
            raise RuntimeError(f"repair left {remaining} broken associations; unsupported relationship rows: {unsupported}")
        conn.commit()
        receipt = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": str(db_path),
            "backup": str(backup_path),
            "reused_exact_edges": counts["attach_exact_edge"],
            "created_occurrence_edges": inserted,
            "repaired_occurrences": len(plan),
            "remaining_broken_associations": remaining,
        }
        receipt_path = backup_path.with_suffix(backup_path.suffix + ".repair.json")
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt, indent=2))
        return 0
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
