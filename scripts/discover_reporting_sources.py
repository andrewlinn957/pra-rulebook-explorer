#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "backend/data/raw/reporting-sources/cor011-lcr-first-run"
UA = "pra-rulebook-explorer-reporting-source-discovery/0.1 (+local research prototype; polite crawl)"

SEED_URLS = [
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting",
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/regulatory-reporting-banking-sector",
    "https://www.bankofengland.co.uk/prudential-regulation/regulatory-reporting/regulatory-reporting-banking-sector/banks-building-societies-and-investment-firms",
    "https://www.prarulebook.co.uk/",
    "https://www.prarulebook.co.uk/pra-rules",
    "https://www.prarulebook.co.uk/pra-rules/reporting-crr",
    "https://www.prarulebook.co.uk/pra-rules/regulatory-reporting",
    "https://www.prarulebook.co.uk/pra-rules/liquidity-coverage-ratio-crr",
    "https://www.prarulebook.co.uk/pra-rules/liquidity-crr",
    "https://www.prarulebook.co.uk/guidance",
]

ALLOWED_HOSTS = {
    "www.bankofengland.co.uk",
    "www.prarulebook.co.uk",
    "prarulebook.co.uk",
    "www.eba.europa.eu",
    "eba.europa.eu",
}

OFFICIAL_PATH_HINTS = (
    "/prudential-regulation/regulatory-reporting",
    "/prudential-regulation/publication/",
    "/-/media/boe/files/prudential-regulation/",
    "/pra-rules/",
    "/guidance/",
)

FILE_EXTS = {
    ".pdf", ".xlsx", ".xls", ".xlsm", ".xltx", ".zip", ".xbrl", ".xml", ".xsd", ".csv", ".json",
    ".txt", ".html",
}
PRIMARY_FILE_EXTS = {".pdf", ".xlsx", ".xls", ".xlsm", ".xltx", ".zip", ".xbrl", ".xml", ".xsd", ".csv", ".json", ".txt"}
KEYWORDS = re.compile(
    r"\b(COR\s*011|COR011|C\s*66|C66|liquidity coverage ratio|LCR|liquidity reporting|reporting|taxonomy|DPM|validation|sample instance|instance file|Annex\s+XXIV|Annex\s+XXV|SS34/15|SS24/15|SS2/19|instructions|template|returns?)\b",
    re.I,
)
DATE_RE = re.compile(r"\b(?:\d{1,2}\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b", re.I)
GENERIC_LINK_TITLES = {
    "", "here", "click here", "download", "downloads", "link", "pdf", "xlsx", "xls",
    "zip", "html", "instructions", "template", "templates", "opens in a new window",
}

@dataclass
class ManifestRow:
    source_id: str
    title: str
    url: str
    local_path: str
    file_type: str
    downloaded_at: str
    http_status: int
    content_length: int
    checksum_sha256: str
    parent_url: str
    discovered_from_link_text: str
    effective_date: str
    publication_date: str
    notes: str
    status: str = "downloaded"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canon_url(url: str) -> str:
    p = urlparse(url)
    # Preserve query because BoE media URLs may use it, strip fragments.
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", p.query, ""))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_id(url: str) -> str:
    return hashlib.sha1(canon_url(url).encode()).hexdigest()[:16]


def safe_name(url: str, title: str = "") -> str:
    p = urlparse(url)
    name = Path(p.path).name or "index"
    ext = Path(name).suffix.lower()
    if not ext or len(ext) > 8:
        ext = ".html"
    stem = Path(name).stem if Path(name).stem else title[:80]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:110] or source_id(url)
    return f"{stem}-{source_id(url)}{ext}"


