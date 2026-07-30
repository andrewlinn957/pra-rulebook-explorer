#!/usr/bin/env python3
"""Backfill source text for every reference visible in legal reading mode.

The reader exposes outgoing ``references``, ``uses_defined_term`` and ``amends``
edges.  A target of one of those edges must have authoritative text, not a UI
placeholder.  This script resolves empty targets from, in order:

* the target's contained provisions;
* an exact glossary identifier or an unambiguous existing definition;
* another node at the exact same source URL;
* an already-downloaded source document; or
* the authoritative target URL.

Every applied value carries machine-readable provenance in ``metadata_json``.
The JSON audit contains one row for every target, including failures.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend/data/rulebook.sqlite3"
DEFAULT_CACHE = ROOT / "backend/data/raw/reader-reference-sources"
DEFAULT_AUDIT = ROOT / "outputs/reader-reference-text-audit.json"
DEFAULT_OVERRIDES = ROOT / "config/reader_reference_source_overrides.json"
GLOSSARY_ENDPOINT = "https://www.prarulebook.co.uk/api/sitecore/Glossary/GlossaryModal"
BANK_SEARCH_ENDPOINT = "https://api.cludo.com/api/v3/1962/9479/search"
BANK_SEARCH_AUTHORIZATION = "SiteKey MTk2Mjo5NDc5Og=="
READER_EDGE_TYPES = ("references", "uses_defined_term", "amends")
STRUCTURAL_TYPES = {
    "chapter",
    "part",
    "guidance_section",
    "guidance_document",
    "glossary",
    "crr_terms_list",
}
GENERIC_TITLES = {
    "bank of england",
    "prudential regulation authority",
    "pra rulebook",
    "here",
    "click here",
    "link",
}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/124.0 Safari/537.36 pra-rulebook-explorer-reference-recovery/1.0"
)
MAX_SOURCE_BYTES = 80 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
MIN_EXTERNAL_TEXT_CHARS = 160


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str) -> str:
    value = html.unescape(value or "").replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def source_text_issue(text: str, title: str = "") -> str:
    """Reject navigation scraps and error pages masquerading as source text."""
    cleaned = clean_text(text)
    combined = f"{title}\n{cleaned[:2000]}"
    if re.search(
        r"(?i)(404\s*[-:]?\s*page not found|page (?:you (?:were )?looking for|cannot be found)|"
        r"access denied|you don.t have permission to access|error 404|rule not found|"
        r"we can.t sign you in)",
        combined,
    ):
        return "source resolved to an error page"
    if len(cleaned) < MIN_EXTERNAL_TEXT_CHARS:
        return (
            f"source returned only {len(cleaned)} characters; "
            f"at least {MIN_EXTERNAL_TEXT_CHARS} are required"
        )
    return ""


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def normalized_url(value: str, *, keep_fragment: bool = True) -> str:
    parts = urlsplit(value or "")
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, host, path, "", fragment))


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value or "")]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass
class Resolution:
    node_id: str
    method: str
    text: str = ""
    source_url: str = ""
    source_title: str = ""
    content_type: str = ""
    source_node_ids: tuple[str, ...] = ()
    source_document_id: str = ""
    retrieval_url: str = ""
    rationale: str = ""
    retrieved_at: str = ""
    content_hash: str = ""
    error: str = ""
    status_code: int | None = None

    @property
    def resolved(self) -> bool:
        return bool(clean_text(self.text))

    def audit_row(self, node: sqlite3.Row) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": node["node_type"],
            "title": node["title"],
            "url": node["url"],
            "resolved": self.resolved,
            "method": self.method,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "source_node_ids": list(self.source_node_ids),
            "source_document_id": self.source_document_id,
            "retrieval_url": self.retrieval_url,
            "rationale": self.rationale,
            "content_type": self.content_type,
            "text_characters": len(self.text),
            "content_hash": self.content_hash,
            "status_code": self.status_code,
            "error": self.error,
        }


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def reader_targets(conn: sqlite3.Connection, *, empty_only: bool = True) -> list[sqlite3.Row]:
    empty_clause = "AND trim(coalesce(n.text,''))=''" if empty_only else ""
    placeholders = ",".join("?" for _ in READER_EDGE_TYPES)
    return list(
        conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json,
                   count(e.id) AS reader_edge_count
            FROM edge e
            JOIN node n ON n.id=e.to_node_id
            WHERE e.edge_type IN ({placeholders})
              {empty_clause}
            GROUP BY n.id
            ORDER BY n.node_type,n.title,n.id
            """,
            READER_EDGE_TYPES,
        )
    )


def parse_metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def aggregate_descendant_text(
    root: sqlite3.Row,
    children: dict[str, list[str]],
    nodes: dict[str, sqlite3.Row],
) -> Resolution:
    source_rows: list[sqlite3.Row] = []
    seen = {root["id"]}

    def visit(node_id: str) -> None:
        child_ids = sorted(
            children.get(node_id, []),
            key=lambda value: natural_key(nodes.get(value, {"title": ""})["title"]),
        )
        for child_id in child_ids:
            if child_id in seen:
                continue
            seen.add(child_id)
            child = nodes.get(child_id)
            if child is None:
                continue
            if clean_text(child["text"] or ""):
                source_rows.append(child)
            visit(child_id)

    visit(root["id"])
    if not source_rows:
        return Resolution(root["id"], "contained_provisions", error="no text-bearing descendants")
    sections = []
    for row in source_rows:
        heading = clean_text(row["title"] or "")
        body = clean_text(row["text"] or "")
        sections.append(f"{heading}\n{body}" if heading and heading != body else body)
    value = "\n\n".join(sections)
    return Resolution(
        root["id"],
        "contained_provisions",
        text=value,
        source_url=root["url"] or "",
        source_title=root["title"] or "",
        content_type="text/plain",
        source_node_ids=tuple(row["id"] for row in source_rows),
        content_hash=sha256_bytes(value.encode("utf-8")),
    )


def glossary_resolution_from_existing(
    row: sqlite3.Row,
    by_hash: dict[str, list[sqlite3.Row]],
    by_title: dict[str, list[sqlite3.Row]],
) -> Resolution | None:
    metadata = parse_metadata(row)
    glossary_hash = metadata.get("glossary_hash")
    candidates = by_hash.get(str(glossary_hash), []) if glossary_hash else []
    method = "existing_glossary_hash"
    if not candidates:
        candidates = by_title.get(normalized_title(row["title"]), [])
        method = "existing_definition_title"
    texts = {clean_text(candidate["text"]): candidate for candidate in candidates if clean_text(candidate["text"])}
    if len(texts) != 1:
        return None
    text, source = next(iter(texts.items()))
    return Resolution(
        row["id"],
        method,
        text=text,
        source_url=source["url"] or "",
        source_title=source["title"] or "",
        content_type="text/plain",
        source_node_ids=(source["id"],),
        content_hash=sha256_bytes(text.encode("utf-8")),
    )


