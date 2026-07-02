#!/usr/bin/env python3
"""Scoped source discovery for COR011/LCR regulatory reporting materials.

Raw-source download only: no DB writes and no legal relationship inference.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import discover_reporting_sources as base

ROOT = base.ROOT
DEFAULT_OUT = ROOT / "backend/data/raw/reporting-sources/cor011-lcr-scoped"

SEED_URLS = base.SEED_URLS

FILE_SCOPE_RE = re.compile(
    r"(" 
    r"COR\s*011|COR011|liquidity\s+coverage\s+ratio|\bLCR\b|"
    r"corep[-\s_]*(liquidity|additional[-\s_]*liquidity|counterbalancing|nsfr)|"
    r"annex[-\s_]*(xxiv|xxv)\b|Annex\s+(XXIV|XXV)\b|"
    r"pillar\s*3[-\s_ ]*liquidity|"
    r"PRA\s*110|PRA110|FSA\s*047|FSA047|FSA\s*048|FSA048|"
    r"intraday[-\s_]*liquidity|liquidity[-\s_]*metric|liquidity[-\s_]*monitor|"
    r"banking[-\s_ ]*(xbrl[-\s_ ]*)?taxonomy|boe[-\s_]*banking|taxonomy[-\s_]*(package|release|dpm|validations?|sample[-\s_]*instances?|change[-\s_]*logs?)|"
    r"dpm[-\s_]*(package|dictionary)|sample[-\s_]*xbrl|xbrl[-\s_]*filing[-\s_]*manual|"
    r"SS\s*34/15|SS34/15|guidelines[-\s_]*for[-\s_]*completing[-\s_]*regulatory[-\s_]*reports|"
    r"SS\s*24/15|SS24/15|approach[-\s_]*to[-\s_]*supervising[-\s_]*liquidity|"
    r"SS\s*2/19|SS2/19|pra[-\s_]*approach[-\s_]*to[-\s_]*interpreting[-\s_]*reporting"
    r")",
    re.I,
)

PAGE_SCOPE_RE = re.compile(
    r"(" 
    r"guidelines for completing regulatory reports|SS\s*34/15|SS34/15|"
    r"supervisory tools.*liquidity|liquidity tools|"
    r"approach to supervising liquidity|SS\s*24/15|SS24/15|"
    r"pra approach to interpreting reporting|SS\s*2/19|SS2/19|"
    r"regulatory reporting: european banking authority taxonomy|eba taxonomy|taxonomy\s*2\.9|"
    r"pillar 2 liquidity|liquidity reporting|PRA110|FSA047|FSA048|"
    r"liquidity coverage ratio"
    r")",
    re.I,
)

DROP_RE = re.compile(
    r"(insurance[-\s_]*sector|credit[-\s_]*union|mortgage|mrel|operational[-\s_]*continuity|securitisation|remuneration|ring[-\s_]*fencing|rfb00[1-8]|branch[-\s_]*return)",
    re.I,
)


def link_context(a) -> str:
    text = a.get_text(" ", strip=True) or a.get("title", "") or a.get("href", "")
    parent = a.parent.get_text(" ", strip=True)[:700] if a.parent else ""
    return f"{text} {parent}"


def is_seed(url: str) -> bool:
    return base.canon_url(url) in {base.canon_url(u) for u in SEED_URLS}


def in_scope_file(url: str, text: str, parent_title: str = "") -> bool:
    hay = f"{text}\n{url}\n{parent_title}"
    if DROP_RE.search(hay) and not re.search(r"annex[-\s_]*(xxiv|xxv)|Annex\s+(XXIV|XXV)|liquidity|taxonomy|xbrl|dpm|sample", hay, re.I):
        return False
    if re.search(r"branch[-\s_]*return|pra101|pra102|pra103|rfb00[1-8]", hay, re.I) and not re.search(r"liquidity|taxonomy|xbrl|dpm|annex[-\s_]*(xxiv|xxv)|Annex\s+(XXIV|XXV)", hay, re.I):
        return False
    if re.search(r"validation", hay, re.I) and not re.search(r"taxonomy|xbrl|boe[-\s_]*banking|banking[-\s_]*(xbrl[-\s_]*)?taxonomy", hay, re.I):
        return False
    if FILE_SCOPE_RE.search(hay):
        return True
    return False


def in_scope_page(url: str, text: str) -> bool:
    hay = f"{text}\n{url}"
    if is_seed(url):
        return True
    if DROP_RE.search(hay) and not re.search(r"liquidity|taxonomy|SS\s*(34|24)/15|SS\s*2/19", hay, re.I):
        return False
    return bool(PAGE_SCOPE_RE.search(hay))


def discover(row: base.ManifestRow, file_only: bool = False) -> list[tuple[str, str, bool, str]]:
    path = ROOT / row.local_path
    if row.file_type != "html" or not path.exists():
        return []
    soup = BeautifulSoup(path.read_bytes(), "lxml")
    found = []
    for a in soup.find_all("a", href=True):
        raw = a.get("href") or ""
        if raw.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if raw.startswith("-/media/"):
            raw = "/" + raw
        url = base.canon_url(urljoin(row.url, raw))
        text = a.get_text(" ", strip=True) or a.get("title", "") or raw
        ctx = link_context(a)
        if not base.allowed_url(url):
            if FILE_SCOPE_RE.search(f"{ctx} {url}") or PAGE_SCOPE_RE.search(f"{ctx} {url}"):
                found.append((url, text, False, "unresolved scoped link outside allowed official scope"))
            continue
        is_file = base.is_probable_file(url, text)
        if is_file and in_scope_file(url, text, row.title):
            found.append((url, text, False, "scoped file linked from official page"))
        elif not file_only and (not is_file) and in_scope_page(url, ctx):
            found.append((url, text, True, "scoped official linked page"))
    seen = set(); out=[]
    for item in found:
        if item[0] not in seen:
            seen.add(item[0]); out.append(item)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--delay", type=float, default=0.75)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--max-linked-pages", type=int, default=25)
    args = ap.parse_args()

    dl = base.Downloader(args.out_dir, delay=args.delay, refresh=args.refresh)

    seed_rows = []
    for url in SEED_URLS:
        row = dl.download(url, parent_url="", link_text="seed page", is_page=True, notes="seed_scope_page")
        if row:
            seed_rows.append(row)

    linked_pages = []
    for row in seed_rows:
        for url, text, is_page, note in discover(row, file_only=False):
            if not base.allowed_url(url):
                dl.unresolved.append({"url": url, "parent_url": row.url, "link_text": text, "reason": note})
                continue
            if is_page:
                if len(linked_pages) >= args.max_linked_pages:
                    dl.skipped.append({"url": url, "parent_url": row.url, "link_text": text, "reason": "max_linked_pages_reached"})
                    continue
                r = dl.download(url, parent_url=row.url, link_text=text, is_page=True, notes=note)
                if r:
                    linked_pages.append(r)
            else:
                dl.download(url, parent_url=row.url, link_text=text, is_page=False, notes=note)

    # From scoped linked pages, take official files only, still filtered by scope.
    for row in linked_pages:
        for url, text, is_page, note in discover(row, file_only=True):
            if not base.allowed_url(url):
                dl.unresolved.append({"url": url, "parent_url": row.url, "link_text": text, "reason": note})
            elif not is_page:
                dl.download(url, parent_url=row.url, link_text=text, is_page=False, notes="scoped file linked from scoped official page")

    dl.write_outputs()

    # Add a machine-readable run summary with the exact seed list and scope regexes.
    summary = {
        "generated_at": base.now(),
        "out_dir": str(args.out_dir),
        "seeds": SEED_URLS,
        "manifest_csv": str(args.out_dir / "source_manifest.csv"),
        "manifest_json": str(args.out_dir / "source_manifest.json"),
        "report": str(args.out_dir / "report.md"),
        "rows": len(dl.rows),
        "failed": len(dl.failed),
        "skipped": len(dl.skipped),
        "unresolved": len(dl.unresolved),
        "notes": "Raw-source discovery/download only. No database build; no inferred legal relationships.",
    }
    (args.out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
