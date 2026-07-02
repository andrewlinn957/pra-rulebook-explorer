#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
LOG = ROOT / "logs" / "residual-rule-reference-repair.json"
DATED_SUFFIX_RE = re.compile(r"/\d{2}-\d{2}-\d{4}$")


def base_url(url: str) -> str:
    return (url or "").split("#", 1)[0].rstrip("/")


def undated(url: str) -> str:
    return DATED_SUFFIX_RE.sub("", base_url(url))


def load_part_index(conn: sqlite3.Connection) -> tuple[dict[str, list[sqlite3.Row]], dict[str, list[sqlite3.Row]]]:
    exact: dict[str, list[sqlite3.Row]] = defaultdict(list)
    by_undated: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT id,node_type,title,url,stable_key FROM node WHERE node_type='part'"):
        b = base_url(row["url"] or "")
        if not b:
            continue
        exact[b].append(row)
        by_undated[undated(b)].append(row)
    return exact, by_undated


def unique_match(matches: list[sqlite3.Row]) -> sqlite3.Row | None:
    # A URL can index the same part more than once only if the DB is already broken.
    unique_by_id = {m["id"]: m for m in matches}
    if len(unique_by_id) == 1:
        return next(iter(unique_by_id.values()))
    return None


def repair(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    part_exact, part_undated = load_part_index(conn)
    candidates: list[dict[str, Any]] = []
    skipped_anchor_refs = 0
    for rr in conn.execute("SELECT id,title,url,stable_key FROM node WHERE node_type='rule_reference'"):
        url = rr["url"] or ""
        if "#" in url:
            skipped_anchor_refs += 1
            continue
        b = base_url(url)
        if not b:
            continue
        target = unique_match(part_exact.get(b, []))
        basis = "exact_part_url"
        if target is None:
            target = unique_match(part_undated.get(undated(b), []))
            basis = "undated_part_url" if target is not None else basis
        if target is None:
            continue
        inbound = conn.execute("SELECT COUNT(*) FROM edge WHERE to_node_id=?", (rr["id"],)).fetchone()[0]
        outbound = conn.execute("SELECT COUNT(*) FROM edge WHERE from_node_id=?", (rr["id"],)).fetchone()[0]
        candidates.append({
            "placeholder_id": rr["id"],
            "placeholder_title": rr["title"],
            "placeholder_url": rr["url"],
            "placeholder_stable_key": rr["stable_key"],
            "target_id": target["id"],
            "target_title": target["title"],
            "target_stable_key": target["stable_key"],
            "basis": basis,
            "inbound_edges": inbound,
            "outbound_edges": outbound,
        })

    self_edges = [dict(r) for r in conn.execute("SELECT id,from_node_id,to_node_id,edge_type,source_method,evidence_text FROM edge WHERE from_node_id=to_node_id")]
    isolated = [dict(r) for r in conn.execute("""
        SELECT n.id,n.title,n.url,n.stable_key
        FROM node n
        WHERE n.node_type='rule_reference'
          AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.from_node_id=n.id OR e.to_node_id=n.id)
    """)]

    updated_edges = 0
    deleted_placeholders = 0
    deleted_self_edges = 0
    deleted_isolated = 0

    if not dry_run:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_to_node_id ON edge(to_node_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_from_node_id ON edge(from_node_id)")
        for edge in self_edges:
            conn.execute("DELETE FROM edge WHERE id=?", (edge["id"],))
            deleted_self_edges += 1
        for c in candidates:
            for edge in conn.execute("SELECT id,metadata_json FROM edge WHERE to_node_id=?", (c["placeholder_id"],)).fetchall():
                try:
                    meta = json.loads(edge["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    meta = {}
                meta.update({
                    "resolved_placeholder_id": c["placeholder_id"],
                    "resolved_placeholder_stable_key": c["placeholder_stable_key"],
                    "resolution_basis": c["basis"],
                    "extraction_run_id": meta.get("extraction_run_id") or "residual_rule_reference_repair",
                })
                conn.execute("UPDATE edge SET to_node_id=?, metadata_json=? WHERE id=?", (c["target_id"], json.dumps(meta, ensure_ascii=False), edge["id"]))
                updated_edges += 1
            conn.execute("DELETE FROM node WHERE id=? AND NOT EXISTS (SELECT 1 FROM edge WHERE from_node_id=? OR to_node_id=?)", (c["placeholder_id"], c["placeholder_id"], c["placeholder_id"]))
            if conn.execute("SELECT changes()").fetchone()[0]:
                deleted_placeholders += 1
        for node in isolated:
            conn.execute("DELETE FROM node WHERE id=?", (node["id"],))
            if conn.execute("SELECT changes()").fetchone()[0]:
                deleted_isolated += 1
        conn.commit()

    summary = {
        "dry_run": dry_run,
        "part_reference_candidates": len(candidates),
        "part_reference_inbound_edges": sum(c["inbound_edges"] for c in candidates),
        "skipped_anchor_refs": skipped_anchor_refs,
        "self_edges": len(self_edges),
        "isolated_rule_references": len(isolated),
        "updated_edges": updated_edges,
        "deleted_placeholders": deleted_placeholders,
        "deleted_self_edges": deleted_self_edges,
        "deleted_isolated": deleted_isolated,
        "sample_part_reference_candidates": candidates[:50],
        "sample_self_edges": self_edges[:20],
        "sample_isolated": isolated[:50],
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
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("sample_")}, indent=2, ensure_ascii=False))
    print(f"wrote {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