def exact_url_resolution(
    row: sqlite3.Row,
    by_url: dict[str, list[sqlite3.Row]],
) -> Resolution | None:
    key = normalized_url(row["url"] or "")
    if not key:
        return None
    candidates = by_url.get(key, [])
    texts = {clean_text(candidate["text"]): candidate for candidate in candidates if clean_text(candidate["text"])}
    if len(texts) != 1:
        return None
    text, source = next(iter(texts.items()))
    return Resolution(
        row["id"],
        "existing_exact_source_url",
        text=text,
        source_url=source["url"] or "",
        source_title=source["title"] or "",
        content_type="text/plain",
        source_node_ids=(source["id"],),
        content_hash=sha256_bytes(text.encode("utf-8")),
    )


def embedded_document_resolution(row: sqlite3.Row, source: sqlite3.Row) -> Resolution:
    """Resolve a PRA fragment/root from the HTML already captured at crawl time."""
    raw_html = source["raw_html"] or ""
    raw_text = clean_text(source["raw_text"] or "")
    if not raw_html and not raw_text:
        return Resolution(
            row["id"],
            "embedded_document_source",
            source_url=source["url"] or "",
            source_document_id=source["id"],
            error="document_source contains no text",
        )
    fragment = urlsplit(row["url"] or "").fragment
    title = ""
    if fragment and raw_html:
        soup = BeautifulSoup(raw_html, "lxml")
        element = soup.find(id=fragment)
        if element is None:
            return Resolution(
                row["id"],
                "embedded_document_fragment",
                source_url=row["url"] or source["url"] or "",
                source_document_id=source["id"],
                error=f"fragment not found in captured source: {fragment}",
            )
        anchor = element
        classes = set(anchor.get("class", [])) if getattr(anchor, "get", None) else set()
        if not ({"chapter-section", "row-block"} & classes):
            anchor = element.find_parent(class_=lambda value: value and ("chapter-section" in value or "row-block" in value)) or element
        pieces = [clean_text(anchor.get_text("\n"))]
        body_blocks = 0
        for sibling in anchor.next_siblings:
            if not getattr(sibling, "get_text", None):
                continue
            sibling_classes = set(sibling.get("class", [])) if getattr(sibling, "get", None) else set()
            sibling_text = clean_text(sibling.get_text("\n"))
            if not sibling_text:
                continue
            if "row-block" in sibling_classes:
                body_blocks += 1
            pieces.append(sibling_text)
            # A structural citation should open with enough following legal
            # provisions to explain the heading without swallowing the whole Part.
            if body_blocks >= 12 or sum(len(part) for part in pieces) >= 60_000:
                break
        value = clean_text("\n\n".join(pieces))
        title = clean_text((element.find(["h1", "h2", "h3", "h4"]) or element).get_text(" "))
        method = "embedded_document_fragment"
    else:
        value = raw_text or extract_html(raw_html.encode("utf-8"))[0]
        method = "embedded_document_source"
    if not value:
        return Resolution(
            row["id"],
            method,
            source_url=row["url"] or source["url"] or "",
            source_document_id=source["id"],
            error="captured source yielded no readable text",
        )
    return Resolution(
        row["id"],
        method,
        text=value,
        source_url=row["url"] or source["url"] or "",
        source_title=title,
        source_document_id=source["id"],
        content_type="text/html",
        retrieved_at=source["fetched_at"] or "",
        content_hash=sha256_bytes(value.encode("utf-8")),
    )


def historical_document_code(url: str) -> str:
    value = (url or "").casefold()
    patterns = (
        r"/(ss|cp|ps)/(?:20)?\d{2}/\1(\d{3,4})(?:update)?(?:[./]|$)",
        r"/(ss|cp|ps)(\d{3,4})(?:update)?(?:[./]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        kind, digits = match.groups()
        if len(digits) >= 3:
            return f"{kind.upper()}{int(digits[:-2])}/{digits[-2:]}"
    return ""


def aggregate_alias_rows(
    row: sqlite3.Row,
    method: str,
    candidates: Iterable[sqlite3.Row],
) -> Resolution | None:
    unique: dict[tuple[str, str], sqlite3.Row] = {}
    for candidate in candidates:
        text = clean_text(candidate["text"] or "")
        if text:
            unique[(clean_text(candidate["title"] or ""), text)] = candidate
    if not unique:
        return None
    ordered = sorted(unique.values(), key=lambda item: natural_key(item["title"]))
    value = "\n\n".join(
        f"{candidate['title']}\n{clean_text(candidate['text'])}" for candidate in ordered
    )
    return Resolution(
        row["id"],
        method,
        text=value,
        source_url=ordered[0]["url"] or row["url"] or "",
        source_title=ordered[0]["title"] or "",
        content_type="text/plain",
        source_node_ids=tuple(candidate["id"] for candidate in ordered),
        content_hash=sha256_bytes(value.encode("utf-8")),
    )


def local_legal_alias_resolution(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    nodes: dict[str, sqlite3.Row],
    by_title: dict[str, list[sqlite3.Row]],
) -> Resolution | None:
    url = row["url"] or ""
    article_match = re.search(r"legislation\.gov\.uk/eur/2013/575/article/(\d+[a-z]?)", url, re.I)
    if article_match:
        article = article_match.group(1).casefold()
        candidates = [
            node
            for node in nodes.values()
            if node["node_type"] == "rule"
            and re.match(rf"^Article\s+{re.escape(article)}(?:\b|\()", node["title"] or "", re.I)
        ]
        resolution = aggregate_alias_rows(row, "local_uk_crr_article", candidates)
        if resolution:
            return resolution

    code = historical_document_code(url)
    if code:
        document_candidates = [
            node
            for node in nodes.values()
            if node["node_type"] == "guidance_document"
            and re.match(rf"^{re.escape(code)}\b", node["title"] or "", re.I)
            and clean_text(node["text"] or "")
        ]
        if document_candidates:
            # Canonical and PDF projections can duplicate a document. Prefer
            # the richest exact-code body while retaining its source identity.
            source = max(document_candidates, key=lambda item: len(item["text"] or ""))
            return aggregate_alias_rows(row, "historical_document_code", [source])
        paragraph_candidates = [
            node
            for node in nodes.values()
            if node["node_type"] == "guidance_paragraph"
            and re.match(rf"^{re.escape(code)}(?:\s|–|-)", node["title"] or "", re.I)
            and clean_text(node["text"] or "")
        ]
        resolution = aggregate_alias_rows(row, "historical_document_code", paragraph_candidates)
        if resolution:
            return resolution

    full_definition = re.search(r"/Glossary/FullDefinition/(\d+)", url, re.I)
    if full_definition:
        glossary_id = full_definition.group(1)
        needle = f'data-glossary-id="{glossary_id}"'
        raw_rows = conn.execute(
            "SELECT raw_html FROM document_source WHERE instr(raw_html,?)>0 LIMIT 20",
            (needle,),
        ).fetchall()
        titles: Counter[str] = Counter()
        pattern = re.compile(
            rf'<a\b[^>]*\btitle="([^"]+)"[^>]*\bdata-glossary-id="{glossary_id}"',
            re.I,
        )
        reverse_pattern = re.compile(
            rf'<a\b[^>]*\bdata-glossary-id="{glossary_id}"[^>]*\btitle="([^"]+)"',
            re.I,
        )
        for raw in raw_rows:
            for title in pattern.findall(raw["raw_html"] or "") + reverse_pattern.findall(raw["raw_html"] or ""):
                titles[clean_text(title)] += 1
        for title, _ in titles.most_common():
            candidates = by_title.get(normalized_title(title), [])
            texts = {clean_text(candidate["text"]): candidate for candidate in candidates if clean_text(candidate["text"])}
            if len(texts) == 1:
                text, source = next(iter(texts.items()))
                return Resolution(
                    row["id"],
                    "legacy_glossary_id",
                    text=text,
                    source_url=source["url"] or url,
                    source_title=source["title"] or title,
                    content_type="text/plain",
                    source_node_ids=(source["id"],),
                    content_hash=sha256_bytes(text.encode("utf-8")),
                )

    # Some old Sitecore links now redirect to a generated 404 URL, but the
    # citation itself still identifies a sibling provision in the same Part.
    incoming = conn.execute(
        """
        SELECT e.evidence_text,source.metadata_json
        FROM edge e JOIN node source ON source.id=e.from_node_id
        WHERE e.to_node_id=?
        """,
        (row["id"],),
    ).fetchall()
    contextual_candidates: dict[str, sqlite3.Row] = {}
    for edge in incoming:
        citation = clean_text(edge["evidence_text"] or "")
        if not re.fullmatch(r"(?:[A-Z]{2,8}\s+)?\d+[A-Z]?(?:\.\d+[A-Z]?)*(?:R)?", citation, re.I):
            continue
        try:
            source_metadata = json.loads(edge["metadata_json"] or "{}")
        except json.JSONDecodeError:
            source_metadata = {}
        part_title = normalized_title(source_metadata.get("part_title", ""))
        for candidate in nodes.values():
            if candidate["node_type"] != "rule" or normalized_title(candidate["title"]) != normalized_title(citation):
                continue
            candidate_part = normalized_title(parse_metadata(candidate).get("part_title", ""))
            if part_title and candidate_part == part_title and clean_text(candidate["text"] or ""):
                contextual_candidates[candidate["id"]] = candidate
    if len(contextual_candidates) == 1:
        return aggregate_alias_rows(
            row, "same_part_citation", contextual_candidates.values()
        )
    return None


def load_source_overrides(path: Path = DEFAULT_OVERRIDES) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"source overrides must be a JSON object: {path}")
    return {
        str(node_id): override
        for node_id, override in value.items()
        if isinstance(override, dict)
    }


