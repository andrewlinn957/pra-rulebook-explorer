#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs/broken-reference-check"
IN_JSON = OUT / "unresolved-external-link-check.json"
OUT_CSV = OUT / "ambiguous-browser-check.csv"
OUT_JSON = OUT / "ambiguous-browser-check.json"

SOFT_404 = re.compile(r"\b404\b|page not found|not found|could not be found|no longer available|content unavailable", re.I)
BLOCKED = re.compile(r"access denied|forbidden|blocked|captcha|enable javascript|just a moment", re.I)

async def check_page(browser, row):
    page = await browser.new_page(viewport={"width": 1280, "height": 1800})
    status = None
    final_url = row["url"]
    title = ""
    body = ""
    error = ""
    try:
        resp = await page.goto(row["url"], wait_until="domcontentloaded", timeout=25000)
        if resp:
            status = resp.status
            final_url = page.url
        title = await page.title()
        body = (await page.locator("body").inner_text(timeout=5000))[:2000]
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        await page.close()
    hay = f"{title}\n{body}"
    if error:
        classification = "browser_error"
        reason = error[:240]
    elif status in {404, 410} or SOFT_404.search(hay):
        classification = "browser_broken"
        reason = f"HTTP {status}; {title}"[:240]
    elif status and status >= 400:
        classification = "browser_blocked" if BLOCKED.search(hay) or status in {401,403,429} else "browser_suspect"
        reason = f"HTTP {status}; {title}"[:240]
    else:
        classification = "browser_ok"
        reason = title[:240]
    return {**row, "browser_status": status or "", "browser_final_url": final_url, "browser_title": title, "browser_classification": classification, "browser_reason": reason}

async def worker(name, browser, q, results):
    while True:
        row = await q.get()
        if row is None:
            q.task_done()
            return
        results.append(await check_page(browser, row))
        q.task_done()

async def main():
    rows = json.loads(IN_JSON.read_text())
    ambiguous = [r for r in rows if r["classification"] in {"suspect", "error", "soft_404"}]
    q = asyncio.Queue()
    for r in ambiguous:
        q.put_nowait(r)
    results = []
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        workers = [asyncio.create_task(worker(str(i), browser, q, results)) for i in range(4)]
        await q.join()
        for _ in workers: q.put_nowait(None)
        await asyncio.gather(*workers)
        await browser.close()
    results.sort(key=lambda r: (r["browser_classification"], str(r["title"]).lower()))
    OUT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    fields = ["browser_classification","browser_status","browser_reason","classification","status","reason","title","url","browser_final_url","live_edges","target_id","stable_key"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in fields})
    summary = {}
    for r in results:
        summary[r["browser_classification"]] = summary.get(r["browser_classification"], 0) + 1
    print(json.dumps({"checked": len(results), "summary": summary, "csv": str(OUT_CSV), "json": str(OUT_JSON)}, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
