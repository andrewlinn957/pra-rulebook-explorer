#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT = ROOT / "outputs/broken-reference-check"

SOFT_404_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"\b404\b",
        r"page not found",
        r"not found",
        r"the page you requested could not be found",
        r"we can['’]t find",
        r"content unavailable",
        r"this page has been moved",
        r"no longer available",
        r"broken link",
    ]
]

@dataclass
class Candidate:
    target_id: str
    title: str
    url: str
    stable_key: str
    live_edges: int


def normalise_url(row: sqlite3.Row) -> str:
    url = (row["url"] or "").strip()
    if not url and (row["stable_key"] or "").startswith("external:"):
        url = row["stable_key"][len("external:"):]
    if not url:
        title = (row["title"] or "").strip()
        if title.startswith(("http://", "https://", "www.")):
            url = title
    if url.startswith("www."):
        url = "https://" + url
    return url


def load_candidates() -> list[Candidate]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT target.id AS target_id,target.title,target.url,target.stable_key,COUNT(*) AS live_edges
        FROM node target
        JOIN edge e ON e.to_node_id=target.id AND e.edge_type='references'
        WHERE target.node_type='external_reference'
          AND json_extract(target.metadata_json,'$.placeholder')=1
        GROUP BY target.id
        ORDER BY target.title,target.id
        """
    ).fetchall()
    out = []
    for row in rows:
        url = normalise_url(row)
        if not url.startswith(("http://", "https://")):
            continue
        out.append(Candidate(row["target_id"], row["title"] or "", url, row["stable_key"] or "", row["live_edges"]))
    return out


def page_title(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:180]


def url_path_suffix(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def classify(url: str, final_url: str, status: int | None, text: str, error: str) -> tuple[str, str]:
    if error:
        return "error", error[:240]
    if status is None:
        return "error", "no status"

    suffixes = {url_path_suffix(url), url_path_suffix(final_url)}

    # Bank of England/PRA media links frequently return HTTP 403 to the
    # checker even when the browser/download link is valid. Andrew's manual
    # review of the 403 set found a clean split: document links ending .pdf or
    # .xlsx are valid, while legacy SharePoint .aspx pages are broken.
    if status == 403 and suffixes & {".pdf", ".xlsx"}:
        return "ok", "HTTP 403 bot-blocked document link; valid by extension review"
    if status == 403 and ".aspx" in suffixes:
        return "broken", "HTTP 403 legacy .aspx page; broken by extension review"

    if status in {404, 410}:
        return "broken", f"HTTP {status}"
    if status >= 400:
        return "suspect", f"HTTP {status}"
    title = page_title(text)
    sample = (title + "\n" + text[:5000]).lower()
    if any(p.search(sample) for p in SOFT_404_PATTERNS):
        # Be stricter for generic 'not found' to avoid false positives from legal text.
        if "not found" in sample or "404" in sample or "page not found" in sample:
            return "soft_404", title or "soft 404 phrase found"
    return "ok", title


async def check_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, c: Candidate) -> dict:
    async with sem:
        status = None
        final_url = c.url
        text = ""
        error = ""
        try:
            r = await client.get(c.url, follow_redirects=True, timeout=20)
            status = r.status_code
            final_url = str(r.url)
            ctype = r.headers.get("content-type", "")
            if "text" in ctype or "html" in ctype or not ctype:
                text = r.text[:12000]
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        classification, reason = classify(c.url, final_url, status, text, error)
        return {
            "target_id": c.target_id,
            "title": c.title,
            "url": c.url,
            "final_url": final_url,
            "status": status if status is not None else "",
            "classification": classification,
            "reason": reason,
            "live_edges": c.live_edges,
            "stable_key": c.stable_key,
        }


async def main() -> int:
    candidates = load_candidates()
    sem = asyncio.Semaphore(12)
    headers = {"User-Agent": "Mozilla/5.0 broken-link-audit/1.0"}
    async with httpx.AsyncClient(headers=headers, verify=False) as client:
        rows = await asyncio.gather(*(check_one(client, sem, c) for c in candidates))
    rows.sort(key=lambda r: (r["classification"] not in {"broken", "soft_404", "suspect", "error"}, r["classification"], str(r["title"]).lower()))
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "unresolved-external-link-check.csv"
    json_path = OUT / "unresolved-external-link-check.json"
    fields = ["classification", "status", "reason", "title", "url", "final_url", "live_edges", "target_id", "stable_key"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    summary = {}
    for r in rows:
        summary[r["classification"]] = summary.get(r["classification"], 0) + 1
    print(json.dumps({"checked": len(rows), "summary": summary, "csv": str(csv_path), "json": str(json_path)}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
