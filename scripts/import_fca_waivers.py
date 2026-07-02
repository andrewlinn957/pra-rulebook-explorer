#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "backend" / "data" / "rulebook.sqlite3"
XLSX_PATH = PROJECT_ROOT / "backend" / "data" / "raw" / "fca-waivers" / "consolidatedlistwaivers.xlsx"
SOURCE_URL = "https://www.fca.org.uk/publication/waivers-permissions/consolidatedlistwaivers.xlsx"
LOG_PATH = PROJECT_ROOT / "logs" / "fca-waivers-import-summary.json"

NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Keep this explicit. These are FCA list labels that can be mapped onto current PRA Rulebook Parts.
PART_ALIASES = {
    "Audit Committee": "Audit Committee",
    "Conds Govern Bus-SII": "Conditions Governing Business",
    "Credit Risk - CRR": "Credit Risk",
    "CRR Credit Risk": "Credit Risk",
    "CRR Def Cap": "Definition of Capital",
    "Def Cap - CRR": "Definition of Capital",
    "DepositProtection": "Depositor Protection",
    "External Audit": "External Audit",
    "Group Sup - SII": "Group Supervision",
    "Housing (CRR Firms)": "Housing",
    "Insurance Company - Reporting": "Insurance Company - Reporting",
    "Insurance Company – Exposure Limits": "Insurance Company – Exposure Limits",
    "INS Gen App - SII": "Insurance General Application",
    "Leverage Ratio (CRR)": "Leverage Ratio (CRR)",
    "Leverage Ratio – Capital Requirements and Buffers": "Leverage Ratio – Capital Requirements and Buffers",
    "Liquidity (CRR)": "Liquidity (CRR)",
    "Liquidity Coverage Ratio (CRR)": "Liquidity Coverage Ratio (CRR)",
    "Lloyds - SII": "Lloyd’s",
    "Matching Adjustment": "Matching Adjustment",
    "MCR - SII": "Minimum Capital Requirement",
    "Own Funds - SII": "Own Funds",
    "Own Funds and Eligible Liabilities (CRR)": "Own Funds and Eligible Liabilities (CRR)",
    "Perm & Wav - CRR": "Capital Buffers",
    "Permissions - CRR": "Capital Buffers",
    "Perm & Wav - Non CRR": "Permissions and Waivers",
    "Public Disc - CRR": "Disclosure (CRR)",
    "Regulatory Reporting": "Regulatory Reporting",
    "Remuneration": "Remuneration",
    "Reporting - SII": "Reporting",
    "Reporting (CRR)": "Reporting (CRR)",
    "Reporting Pillar 2": "Reporting Pillar 2",
    "Resolution Assessment": "Resolution Assessment",
    "Ring-fenced Bodies": "Ring-fenced Bodies",
    "Run off Ops - SII": "Run-off Operations",
    "SCR - IM - SII": "Solvency Capital Requirement - Internal Models",
    "SCR - Standard - SII": "Solvency Capital Requirement - Standard Formula",
    "SCR Provisions - SII": "Solvency Capital Requirement - General Provisions",
    "SDDT Regime - General Application": "SDDT Regime – General Application",
    "Tech Provisions- SII": "Technical Provisions",
    "Trans measures - SII": "Transitional Measure on Technical Provisions",
    "3rd Country Br - SII": "Third Country Branches",
}

# Generic CRR/Solvency II labels are too broad to attach to a Part. We only map them if a sub-rule
# identifies a unique Article in the current Rulebook graph.
ARTICLE_ONLY_REFS = {"CRR", "Solvency II"}

EXCLUDED_REFS = {
    "", "BIPRU", "CASS", "CIS", "COB", "COBS", "COBS Annex", "GENPRU", "IPRU(FSOC)",
    "IPRU(INS)", "IPRU(INV)", "MIFIDPRU", "PRU", "SUP", "SYSC", "TC",
}


