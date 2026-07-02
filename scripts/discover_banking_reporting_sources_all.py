#!/usr/bin/env python3
"""Download all official banking reporting materials linked from the BoE/PRA pages.

Raw-source download only. Reuses the COR011 downloader/provenance manifest model,
but widens scope beyond liquidity to all banking, building society and
PRA-designated investment firm reporting artefacts linked from the official pages.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import discover_reporting_sources as base

ROOT = base.ROOT
DEFAULT_OUT = ROOT / "backend/data/raw/reporting-sources/banking-reporting-all"

SEED_URLS = [
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting",
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/regulatory-reporting-banking-sector",
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/regulatory-reporting-banking-sector/banks-building-societies-and-investment-firms",
    "https://www.prarulebook.co.uk/pra-rules/reporting-crr",
    "https://www.prarulebook.co.uk/pra-rules/regulatory-reporting",
]

REPORTING_FILE_RE = re.compile(
    r"(" 
    r"annex|corep|finrep|pillar\s*3|taxonomy|dpm|xbrl|validation|sample\s*instance|known\s*issues|filing\s*manual|"
    r"PRA\s*\d{3}|FSA\s*\d{3}|RFB\s*\d{3}|REP\s*\d{3}a?|LVR\s*\d{3}|COR\s*\d{3}|IDY\s*\d{3}|MLAR|"
    r"leverage|large\s*exposures|credit\s*risk|counterparty|market\s*risk|liquidity|NSFR|LCR|remuneration|asset\s*encumbrance|branch\s*return|"
    r"reporting\s*template|data\s*items?|instructions?|guidance|notes\s*on\s*submitting|schedule|Q&As?"
    r")",
    re.I,
)

REPORTING_PAGE_RE = re.compile(
    r"(regulatory[-\s]*reporting|reporting|disclosure|taxonomy|corep|finrep|pillar|leverage|basel|branch[-\s]*return|liquidity|capital|resolution|remuneration)",
    re.I,
)

DROP_RE = re.compile(r"(careers|museum|linkedin|twitter|facebook|instagram|youtube|subscribe|freedom-of-information|privacy|cookie|contact|glossary)", re.I)


def link_context(a) -> str:
    text = a.get_text(" ", strip=True) or a.get("title", "") or a.get("href", "")
    parent = a.parent.get_text(" ", strip=True)[:900] if a.parent else ""
    return f"{text} {parent}"


def discover(row: base.ManifestRow, include_pages: bool) -> list[tuple[str, str, bool, str]]:
    path = ROOT / row.local_path
    if row.file_type != "html" or not path.exists():
        return []
    soup = BeautifulSoup(path.read_bytes(), "lxml")
    found = []
    for a in soup.find_all("a", href=True):
        raw = a.get("href") or ""
        if raw.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if raw.startswith("-/media/"):
            raw = "/" + raw
        url = base.canon_url(urljoin(row.url, raw))
        text = a.get_text(" ", strip=True) or a.get("title", "") or raw
        ctx = link_context(a)
        hay = f"{text} {ctx} {url}"
        if DROP_RE.search(hay):
            continue
        if not base.allowed_url(url):
            if REPORTING_FILE_RE.search(hay) or REPORTING_PAGE_RE.search(hay):
                found.append((url, text, False, "unresolved official-scope candidate outside downloader allowlist"))
            continue
        is_file = base.is_probable_file(url, text) or bool(re.search(r"/[-]/media/|/-/media/", url))
        if is_file and REPORTING_FILE_RE.search(hay):
            found.append((url, text, False, "official banking reporting file link"))
        elif include_pages and (not is_file) and REPORTING_PAGE_RE.search(hay):
            found.append((url, text, True, "official reporting page link"))
    seen = set(); out = []
    for item in found:
        if item[0] not in seen:
            seen.add(item[0]); out.append(item)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--delay", type=float, default=0.25)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--max-linked-pages", type=int, default=80)
    args = ap.parse_args()

    dl = base.Downloader(args.out_dir, delay=args.delay, refresh=args.refresh)
    seed_rows = []
    for url in SEED_URLS:
        row = dl.download(url, parent_url="", link_text="seed page", is_page=True, notes="banking_reporting_all_seed")
        if row:
            seed_rows.append(row)

    linked_pages = []
    for row in seed_rows:
        for url, text, is_page, note in discover(row, include_pages=True):
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

    for row in linked_pages:
        for url, text, is_page, note in discover(row, include_pages=False):
            if not base.allowed_url(url):
                dl.unresolved.append({"url": url, "parent_url": row.url, "link_text": text, "reason": note})
            elif not is_page:
                dl.download(url, parent_url=row.url, link_text=text, is_page=False, notes="official reporting file linked from official reporting page")

    dl.write_outputs()
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
        "notes": "Raw official-source download only; no DB writes and no relationship inference.",
    }
    (args.out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
