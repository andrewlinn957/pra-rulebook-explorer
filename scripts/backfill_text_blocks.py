#!/usr/bin/env python3
"""Backfill text_blocks metadata for rules from raw HTML snapshots."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup
from backend.rulebook_scraper.parse import extract_text_blocks
from scripts.safe_connect import connect

DB = ROOT / "backend/data/rulebook.sqlite3"


def main() -> int:
    conn = connect(DB)
    print("loading snapshots into memory...", flush=True)
    snapshots = {}
    for row in conn.execute("SELECT url, raw_html FROM document_snapshot"):
        snapshots[row["url"].split("?")[0].rstrip("/")] = row["raw_html"]
    print(f"loaded {len(snapshots)} snapshots", flush=True)
    soup_cache = {}
    rules = conn.execute(
        "SELECT id, url, metadata_json FROM node WHERE node_type IN (\x27rule\x27,\x27provision\x27)"
    ).fetchall()
    updated = 0
    no_source = 0
    for row in rules:
        url = row["url"] or ""
        if "#" not in url:
            no_source += 1
            continue
        page_url, _, html_id = url.partition("#")
        if not html_id:
            no_source += 1
            continue
        key = page_url.split("?")[0].rstrip("/")
        raw = snapshots.get(key)
        if not raw:
            no_source += 1
            continue
        soup = soup_cache.get(key)
        if soup is None:
            soup = BeautifulSoup(raw, "lxml")
            soup_cache[key] = soup
        el = soup.find(id=html_id)
        if not el:
            no_source += 1
            continue
        col2 = el.select_one(".div-row__col-2")
        blocks = extract_text_blocks(col2)
        if blocks is None:
            continue
        meta = json.loads(row["metadata_json"] or "{}")
        meta["text_blocks"] = blocks
        conn.execute(
            "UPDATE node SET metadata_json=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False, sort_keys=True), row["id"]),
        )
        updated += 1
    conn.commit()
    print(f"updated={updated} no_source={no_source} total={len(rules)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
