#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
LOG = ROOT / "logs" / "pra-legacy-content-reference-repair.json"
DATED_SUFFIX_RE = re.compile(r"/\d{2}-\d{2}-\d{4}$")
LEGACY_CONTENT_RE = re.compile(r"prarulebook\.co\.uk/rulebook/Content/(Part|Chapter)/", re.I)
PRA_HOST_RE = re.compile(r"prarulebook\.co\.uk/", re.I)


def strip_fragment(url: str) -> str:
    parts = urlsplit(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def normalise_url(url: str) -> str:
    parts = urlsplit(url or "")
    host = parts.netloc.lower().removeprefix("www.")
    path = DATED_SUFFIX_RE.sub("", parts.path.rstrip("/"))
    return urlunsplit(("https", host, path, "", parts.fragment))


def undated_no_fragment(url: str) -> str:
    parts = urlsplit(normalise_url(url))
    path = DATED_SUFFIX_RE.sub("", parts.path.rstrip("/"))
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def load_target_index(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    index: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("""
        SELECT id,node_type,title,url,stable_key
        FROM node
        WHERE node_type IN ('part','chapter','rule')
          AND coalesce(url,'') <> ''
    """):
        url = row["url"] or ""
        node_type = row["node_type"]
        keys = {normalise_url(url)}
        if node_type == "part":
            # A legacy Part redirect without a fragment should resolve to the part,
            # not every chapter/rule below that URL.
            keys.add(strip_fragment(normalise_url(url)))
            keys.add(undated_no_fragment(url))
        for key in keys:
            if key:
                index[key].append(row)
    return index


def unique_match(matches: list[sqlite3.Row]) -> sqlite3.Row | None:
    unique = {m["id"]: m for m in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def resolve_redirect(url: str, session: requests.Session) -> tuple[int | None, str | None, str | None]:
    try:
        response = session.get(url, allow_redirects=True, timeout=15)
        return response.status_code, response.url, None
    except requests.RequestException as exc:
        return None, None, str(exc)


def repair(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    target_index = load_target_index(conn)
    session = requests.Session()
    session.headers["User-Agent"] = "pra-rulebook-explorer-quality-repair/1.0"

    candidates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for node in conn.execute("""
        SELECT n.id,n.node_type,n.title,n.url,n.stable_key,n.metadata_json,COUNT(e.id) AS inbound_edges
        FROM node n
        JOIN edge e ON e.to_node_id=n.id
        WHERE n.node_type IN ('external_reference','rule_reference')
          AND json_extract(n.metadata_json,'$.placeholder')=1
          AND n.url LIKE '%prarulebook.co.uk/%'
        GROUP BY n.id
        ORDER BY inbound_edges DESC,n.id
    """):
        status, final_url, error = resolve_redirect(node["url"] or "", session)
        if error or status is None or status >= 400 or not final_url:
            unresolved.append({"placeholder_id": node["id"], "url": node["url"], "status": status, "error": error})
            continue
        if urlsplit(final_url).fragment:
            # For anchored links, only resolve when that exact anchor exists.
            # Falling back to the parent part would hide a real unresolved rule/chapter reference.
            keys = [normalise_url(final_url)]
        else:
            keys = [normalise_url(final_url), strip_fragment(normalise_url(final_url)), undated_no_fragment(final_url)]
        target = None
        basis = None
        for key in keys:
            target = unique_match(target_index.get(key, []))
            if target is not None:
                basis = f"redirect_to_{key}"
                break
        if target is None:
            unresolved.append({"placeholder_id": node["id"], "url": node["url"], "status": status, "final_url": final_url, "keys": keys})
            continue
        candidates.append({
            "placeholder_id": node["id"],
            "placeholder_title": node["title"],
            "placeholder_url": node["url"],
            "placeholder_stable_key": node["stable_key"],
            "redirect_status": status,
            "redirect_final_url": final_url,
            "target_id": target["id"],
            "target_type": target["node_type"],
            "target_title": target["title"],
            "target_url": target["url"],
            "target_stable_key": target["stable_key"],
            "basis": basis,
            "inbound_edges": node["inbound_edges"],
        })

    updated_edges = 0
    deleted_placeholders = 0
    if not dry_run:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_to_node_id ON edge(to_node_id)")
        for c in candidates:
            for edge in conn.execute("SELECT id,metadata_json FROM edge WHERE to_node_id=?", (c["placeholder_id"],)).fetchall():
                try:
                    meta = json.loads(edge["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    meta = {}
                original_href = meta.get("href")
                original_target_key = meta.get("target_key")
                meta.update({
                    "href": c["redirect_final_url"],
                    "target_key": c["redirect_final_url"] if urlsplit(c["redirect_final_url"]).fragment else c["target_stable_key"],
                    "original_href": original_href,
                    "original_target_key": original_target_key,
                    "resolved_placeholder_id": c["placeholder_id"],
                    "resolved_placeholder_stable_key": c["placeholder_stable_key"],
                    "resolution_basis": "pra_placeholder_redirect",
                    "redirect_final_url": c["redirect_final_url"],
                    "extraction_run_id": meta.get("extraction_run_id") or "pra_placeholder_redirect_repair",
                })
                conn.execute(
                    "UPDATE edge SET to_node_id=?, metadata_json=? WHERE id=?",
                    (c["target_id"], json.dumps(meta, ensure_ascii=False), edge["id"]),
                )
                updated_edges += 1
            conn.execute(
                "DELETE FROM node WHERE id=? AND NOT EXISTS (SELECT 1 FROM edge WHERE from_node_id=? OR to_node_id=?)",
                (c["placeholder_id"], c["placeholder_id"], c["placeholder_id"]),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                deleted_placeholders += 1
        conn.commit()

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "candidate_placeholders": len(candidates),
        "candidate_inbound_edges": sum(c["inbound_edges"] for c in candidates),
        "unresolved_placeholders": len(unresolved),
        "updated_edges": updated_edges,
        "deleted_placeholders": deleted_placeholders,
        "sample_candidates": candidates[:100],
        "sample_unresolved": unresolved[:100],
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
