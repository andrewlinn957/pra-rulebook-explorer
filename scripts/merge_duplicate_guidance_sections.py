#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
LOG = ROOT / "logs" / "duplicate-guidance-section-merge.json"


def text_hash(value: str | None) -> str:
    txt = re.sub(r"\s+", " ", (value or "").strip())
    return hashlib.sha1(txt.encode("utf-8")).hexdigest() if txt else ""


def metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def find_pairs(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buckets: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT id,node_type,title,url,stable_key,text,metadata_json FROM node WHERE node_type='guidance_section'"):
        key = ((row["title"] or "").strip(), row["url"] or "", text_hash(row["text"]))
        buckets[key].append(row)

    pairs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for key, rows in buckets.items():
        if len(rows) < 2:
            continue
        enriched = [r for r in rows if metadata(r).get("html_id")]
        fallback = [r for r in rows if not metadata(r).get("html_id")]
        if len(enriched) == 1 and len(fallback) == 1 and len(rows) == 2:
            src, dst = fallback[0], enriched[0]
            pairs.append({
                "source_id": src["id"],
                "source_stable_key": src["stable_key"],
                "target_id": dst["id"],
                "target_stable_key": dst["stable_key"],
                "title": dst["title"],
                "url": dst["url"],
                "source_inbound": conn.execute("SELECT COUNT(*) FROM edge WHERE to_node_id=?", (src["id"],)).fetchone()[0],
                "source_outbound": conn.execute("SELECT COUNT(*) FROM edge WHERE from_node_id=?", (src["id"],)).fetchone()[0],
                "target_inbound": conn.execute("SELECT COUNT(*) FROM edge WHERE to_node_id=?", (dst["id"],)).fetchone()[0],
                "target_outbound": conn.execute("SELECT COUNT(*) FROM edge WHERE from_node_id=?", (dst["id"],)).fetchone()[0],
            })
        else:
            skipped.append({
                "key": key,
                "rows": [{"id": r["id"], "stable_key": r["stable_key"], "metadata": metadata(r)} for r in rows],
            })
    return pairs, skipped


def repair(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    pairs, skipped = find_pairs(conn)
    updated_inbound = 0
    updated_outbound = 0
    deleted_self_edges = 0
    deleted_duplicate_edges = 0
    deleted_sources = 0

    if not dry_run:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_to_node_id ON edge(to_node_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_from_node_id ON edge(from_node_id)")
        affected_nodes = set()
        for p in pairs:
            src = p["source_id"]
            dst = p["target_id"]
            affected_nodes.update([src, dst])
            for edge in conn.execute("SELECT id,metadata_json FROM edge WHERE to_node_id=?", (src,)).fetchall():
                meta = json.loads(edge["metadata_json"] or "{}") if edge["metadata_json"] else {}
                meta.update({
                    "merged_guidance_section_id": src,
                    "merged_guidance_section_stable_key": p["source_stable_key"],
                    "merge_basis": "same_title_url_text_prefer_html_id",
                    "extraction_run_id": meta.get("extraction_run_id") or "duplicate_guidance_section_merge",
                })
                conn.execute("UPDATE edge SET to_node_id=?, metadata_json=? WHERE id=?", (dst, json.dumps(meta, ensure_ascii=False), edge["id"]))
                updated_inbound += 1
            for edge in conn.execute("SELECT id,metadata_json FROM edge WHERE from_node_id=?", (src,)).fetchall():
                meta = json.loads(edge["metadata_json"] or "{}") if edge["metadata_json"] else {}
                meta.update({
                    "merged_guidance_section_id": src,
                    "merged_guidance_section_stable_key": p["source_stable_key"],
                    "merge_basis": "same_title_url_text_prefer_html_id",
                    "extraction_run_id": meta.get("extraction_run_id") or "duplicate_guidance_section_merge",
                })
                conn.execute("UPDATE edge SET from_node_id=?, metadata_json=? WHERE id=?", (dst, json.dumps(meta, ensure_ascii=False), edge["id"]))
                updated_outbound += 1
            conn.execute("DELETE FROM edge WHERE from_node_id=to_node_id AND (from_node_id=? OR from_node_id=?)", (src, dst))
            deleted_self_edges += conn.execute("SELECT changes()").fetchone()[0]
            conn.execute("DELETE FROM node WHERE id=? AND NOT EXISTS (SELECT 1 FROM edge WHERE from_node_id=? OR to_node_id=?)", (src, src, src))
            deleted_sources += conn.execute("SELECT changes()").fetchone()[0]

        ids = list(affected_nodes)
        if ids:
            placeholders = ",".join("?" for _ in ids)
            duplicate_ids = [r[0] for r in conn.execute(f"""
                SELECT id FROM edge
                WHERE (from_node_id IN ({placeholders}) OR to_node_id IN ({placeholders}))
                  AND id NOT IN (
                    SELECT MIN(id) FROM edge
                    GROUP BY from_node_id,to_node_id,edge_type,source_method,evidence_text
                  )
            """, ids + ids)]
            for edge_id in duplicate_ids:
                conn.execute("DELETE FROM edge WHERE id=?", (edge_id,))
            deleted_duplicate_edges = len(duplicate_ids)
        conn.commit()

    summary = {
        "dry_run": dry_run,
        "merge_pairs": len(pairs),
        "skipped_buckets": len(skipped),
        "source_inbound_edges": sum(p["source_inbound"] for p in pairs),
        "source_outbound_edges": sum(p["source_outbound"] for p in pairs),
        "updated_inbound_edges": updated_inbound,
        "updated_outbound_edges": updated_outbound,
        "deleted_self_edges": deleted_self_edges,
        "deleted_duplicate_edges": deleted_duplicate_edges,
        "deleted_source_nodes": deleted_sources,
        "sample_pairs": pairs[:50],
        "skipped": skipped,
    }
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
    print(json.dumps({k: v for k, v in summary.items() if k not in {"sample_pairs", "skipped"}}, indent=2, ensure_ascii=False))
    print(f"wrote {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
