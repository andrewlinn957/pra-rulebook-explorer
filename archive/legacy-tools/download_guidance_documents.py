#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.prarulebook.co.uk"
UA = "rulebookexp-guidance-downloader/0.1 (+local research prototype)"
DOC_TYPES = {"supervisory_statement", "statement_of_policy"}
NOT_IN_FORCE_RE = re.compile(
    r"\b(deleted|no longer in force|not in force|has been deleted|has been superseded|superseded by|withdrawn)\b",
    re.I,
)
PDF_RE = re.compile(r"\.pdf(?:$|[?#])", re.I)

@dataclass
class Item:
    node_id: str
    title: str
    document_type: str
    url: str
    status: str
    reason: str
    source_file: str = ""
    content_type: str = ""
    bytes: int = 0
    sha256: str = ""
    candidates: list[str] | None = None


def norm_url(url: str) -> str:
    return urljoin(BASE, url)


def safe_name(title: str, url: str, ext: str) -> str:
    code = re.match(r"\s*((?:L?SS|SoP)\s*\d*/\d*|(?:L?SS|SoP)\d+/\d+)", title, re.I)
    prefix = code.group(1) if code else Path(urlparse(url).path).name
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (prefix + "-" + title)[:140]).strip("-.")
    return f"{s or hashlib.sha1(url.encode()).hexdigest()[:12]}{ext}"


def visible_header_text(soup: BeautifulSoup) -> str:
    chunks=[]
    for sel in ["header", ".govuk-notification-banner", ".notification", ".alert", "main h1", "h1", "main"]:
        for el in soup.select(sel)[:3]:
            txt=el.get_text(" ", strip=True)
            if txt and txt not in chunks:
                chunks.append(txt)
    return " \n".join(chunks)[:5000]


def candidate_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    out=[]
    page_path=urlparse(page_url).path.strip('/').rsplit('/', 1)[0]
    for a in soup.find_all("a", href=True):
        raw=a["href"]
        if "%s" in raw:
            continue
        href=norm_url(urljoin(page_url, raw))
        text=a.get_text(" ", strip=True).lower()
        if PDF_RE.search(href) or "pdf" in text or "download" in text or "export guidance" in text:
            if href not in out:
                out.append(href)
    def score(u: str):
        p=urlparse(u)
        path=p.path.strip('/')
        same_doc = path.startswith(page_path)
        root_export = same_doc and path.count('/') <= page_path.count('/') + 1
        external_legacy = 'bankofengland.co.uk/pra/documents/' in u.lower()
        return (
            0 if root_export else 1,
            0 if same_doc else 1,
            0 if PDF_RE.search(u) else 1,
            1 if external_legacy else 0,
            len(u),
        )
    out.sort(key=score)
    return out


def load_docs(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    conn.row_factory=sqlite3.Row
    for r in conn.execute("""
        select id,title,url,metadata_json from node
        where node_type='guidance_document'
        order by title,url
    """):
        meta=json.loads(r["metadata_json"] or "{}")
        if meta.get("document_type") in DOC_TYPES:
            yield r


def download_all(db: Path, out_dir: Path, refresh: bool=False, limit: int|None=None) -> list[Item]:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_dir=out_dir/"pages"; file_dir=out_dir/"files"
    html_dir.mkdir(exist_ok=True); file_dir.mkdir(exist_ok=True)
    session=requests.Session(); session.headers.update({"User-Agent": UA})
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    items=[]
    for idx,r in enumerate(load_docs(conn), start=1):
        if limit and len(items)>=limit: break
        meta=json.loads(r["metadata_json"] or "{}")
        url=r["url"]
        page_path=html_dir/(hashlib.sha1(url.encode()).hexdigest()+".html")
        try:
            if page_path.exists() and not refresh:
                html=page_path.read_text(encoding="utf-8")
            else:
                resp=session.get(url, timeout=40)
                resp.raise_for_status()
                html=resp.text
                page_path.write_text(html, encoding="utf-8")
            soup=BeautifulSoup(html,"html.parser")
            header=visible_header_text(soup)
            title=(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else r["title"])
            combined=(title+"\n"+header)[:6000]
            if NOT_IN_FORCE_RE.search(combined) or "(Deleted)" in r["title"] or r["title"].lower().startswith("deleted-"):
                items.append(Item(r["id"], r["title"], meta.get("document_type",""), url, "skipped", "not_in_force_or_deleted_header", candidates=[]))
                continue
            links=candidate_links(soup,url)
            if not links:
                # Some pages are themselves substantive HTML. Save page as source.
                fn=safe_name(r["title"],url,".html")
                dest=file_dir/fn
                if not dest.exists() or refresh: dest.write_text(html, encoding="utf-8")
                sha=hashlib.sha256(dest.read_bytes()).hexdigest()
                items.append(Item(r["id"], r["title"], meta.get("document_type",""), url, "downloaded", "saved_html_page_no_document_link", str(dest), "text/html", dest.stat().st_size, sha, []))
                continue
            errors=[]
            saved=None
            for chosen in links:
                try:
                    resp=session.get(chosen, timeout=60)
                    resp.raise_for_status()
                    ctype=resp.headers.get("content-type","").split(";")[0].strip()
                    ext=".pdf" if PDF_RE.search(chosen) or ctype=="application/pdf" else ".html"
                    fn=safe_name(r["title"],chosen,ext)
                    dest=file_dir/fn
                    if not dest.exists() or refresh:
                        dest.write_bytes(resp.content)
                    sha=hashlib.sha256(dest.read_bytes()).hexdigest()
                    saved=Item(r["id"], r["title"], meta.get("document_type",""), url, "downloaded", "downloaded_linked_document", str(dest), ctype, dest.stat().st_size, sha, links[:10])
                    break
                except Exception as exc:
                    errors.append(f"{chosen}: {type(exc).__name__}: {exc}")
            if saved:
                items.append(saved)
            else:
                raise RuntimeError("all candidate downloads failed: " + " | ".join(errors[:5]))
        except Exception as exc:
            items.append(Item(r["id"], r["title"], meta.get("document_type",""), url, "error", f"{type(exc).__name__}: {exc}", candidates=[]))
    return items


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("backend/data/rulebook.sqlite3"))
    ap.add_argument("--out-dir", type=Path, default=Path("backend/data/raw/guidance-documents"))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--limit", type=int)
    args=ap.parse_args()
    items=download_all(args.db,args.out_dir,args.refresh,args.limit)
    manifest=args.out_dir/"manifest.json"
    manifest.write_text(json.dumps([asdict(i) for i in items], indent=2, ensure_ascii=False), encoding="utf-8")
    counts={}
    for i in items:
        counts[i.status]=counts.get(i.status,0)+1
        counts[f"{i.document_type}:{i.status}"]=counts.get(f"{i.document_type}:{i.status}",0)+1
    summary={"counts":counts,"manifest":str(manifest)}
    (args.out_dir/"summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
