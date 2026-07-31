#!/usr/bin/env python3
"""Dry-run or apply evidence-checked reference-recall proposals.

The staging database is deliberately separate from the Rulebook database.  A
normal invocation only audits eligible proposals.  ``--apply`` is required to
write anything, and the write path re-checks the source hash, exact span,
target identity, duplicate relationships and self-reference constraints in one
transaction before inserting versioned edges and occurrences.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reference_recall_audit import connect_source, digest, source_text  # noqa: E402
from scripts.reference_recall_stage import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_STAGE,
    relationship_for_target,
)


MATERIALIZER_VERSION = "reference-recall-materializer-v1"
SOURCE_METHOD = "reference_recall_stage_v1"
DEFAULT_AUDIT = ROOT / "logs" / "reference-recall-materialize-20260731.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: object, length: int = 24) -> str:
    return hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:length]


def connect_writable(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def load_nodes(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["id"]: row
        for row in conn.execute(
            "SELECT id,node_type,title,text,url,metadata_json FROM node"
        )
    }


def compact(value: str) -> str:
    return " ".join((value or "").split())


def evidence_window(text: str, start: int, end: int, radius: int = 220) -> str:
    return compact(text[max(0, start - radius) : min(len(text), end + radius)])


def source_hash_matches(row: sqlite3.Row, source: sqlite3.Row) -> bool:
    return digest(source_text(source)) == row["source_text_hash"]


def exact_span_matches(row: sqlite3.Row, source: sqlite3.Row) -> bool:
    start, end = row["span_start"], row["span_end"]
    quote = row["quoted_text"] or ""
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    text = source_text(source)
    return 0 <= start <= end <= len(text) and bool(quote) and text[start:end] == quote


def edge_type(row: sqlite3.Row, target: sqlite3.Row) -> tuple[str, str]:
    relationship, expected = relationship_for_target(target)
    stored = str(row["relationship_type"] or "")
    if stored in {"DEF", "REF"}:
        relationship = stored
        expected = "uses_defined_term" if stored == "DEF" else "references"
    return relationship, expected


def existing_edge(conn: sqlite3.Connection, source_id: str, target_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id,edge_type,source_method FROM edge WHERE from_node_id=? AND to_node_id=? ORDER BY id LIMIT 1",
        (source_id, target_id),
    ).fetchone()


def occurrence_exists(conn: sqlite3.Connection, source_id: str, start: int, end: int) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1 FROM reference_occurrence
            WHERE source_node_id=? AND status='materialized'
              AND span_start < ? AND span_end > ?
            LIMIT 1
            """,
            (source_id, end, start),
        ).fetchone()
    )


def proposal_result(
    stage_row: sqlite3.Row,
    source: sqlite3.Row | None,
    target: sqlite3.Row | None,
    source_conn: sqlite3.Connection,
    *,
    allow_existing_edge: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "proposal_id": stage_row["proposal_id"],
        "source_node_id": stage_row["source_node_id"],
        "target_node_id": stage_row["target_node_id"],
        "proposal_method": stage_row["proposal_method"],
        "confidence": float(stage_row["confidence"] or 0),
        "status": "eligible",
        "reasons": [],
    }
    if source is None:
        result.update(status="source_missing", reasons=["source_node_missing"])
        return result
    if target is None:
        result.update(status="target_missing", reasons=["target_node_missing"])
        return result
    if source["id"] == target["id"]:
        result.update(status="self_reference", reasons=["source_and_target_are_identical"])
        return result
    if not source_hash_matches(stage_row, source):
        result.update(status="source_changed", reasons=["source_text_hash_does_not_match_stage"])
        return result
    if not exact_span_matches(stage_row, source):
        result.update(status="span_invalid", reasons=["quoted_text_is_not_exact_source_substring"])
        return result
    relation, graph_type = edge_type(stage_row, target)
    result.update(relationship_type=relation, edge_type=graph_type)
    existing = existing_edge(source_conn, source["id"], target["id"])
    if existing:
        if not allow_existing_edge:
            result.update(
                status="duplicate_edge",
                reasons=["source_target_relationship_already_exists"],
                existing_edge_id=existing["id"],
                existing_edge_type=existing["edge_type"],
            )
            return result
        result.update(
            existing_edge_id=existing["id"],
            existing_edge_type=existing["edge_type"],
            reasons=["source_target_relationship_reused_for_new_occurrence"],
        )
    start, end = int(stage_row["span_start"]), int(stage_row["span_end"])
    if occurrence_exists(source_conn, source["id"], start, end):
        result.update(status="duplicate_occurrence", reasons=["source_span_already_has_materialized_occurrence"])
        return result
    result["reasons"] = ["source_hash_span_target_and_duplicate_checks_pass"]
    return result