def curated_local_resolution(
    row: sqlite3.Row,
    override: dict[str, Any],
    nodes: dict[str, sqlite3.Row],
    children: dict[str, list[str]],
) -> Resolution | None:
    expected_url = override.get("original_url")
    if expected_url and normalized_url(expected_url) != normalized_url(row["url"] or ""):
        return None
    candidates: list[sqlite3.Row] = []
    structural_resolutions: list[Resolution] = []
    for source_id in override.get("source_node_ids", []):
        source = nodes.get(str(source_id))
        if source is not None:
            if clean_text(source["text"] or ""):
                candidates.append(source)
            else:
                contained = aggregate_descendant_text(source, children, nodes)
                if contained.resolved:
                    structural_resolutions.append(contained)
    title_prefix = clean_text(str(override.get("source_title_prefix") or ""))
    if title_prefix:
        allowed_types = set(
            override.get(
                "source_node_types",
                ["guidance_document", "guidance_paragraph", "rule", "defined_term"],
            )
        )
        candidates.extend(
            source
            for source in nodes.values()
            if source["node_type"] in allowed_types
            and clean_text(source["title"] or "").startswith(title_prefix)
        )
    direct_resolution = aggregate_alias_rows(row, "curated_local_source", candidates)
    pieces = [
        resolution.text
        for resolution in [direct_resolution, *structural_resolutions]
        if resolution and resolution.resolved
    ]
    if not pieces:
        return None
    text = clean_text("\n\n".join(pieces))
    source_ids = tuple(
        dict.fromkeys(
            [
                *(direct_resolution.source_node_ids if direct_resolution else ()),
                *(
                    source_id
                    for resolution in structural_resolutions
                    for source_id in resolution.source_node_ids
                ),
            ]
        )
    )
    source_title = clean_text(str(override.get("source_title") or ""))
    if not source_title and direct_resolution:
        source_title = direct_resolution.source_title
    source_url = clean_text(str(override.get("source_url") or ""))
    if not source_url and direct_resolution:
        source_url = direct_resolution.source_url
    return Resolution(
        row["id"],
        "curated_local_source",
        text=text[:MAX_TEXT_CHARS],
        source_url=source_url,
        source_title=source_title,
        content_type="text/plain",
        source_node_ids=source_ids,
        rationale=clean_text(str(override.get("rationale") or "")),
        content_hash=sha256_bytes(text.encode("utf-8")),
    )


