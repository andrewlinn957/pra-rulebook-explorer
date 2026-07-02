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
LOG = ROOT / "logs" / "rule-reference-placeholder-resolution.json"
DATED_SUFFIX_RE = re.compile(r"/\d{2}-\d{2}-\d{4}$")


def sha1_16(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def split_url(url: str) -> tuple[str, str] | None:
    if not url or "#" not in url:
        return None
    base, html_id = url.split("#", 1)
    if not base or not html_id:
        return None
    return base, html_id


def undated(base_url: str) -> str:
    return DATED_SUFFIX_RE.sub("", base_url)


def load_real_node_indexes(conn: sqlite3.Connection) -> tuple[dict[tuple[str, str], list[sqlite3.Row]], dict[tuple[str, str], list[sqlite3.Row]]]:
    exact: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    dated: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT id,node_type,title,url,stable_key,metadata_json FROM node WHERE node_type <> 'rule_reference'"):
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        html_id = meta.get("html_id")
        parsed = split_url(row["url"] or "")
        if not html_id or not parsed:
            continue
        base, _ = parsed
        exact[(base, html_id)].append(row)
        base_undated = undated(base)
        if base_undated != base:
            dated[(base_undated, html_id)].append(row)
    return exact, dated


def resolve(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    exact, dated = load_real_node_indexes(conn)
    placeholder_rows = list(conn.execute("SELECT id,title,url,stable_key,metadata_json FROM node WHERE node_type='rule_reference' AND instr(url,'#') > 0"))
    resolutions: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    multi: list[dict[str, Any]] = []

    for row in placeholder_rows:
        parsed = split_url(row["url"] or "")
        if not parsed:
            continue
        base, html_id = parsed
        matches = exact.get((base, html_id), [])
        basis = "exact_html_id_url"
        if not matches:
            matches = dated.get((base, html_id), [])
            basis = "dated_html_id_url"
        if len(matches) == 1:
            target = matches[0]
            inbound_count = conn.execute("SELECT COUNT(*) FROM edge WHERE to_node_id=?", (row["id"],)).fetchone()[0]
            resolutions.append({
                "placeholder_id": row["id"],
                "placeholder_title": row["title"],
                "placeholder_url": row["url"],
                "placeholder_stable_key": row["stable_key"],
                "target_id": target["id"],
                "target_type": target["node_type"],
                "target_title": target["title"],
                "target_stable_key": target["stable_key"],
                "basis": basis,
                "inbound_edges": inbound_count,
            })
        elif len(matches) > 1:
            multi.append({
                "placeholder_id": row["id"],
                "placeholder_title": row["title"],
                "placeholder_url": row["url"],
                "match_count": len(matches),
                "matches": [{"id": m["id"], "node_type": m["node_type"], "title": m["title"], "stable_key": m["stable_key"]} for m in matches[:10]],
            })
        else:
            unresolved.append({"id": row["id"], "title": row["title"], "url": row["url"], "stable_key": row["stable_key"]})

    updated_edges = 0
    deleted_self_edges = 0
    deleted_placeholders = 0
    if not dry_run:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_to_node_id ON edge(to_node_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_from_node_id ON edge(from_node_id)")
        for r in resolutions:
            edges = list(conn.execute("SELECT id,from_node_id,metadata_json FROM edge WHERE to_node_id=?", (r["placeholder_id"],)))
            for edge in edges:
                if edge["from_node_id"] == r["target_id"]:
                    conn.execute("DELETE FROM edge WHERE id=?", (edge["id"],))
                    deleted_self_edges += 1
                    continue
                try:
                    meta = json.loads(edge["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    meta = {}
                meta.update({
                    "resolved_placeholder_id": r["placeholder_id"],
                    "resolved_placeholder_stable_key": r["placeholder_stable_key"],
                    "resolution_basis": r["basis"],
                    "extraction_run_id": meta.get("extraction_run_id") or "rule_reference_placeholder_resolution",
                })
                conn.execute(
                    "UPDATE edge SET to_node_id=?, metadata_json=? WHERE id=?",
                    (r["target_id"], json.dumps(meta, ensure_ascii=False), edge["id"]),
                )
                updated_edges += 1
            conn.execute("DELETE FROM node WHERE id=? AND NOT EXISTS (SELECT 1 FROM edge WHERE from_node_id=? OR to_node_id=?)", (r["placeholder_id"], r["placeholder_id"], r["placeholder_id"]))
            deleted_placeholders += conn.total_changes  # corrected below from explicit recount
        duplicate_ids: list[str] = []
        conn.commit()
        deleted_placeholders = len(resolutions) - conn.execute(
            "SELECT COUNT(*) FROM node WHERE node_type='rule_reference' AND id IN (%s)" % ",".join("?" for _ in resolutions),
            [r["placeholder_id"] for r in resolutions],
        ).fetchone()[0] if resolutions else 0
    else:
        duplicate_ids = []

    summary = {
        "dry_run": dry_run,
        "placeholder_hash_nodes": len(placeholder_rows),
        "resolvable_unique": len(resolutions),
        "unresolved": len(unresolved),
        "multi_match": len(multi),
        "resolvable_inbound_edges": sum(r["inbound_edges"] for r in resolutions),
        "updated_edges": updated_edges,
        "deleted_self_edges": deleted_self_edges,
        "deleted_duplicate_edges": len(duplicate_ids) if not dry_run else None,
        "deleted_placeholders": deleted_placeholders,
        "target_type_counts": {},
        "sample_resolutions": resolutions[:50],
        "sample_unresolved": unresolved[:50],
        "sample_multi": multi[:20],
    }
    for r in resolutions:
        summary["target_type_counts"][r["target_type"]] = summary["target_type_counts"].get(r["target_type"], 0) + 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--apply", action="store_true", help="apply changes; default is dry run")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = resolve(conn, dry_run=not args.apply)
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("sample_")}, indent=2, ensure_ascii=False))
    print(f"wrote {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