def sha1_16(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def col_index(ref: str) -> int:
    n = 0
    for ch in "".join(c for c in ref if c.isalpha()):
        n = n * 26 + ord(ch.upper()) - 64
    return n


def download_xlsx(path: Path = XLSX_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
        path.write_bytes(response.read())


def read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", NS):
                shared.append("".join(t.text or "" for t in si.findall(".//a:t", NS)))
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        header: dict[int, str] = {}
        records: list[dict[str, str]] = []
        for row in root.findall(".//a:row", NS):
            row_no = int(row.attrib["r"])
            values: dict[int, str] = {}
            for cell in row.findall("a:c", NS):
                v = cell.find("a:v", NS)
                value = "" if v is None else (v.text or "")
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                values[col_index(cell.attrib["r"])] = value.strip() if isinstance(value, str) else value
            if row_no == 7:
                header = {idx: val for idx, val in values.items() if val}
            elif row_no > 7 and header:
                rec = {name: str(values.get(idx, "")).strip() for idx, name in header.items()}
                if any(rec.values()):
                    records.append(rec)
        return records


def load_graph_indexes(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    parts = {r["title"]: dict(r) for r in conn.execute("SELECT id,title,url FROM node WHERE node_type='part'")}
    nodes = {r["id"]: dict(r) for r in conn.execute("SELECT id,node_type,title,url FROM node")}
    children: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT from_node_id,to_node_id FROM edge WHERE edge_type='contains'"):
        children[r["from_node_id"]].append(r["to_node_id"])
    descendants: dict[str, set[str]] = {}
    for title, part in parts.items():
        seen: set[str] = set()
        q = deque(children.get(part["id"], []))
        while q:
            nid = q.popleft()
            if nid in seen:
                continue
            seen.add(nid)
            q.extend(children.get(nid, []))
        descendants[part["id"]] = seen
    title_index: dict[str, list[str]] = defaultdict(list)
    indexable_types = {"part", "chapter", "rule", "guidance_document", "guidance_section", "guidance_paragraph"}
    for nid, n in nodes.items():
        if n["node_type"] in indexable_types:
            title_index[normalise_title(n["title"])].append(nid)
    return {"parts": parts, "nodes": nodes, "descendants": descendants, "title_index": title_index}


def normalise_title(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("–", "-").strip()).lower()


def clean_ref(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("Rule Ref", "").strip())


def article_titles(sub_rule: str) -> list[str]:
    s = sub_rule or ""
    refs: list[str] = []
    for m in re.finditer(r"\b(?:Article|Ar)\s*([0-9]+[A-Za-z]?)(?:\s*\(([^)]*)\))?", s, re.I):
        art = m.group(1)
        para = (m.group(2) or "").replace(" ", "")
        if para and re.fullmatch(r"[A-Za-z]+", para):
            refs.append(f"Article {art}{para.lower()}")
        elif para and re.fullmatch(r"[0-9A-Za-z]+", para):
            refs.append(f"Article {art}({para})")
        else:
            refs.append(f"Article {art}")
    # Bare CRR style: 26 (3)
    if not refs:
        for m in re.finditer(r"\b([0-9]{1,3}[A-Za-z]?)\s*\(\s*([0-9A-Za-z]+)\s*\)", s):
            refs.append(f"Article {m.group(1)}({m.group(2)})")
    return unique(refs)


def rule_titles(sub_rule: str) -> list[str]:
    s = sub_rule or ""
    if re.search(r"\b(?:Article|Ar)\b", s, re.I):
        return []
    # Broad chapter/rule ranges like "Ru 1-7" are intentionally kept at Part level.
    if re.search(r"\b(?:Ru|Rule|Ch)\.?\s*[0-9]+[A-Za-z]?(?:\.[0-9]+[A-Za-z]?)*\s*-\s*[0-9]+", s, re.I):
        return []
    s = re.sub(r"\b(?:Ru|Rule)\.?\s*", " ", s, flags=re.I)
    s = re.sub(r"\bCh\.?\s*", " ", s, flags=re.I)
    refs: list[str] = []
    for m in re.finditer(r"\b([0-9]+[A-Za-z]?(?:\.[0-9]+[A-Za-z]?)*(?:\([0-9A-Za-z]+\))?)\b", s):
        token = m.group(1).strip()
        if token and token.upper() not in {"N", "A"}:
            refs.append(token)
    return unique(refs)


def unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def find_title(indexes: dict[str, Any], title: str, within_part_id: str | None = None) -> list[str]:
    candidates = indexes["title_index"].get(normalise_title(title), [])
    if not candidates and re.fullmatch(r"Article [0-9]+[A-Za-z]?", title):
        prefix = normalise_title(title) + " "
        candidates = [
            nid for nid, node in indexes["nodes"].items()
            if node["node_type"] in {"chapter", "rule"} and normalise_title(node["title"]).startswith(prefix)
        ]
    if within_part_id:
        allowed = indexes["descendants"].get(within_part_id, set()) | {within_part_id}
        candidates = [nid for nid in candidates if nid in allowed]
    return candidates


def resolve_targets(rec: dict[str, str], indexes: dict[str, Any]) -> tuple[list[str], str]:
    ref = clean_ref(rec.get("Rule Handbook: Rule Ref", ""))
    sub = rec.get("Sub Rule Number", "")
    parts = indexes["parts"]
    # Exclude clearly FCA Handbook rows unless explicitly aliased above.
    if ref in EXCLUDED_REFS:
        return [], "excluded_handbook"

    part_title = PART_ALIASES.get(ref, ref if ref in parts else None)
    targets: list[str] = []

    if ref in ARTICLE_ONLY_REFS:
        for title in article_titles(sub):
            ids = find_title(indexes, title)
            if len(ids) == 1:
                targets.extend(ids)
        return unique(targets), "article_only" if targets else "unresolved_article_only"

    if not part_title or part_title not in parts:
        return [], "unmapped_rule_ref"

    part_id = parts[part_title]["id"]
    # Prefer exact article nodes. CRR permission labels can be broad/misaligned, so a unique
    # current Article node is safer than falling back to the listed high-level category.
    for title in article_titles(sub):
        ids = find_title(indexes, title, within_part_id=part_id)
        if not ids and "CRR" in ref:
            global_ids = find_title(indexes, title)
            if len(global_ids) == 1:
                ids = global_ids
        targets.extend(ids)
    for title in rule_titles(sub):
        ids = find_title(indexes, title, within_part_id=part_id)
        targets.extend(ids)
    targets = unique(targets)
    if targets:
        return targets, "mapped_to_rule"
    # N/A, broad ranges, or references that do not match child rule titles attach to Part.
    return [part_id], "mapped_to_part"


def import_permissions(conn: sqlite3.Connection, records: list[dict[str, str]], *, dry_run: bool = False) -> dict[str, Any]:
    indexes = load_graph_indexes(conn)
    live = [r for r in records if not r.get("End Date", "").strip() and r.get("Waiver Status", "Completed Approved") in {"", "Completed Approved"}]
    summary: dict[str, Any] = {"xlsx_rows": len(records), "live_rows": len(live)}
    reason_counts: Counter[str] = Counter()
    ref_counts: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    nodes: dict[str, tuple] = {}
    edges: dict[str, tuple] = {}
    unresolved_examples: dict[str, list[dict[str, str]]] = defaultdict(list)

    for rec in live:
        ref = clean_ref(rec.get("Rule Handbook: Rule Ref", ""))
        targets, reason = resolve_targets(rec, indexes)
        reason_counts[reason] += 1
        ref_counts[ref] += 1
        if not targets:
            if len(unresolved_examples[reason]) < 20:
                unresolved_examples[reason].append(rec)
            continue
        frn = rec.get("FRN", "")
        org = rec.get("Organisation Name", "")
        waiver_ref = rec.get("Waiver Ref", "")
        sub_rule = rec.get("Sub Rule Number", "")
        stable_key = f"fca_permission:{frn}:{waiver_ref}:{ref}:{sub_rule}:{rec.get('Start Date','')}"
        node_id = sha1_16(stable_key)
        meta = {
            "frn": frn,
            "organisation_name": org,
            "waiver_ref": waiver_ref,
            "rule_handbook_ref": ref,
            "sub_rule_number": sub_rule,
            "waiver_status": rec.get("Waiver Status", ""),
            "start_date": rec.get("Start Date", ""),
            "end_date": rec.get("End Date", ""),
            "source": "FCA consolidated waivers list",
            "source_url": SOURCE_URL,
            "evidence_status": "document_metadata",
            "extraction_run_id": "fca_waivers_import",
        }
        title = org or f"FRN {frn}"
        text = f"{title} has an active FCA/PRA waiver, modification or permission for {ref} {sub_rule}."
        nodes[node_id] = (node_id, "permission", stable_key, title, text, "", json.dumps(meta, ensure_ascii=False))
        for target_id in targets:
            target_counts[target_id] += 1
            edge_id = sha1_16(f"has_permission:{target_id}:{node_id}")
            evidence = f"{ref} {sub_rule}; {waiver_ref}".strip()
            edges[edge_id] = (edge_id, target_id, node_id, "has_permission", "fca_waivers_list", 1.0, evidence, SOURCE_URL, json.dumps(meta, ensure_ascii=False))

    summary.update({
        "reason_counts": dict(reason_counts),
        "live_rows_by_rule_ref_top": ref_counts.most_common(80),
        "permission_nodes": len(nodes),
        "has_permission_edges": len(edges),
        "target_nodes_with_permissions": len(target_counts),
        "unresolved_examples": unresolved_examples,
    })
    if not dry_run:
        conn.execute("DELETE FROM edge WHERE source_method='fca_waivers_list' OR edge_type='has_permission'")
        conn.execute("DELETE FROM node WHERE node_type='permission' AND stable_key LIKE 'fca_permission:%'")
        conn.executemany(
            """
            INSERT INTO node (id,node_type,stable_key,title,text,url,metadata_json)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(stable_key) DO UPDATE SET title=excluded.title,text=excluded.text,url=excluded.url,metadata_json=excluded.metadata_json
            """,
            nodes.values(),
        )
        conn.executemany(
            """
            INSERT INTO edge (id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET from_node_id=excluded.from_node_id,to_node_id=excluded.to_node_id,edge_type=excluded.edge_type,source_method=excluded.source_method,confidence=excluded.confidence,evidence_text=excluded.evidence_text,source_url=excluded.source_url,metadata_json=excluded.metadata_json
            """,
            edges.values(),
        )
        conn.commit()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--xlsx", type=Path, default=XLSX_PATH)
    args = ap.parse_args()

    if args.download or not args.xlsx.exists():
        download_xlsx(args.xlsx)
    records = read_xlsx_rows(args.xlsx)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    summary = import_permissions(conn, records, dry_run=args.dry_run)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"wrote {LOG_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