def clean_link_title(title: str) -> str:
    """Remove file-format UI chrome from titles scraped from link text."""
    title = re.sub(r"\s+", " ", (title or "").replace("\xa0", " ")).strip()
    title = re.sub(r"\s*\*+\s*$", "", title)
    title = re.sub(r"\s+opens in a new window\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*[\[(]\s*(?:pdf|xlsx|xls|zip|html)\s*[\])]\s*[\[(]\s*[\d.]+\s*(?:kb|mb|gb)\s*[\)]\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*[\[(]?\s*(?:pdf|xlsx|xls|zip|html)(?:\s+[\d.]+\s*(?:kb|mb|gb))?\s*[\])]\s*$", "", title, flags=re.I)
    return title.strip(" -–—")


def title_from_url(url: str) -> str:
    """Derive a readable fallback title when link text is generic, e.g. 'here'."""
    stem = Path(urlparse(url).path).stem or Path(urlparse(url).path).name or source_id(url)
    words = re.sub(r"[_-]+", " ", stem).strip()
    words = re.sub(r"\s+", " ", words)
    if not words:
        return url

    def fix_word(word: str) -> str:
        if re.fullmatch(r"(?i)(pra|fsa|rfb|cor|lvr|rep|idy|corep|finrep|nsfr|lcr|le|dpm|xbrl|xml|qa|qas|q&a|q&as|mlar)", word):
            return word.upper().replace("QAS", "Q&As")
        m = re.fullmatch(r"(?i)(pra|fsa|rfb|cor|lvr|rep|idy)(\d+[a-z]?)", word)
        if m:
            return m.group(1).upper() + m.group(2)
        m = re.fullmatch(r"(?i)(fsa|pra|rfb|cor|lvr|rep|idy)(\d+[a-z]?)(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)(\d{4})", word)
        if m:
            return f"{m.group(1).upper()}{m.group(2)} {m.group(3).title()} {m.group(4)}"
        m = re.fullmatch(r"(?i)(mlar)(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)(\d{4})", word)
        if m:
            return f"{m.group(1).upper()} {m.group(2).title()} {m.group(3)}"
        return word.capitalize() if word.islower() else word

    return " ".join(fix_word(w) for w in words.split())


def choose_download_title(url: str, *, link_text: str = "", title_hint: str = "") -> str:
    hinted = clean_link_title(title_hint)
    linked = clean_link_title(link_text)
    if hinted and hinted.lower() not in GENERIC_LINK_TITLES:
        return hinted
    if linked and linked.lower() not in GENERIC_LINK_TITLES:
        return linked
    return title_from_url(url)


def ext_file_type(url: str, ctype: str = "") -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext:
        return ext.lstrip(".")
    if "html" in ctype:
        return "html"
    guess = mimetypes.guess_extension((ctype or "").split(";")[0].strip())
    return (guess or "").lstrip(".") or "unknown"


def page_title(soup: BeautifulSoup, fallback: str) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(" ", strip=True):
        return h1.get_text(" ", strip=True)
    title = soup.find("title")
    if title and title.get_text(" ", strip=True):
        return title.get_text(" ", strip=True)
    return fallback


def extract_dates(soup: BeautifulSoup) -> tuple[str, str]:
    text_bits = []
    for sel in ["time", ".published-date", ".publication-date", ".date", ".effective", "main"]:
        for el in soup.select(sel)[:5]:
            txt = el.get_text(" ", strip=True)
            if txt:
                text_bits.append(txt)
    text = "\n".join(text_bits)[:8000]
    pub = ""
    eff = ""
    for m in re.finditer(r"(?:Published|Publication date|Date published)[:\s]+([^\n|]{0,80})", text, re.I):
        ds = DATE_RE.search(m.group(1))
        if ds:
            pub = ds.group(0); break
    for m in re.finditer(r"(?:Effective|Effective date|Coming into force)[:\s]+([^\n|]{0,80})", text, re.I):
        ds = DATE_RE.search(m.group(1))
        if ds:
            eff = ds.group(0); break
    if not pub:
        ds = DATE_RE.search(text)
        if ds: pub = ds.group(0)
    return eff, pub