def reconstructed_context_urls(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """Recover URLs truncated at spaces by legacy plain-text link extraction."""
    original = row["url"] or ""
    if not original:
        return []
    variants = {original, original.replace("http://", ""), original.replace("https://", "")}
    source_rows = conn.execute(
        """
        SELECT source.text
        FROM edge e JOIN node source ON source.id=e.from_node_id
        WHERE e.to_node_id=? AND trim(coalesce(source.text,''))<>''
        """,
        (row["id"],),
    ).fetchall()
    candidates: set[str] = set()
    for source in source_rows:
        text = source["text"] or ""
        for variant in variants:
            start = text.casefold().find(variant.casefold())
            if start < 0:
                continue
            value = text[start : start + 500]
            match = re.match(
                r"(?P<url>(?:https?://)?(?:www\.)?[A-Za-z0-9.-]+/[^\n]{0,420}?"
                r"\.(?:pdf|xlsx|xltx|xls|docx?|html?|aspx|cfm)(?:/[A-Za-z0-9-]+)?)"
                r"(?=[\s,.;)\]]|$)",
                value,
                re.I,
            )
            if not match:
                continue
            candidate = clean_text(match.group("url")).rstrip(".,;)")
            if not candidate.startswith(("http://", "https://")):
                candidate = f"https://{candidate}"
            candidate = re.sub(r"(?<=/)\s+", "", candidate)
            candidate = re.sub(r"(\d-)\s+(\d)", r"\1\2", candidate)
            candidate_identity = re.sub(r"^https?://", "", candidate, flags=re.I)
            original_identity = re.sub(r"^https?://", "", original, flags=re.I)
            if len(candidate_identity) > len(original_identity):
                candidates.add(candidate)
    return sorted(candidates, key=len, reverse=True)


def fuzzy_downloaded_source_resolution(
    row: sqlite3.Row,
    sources: Iterable[sqlite3.Row],
) -> sqlite3.Row | None:
    url = row["url"] or ""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host = parts.netloc.casefold().removeprefix("www.")
    query_path = re.sub(r"[^a-z0-9]+", " ", parts.path.casefold()).strip()
    if len(query_path) < 20:
        return None
    query_stem = re.sub(
        r"(?:-v\d+)?\.(?:xls|xlsx|xltx|xlsm|pdf|docx?)$",
        "",
        Path(parts.path.casefold()).name,
    )
    query_tokens = set(query_path.split())
    ranked: list[tuple[float, sqlite3.Row]] = []
    for source in sources:
        source_url = source["url"] or ""
        source_parts = urlsplit(source_url if "://" in source_url else f"http://{source_url}")
        if source_parts.netloc.casefold().removeprefix("www.") != host:
            continue
        source_path = re.sub(r"[^a-z0-9]+", " ", source_parts.path.casefold()).strip()
        if not source_path:
            continue
        source_tokens = set(source_path.split())
        overlap = len(query_tokens & source_tokens) / max(1, len(query_tokens | source_tokens))
        strict_prefix = source_path.startswith(query_path) or query_path.startswith(source_path)
        source_stem = re.sub(
            r"(?:-v\d+)?\.(?:xls|xlsx|xltx|xlsm|pdf|docx?)$",
            "",
            Path(source_parts.path.casefold()).name,
        )
        filename_successor = bool(
            query_stem
            and source_stem
            and (
                source_stem == query_stem
                or source_stem.startswith(f"{query_stem}-v")
                or query_stem.startswith(f"{source_stem}-v")
            )
        )
        if filename_successor:
            ranked.append((1.0, source))
            continue
        if overlap < 0.55 and not strict_prefix:
            continue
        score = difflib.SequenceMatcher(None, query_path, source_path).ratio()
        if strict_prefix:
            score = max(score, min(len(query_path), len(source_path)) / max(len(query_path), len(source_path)))
        ranked.append((score, source))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 0.92:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.04:
        return None
    return ranked[0][1]


def extract_pdf(payload: bytes) -> tuple[str, str]:
    reader = PdfReader(io.BytesIO(payload))
    pages: list[str] = []
    for page in reader.pages:
        try:
            value = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
        except (KeyError, TypeError):
            value = page.extract_text() or ""
        value = clean_text(value)
        if value:
            pages.append(value)
        if sum(len(page_text) for page_text in pages) >= MAX_TEXT_CHARS:
            break
    return clean_text("\n\n".join(pages))[:MAX_TEXT_CHARS], ""


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    return [
        clean_text("".join(part.text or "" for part in item.iter(f"{namespace}t")))
        for item in root.findall(f"{namespace}si")
    ]


def _xlsx_sheet_names(archive: zipfile.ZipFile) -> dict[str, str]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_namespace = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in relationships.findall(f"{package_namespace}Relationship")
    }
    result: dict[str, str] = {}
    for sheet in workbook.findall(f".//{namespace}sheet"):
        target = targets.get(sheet.attrib.get(f"{rel_namespace}id", ""), "")
        if target:
            result["xl/" + target.lstrip("/")] = sheet.attrib.get("name", "Worksheet")
    return result


def extract_xlsx(payload: bytes) -> tuple[str, str]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    lines: list[str] = []
    total_characters = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        shared = _xlsx_shared_strings(archive)
        sheets = _xlsx_sheet_names(archive)
        for sheet_path, sheet_name in sheets.items():
            heading = f"Worksheet: {sheet_name}"
            lines.append(heading)
            total_characters += len(heading)
            try:
                root = ElementTree.fromstring(archive.read(sheet_path))
            except KeyError:
                continue
            row_values: list[str] = []
            for cell in root.findall(f".//{namespace}c"):
                kind = cell.attrib.get("t", "")
                value_el = cell.find(f"{namespace}v")
                inline = cell.find(f"{namespace}is")
                value = ""
                if kind == "inlineStr" and inline is not None:
                    value = "".join(part.text or "" for part in inline.iter(f"{namespace}t"))
                elif value_el is not None:
                    value = value_el.text or ""
                    if kind == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                value = clean_text(value)
                if value:
                    row_values.append(value)
                    total_characters += len(value)
                if total_characters >= MAX_TEXT_CHARS:
                    break
            lines.extend(row_values)
            if total_characters >= MAX_TEXT_CHARS:
                break
    return clean_text("\n".join(lines))[:MAX_TEXT_CHARS], ""


def extract_docx(payload: bytes) -> tuple[str, str]:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.findall(f".//{namespace}p"):
        value = clean_text("".join(part.text or "" for part in paragraph.iter(f"{namespace}t")))
        if value:
            paragraphs.append(value)
    return clean_text("\n".join(paragraphs))[:MAX_TEXT_CHARS], ""


def extract_legacy_xls(payload: bytes) -> tuple[str, str]:
    """Convert a legacy BIFF workbook with LibreOffice, then read its cells."""
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        return "", ""
    with tempfile.TemporaryDirectory(prefix="reader-reference-xls-") as temp_name:
        temp_dir = Path(temp_name)
        source = temp_dir / "source.xls"
        source.write_bytes(payload)
        profile = (temp_dir / "profile").as_uri()
        result = subprocess.run(
            [
                executable,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(temp_dir),
                str(source),
            ],
            check=False,
            capture_output=True,
            timeout=90,
        )
        converted = temp_dir / "source.xlsx"
        if result.returncode != 0 or not converted.exists():
            return "", ""
        return extract_xlsx(converted.read_bytes())


def extract_html(payload: bytes, content_type: str = "") -> tuple[str, str]:
    soup = BeautifulSoup(payload, "lxml")
    title = ""
    title_el = soup.select_one('meta[property="og:title"]')
    if title_el:
        title = clean_text(title_el.get("content", ""))
    if not title:
        title = clean_text((soup.find("h1") or soup.find("title") or "").get_text(" ") if (soup.find("h1") or soup.find("title")) else "")
    for unwanted in soup.select("script,style,svg,noscript,nav,header,footer,form,.cookie-banner,.global-header,.global-footer"):
        unwanted.decompose()
    container = (
        soup.select_one(".rulebook-content")
        or soup.find("main")
        or soup.find("article")
        or soup.select_one('[role="main"]')
        or soup.select_one(".page-content")
        or soup.body
        or soup
    )
    value = clean_text(container.get_text("\n"))
    return value[:MAX_TEXT_CHARS], title


def extract_json(payload: bytes) -> tuple[str, str]:
    value = json.loads(payload.decode("utf-8", errors="replace"))
    if isinstance(value, dict):
        title = clean_text(str(value.get("Term") or value.get("title") or value.get("name") or ""))
        body = value.get("Definition") or value.get("description") or value.get("text")
        if isinstance(body, str):
            body_text, _ = extract_html(body.encode("utf-8"))
            return body_text, title
    return clean_text(json.dumps(value, ensure_ascii=False, indent=2))[:MAX_TEXT_CHARS], ""


def extract_source(payload: bytes, content_type: str, url: str) -> tuple[str, str]:
    media_type = (content_type or "").split(";", 1)[0].lower()
    suffix = Path(urlsplit(url).path).suffix.lower()
    leading = payload[:100].lstrip().lower()
    if leading.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return extract_html(payload, content_type)
    if (
        payload.startswith(b"PK")
        and (
            "spreadsheet" in media_type
            or suffix in {".xlsx", ".xltx", ".xlsm"}
            or "xl/workbook.xml" in zipfile.ZipFile(io.BytesIO(payload)).namelist()
        )
    ):
        return extract_xlsx(payload)
    if payload.startswith(b"PK") and "word/document.xml" in zipfile.ZipFile(io.BytesIO(payload)).namelist():
        return extract_docx(payload)
    if payload.startswith(b"\xd0\xcf\x11\xe0") and (
        "excel" in media_type or suffix == ".xls"
    ):
        return extract_legacy_xls(payload)
    if payload.startswith(b"%PDF") or media_type == "application/pdf" or suffix == ".pdf":
        return extract_pdf(payload)
    if media_type == "application/json" or suffix == ".json":
        return extract_json(payload)
    if "html" in media_type or suffix in {"", ".html", ".htm", ".aspx", ".shtml", ".cfm"}:
        return extract_html(payload, content_type)
    if media_type.startswith("text/"):
        return clean_text(payload.decode("utf-8", errors="replace"))[:MAX_TEXT_CHARS], ""
    return "", ""