def edge_row(stage_row: sqlite3.Row, source: sqlite3.Row, target: sqlite3.Row, relationship: str, graph_type: str) -> tuple[Any, ...]:
    edge_id = stable_id(source["id"], target["id"], graph_type, SOURCE_METHOD, length=20)
    evidence = evidence_window(source_text(source), int(stage_row["span_start"]), int(stage_row["span_end"]))
    evidence_json = json.loads(stage_row["evidence_json"] or "{}")
    metadata = {
        "reference": stage_row["quoted_text"],
        "target_title": target["title"] or "",
        "target_node_type": target["node_type"],
        "relationship_type": relationship,
        "source_span": {
            "start": int(stage_row["span_start"]),
            "end": int(stage_row["span_end"]),
        },
        "source_text_hash": stage_row["source_text_hash"],
        "stage_proposal_id": stage_row["proposal_id"],
        "stage_version": MATERIALIZER_VERSION,
        "proposal_method": stage_row["proposal_method"],
        "evidence": evidence_json,
    }
    return (
        edge_id,
        source["id"],
        target["id"],
        graph_type,
        SOURCE_METHOD,
        float(stage_row["confidence"] or 0),
        evidence,
        source["url"] or "",
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )


def occurrence_row(stage_row: sqlite3.Row, source: sqlite3.Row, target: sqlite3.Row, edge_id: str, relationship: str) -> tuple[Any, ...]:
    start, end = int(stage_row["span_start"]), int(stage_row["span_end"])
    quoted = stage_row["quoted_text"] or ""
    occurrence_id = stable_id(source["id"], target["id"], start, end, quoted, SOURCE_METHOD, length=28)
    group_id = stable_id(source["id"], start, end, quoted, SOURCE_METHOD, length=24)
    evidence_json = json.loads(stage_row["evidence_json"] or "{}")
    metadata = {
        "source_text_hash": stage_row["source_text_hash"],
        "stage_proposal_id": stage_row["proposal_id"],
        "stage_version": MATERIALIZER_VERSION,
        "proposal_method": stage_row["proposal_method"],
        "target_node_type": target["node_type"],
        "evidence": evidence_json,
    }
    text = source_text(source)
    return (
        occurrence_id,
        group_id,
        source["id"],
        target["id"],
        edge_id,
        relationship,
        (stage_row["citation_kind"] or stage_row["target_node_type"] or "reference").lower(),
        quoted,
        quoted,
        None,
        None,
        "",
        start,
        end,
        "materialized",
        SOURCE_METHOD,
        float(stage_row["confidence"] or 0),
        evidence_window(text, start, end),
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )


def materialize(args: argparse.Namespace) -> dict[str, Any]:
    stage_conn = connect_source(args.stage)
    source_conn = connect_writable(args.db) if args.apply else connect_source(args.db)
    nodes = load_nodes(source_conn)
    methods = {method.strip() for method in (args.method or []) if method.strip()}
    query = "SELECT * FROM staged_repair WHERE status='eligible'"
    params: list[Any] = []
    if methods:
        query += " AND proposal_method IN (" + ",".join("?" for _ in methods) + ")"
        params.extend(sorted(methods))
    query += " ORDER BY source_node_id,span_start,span_end,proposal_id"
    staged_rows = stage_conn.execute(query, params).fetchall()

    results: list[dict[str, Any]] = []
    edge_rows: list[tuple[Any, ...]] = []
    occurrence_rows: list[tuple[Any, ...]] = []
    accepted_ids: set[str] = set()
    for row in staged_rows:
        source = nodes.get(row["source_node_id"])
        target = nodes.get(row["target_node_id"])
        result = proposal_result(
            row,
            source,
            target,
            source_conn,
            allow_existing_edge=bool(args.allow_existing_edge),
        )
        if result["status"] == "eligible":
            relationship, graph_type = edge_type(row, target)
            existing_edge_id = result.get("existing_edge_id")
            edge_values = edge_row(row, source, target, relationship, graph_type)
            edge_id = existing_edge_id or edge_values[0]
            occurrence_values = occurrence_row(row, source, target, edge_values[0], relationship)
            # Multiple spans can share one edge; keep each occurrence but only
            # insert the edge once per apply transaction.
            if not existing_edge_id and edge_values[0] not in accepted_ids:
                edge_rows.append(edge_values)
                accepted_ids.add(edge_values[0])
            # Rebuild the occurrence when an existing edge is reused so its
            # edge_id points at the already-materialised relationship.
            if existing_edge_id:
                occurrence_values = occurrence_row(
                    row, source, target, existing_edge_id, relationship
                )
            occurrence_rows.append(occurrence_values)
            result.update(edge_id=edge_id, occurrence_id=occurrence_values[0])
        results.append(result)

    counts = Counter(result["status"] for result in results)
    summary: dict[str, Any] = {
        "materializer_version": MATERIALIZER_VERSION,
        "stage": str(args.stage),
        "db": str(args.db),
        "apply": bool(args.apply),
        "methods": sorted(methods),
        "eligible_rows_considered": len(staged_rows),
        "edge_rows_ready": len(edge_rows),
        "occurrence_rows_ready": len(occurrence_rows),
        "status_counts": dict(sorted(counts.items())),
        "generated_at": now(),
        "applied": False,
    }
    if args.apply and (edge_rows or occurrence_rows):
        try:
            source_conn.execute("BEGIN IMMEDIATE")
            source_conn.executemany(
                """
                INSERT INTO edge(
                  id,from_node_id,to_node_id,edge_type,source_method,confidence,
                  evidence_text,source_url,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO NOTHING
                """,
                edge_rows,
            )
            source_conn.executemany(
                """
                INSERT INTO reference_occurrence(
                  occurrence_id,group_id,source_node_id,target_node_id,edge_id,
                  relationship_type,citation_kind,citation_text,group_text,
                  instrument_id,provision_path,qualifier,span_start,span_end,
                  status,source_method,confidence,context_text,metadata_json,
                  updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(occurrence_id) DO NOTHING
                """,
                occurrence_rows,
            )
            source_conn.commit()
            summary.update(applied=True, applied_edges=len(edge_rows), applied_occurrences=len(occurrence_rows))
        except Exception:
            source_conn.rollback()
            raise

    audit_path = args.audit
    if audit_path:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps({**summary, "rows": results}, indent=2, ensure_ascii=False), encoding="utf-8")
        summary["audit"] = str(audit_path)
    stage_conn.close()
    source_conn.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--method", action="append", help="Limit to one staged proposal method (repeatable).")
    parser.add_argument(
        "--allow-existing-edge",
        action="store_true",
        help="Reuse an existing source-target edge when materialising a new exact occurrence.",
    )
    parser.add_argument("--apply", action="store_true", help="Commit eligible edges and occurrences; default is read-only dry run.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(materialize(args), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