def allowed_url(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or p.netloc.lower() not in ALLOWED_HOSTS:
        return False
    if p.netloc.lower().endswith("bankofengland.co.uk"):
        return any(h in p.path for h in OFFICIAL_PATH_HINTS[:3])
    if p.netloc.lower().endswith("prarulebook.co.uk"):
        return p.path in {"", "/", "/pra-rules", "/guidance"} or any(h in p.path for h in OFFICIAL_PATH_HINTS[3:])
    if p.netloc.lower().endswith("eba.europa.eu"):
        return True
    return False


def is_relevant(text: str, url: str) -> bool:
    hay = f"{text}\n{url}"
    return bool(KEYWORDS.search(hay))


def is_probable_file(url: str, text: str = "") -> bool:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in PRIMARY_FILE_EXTS:
        return True
    # BoE media files often omit a suffix but are linked from labels saying PDF/XLSX/ZIP.
    return bool(re.search(r"\b(pdf|xlsx|xls|zip|taxonomy|dpm|validation|sample instance|xml|xbrl)\b", text, re.I) and "/-/media/" in url)


class Downloader:
    def __init__(self, out_dir: Path, delay: float = 0.75, refresh: bool = False):
        self.out_dir = out_dir
        self.pages_dir = out_dir / "pages"
        self.files_dir = out_dir / "files"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.delay = delay
        self.refresh = refresh
        self.robots: dict[str, list[str]] = {}
        self.rows: dict[str, ManifestRow] = {}
        self.failed: list[dict] = []
        self.skipped: list[dict] = []
        self.unresolved: list[dict] = []
        self.last_request = 0.0

    def can_fetch(self, url: str) -> bool:
        # Keep this deliberately simple. The two in-scope sites publish short
        # User-agent:* robots files with Disallow path prefixes; Python's
        # RobotFileParser currently misreads these responses in this environment
        # as globally disallowed. We still honour explicit Disallow prefixes.
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            disallows: list[str] = []
            try:
                r = self.session.get(base + "/robots.txt", timeout=(10, 20))
                if r.status_code < 400:
                    active = False
                    for raw in r.text.splitlines():
                        line = raw.split("#", 1)[0].strip()
                        if not line or ":" not in line:
                            continue
                        k, v = [x.strip() for x in line.split(":", 1)]
                        kl = k.lower()
                        if kl == "user-agent":
                            active = v == "*"
                        elif active and kl == "disallow" and v:
                            disallows.append(v)
            except Exception:
                pass
            self.robots[base] = disallows
        path = p.path or "/"
        for dis in self.robots[base]:
            # Minimal wildcard support for patterns such as /*?*p=1.
            if "*" in dis:
                pat = "^" + re.escape(dis).replace("\\*", ".*")
                if re.search(pat, path + ("?" + p.query if p.query else "")):
                    return False
            elif path.startswith(dis):
                return False
        return True

    def polite_get(self, url: str) -> requests.Response:
        elapsed = time.time() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        resp = self.session.get(url, timeout=(10, 45), allow_redirects=True)
        self.last_request = time.time()
        return resp

    def local_path_for(self, url: str, title: str, is_page: bool) -> Path:
        return (self.pages_dir if is_page else self.files_dir) / safe_name(url, title)

    def download(self, url: str, *, parent_url: str, link_text: str, is_page: bool, title_hint: str = "", notes: str = "") -> ManifestRow | None:
        url = canon_url(url)
        sid = source_id(url)
        if sid in self.rows:
            return self.rows[sid]
        if not allowed_url(url):
            self.unresolved.append({"url": url, "parent_url": parent_url, "link_text": link_text, "reason": "outside_allowed_scope"})
            return None
        if not self.can_fetch(url):
            self.skipped.append({"url": url, "parent_url": parent_url, "link_text": link_text, "reason": "robots_disallow"})
            return None
        try:
            print(f"GET {'page' if is_page else 'file'} {url}", flush=True)
            resp = self.polite_get(url)
            status = resp.status_code
            content = resp.content
            ctype = resp.headers.get("content-type", "")
            if status >= 400:
                self.failed.append({"url": url, "parent_url": parent_url, "link_text": link_text, "http_status": status, "reason": resp.text[:300]})
                return None
            checksum = sha256_bytes(content)
            title = choose_download_title(url, link_text=link_text, title_hint=title_hint)
            eff = pub = ""
            if is_page or "html" in ctype:
                soup = BeautifulSoup(content, "lxml")
                title = page_title(soup, title)
                eff, pub = extract_dates(soup)
                is_page = True
            file_type = ext_file_type(url, ctype)
            local = self.local_path_for(url, title, is_page=is_page)
            write_note = ""
            if local.exists():
                old = sha256_bytes(local.read_bytes())
                if old == checksum and not self.refresh:
                    write_note = "existing file unchanged; not overwritten"
                else:
                    local.write_bytes(content)
                    write_note = "existing file replaced; checksum changed" if old != checksum else "existing file refreshed"
            else:
                local.write_bytes(content)
                write_note = "downloaded"
            row = ManifestRow(
                source_id=sid,
                title=title,
                url=url,
                local_path=str(local.relative_to(ROOT)),
                file_type=file_type,
                downloaded_at=now(),
                http_status=status,
                content_length=len(content),
                checksum_sha256=checksum,
                parent_url=parent_url,
                discovered_from_link_text=link_text,
                effective_date=eff,
                publication_date=pub,
                notes="; ".join(x for x in [notes, write_note, ctype] if x),
            )
            self.rows[sid] = row
            return row
        except Exception as exc:
            self.failed.append({"url": url, "parent_url": parent_url, "link_text": link_text, "reason": f"{type(exc).__name__}: {exc}"})
            return None

    def discover_links(self, row: ManifestRow) -> list[tuple[str, str, str]]:
        path = ROOT / row.local_path
        if row.file_type != "html" or not path.exists():
            return []
        soup = BeautifulSoup(path.read_bytes(), "lxml")
        out=[]
        for a in soup.find_all("a", href=True):
            raw = a.get("href") or ""
            if raw.startswith(("mailto:", "tel:", "javascript:")) or "%s" in raw:
                continue
            url = canon_url(urljoin(row.url, raw))
            text = a.get_text(" ", strip=True) or a.get("title", "") or raw
            context = " ".join([text, a.parent.get_text(" ", strip=True)[:500] if a.parent else ""])
            if not allowed_url(url):
                if is_relevant(context, url):
                    self.unresolved.append({"url": url, "parent_url": row.url, "link_text": text, "reason": "relevant_but_outside_allowed_hosts_or_paths"})
                continue
            if is_probable_file(url, text) and is_relevant(context, url):
                out.append((url, text, "direct relevant official file link"))
            elif is_relevant(context, url) and not is_probable_file(url, text):
                # Relevant official HTML pages linked from seed pages can contain the actual official files.
                # Keep this to one extra page layer, not a broad crawl.
                out.append((url, text, "direct relevant official html link"))
        # Preserve order, dedupe.
        seen=set(); dedup=[]
        for item in out:
            if item[0] not in seen:
                seen.add(item[0]); dedup.append(item)
        return dedup

    def write_outputs(self) -> None:
        rows = list(self.rows.values())
        csv_path = self.out_dir / "source_manifest.csv"
        json_path = self.out_dir / "source_manifest.json"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else list(ManifestRow.__dataclass_fields__.keys()))
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        json_path.write_text(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False), encoding="utf-8")
        report = self.build_report(rows)
        (self.out_dir / "report.md").write_text(report, encoding="utf-8")
        (self.out_dir / "failed_downloads.json").write_text(json.dumps(self.failed, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out_dir / "skipped_links.json").write_text(json.dumps(self.skipped, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out_dir / "unresolved_links.json").write_text(json.dumps(self.unresolved, indent=2, ensure_ascii=False), encoding="utf-8")

    def build_report(self, rows: list[ManifestRow]) -> str:
        by_type={}
        for r in rows:
            by_type[r.file_type]=by_type.get(r.file_type,0)+1
        failed = len(self.failed)
        skipped = len(self.skipped)
        unresolved = len(self.unresolved)
        lines = [
            "# COR011 / LCR reporting source discovery report",
            "",
            f"Generated: {now()}",
            "",
            "## Scope",
            "Source discovery and raw download only. No database build and no inferred legal relationships.",
            "",
            "## Summary",
            f"- Manifest rows: {len(rows)}",
            f"- Downloaded/available by type: {by_type}",
            f"- Failed downloads: {failed}",
            f"- Skipped links: {skipped}",
            f"- Unresolved relevant links outside allowed scope: {unresolved}",
            "",
            "## Downloaded files/pages",
        ]
        for r in sorted(rows, key=lambda x: (x.file_type, x.title.lower())):
            lines.append(f"- [{r.file_type}] {r.title} | {r.url} | `{r.local_path}` | {r.notes}")
        lines += ["", "## Failed downloads"]
        if self.failed:
            for f in self.failed:
                lines.append(f"- {f.get('http_status','')} {f['url']} from {f.get('parent_url','')} | {f.get('reason','')}")
        else:
            lines.append("- None")
        lines += ["", "## Skipped files/links"]
        if self.skipped:
            for s in self.skipped:
                lines.append(f"- {s['url']} from {s.get('parent_url','')} | {s.get('reason','')}")
        else:
            lines.append("- None")
        lines += ["", "## Unresolved relevant links"]
        if self.unresolved:
            for u in self.unresolved:
                lines.append(f"- {u['url']} from {u.get('parent_url','')} ({u.get('link_text','')}) | {u.get('reason','')}")
        else:
            lines.append("- None")
        return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--delay", type=float, default=0.75)
    ap.add_argument("--max-linked-pages", type=int, default=40)
    args = ap.parse_args()

    dl = Downloader(args.out_dir, delay=args.delay, refresh=args.refresh)

    # 1. Download fixed seed pages.
    seed_rows=[]
    for url in SEED_URLS:
        row = dl.download(url, parent_url="", link_text="seed page", is_page=True, notes="seed_scope_page")
        if row:
            seed_rows.append(row)

    # 2. Discover directly relevant official links from seed pages.
    discovered=[]
    for row in seed_rows:
        discovered.extend((url, text, note, row.url) for url, text, note in dl.discover_links(row))

    # 3. Download direct files and a bounded one-hop set of relevant official HTML pages.
    linked_pages=[]
    for url, text, note, parent in discovered:
        if is_probable_file(url, text):
            dl.download(url, parent_url=parent, link_text=text, is_page=False, notes=note)
        else:
            if len(linked_pages) >= args.max_linked_pages:
                dl.skipped.append({"url": url, "parent_url": parent, "link_text": text, "reason": "max_linked_pages_reached"})
                continue
            row = dl.download(url, parent_url=parent, link_text=text, is_page=True, notes=note)
            if row:
                linked_pages.append(row)

    # 4. From those directly relevant official pages, download relevant official files only.
    for row in linked_pages:
        for url, text, note in dl.discover_links(row):
            if is_probable_file(url, text) and is_relevant(f"{text} {row.title}", url):
                dl.download(url, parent_url=row.url, link_text=text, is_page=False, notes="file linked from directly relevant official html page")

    dl.write_outputs()
    summary = {
        "out_dir": str(args.out_dir),
        "manifest_csv": str(args.out_dir / "source_manifest.csv"),
        "manifest_json": str(args.out_dir / "source_manifest.json"),
        "report": str(args.out_dir / "report.md"),
        "rows": len(dl.rows),
        "failed": len(dl.failed),
        "skipped": len(dl.skipped),
        "unresolved": len(dl.unresolved),
    }
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
