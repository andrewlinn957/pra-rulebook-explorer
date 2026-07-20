#!/usr/bin/env python3
"""Create missing InstructionSet evidence routes for instruction PDFs.

This repair keeps physical files as ``SourceDocument`` nodes and creates a
separate semantic ``InstructionSet`` node when no deterministic evidence route
already exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect as connect_db
from scripts.validate_reporting_source_evidence import has_instruction_evidence, source_node_id

DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return value[:80] or "instruction"


def stable_edge_id(source: str, edge_type: str, target: str) -> str:
    digest = hashlib.sha1(f"{source}|{edge_type}|{target}".encode("utf-8")).hexdigest()[:16]
    return f"edge:instruction-evidence:{digest}"


def instruction_node_for(row: sqlite3.Row) -> tuple[str, str, dict[str, Any]]:
    title = row["title"] or row["source_id"]
    label = title if re.search(r"instruction", title, re.I) else f"{title} instructions"
    node_id = f"instruction_set:source:{slug(row['source_id'])}"
    props = {
        "source_document_ids": [row["source_id"]],
        "source_url": row["url"],
        "repair_source": "repair_reporting_instruction_evidence",
    }
    return node_id, label, props


def repair_instruction_evidence(db_path: Path = DB_PATH, *, apply: bool = False) -> dict[str, Any]:
    conn = connect_db(db_path)
    rows = conn.execute(
        """
        SELECT sd.source_id,sd.title,sd.url,sd.local_path,sd.file_type,c.source_kind
        FROM source_document_cleanup c
        JOIN source_document sd ON sd.source_id=c.source_id
        WHERE c.decision='canonical'
          AND c.source_kind='instruction_pdf'
        ORDER BY sd.source_id
        """
    ).fetchall()
    candidates = [row for row in rows if not has_instruction_evidence(conn, row["source_id"])]
    nodes_created = edges_created = 0
    if apply:
        for row in candidates:
            src_node = source_node_id(row["source_id"])
            if not conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (src_node,)).fetchone():
                conn.execute(
                    """
                    INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,review_status)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (src_node, "SourceDocument", row["title"], "source_document", row["source_id"], "{}", "accepted_candidate"),
                )
            instruction_id, label, props = instruction_node_for(row)
            existed = conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (instruction_id,)).fetchone()
            conn.execute(
                """
                INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,review_status)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                  label=excluded.label,
                  properties_json=excluded.properties_json,
                  review_status=CASE
                    WHEN graph_node.review_status='accepted_candidate' THEN graph_node.review_status
                    ELSE excluded.review_status
                  END
                """,
                (
                    instruction_id,
                    "InstructionSet",
                    label,
                    "instruction_source_repair",
                    instruction_id,
                    json.dumps(props, sort_keys=True),
                    "accepted_candidate",
                ),
            )
            nodes_created += 0 if existed else 1
            edge_id = stable_edge_id(instruction_id, "EVIDENCED_BY", src_node)
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO graph_edge(
                  edge_id,source_node_id,target_node_id,edge_type,properties_json,confidence,extraction_method,review_status
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    edge_id,
                    instruction_id,
                    src_node,
                    "EVIDENCED_BY",
                    json.dumps({"source": "repair_reporting_instruction_evidence"}, sort_keys=True),
                    0.86,
                    "deterministic_instruction_source_repair",
                    "accepted_candidate",
                ),
            )
            edges_created += 1 if conn.total_changes > before else 0
        conn.commit()
    conn.close()
    return {
        "status": "applied" if apply else "dry_run",
        "instruction_sources": len(rows),
        "missing_evidence_routes": len(candidates),
        "nodes_created": nodes_created,
        "edges_created": edges_created,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    print(json.dumps(repair_instruction_evidence(args.db, apply=args.apply), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
