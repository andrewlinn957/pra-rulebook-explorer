#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT = ROOT / "logs/graph-completeness-audit.json"

RULE_NUMBER_RE = re.compile(r"^\d+[A-Z]?(?:\.\d+[A-Z]?)*$|^\d+[A-Z]?$")
GUIDANCE_PARA_RE = re.compile(r"^\d+(?:\.\d+)*[A-Z]?$")
GUIDANCE_WRAPPER_RE = re.compile(
    r"has not (?:yet )?been added to the PRA Rulebook|"
    r"not been added to the PRA Rulebook|"
    r"available on the Bank of England website|"
    r"revised version has not yet been added to the Rulebook|"
    r"\b\[Deleted\]\b|\(Deleted\)",
    re.IGNORECASE,
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def path_key(url: str) -> str:
    return urlparse(url).path.strip("/")


def is_guidance_wrapper_page(soup: BeautifulSoup, content, visible_text: str) -> bool:
    """Return True for PRA guidance shells with no inline Rulebook body.

    Some current guidance detail URLs are intentionally just wrappers: deleted
    statements, "not yet added to Rulebook" notices, or links to Bank of England
    PDFs/publication pages. They have substantial chrome/related-link text but
    no `.row-block` provisions to parse, so they should not be reported as
    parser coverage failures.
    """
    if content.select(".row-block"):
        return False
    title_el = soup.select_one("h1") or soup.select_one("title")
    title_text = clean_text(title_el.get_text(" ")) if title_el else ""
    link_hrefs = " ".join(a.get("href", "") for a in content.find_all("a"))
    has_external_publication_link = "bankofengland.co.uk/prudential-regulation/publication" in link_hrefs
    has_export_only_body = "Content loading" in visible_text and "Export guidance as PDF" in visible_text
    return bool(
        GUIDANCE_WRAPPER_RE.search(visible_text)
        or GUIDANCE_WRAPPER_RE.search(title_text)
        or (has_external_publication_link and has_export_only_body)
    )


def load_nodes(conn: sqlite3.Connection):
    by_html = defaultdict(list)
    by_url_prefix = defaultdict(list)
    all_ids = set()
    for r in conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node"):
        all_ids.add(r[0])
        meta = json.loads(r[6] or "{}")
        html_id = meta.get("html_id")
        if html_id:
            by_html[html_id].append(dict(id=r[0], node_type=r[1], stable_key=r[2], title=r[3], text=r[4] or "", url=r[5] or "", meta=meta))
        if r[5]:
            by_url_prefix[r[5].split("#", 1)[0]].append(dict(id=r[0], node_type=r[1], stable_key=r[2], title=r[3], text=r[4] or "", url=r[5] or "", meta=meta))
    return by_html, by_url_prefix, all_ids


def audit_parts(conn, by_html):
    issues = []
    counts = Counter()
    for url, html in conn.execute("SELECT url,raw_html FROM document_source WHERE source_type='part' ORDER BY url"):
        soup = BeautifulSoup(html, "lxml")
        content = soup.select_one(".rulebook-content") or soup
        current_chapter = None
        for el in content.find_all("div", recursive=True):
            classes = set(el.get("class", []))
            if "chapter-section" in classes:
                current_chapter = el
                counts["chapter_sections"] += 1
                html_id = el.get("id", "")
                if html_id and not by_html.get(html_id):
                    issues.append({"kind": "part_chapter_missing_html_node", "url": url, "html_id": html_id, "text": clean_text(el.get_text(" "))[:300]})
                continue
            if "row-block" not in classes:
                continue
            counts["part_row_blocks"] += 1
            html_id = el.get("id", "")
            number_el = el.select_one(".rule-number:not(.chapter-number)")
            number = clean_text(number_el.get_text(" ")).rstrip(".") if number_el else ""
            body_el = el.select_one(".div-row__col-2")
            body_text = clean_text(body_el.get_text(" ")) if body_el else clean_text(el.get_text(" "))
            heading_el = el.select_one("h2, h3, h4")
            heading_text = clean_text(heading_el.get_text(" ")) if heading_el else ""
            if number and RULE_NUMBER_RE.match(number):
                counts["part_numbered_rows"] += 1
                if html_id and not by_html.get(html_id):
                    issues.append({"kind": "part_numbered_row_missing_html_node", "url": url, "html_id": html_id, "number": number, "text": body_text[:300]})
            elif heading_text and html_id:
                counts["part_heading_rows"] += 1
                if not by_html.get(html_id):
                    issues.append({"kind": "part_heading_row_missing_html_node", "url": url, "html_id": html_id, "heading": heading_text[:300]})
            elif body_text and len(body_text) > 20:
                counts["part_unnumbered_substantive_rows"] += 1
                nodes = by_html.get(html_id, []) if html_id else []
                has_rule = any(n["node_type"] == "rule" and n["meta"].get("unnumbered_row") for n in nodes)
                if not html_id or not has_rule:
                    issues.append({"kind": "part_unnumbered_substantive_row_missing_rule", "url": url, "html_id": html_id, "number": number, "text": body_text[:500]})
    return counts, issues


def audit_guidance(conn, by_html, by_url_prefix):
    issues = []
    counts = Counter()
    for url, html in conn.execute("SELECT url,raw_html FROM document_source WHERE source_type='guidance_detail' ORDER BY url"):
        soup = BeautifulSoup(html, "lxml")
        content = soup.select_one(".rulebook-content") or soup.select_one(".page-content") or soup
        nodes_for_doc = by_url_prefix.get(url, [])
        paragraph_nodes = [n for n in nodes_for_doc if n["node_type"] == "guidance_paragraph"]
        section_nodes = [n for n in nodes_for_doc if n["node_type"] == "guidance_section"]
        if len(nodes_for_doc) <= 1:
            visible_text = clean_text(content.get_text(" "))
            row_blocks = content.select(".row-block")
            paras = [clean_text(p.get_text(" ")) for p in content.find_all(["p", "li"])]
            substantive = [p for p in paras if len(p) > 40 and not p.lower().startswith(("print", "pdf"))]
            if is_guidance_wrapper_page(soup, content, visible_text):
                counts["guidance_wrapper_or_external_docs"] += 1
            elif row_blocks or len(substantive) >= 3 or len(visible_text) > 1500:
                issues.append({"kind": "guidance_detail_low_parse_but_substantive_html", "url": url, "node_count": len(nodes_for_doc), "row_blocks": len(row_blocks), "substantive_blocks": len(substantive), "text_len": len(visible_text), "sample": visible_text[:500]})
            counts["guidance_low_parse_docs"] += 1
        for el in content.find_all("div", recursive=True):
            classes = set(el.get("class", []))
            if "row-block" not in classes:
                continue
            counts["guidance_row_blocks"] += 1
            html_id = el.get("id", "")
            number_el = el.select_one(".rule-number:not(.chapter-number)")
            para = clean_text(number_el.get_text(" ")).rstrip(".") if number_el else ""
            body_el = el.select_one(".div-row__col-2")
            text = clean_text(body_el.get_text(" ")) if body_el else clean_text(el.get_text(" "))
            if para and GUIDANCE_PARA_RE.match(para) and len(text) > 20:
                counts["guidance_numbered_rows"] += 1
                if html_id and not by_html.get(html_id):
                    issues.append({"kind": "guidance_numbered_row_missing_html_node", "url": url, "html_id": html_id, "paragraph": para, "text": text[:400]})
            elif len(text) > 60:
                counts["guidance_unnumbered_substantive_rows"] += 1
                if html_id and not by_html.get(html_id):
                    issues.append({"kind": "guidance_unnumbered_substantive_row_missing_node", "url": url, "html_id": html_id, "text": text[:400]})
    return counts, issues


def audit_edges(conn):
    issues = []
    counts = Counter()
    missing = conn.execute("SELECT COUNT(*) FROM edge e LEFT JOIN node n ON n.id=e.to_node_id WHERE n.id IS NULL").fetchone()[0]
    counts["missing_edge_targets"] = missing
    if missing:
        issues.append({"kind": "missing_edge_targets", "count": missing})
    collapsed = conn.execute("""
        SELECT COUNT(*) FROM edge e
        WHERE e.edge_type='references'
          AND e.metadata_json LIKE '%/pra-rules/%#%'
          AND json_extract(e.metadata_json,'$.target_key') NOT LIKE '%#%'
    """).fetchone()[0]
    counts["collapsed_hash_reference_edges"] = collapsed
    if collapsed:
        issues.append({"kind": "collapsed_hash_reference_edges", "count": collapsed})
    dup_res_unres = conn.execute("""
        SELECT COUNT(*) FROM edge u
        WHERE u.source_method='html_anchor_unresolved'
          AND EXISTS (
            SELECT 1 FROM edge r
            WHERE r.source_method='html_anchor_resolved'
              AND r.from_node_id=u.from_node_id
              AND json_extract(r.metadata_json,'$.href')=json_extract(u.metadata_json,'$.href')
          )
    """).fetchone()[0]
    counts["duplicate_resolved_unresolved_anchor_edges"] = dup_res_unres
    if dup_res_unres:
        issues.append({"kind": "duplicate_resolved_unresolved_anchor_edges", "count": dup_res_unres})
    return counts, issues


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    by_html, by_url_prefix, all_ids = load_nodes(conn)
    part_counts, part_issues = audit_parts(conn, by_html)
    guidance_counts, guidance_issues = audit_guidance(conn, by_html, by_url_prefix)
    edge_counts, edge_issues = audit_edges(conn)
    all_issues = part_issues + guidance_issues + edge_issues
    report = {
        "counts": dict(part_counts + guidance_counts + edge_counts),
        "issues_by_kind": dict(Counter(i["kind"] for i in all_issues)),
        "issue_count": len(all_issues),
        "issues": all_issues[:1000],
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["counts", "issues_by_kind", "issue_count"]}, indent=2, ensure_ascii=False))
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
