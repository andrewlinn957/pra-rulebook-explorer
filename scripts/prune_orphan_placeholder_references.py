#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
LOG = ROOT / "logs" / "orphan-placeholder-reference-prune.json"

REFERENCE_TYPES = ("rule_reference", "external_reference")


def repair(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        """
        SELECT n.id,n.node_type,n.title,n.url,n.stable_key
        FROM node n
        WHERE n.node_type IN ('rule_reference','external_reference')
          AND json_extract(n.metadata_json,'$.placeholder')=1
          AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.from_node_id=n.id OR e.to_node_id=n.id)
        ORDER BY n.node_type,n.title,n.id
        """
    )]

    deleted = 0
    if not dry_run:
        for row in rows:
            conn.execute("DELETE FROM node WHERE id=?", (row["id"],))
            if conn.execute("SELECT changes()").fetchone()[0]:
                deleted += 1
        conn.commit()

    summary = {
        "dry_run": dry_run,
        "orphan_placeholder_references": len(rows),
        "deleted_placeholders": deleted,
        "by_type": {},
        "sample_orphans": rows[:100],
    }
    for row in rows:
        summary["by_type"][row["node_type"]] = summary["by_type"].get(row["node_type"], 0) + 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = repair(conn, dry_run=not args.apply)
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("sample_")}, indent=2, ensure_ascii=False))
    print(f"wrote {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