def extract_source_cached(
    payload: bytes,
    content_type: str,
    url: str,
    cache_dir: Path,
) -> tuple[str, str]:
    media_type = (content_type or "").split(";", 1)[0].lower()
    leading = payload[:100].lstrip().lower()
    is_html = (
        "html" in media_type
        or leading.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
    )
    cache_version = "EXTRACT_V4_HTML_MAIN" if is_html else "EXTRACT_V3"
    key = cache_key(cache_version, sha256_bytes(payload))
    path = cache_dir / "extracted" / f"{key}.json"
    if path.exists():
        value = json.loads(path.read_text())
        return value.get("text", ""), value.get("title", "")
    text, title = extract_source(payload, content_type, url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"text": text, "title": title}, ensure_ascii=False),
    )
    return text, title


def cache_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}|{value}".encode("utf-8")).hexdigest()


def cached_fetch(
    url: str,
    cache_dir: Path,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    request_identity = json.dumps(json_body, sort_keys=True) if json_body is not None else url
    key = cache_key(method, request_identity)
    body_path = cache_dir / f"{key}.bin"
    meta_path = cache_dir / f"{key}.json"
    if body_path.exists() and meta_path.exists():
        return body_path.read_bytes(), json.loads(meta_path.read_text())
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if "prarulebook.co.uk" in url:
        headers.update(
            {
                "Referer": "https://www.prarulebook.co.uk/",
                "Origin": "https://www.prarulebook.co.uk",
            }
        )
    response = None
    for attempt in range(3):
        if method == "POST":
            response = requests.post(url, json=json_body, headers=headers, timeout=30)
        else:
            response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        if response.status_code not in {403, 429, 502, 503, 504} or attempt == 2:
            break
        time.sleep(1.5 * (attempt + 1))
    assert response is not None
    response.raise_for_status()
    payload = response.content
    if len(payload) > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    metadata = {
        "request_url": url,
        "final_url": response.url,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "retrieved_at": utc_now(),
        "content_hash": sha256_bytes(payload),
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    body_path.write_bytes(payload)
    meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
    return payload, metadata


def fetch_glossary(row: sqlite3.Row, cache_dir: Path) -> Resolution:
    metadata = parse_metadata(row)
    glossary_hash = str(metadata.get("glossary_hash") or "")
    if not glossary_hash:
        return Resolution(row["id"], "glossary_api", error="missing glossary_hash")
    request = {
        "glossaryTermGroupId": glossary_hash,
        "effectiveDate": "01/06/2026",
        "hasBreadcrumb": False,
        "appendToBreadcrumb": False,
        "breadcrumbList": {},
    }
    try:
        payload, fetch_meta = cached_fetch(
            GLOSSARY_ENDPOINT,
            cache_dir / "glossary",
            method="POST",
            json_body=request,
        )
        text, title = extract_json(payload)
        if not text:
            return Resolution(
                row["id"],
                "glossary_api",
                source_url=row["url"] or GLOSSARY_ENDPOINT,
                source_title=title,
                content_type=fetch_meta["content_type"],
                retrieved_at=fetch_meta["retrieved_at"],
                content_hash=fetch_meta["content_hash"],
                status_code=fetch_meta["status_code"],
                error="glossary response contained no definition",
            )
        return Resolution(
            row["id"],
            "glossary_api",
            text=text,
            source_url=row["url"] or GLOSSARY_ENDPOINT,
            source_title=title,
            content_type=fetch_meta["content_type"],
            retrieved_at=fetch_meta["retrieved_at"],
            content_hash=sha256_bytes(text.encode("utf-8")),
            status_code=fetch_meta["status_code"],
        )
    except Exception as exc:  # audit every retrieval failure instead of aborting the run
        return Resolution(row["id"], "glossary_api", source_url=row["url"] or "", error=str(exc))


def cached_glossary_resolution(row: sqlite3.Row, cache_dir: Path) -> Resolution | None:
    metadata = parse_metadata(row)
    glossary_hash = str(metadata.get("glossary_hash") or "")
    if not glossary_hash:
        return None
    request = {
        "glossaryTermGroupId": glossary_hash,
        "effectiveDate": "01/06/2026",
        "hasBreadcrumb": False,
        "appendToBreadcrumb": False,
        "breadcrumbList": {},
    }
    key = cache_key("POST", json.dumps(request, sort_keys=True))
    glossary_cache = cache_dir / "glossary"
    if not (glossary_cache / f"{key}.bin").exists() or not (glossary_cache / f"{key}.json").exists():
        return None
    return fetch_glossary(row, cache_dir)


def source_document_resolution(row: sqlite3.Row, source: sqlite3.Row, project_root: Path) -> Resolution:
    local_path = Path(source["local_path"] or "")
    if local_path and not local_path.is_absolute():
        local_path = project_root / local_path
    if not local_path.exists():
        return Resolution(
            row["id"],
            "downloaded_source_document",
            source_url=source["url"] or "",
            source_title=source["title"] or "",
            source_document_id=source["source_id"],
            error=f"local source missing: {local_path}",
        )
    try:
        payload = local_path.read_bytes()
        text, extracted_title = extract_source_cached(
            payload,
            source["file_type"] or "",
            source["url"] or str(local_path),
            DEFAULT_CACHE,
        )
        if not text:
            return Resolution(
                row["id"],
                "downloaded_source_document",
                source_url=source["url"] or "",
                source_title=source["title"] or extracted_title,
                source_document_id=source["source_id"],
                content_type=source["file_type"] or "",
                content_hash=sha256_bytes(payload),
                error="downloaded source yielded no readable text",
            )
        return Resolution(
            row["id"],
            "downloaded_source_document",
            text=text,
            source_url=source["url"] or "",
            source_title=source["title"] or extracted_title,
            source_document_id=source["source_id"],
            content_type=source["file_type"] or "",
            retrieved_at=source["downloaded_at"] or "",
            content_hash=sha256_bytes(text.encode("utf-8")),
        )
    except Exception as exc:
        return Resolution(
            row["id"],
            "downloaded_source_document",
            source_url=source["url"] or "",
            source_title=source["title"] or "",
            source_document_id=source["source_id"],
            error=str(exc),
        )


def fetch_external(
    row: sqlite3.Row,
    cache_dir: Path,
    alternate_urls: Iterable[str] | dict[str, str] = (),
) -> Resolution:
    url = row["url"] or ""
    if not url:
        return Resolution(row["id"], "authoritative_url", error="missing source URL")
    alternate_methods = (
        dict(alternate_urls)
        if isinstance(alternate_urls, dict)
        else {candidate: "reconstructed_source_url" for candidate in alternate_urls}
    )
    urls = [url, *(candidate for candidate in alternate_methods if candidate != url)]
    errors: list[str] = []
    for candidate_url in urls:
        resolution = _fetch_external_url(row, cache_dir, candidate_url)
        if resolution.resolved:
            if candidate_url != url:
                resolution.method = alternate_methods[candidate_url]
            return resolution
        if resolution.error:
            errors.append(f"{candidate_url}: {resolution.error}")
    return Resolution(
        row["id"],
        "authoritative_url",
        source_url=url,
        error="; ".join(errors) or "no readable source response",
    )


def _fetch_external_url(row: sqlite3.Row, cache_dir: Path, url: str) -> Resolution:
    try:
        payload, metadata = cached_fetch(url, cache_dir / "external")
        text, title = extract_source_cached(
            payload,
            metadata["content_type"],
            metadata["final_url"],
            cache_dir,
        )
        text_issue = source_text_issue(text, title)
        if text_issue:
            if "bankofengland.co.uk" in urlsplit(url if "://" in url else f"http://{url}").netloc.casefold():
                search_result = fetch_bank_search(row, cache_dir)
                if search_result.resolved:
                    return search_result
            proxy_result = fetch_text_proxy(row, cache_dir, url)
            if proxy_result.resolved:
                return proxy_result
            return Resolution(
                row["id"],
                "authoritative_url",
                source_url=metadata["final_url"],
                source_title=title,
                content_type=metadata["content_type"],
                retrieved_at=metadata["retrieved_at"],
                content_hash=metadata["content_hash"],
                status_code=metadata["status_code"],
                error=text_issue,
            )
        return Resolution(
            row["id"],
            "authoritative_url",
            text=text,
            source_url=metadata["final_url"],
            source_title=title,
            content_type=metadata["content_type"],
            retrieved_at=metadata["retrieved_at"],
            content_hash=sha256_bytes(text.encode("utf-8")),
            status_code=metadata["status_code"],
        )
    except Exception as exc:
        if "bankofengland.co.uk" in urlsplit(url if "://" in url else f"http://{url}").netloc.casefold():
            search_result = fetch_bank_search(row, cache_dir)
            if search_result.resolved:
                return search_result
            bank_error = f"; Bank search fallback: {search_result.error}"
        else:
            bank_error = ""
        proxy_result = fetch_text_proxy(row, cache_dir, url)
        if proxy_result.resolved:
            return proxy_result
        return Resolution(
            row["id"],
            "authoritative_url",
            source_url=url,
            error=f"{exc}{bank_error}; text proxy fallback: {proxy_result.error}",
        )


def fetch_text_proxy(row: sqlite3.Row, cache_dir: Path, source_url: str) -> Resolution:
    """Read an official source through Jina's text renderer when direct HTML is unusable."""
    proxy_url = f"https://r.jina.ai/{source_url}"
    try:
        payload, metadata = cached_fetch(proxy_url, cache_dir / "text-proxy")
        value = payload.decode("utf-8", errors="replace")
        if re.search(r"^Warning:\s+Target URL returned error", value, re.M):
            return Resolution(
                row["id"],
                "authoritative_text_proxy",
                source_url=source_url,
                retrieval_url=metadata["final_url"],
                error="source returned an error through the text renderer",
            )
        title_match = re.search(r"^Title:\s*(.+)$", value, re.M)
        body_match = re.search(r"^Markdown Content:\s*$", value, re.M)
        title = clean_text(title_match.group(1)) if title_match else ""
        text = clean_text(value[body_match.end() :] if body_match else value)
        text_issue = source_text_issue(text, title)
        if text_issue:
            return Resolution(
                row["id"],
                "authoritative_text_proxy",
                source_url=source_url,
                retrieval_url=metadata["final_url"],
                source_title=title,
                error=text_issue,
            )
        return Resolution(
            row["id"],
            "authoritative_text_proxy",
            text=text[:MAX_TEXT_CHARS],
            source_url=source_url,
            retrieval_url=metadata["final_url"],
            source_title=title,
            content_type="text/markdown",
            retrieved_at=metadata["retrieved_at"],
            content_hash=sha256_bytes(text.encode("utf-8")),
            status_code=metadata["status_code"],
        )
    except Exception as exc:
        return Resolution(
            row["id"],
            "authoritative_text_proxy",
            source_url=source_url,
            retrieval_url=proxy_url,
            error=str(exc),
        )


def _bank_search_query(row: sqlite3.Row) -> str:
    code = historical_document_code(row["url"] or "")
    if code:
        return code
    path = urlsplit(row["url"] or "").path
    name = Path(path).stem
    if name.casefold() in {"default", "index_en", "applying"}:
        parts = [part for part in path.split("/") if part]
        name = " ".join(parts[-3:])
    return clean_text(re.sub(r"[-_/]+", " ", name))


def _field(document: dict[str, Any], name: str) -> str:
    field = (document.get("Fields") or {}).get(name) or {}
    return clean_text(str(field.get("Value") or ""))


def fetch_bank_search(row: sqlite3.Row, cache_dir: Path) -> Resolution:
    query = _bank_search_query(row)
    if len(query) < 3:
        return Resolution(row["id"], "bank_search_index", error="no usable Bank search query")
    request = {"query": query, "page": 1, "perPage": 20}
    key = cache_key("BANK_SEARCH", json.dumps(request, sort_keys=True))
    search_cache = cache_dir / "bank-search"
    body_path = search_cache / f"{key}.bin"
    meta_path = search_cache / f"{key}.json"
    try:
        if body_path.exists() and meta_path.exists():
            payload = body_path.read_bytes()
            fetch_meta = json.loads(meta_path.read_text())
        else:
            response = requests.post(
                BANK_SEARCH_ENDPOINT,
                json=request,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/json",
                    "Authorization": BANK_SEARCH_AUTHORIZATION,
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.content
            fetch_meta = {
                "request_url": BANK_SEARCH_ENDPOINT,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", "application/json"),
                "retrieved_at": utc_now(),
                "content_hash": sha256_bytes(payload),
            }
            search_cache.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(payload)
            meta_path.write_text(json.dumps(fetch_meta, indent=2, ensure_ascii=False))
        result = json.loads(payload)
    except Exception as exc:
        return Resolution(row["id"], "bank_search_index", error=str(exc))

    code = historical_document_code(row["url"] or "")
    query_tokens = set(normalized_title(query).split())
    ranked: list[tuple[float, dict[str, Any]]] = []
    for document in result.get("TypedDocuments", []):
        title = _field(document, "Title")
        description = _field(document, "Description") or _field(document, "i_text")
        source_url = _field(document, "Url")
        if not source_url:
            continue
        title_norm = normalized_title(title)
        description_norm = normalized_title(description[:500])
        if code:
            code_norm = normalized_title(code)
            kind = code[:2]
            number = code[2:]
            long_kind = {
                "CP": "Consultation Paper",
                "PS": "Policy Statement",
                "SS": "Supervisory Statement",
            }.get(kind, kind)
            long_code_norm = normalized_title(f"{long_kind} {number}")
            url_code_norm = normalized_title(Path(urlsplit(source_url).path).name)
            compact_code = normalized_title(code.replace("/", ""))
            if title_norm.startswith(code_norm) or title_norm.startswith(long_code_norm):
                score = 10.0
            elif description_norm.startswith(code_norm) or description_norm.startswith(long_code_norm):
                score = 9.0
            elif code_norm in title_norm or long_code_norm in title_norm:
                score = 8.0
            elif code_norm in description_norm or long_code_norm in description_norm:
                score = 6.0
            elif compact_code and compact_code in url_code_norm:
                score = 7.0
            else:
                continue
        else:
            result_tokens = set(
                normalized_title(
                    f"{title} {urlsplit(source_url).path.replace('/', ' ')}"
                ).split()
            )
            score = len(query_tokens & result_tokens) / max(1, len(query_tokens))
            if score < 0.6:
                continue
        ranked.append((score, document))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return Resolution(row["id"], "bank_search_index", error=f"no authoritative result for {query!r}")
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0] and ranked[0][0] < 9:
        return Resolution(row["id"], "bank_search_index", error=f"ambiguous authoritative results for {query!r}")
    document = ranked[0][1]
    title = _field(document, "Title")
    description = _field(document, "Description") or _field(document, "i_text")
    source_url = _field(document, "Url")
    try:
        payload, metadata = cached_fetch(source_url, cache_dir / "external")
        body, extracted_title = extract_source_cached(
            payload,
            metadata["content_type"],
            metadata["final_url"],
            cache_dir,
        )
        if body:
            description = body
        title = title or extracted_title
        source_url = metadata["final_url"]
    except Exception as exc:
        if len(description) < 80:
            return Resolution(
                row["id"],
                "bank_search_index",
                source_url=source_url,
                source_title=title,
                error=f"matched result had no body and could not be fetched: {exc}",
            )
    text_issue = source_text_issue(description, title)
    if text_issue:
        return Resolution(
            row["id"],
            "bank_search_index",
            source_url=source_url,
            source_title=title,
            error=f"matched Bank result is not substantive: {text_issue}",
        )
    return Resolution(
        row["id"],
        "bank_search_index",
        text=description[:MAX_TEXT_CHARS],
        source_url=source_url,
        source_title=title,
        content_type="application/json",
        retrieved_at=fetch_meta.get("retrieved_at", ""),
        content_hash=sha256_bytes(description.encode("utf-8")),
        status_code=fetch_meta.get("status_code"),
    )


def cached_external_resolution(row: sqlite3.Row, cache_dir: Path) -> Resolution | None:
    url = row["url"] or ""
    if not url:
        return None
    key = cache_key("GET", url)
    external_cache = cache_dir / "external"
    if not (external_cache / f"{key}.bin").exists() or not (external_cache / f"{key}.json").exists():
        return None
    return fetch_external(row, cache_dir)


def build_indexes(
    conn: sqlite3.Connection,
) -> tuple[
    dict[str, sqlite3.Row],
    dict[str, list[str]],
    dict[str, list[sqlite3.Row]],
    dict[str, list[sqlite3.Row]],
    dict[str, list[sqlite3.Row]],
    dict[str, sqlite3.Row],
    dict[str, sqlite3.Row],
    list[sqlite3.Row],
]:
    nodes = {row["id"]: row for row in conn.execute("SELECT * FROM node")}
    children: dict[str, list[str]] = defaultdict(list)
    for parent_id, child_id in conn.execute(
        "SELECT from_node_id,to_node_id FROM edge WHERE edge_type='contains'"
    ):
        children[parent_id].append(child_id)
    by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
    by_title: dict[str, list[sqlite3.Row]] = defaultdict(list)
    by_url: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for node in nodes.values():
        if not clean_text(node["text"] or ""):
            continue
        metadata = parse_metadata(node)
        glossary_hash = metadata.get("glossary_hash")
        if glossary_hash:
            by_hash[str(glossary_hash)].append(node)
        by_title[normalized_title(node["title"])].append(node)
        if node["url"]:
            by_url[normalized_url(node["url"])].append(node)
    source_by_url: dict[str, sqlite3.Row] = {}
    downloaded_sources = []
    for source in conn.execute("SELECT * FROM source_document ORDER BY downloaded_at DESC"):
        local_path = Path(source["local_path"] or "")
        if local_path and not local_path.is_absolute():
            local_path = ROOT / local_path
        if local_path.exists():
            downloaded_sources.append(source)
    for source in downloaded_sources:
        key = normalized_url(source["url"] or "")
        if key and key not in source_by_url:
            source_by_url[key] = source
    embedded_by_url: dict[str, sqlite3.Row] = {}
    for source in conn.execute("SELECT * FROM document_source ORDER BY fetched_at DESC"):
        key = normalized_url(source["url"] or "", keep_fragment=False)
        if key and key not in embedded_by_url:
            embedded_by_url[key] = source
    return (
        nodes,
        children,
        by_hash,
        by_title,
        by_url,
        source_by_url,
        embedded_by_url,
        downloaded_sources,
    )


def resolve_targets(
    conn: sqlite3.Connection,
    targets: list[sqlite3.Row],
    *,
    cache_dir: Path,
    fetch: bool,
    fetch_glossary_api: bool = True,
    fetch_external_sources: bool = True,
    workers: int,
    source_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[Resolution]:
    (
        nodes,
        children,
        by_hash,
        by_title,
        by_url,
        source_by_url,
        embedded_by_url,
        downloaded_sources,
    ) = build_indexes(conn)
    resolutions: dict[str, Resolution] = {}
    source_overrides = load_source_overrides() if source_overrides is None else source_overrides
    alternate_urls: dict[str, dict[str, str]] = {
        row["id"]: {
            url: "reconstructed_source_url"
            for url in reconstructed_context_urls(conn, row)
        }
        for row in targets
    }
    for row in targets:
        override = source_overrides.get(row["id"], {})
        expected_url = override.get("original_url")
        replacement_url = override.get("source_url")
        if (
            replacement_url
            and (
                not expected_url
                or normalized_url(expected_url) == normalized_url(row["url"] or "")
            )
        ):
            alternate_urls[row["id"]][str(replacement_url)] = "curated_authoritative_url"
    pending: list[tuple[sqlite3.Row, str]] = []
    for row in targets:
        resolution: Resolution | None = None
        if row["node_type"] in STRUCTURAL_TYPES:
            resolution = aggregate_descendant_text(row, children, nodes)
            if not resolution.resolved:
                resolution = None
        if resolution is None and row["node_type"] == "defined_term":
            resolution = glossary_resolution_from_existing(row, by_hash, by_title)
            if resolution is None:
                resolution = cached_glossary_resolution(row, cache_dir)
            if resolution is None:
                if fetch and fetch_glossary_api:
                    pending.append((row, "glossary"))
                else:
                    resolutions[row["id"]] = Resolution(
                        row["id"],
                        "not_attempted",
                        source_url=row["url"] or "",
                        error="network retrieval disabled",
                    )
                continue
        if resolution is None and row["url"]:
            embedded = embedded_by_url.get(normalized_url(row["url"], keep_fragment=False))
            if embedded is not None:
                resolution = embedded_document_resolution(row, embedded)
        if resolution is None and row["node_type"] in {"external_reference", "rule_reference"}:
            resolution = local_legal_alias_resolution(conn, row, nodes, by_title)
        if resolution is None and row["id"] in source_overrides:
            resolution = curated_local_resolution(
                row,
                source_overrides[row["id"]],
                nodes,
                children,
            )
        if resolution is None:
            resolution = exact_url_resolution(row, by_url)
        if resolution is None and row["url"]:
            source = source_by_url.get(normalized_url(row["url"]))
            if source is not None:
                resolution = source_document_resolution(row, source, ROOT)
                if not resolution.resolved and fetch:
                    resolution = None
        if resolution is None and row["url"]:
            fuzzy_source = fuzzy_downloaded_source_resolution(row, downloaded_sources)
            if fuzzy_source is not None:
                resolution = source_document_resolution(row, fuzzy_source, ROOT)
                if resolution.resolved:
                    resolution.method = "fuzzy_downloaded_source_url"
                elif fetch:
                    resolution = None
        if resolution is None:
            resolution = cached_external_resolution(row, cache_dir)
            if resolution is None:
                for alternate_url, alternate_method in alternate_urls.get(row["id"], {}).items():
                    alternate_row = dict(row)
                    alternate_row["url"] = alternate_url
                    alternate_resolution = cached_external_resolution(alternate_row, cache_dir)
                    if alternate_resolution is not None and alternate_resolution.resolved:
                        alternate_resolution.node_id = row["id"]
                        alternate_resolution.method = alternate_method
                        resolution = alternate_resolution
                        break
            if resolution is not None and not resolution.resolved:
                resolution = None
        if resolution is not None:
            resolutions[row["id"]] = resolution
        elif fetch and fetch_external_sources:
            pending.append((row, "external"))
        else:
            resolutions[row["id"]] = Resolution(
                row["id"],
                "not_attempted",
                source_url=row["url"] or "",
                error="network retrieval disabled",
            )

    glossary_pending = [item for item in pending if item[1] == "glossary"]
    external_pending = [item for item in pending if item[1] == "external"]

    def run_pending(items: list[tuple[sqlite3.Row, str]], max_workers: int) -> None:
        if not items:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    fetch_glossary if kind == "glossary" else fetch_external,
                    row,
                    cache_dir,
                    *(() if kind == "glossary" else (alternate_urls.get(row["id"], {}),)),
                ): row
                for row, kind in items
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    resolutions[row["id"]] = future.result()
                except Exception as exc:
                    resolutions[row["id"]] = Resolution(
                        row["id"], "unexpected_error", source_url=row["url"] or "", error=str(exc)
                    )

    # The PRA glossary endpoint is protected by a strict rate limiter. Serial
    # requests are slower but deterministic and avoid turning valid terms into
    # false 403 failures. General source URLs can still be fetched concurrently.
    run_pending(glossary_pending, 1)
    run_pending(external_pending, max(1, workers))
    for row in targets:
        resolution = resolutions[row["id"]]
        override = source_overrides.get(row["id"], {})
        expected_url = override.get("original_url")
        if (
            resolution.resolved
            and override
            and (
                not expected_url
                or normalized_url(str(expected_url)) == normalized_url(row["url"] or "")
            )
        ):
            configured_title = clean_text(str(override.get("source_title") or ""))
            if configured_title:
                resolution.source_title = configured_title
            resolution.rationale = clean_text(str(override.get("rationale") or ""))
    return [resolutions[row["id"]] for row in targets]


def apply_resolutions(
    conn: sqlite3.Connection,
    targets: Iterable[sqlite3.Row],
    resolutions: Iterable[Resolution],
) -> int:
    target_by_id = {row["id"]: row for row in targets}
    updated = 0
    for resolution in resolutions:
        if not resolution.resolved:
            continue
        row = target_by_id[resolution.node_id]
        metadata = parse_metadata(row)
        provenance = {
            "method": resolution.method,
            "source_url": resolution.source_url,
            "source_title": resolution.source_title,
            "source_node_ids": list(resolution.source_node_ids),
            "source_document_id": resolution.source_document_id,
            "retrieval_url": resolution.retrieval_url,
            "rationale": resolution.rationale,
            "content_type": resolution.content_type,
            "retrieved_at": resolution.retrieved_at,
            "content_hash": resolution.content_hash,
            "applied_at": utc_now(),
        }
        metadata["reader_reference_text"] = {key: value for key, value in provenance.items() if value}
        title = row["title"]
        if (
            resolution.source_title
            and normalized_title(title) in GENERIC_TITLES
            and normalized_title(resolution.source_title) not in GENERIC_TITLES
        ):
            metadata["reader_reference_text"]["original_title"] = title
            title = resolution.source_title
        conn.execute(
            "UPDATE node SET title=?,text=?,metadata_json=? WHERE id=? AND trim(coalesce(text,''))=''",
            (
                title,
                clean_text(resolution.text),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                resolution.node_id,
            ),
        )
        updated += conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    return updated


def write_audit(
    path: Path,
    targets: list[sqlite3.Row],
    resolutions: list[Resolution],
    *,
    applied: bool,
    updated: int,
    remaining: int,
) -> dict[str, Any]:
    resolution_by_id = {resolution.node_id: resolution for resolution in resolutions}
    rows = [resolution_by_id[row["id"]].audit_row(row) for row in targets]
    summary = {
        "generated_at": utc_now(),
        "applied": applied,
        "targets": len(targets),
        "resolved": sum(row["resolved"] for row in rows),
        "unresolved": sum(not row["resolved"] for row in rows),
        "updated": updated,
        "remaining_reader_targets_without_text": remaining,
        "by_method": dict(sorted(Counter(row["method"] for row in rows).items())),
        "unresolved_by_type": dict(
            sorted(Counter(row["node_type"] for row in rows if not row["resolved"]).items())
        ),
    }
    result = {"summary": summary, "targets": rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--source-overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--fetch", action="store_true", help="retrieve authoritative URLs not available locally")
    parser.add_argument(
        "--skip-glossary-api",
        action="store_true",
        help="fetch general sources but do not call the rate-limited PRA glossary endpoint",
    )
    parser.add_argument(
        "--skip-external-fetch",
        action="store_true",
        help="call the glossary API but do not retrieve general external sources",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    conn = connect(args.db)
    targets = reader_targets(conn)
    resolutions = resolve_targets(
        conn,
        targets,
        cache_dir=args.cache_dir,
        fetch=args.fetch,
        fetch_glossary_api=not args.skip_glossary_api,
        fetch_external_sources=not args.skip_external_fetch,
        workers=args.workers,
        source_overrides=load_source_overrides(args.source_overrides),
    )
    updated = apply_resolutions(conn, targets, resolutions) if args.apply else 0
    remaining = len(reader_targets(conn)) if args.apply else sum(not item.resolved for item in resolutions)
    audit = write_audit(
        args.audit_output,
        targets,
        resolutions,
        applied=args.apply,
        updated=updated,
        remaining=remaining,
    )
    print(json.dumps(audit["summary"], indent=2, ensure_ascii=False))
    return 0 if audit["summary"]["unresolved"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
