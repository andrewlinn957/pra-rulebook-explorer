#!/usr/bin/env python3
"""General PRA/BoE reporting package projection.

Extends the COR011 reporting knowledge-graph pattern to the remaining official
Bank/PRA reporting materials already held in source_document/source_span.

Design constraints:
- no new tables and no ontology redesign;
- preserve source_document/source_span and raw files;
- append/update graph_node/graph_edge rows using only allowed node/edge types;
- create per-data-item package JSON and QA reports mirroring the COR011 package view.

This is deliberately conservative: it creates evidenced package scaffolds,
template/instruction/taxonomy/validation links, and bounded parsed row/column/
datapoint structures from official template spreadsheets where the row/column
codes are explicit. Ambiguous legal or calculation links remain candidate review
items rather than being silently asserted.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages"

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

ALLOWED_NODE_TYPES = {
    "SourceDocument","SourceSpan","RulebookPart","Provision","NormativeStatement","ReportingObligation","DataItem",
    "TemplateSet","InstructionSet","Template","TemplateRow","TemplateColumn","DataPoint","Concept","Metric",
    "CalculationRule","Permission","ScopeRule","ValidationRule","DefinedTerm","FirmType","EffectivePeriod",
}
ALLOWED_EDGE_TYPES = {
    "CONTAINS","ESTABLISHED_BY","LEGAL_BASIS","USES_TEMPLATE","USES_INSTRUCTIONS","HAS_ROW","HAS_COLUMN",
    "HAS_DATAPOINT","REPORTS_CONCEPT","REPORTS_METRIC","REFERENCES_RULE","DEFINES","USES_DEFINED_TERM",
    "CALCULATES","USES_INPUT","FEEDS_CALCULATION","MAY_BE_AFFECTED_BY_PERMISSION","APPLIES_TO","SUBJECT_TO",
    "HAS_SCOPE_RULE","HAS_VALIDATION_RULE","EVIDENCED_BY","IN_FORCE_FROM","REVOKED_BY","AMENDED_BY",
}

CODE_RE = re.compile(r"\b(COR\s*\d{3}|PRA\s*\d{3}|FSA\s*\d{3}|RFB\s*\d{3}|REP\s*\d{3}a?|LVR\s*\d{3}|IDY\s*\d{3}|MLAR)\b", re.I)
C_TEMPLATE_RE = re.compile(r"\bC\s*([0-9]{2,3})[._ ]([0-9]{2})\b", re.I)
ROW_RE = re.compile(r"^(?:r)?\s*([0-9]{3,5}|[A-Z]{1,3}[0-9]{1,4})$", re.I)
COL_RE = re.compile(r"^(?:c)?\s*([0-9]{3,5}|[A-Z]{1,3})$", re.I)

DOMAIN_RULES = [
    ("liquidity", re.compile(r"liquidity|lcr|nsfr|stable funding|pra110|fsa047|fsa048|idy", re.I)),
    ("FINREP", re.compile(r"finrep|financial information|financial statements|asset encumbrance", re.I)),
    ("counterparty credit risk", re.compile(r"counterparty|\bccr\b", re.I)),
    ("capital / COREP own funds", re.compile(r"own[- ]funds|capital adequacy|capital\+|capitalplus|sddt|output floor|corep-own-funds|pra10[1-9]|pra11[7-9]", re.I)),
    ("credit risk", re.compile(r"credit[- ]risk|standardised approach|irb|specialised lending|immovable property", re.I)),
    ("market risk", re.compile(r"market[- ]risk|\bima\b", re.I)),
    ("leverage", re.compile(r"leverage|\blvr", re.I)),
    ("large exposures", re.compile(r"large[- ]exposures|concentration risk|corep-le", re.I)),
    ("resolution", re.compile(r"resolution|mrel", re.I)),
    ("remuneration", re.compile(r"remuneration", re.I)),
    ("operational resilience / other", re.compile(r"operational[- ]risk|operational[- ]resilience|branch return|close links|controllers|rep00|rfb", re.I)),
]

SUBMISSION_SYSTEMS = [
    ("RegData", re.compile(r"regdata", re.I)),
    ("BEEDS", re.compile(r"beeds|bank of england electronic data submission", re.I)),
    ("OSCA", re.compile(r"osca", re.I)),
]

@dataclass
class DataItemPackage:
    code: str
    title: str
    reporting_domain: str
    source_document_ids: list[str]
    template_document_ids: list[str]
    instruction_document_ids: list[str]
    taxonomy_document_ids: list[str]
    validation_document_ids: list[str]
    legal_basis_node_ids: list[str]
    package_path: str = ""


def stable(prefix: str, *parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return f"{prefix}:{hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:16]}"


def edge_id(src: str, typ: str, tgt: str, span: str | None, method: str) -> str:
    return stable("edge", src, typ, tgt, span or "", method)


def norm_code(text: str) -> str:
    return re.sub(r"\s+", "", text.upper())


def rel(path: str | None) -> Path | None:
    if not path:
        return None
    return ROOT / path


def domain_for(text: str, code: str | None = None) -> str:
    """Classify package domain conservatively from evidenced code/path hints.

    Prefer exact reporting-code/taxonomy-path evidence over broad package haystacks:
    source groups can legitimately contain mixed historical, taxonomy and
    consultation artefacts, and a single stray liquidity/FINREP phrase should
    not pull a clearly capital_plus/leverage/structural_reform package into the
    wrong domain.
    """
    c = (code or "").upper()
    if c.startswith("LVR") or c == "COREP-LEVERAGE":
        return "leverage"
    if c in {"PRA001", "PRA101", "PRA102", "PRA103", "PRA104", "PRA105", "PRA106", "PRA107", "PRA108", "PRA109", "PRA111", "PRA112", "PRA113", "PRA114", "PRA116", "PRA117", "PRA118", "PRA119", "COREP-OWN-FUNDS", "COREP-G-SII-BUFFER", "PILLAR3-DISCLOSURE"}:
        return "capital / COREP own funds"
    if c in {"COREP-CREDIT-RISK", "COREP-LOSSES-IMMOVABLE-PROPERTY"}:
        return "credit risk"
    if c == "COREP-CCR":
        return "counterparty credit risk"
    if c == "COREP-MARKET-RISK":
        return "market risk"
    if c == "COREP-LARGE-EXPOSURES":
        return "large exposures"
    if c == "FINREP":
        return "FINREP"
    if c.startswith("RFB"):
        return "operational resilience / other"
    if c in {"BOE-BANKING-TAXONOMY", "PRA115"}:
        return "other PRA/BoE reporting"
    for label, rx in DOMAIN_RULES:
        if rx.search(text):
            return label
    return "other PRA/BoE reporting"


def title_for_code(code: str, docs: list[sqlite3.Row]) -> str:
    explicit = [d["title"] for d in docs if d["title"] and re.search(re.escape(code), d["title"], re.I)]
    if explicit:
        return re.sub(r"\s+", " ", explicit[0]).strip()[:240]
    if docs:
        return re.sub(r"\s+", " ", docs[0]["title"] or code).strip()[:240]
    return code


class Builder:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.counts = Counter()
        self.review: list[dict[str, Any]] = []
        self.low_conf: list[dict[str, Any]] = []
        self.packages: dict[str, DataItemPackage] = {}
        self.source_span_cache: dict[str, str] = {}
        self.direct_legal_review = self.load_direct_legal_review()

    def load_direct_legal_review(self) -> dict[tuple[str, str, str], dict[str, str]]:
        """Load manual legal sanity decisions, if present.

        This makes review outcomes durable across rebuilds without changing the
        graph schema or ontology.  The CSV is produced by
        scripts/apply_direct_legal_sanity_review.py / direct review workflow.
        """
        path = OUT / "audit_exports/domain_reviews/direct_legal_sanity_review.csv"
        if not path.exists():
            return {}
        out: dict[tuple[str, str, str], dict[str, str]] = {}
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = r.get("data_item_code") or ""
                tgt = r.get("provision_node_id") or ""
                decision = r.get("direct_sanity_decision") or ""
                if not code or not tgt or not decision:
                    continue
                src = f"reporting_obligation:{code}"
                out[(src, "LEGAL_BASIS", tgt)] = {
                    "decision": decision,
                    "rationale": r.get("direct_sanity_rationale") or "",
                    "review_source": str(path.relative_to(ROOT)),
                }
        return out

    def doc_span(self, source_id: str) -> str | None:
        if source_id in self.source_span_cache:
            return self.source_span_cache[source_id]
        r = self.conn.execute(
            "SELECT span_id FROM source_span WHERE source_id=? ORDER BY CASE span_type WHEN 'html_document' THEN 0 WHEN 'xlsx_workbook' THEN 1 WHEN 'pdf_page' THEN 2 ELSE 9 END, length(COALESCE(normalised_text,'')) LIMIT 1",
            (source_id,),
        ).fetchone()
        if not r:
            d = self.conn.execute("SELECT title FROM source_document WHERE source_id=?", (source_id,)).fetchone()
            if not d:
                return None
            sid = stable("span", source_id, "document_anchor", d["title"] or source_id)
            self.conn.execute(
                "INSERT OR IGNORE INTO source_span(span_id,source_id,span_type,anchor,raw_text,normalised_text) VALUES (?,?,?,?,?,?)",
                (sid, source_id, "document_anchor", "document", d["title"] or source_id, d["title"] or source_id),
            )
            self.counts["source_spans_created"] += 1
            self.source_span_cache[source_id] = sid
            return sid
        self.source_span_cache[source_id] = r["span_id"]
        return r["span_id"]

    def node(self, node_id: str, node_type: str, label: str, source_table: str | None = None, source_pk: str | None = None, props: dict[str, Any] | None = None, status: str = "candidate") -> None:
        assert node_type in ALLOWED_NODE_TYPES, node_type
        self.conn.execute(
            """
            INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,review_status)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
              node_type=excluded.node_type,label=excluded.label,source_table=COALESCE(excluded.source_table,graph_node.source_table),
              source_pk=COALESCE(excluded.source_pk,graph_node.source_pk),properties_json=excluded.properties_json,
              review_status=CASE WHEN graph_node.review_status='accepted_candidate' THEN graph_node.review_status ELSE excluded.review_status END
            """,
            (node_id, node_type, label[:500], source_table, source_pk, json.dumps(props or {}, ensure_ascii=False), status),
        )
        self.counts[f"node_{node_type}"] += 1

    def edge(self, src: str, typ: str, tgt: str, span: str | None, confidence: float, method: str, status: str, explanation: str) -> None:
        assert typ in ALLOWED_EDGE_TYPES, typ
        if not span:
            status = "needs_review"
            confidence = min(confidence, 0.55)
        eid = edge_id(src, typ, tgt, span, method)
        props: dict[str, Any] = {"explanation": explanation[:500]}
        direct_review = self.direct_legal_review.get((src, typ, tgt))
        if direct_review:
            decision = direct_review["decision"]
            props["direct_legal_sanity_review"] = direct_review
            if decision == "accept_specific_reporting_basis":
                status = "accepted_candidate"
                confidence = max(confidence, 0.86)
                method = "manual_legal_sanity_review"
                explanation = f"Manual legal sanity review accepted this package/provision relationship. {direct_review['rationale']}"
                props["explanation"] = explanation[:500]
            elif decision == "reject_do_not_promote":
                status = "rejected_candidate"
                confidence = min(confidence, 0.20)
                method = "manual_legal_sanity_review"
                explanation = f"Manual legal sanity review rejected this package/provision relationship. {direct_review['rationale']}"
                props["explanation"] = explanation[:500]
            elif decision == "accept_specific_schedule_basis":
                # Keep as candidate: useful due-date/frequency/applicability
                # evidence, but the current ontology has no narrower schedule
                # edge type and LEGAL_BASIS would overstate the relationship.
                status = "candidate"
                method = "manual_legal_sanity_review_schedule"
                explanation = f"Manual legal sanity review found schedule/applicability evidence only. {direct_review['rationale']}"
                props["explanation"] = explanation[:500]
            elif decision in {"keep_context_no_promotion", "keep_candidate_manual_review"}:
                status = "candidate"
                method = "manual_legal_sanity_review_context"
                explanation = f"Manual legal sanity review retained this as context/candidate only. {direct_review['rationale']}"
                props["explanation"] = explanation[:500]
        self.conn.execute(
            """
            INSERT OR REPLACE INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (eid, src, tgt, typ, json.dumps(props, ensure_ascii=False), span, confidence, method, status),
        )
        if confidence < 0.75 or status in {"candidate", "needs_review"}:
            item = {"edge_id": eid, "source_node_id": src, "edge_type": typ, "target_node_id": tgt, "evidence_span_id": span or "", "confidence": confidence, "review_status": status, "explanation": explanation}
            if confidence < 0.75:
                self.low_conf.append(item)
            self.review.append(item)
        self.counts[f"edge_{typ}"] += 1

    def source_docs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM source_document
            WHERE source_status IN ('downloaded','extracted')
              AND (
                url LIKE 'https://www.bankofengland.co.uk/%'
                OR url LIKE 'http://www.bankofengland.co.uk/%'
                OR url LIKE 'https://www.prarulebook.co.uk/%'
                OR url LIKE 'http://www.prarulebook.co.uk/%'
                OR url LIKE 'https://www.fca.org.uk/%'
                OR url LIKE 'https://www.eba.europa.eu/%'
                OR url LIKE 'https://www.legislation.gov.uk/%'
              )
              AND lower(coalesce(title,'') || ' ' || coalesce(url,'') || ' ' || coalesce(local_path,'')) GLOB '*[a-z0-9]*'
            """
        ).fetchall()

    def classify_docs(self) -> dict[str, list[sqlite3.Row]]:
        by_code: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for d in self.source_docs():
            hay = f"{d['title'] or ''} {d['url'] or ''} {d['local_path'] or ''}"
            # C-template annexes are handled via template-set packages, not as standalone data items.
            for m in CODE_RE.finditer(hay):
                code = norm_code(m.group(1))
                if code == "COR011":
                    # COR011 is already complete; leave it as pilot baseline but include source link evidence below.
                    continue
                by_code[code].append(d)
            # Annex/corep files without item code: create family packages so they are not lost.
            if not CODE_RE.search(hay):
                if re.search(r"corep|finrep|pillar3|taxonomy|dpm|validation|sample instances|large exposures|asset encumbrance", hay, re.I):
                    fam = self.family_code(hay)
                    by_code[fam].append(d)
            # The official interim intraday liquidity instruction PDF names IDY001/2/3
            # in the PDF text rather than in the URL/title, so link it explicitly to
            # the three IDY template packages without adding any new ontology terms.
            if re.search(r"intraday-liquidity-monitoring-reporting\.pdf|Interim intraday reporting notes", hay, re.I):
                for code in ["IDY001", "IDY002", "IDY003"]:
                    by_code[code].append(d)
            # The official forecast balance-sheet instructions are published as
            # one range document for PRA104-PRA106, so attach that single
            # instruction source to each affected return.
            if re.search(r"pra104-106-instructions\.pdf|PRA104-106 instructions|PRA104\s*[–-]\s*PRA106", hay, re.I):
                for code in ["PRA104", "PRA105", "PRA106"]:
                    by_code[code].append(d)
            # Own funds/core capital instruction annexes are named by annex rather
            # than by PRA return code. Map them to the package codes used by the
            # PRA/BoE reporting source set.
            if re.search(r"annex-ii-reporting-instructions\.pdf|ANNEX II INSTRUCTIONS FOR REPORTING ON OWN FUNDS", hay, re.I):
                by_code["PRA001"].append(d)
            if re.search(r"annex-iia-instructions-for-reporting-on-own-funds-and-own-funds-requirements-for-sddts\.pdf|ANNEX IIA INSTRUCTIONS FOR REPORTING ON OWN FUNDS", hay, re.I):
                by_code["PRA116"].append(d)
            # The current leverage instructions cover the LVR001-LVR002 range,
            # but titles often abbreviate that as LVR001-002, which the generic
            # code regex reads only as LVR001. Attach the official instruction
            # document to both leverage returns deterministically.
            if re.search(r"LVR001\s*[–-]\s*002|LVR001\s*[–-]\s*LVR002|data-items-instructions-for-reporting-on-leverage\.pdf|annex-xi-reporting-on-leverage\.pdf", hay, re.I):
                for code in ["LVR001", "LVR002"]:
                    by_code[code].append(d)
        return by_code

    def family_code(self, hay: str) -> str:
        h = hay.lower()
        if "corep-own-funds" in h or "reporting on own funds" in h:
            return "COREP-OWN-FUNDS"
        if "corep-losses-immovable-property" in h or "losses immovable property" in h:
            return "COREP-LOSSES-IMMOVABLE-PROPERTY"
        if "corep-g-sii-buffer" in h or "g-sii buffer" in h:
            return "COREP-G-SII-BUFFER"
        if "counterparty" in h or "ccr" in h:
            return "COREP-CCR"
        if "large" in h or "concentration" in h:
            return "COREP-LARGE-EXPOSURES"
        if "leverage" in h:
            return "COREP-LEVERAGE"
        if "liquidity" in h or "nsfr" in h:
            return "COREP-LIQUIDITY"
        if "credit-risk" in h or "credit risk" in h:
            return "COREP-CREDIT-RISK"
        if "market-risk" in h or "market risk" in h:
            return "COREP-MARKET-RISK"
        if "pillar3" in h:
            return "PILLAR3-DISCLOSURE"
        if "finrep" in h or "asset encumbrance" in h or "financial information" in h:
            return "FINREP"
        if "taxonomy" in h or "dpm" in h or "sample" in h or "validation" in h:
            return "BOE-BANKING-TAXONOMY"
        return "COREP-OTHER"

    def ensure_normalised_obligation(self, code: str, title: str, domain: str, span: str | None) -> str:
        oid = f"reporting_obligation:{code}"
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reporting_obligation(obligation_id,data_item_code,title,domain,source_span_id)
            VALUES (?,?,?,?,?)
            """,
            (oid, code, title[:240], domain, span),
        )
        self.counts["normalised_reporting_obligations"] += 1
        return oid

    def parse_xlsx_cells(self, path: Path) -> dict[str, list[dict[str, Any]]]:
        if not path.exists() or path.suffix.lower() not in {".xlsx", ".xlsm", ".xltx"}:
            return {}
        with zipfile.ZipFile(path) as z:
            try:
                shared = self.shared_strings(z)
                sheets = self.sheets(z)
            except Exception:
                return {}
            out = {}
            for sheet_name, target in sheets:
                if target not in z.namelist():
                    continue
                root = ET.fromstring(z.read(target))
                rows = []
                for row in root.findall(f".//{NS_MAIN}sheetData/{NS_MAIN}row"):
                    rnum = int(float(row.attrib.get("r", "0") or 0))
                    vals = []
                    for c in row.findall(f"{NS_MAIN}c"):
                        ref = c.attrib.get("r", "")
                        val = self.cell_value(c, shared)
                        if val != "":
                            vals.append({"ref": ref, "col": re.sub(r"\d", "", ref), "value": str(val)})
                    if vals:
                        rows.append({"row": rnum, "cells": vals, "text": " | ".join(v["value"] for v in vals)})
                out[sheet_name] = rows
            return out

    def shared_strings(self, z: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        return ["".join(t.text or "" for t in si.iter(f"{NS_MAIN}t")) for si in root.findall(f"{NS_MAIN}si")]

    def sheets(self, z: zipfile.ZipFile) -> list[tuple[str, str]]:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall(f"{PKG_REL}Relationship")}
        out = []
        for sh in wb.findall(f"{NS_MAIN}sheets/{NS_MAIN}sheet"):
            name = sh.attrib.get("name", "Sheet")
            rid = sh.attrib.get(f"{NS_REL}id")
            target = rid_to_target.get(rid, "")
            if target and not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            out.append((name, target))
        return out

    def cell_value(self, c: ET.Element, shared: list[str]) -> str:
        typ = c.attrib.get("t")
        if typ == "inlineStr":
            return "".join(t.text or "" for t in c.iter(f"{NS_MAIN}t"))
        v = c.find(f"{NS_MAIN}v")
        f = c.find(f"{NS_MAIN}f")
        if v is None and f is None:
            return ""
        if typ == "s" and v is not None:
            try:
                return shared[int(v.text or "0")]
            except Exception:
                return v.text or ""
        if f is not None:
            return "=" + (f.text or "")
        return v.text or ""

    def parse_template_doc(self, code: str, d: sqlite3.Row, domain: str) -> list[str]:
        path = rel(d["local_path"])
        if not path or path.suffix.lower() not in {".xlsx", ".xlsm", ".xltx"} or not path.exists():
            return []
        data = self.parse_xlsx_cells(path)
        template_ids = []
        for sheet, rows in data.items():
            if not rows:
                continue
            sheet_context = f"{sheet} " + " ".join(r["text"] for r in rows[:8])
            cm = C_TEMPLATE_RE.search(sheet_context)
            template_code = f"C{cm.group(1)}.{cm.group(2)}" if cm else code
            # Avoid one giant duplicate per hidden/notes sheet with no coded rows.
            explicit_rows = [r for r in rows if any(ROW_RE.match(str(c["value"]).strip()) for c in r["cells"][:4])]
            if not explicit_rows and len(rows) > 15:
                continue
            tid = f"template:{code}:{re.sub(r'[^A-Za-z0-9_.-]+','_',template_code + '_' + sheet)[:120]}"
            title = re.sub(r"\s+", " ", sheet_context).strip()[:240] or template_code
            self.conn.execute(
                "INSERT OR REPLACE INTO template(template_id,template_code,title,annex,source_id) VALUES (?,?,?,?,?)",
                (tid, template_code, title, self.annex_label(d), d["source_id"]),
            )
            self.node(tid, "Template", f"{template_code} {sheet}", "template", tid, {"data_item_code": code, "domain": domain, "source_id": d["source_id"]})
            template_ids.append(tid)
            span = self.doc_span(d["source_id"])
            # infer columns from explicit column code rows, otherwise use the first non-empty header row bounded.
            cols = self.infer_columns(rows)
            for col_code, col_label in list(cols.items())[:80]:
                cid = f"column:{tid}:{col_code}"
                self.conn.execute("INSERT OR REPLACE INTO template_column(column_id,template_id,column_code,column_order,label) VALUES (?,?,?,?,?)", (cid, tid, col_code, self.sort_num(col_code), col_label[:500]))
                self.node(cid, "TemplateColumn", f"{template_code} column {col_code} {col_label[:120]}", "template_column", cid, {"template_id": tid})
                self.edge(tid, "HAS_COLUMN", cid, span, 0.88, "deterministic_xlsx_parse", "candidate", "Column code/header parsed from official template workbook.")
            for rr in explicit_rows[:1500]:
                row_code = None
                for c in rr["cells"][:4]:
                    m = ROW_RE.match(str(c["value"]).strip())
                    if m:
                        row_code = m.group(1).upper()
                        break
                if not row_code:
                    continue
                label = " | ".join(str(c["value"]) for c in rr["cells"][:8] if not ROW_RE.match(str(c["value"]).strip()))[:500]
                rid = f"row:{tid}:{row_code}"
                self.conn.execute("INSERT OR REPLACE INTO template_row(row_id,template_id,row_code,row_order,label) VALUES (?,?,?,?,?)", (rid, tid, row_code, self.sort_num(row_code), label))
                self.node(rid, "TemplateRow", f"{template_code} row {row_code} {label[:120]}", "template_row", rid, {"template_id": tid})
                self.edge(tid, "HAS_ROW", rid, span, 0.90, "deterministic_xlsx_parse", "candidate", "Row code parsed from official template workbook.")
                for col_code in list(cols.keys())[:40] or ["value"]:
                    did = f"datapoint:{tid}:r{row_code}:c{col_code}"
                    cid = f"column:{tid}:{col_code}"
                    self.conn.execute("INSERT OR REPLACE INTO datapoint(datapoint_id,template_id,row_id,column_id,data_type,concept_label) VALUES (?,?,?,?,?,?)", (did, tid, rid, cid if cols else None, "reported_value", label or title))
                    self.node(did, "DataPoint", label[:240] or did, "datapoint", did, {"template_id": tid, "row_code": row_code, "column_code": col_code})
                    self.edge(tid, "HAS_DATAPOINT", did, span, 0.82, "deterministic_xlsx_parse", "candidate", "Datapoint projected from explicit row/column intersection in official template workbook.")
                    concept_id = self.concept_for(label or title, domain)
                    if concept_id:
                        self.edge(did, "REPORTS_CONCEPT", concept_id, span, 0.76, "semantic_keyword", "candidate", "Concept inferred from template row/title terms; queued for review.")
        return template_ids

    def infer_columns(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        cols: dict[str, str] = {}
        for rr in rows[:35]:
            for c in rr["cells"]:
                val = str(c["value"]).strip()
                m = COL_RE.match(val)
                if m:
                    cols.setdefault(m.group(1).upper(), rr["text"][:300])
            if len(cols) >= 80:
                break
        if not cols:
            # Fallback to visible spreadsheet columns in first informative row.
            for rr in rows[:10]:
                if len(rr["cells"]) >= 2:
                    for c in rr["cells"][:20]:
                        cols.setdefault(c["col"].upper() or c["ref"], str(c["value"])[:200])
                    break
        return cols

    def sort_num(self, code: str | None) -> int | None:
        if not code:
            return None
        m = re.search(r"\d+", code)
        return int(m.group(0)) if m else None

    def annex_label(self, d: sqlite3.Row) -> str:
        m = re.search(r"Annex\s+[IVXLCDM]+[A-Z]?", d["title"] or "", re.I)
        return m.group(0) if m else "official template/data item"

    def concept_for(self, text: str, domain: str) -> str | None:
        label = None
        t = text.lower()
        for key in ["own funds", "capital", "credit risk", "counterparty credit risk", "market risk", "leverage", "large exposures", "liquidity", "net stable funding", "asset encumbrance", "remuneration", "operational risk", "financial information"]:
            if key in t or key in domain.lower():
                label = key.title()
                break
        if not label and domain != "other PRA/BoE reporting":
            label = domain
        if not label:
            return None
        cid = "concept:" + re.sub(r"[^A-Za-z0-9]+", "", label.title())
        self.conn.execute("INSERT OR REPLACE INTO concept(concept_id,concept_type,label,description) VALUES (?,?,?,?)", (cid, "reporting_concept", label, f"Concept inferred for {domain} reporting package."))
        self.node(cid, "Concept", label, "concept", cid, {"domain": domain})
        return cid

    def submission_system(self, docs: list[sqlite3.Row]) -> str | None:
        # Prefer actual source spans around the docs; fallback to title/url hints.
        doc_ids = [d["source_id"] for d in docs[:20]]
        if doc_ids:
            q = ",".join("?" for _ in doc_ids)
            text = "\n".join(r[0] or "" for r in self.conn.execute(f"SELECT normalised_text FROM source_span WHERE source_id IN ({q}) AND normalised_text IS NOT NULL LIMIT 500", doc_ids))
            for name, rx in SUBMISSION_SYSTEMS:
                if rx.search(text):
                    return name
        hay = " ".join((d["title"] or "") + " " + (d["url"] or "") for d in docs)
        for name, rx in SUBMISSION_SYSTEMS:
            if rx.search(hay):
                return name
        return None

    def legal_basis_candidates(self, code: str, domain: str) -> list[tuple[str, str | None, float]]:
        terms = [code]
        if "liquidity" in domain: terms += ["Liquidity", "Reporting (CRR)"]
        elif "capital" in domain or "credit" in domain or "market" in domain or "leverage" in domain or "large" in domain: terms += ["Reporting (CRR)", "Own Funds", "Capital"]
        elif "FINREP" in domain: terms += ["Regulatory Reporting", "financial information"]
        else: terms += ["Regulatory Reporting"]
        out = []
        for term in terms:
            rows = self.conn.execute("SELECT provision_id,provision_label,text FROM provision WHERE provision_label LIKE ? OR text LIKE ? LIMIT 8", (f"%{term}%", f"%{term}%")).fetchall()
            for r in rows:
                out.append((r["provision_id"], None, 0.72 if term == code else 0.68))
        seen = set(); uniq=[]
        for pid, span, conf in out:
            if pid not in seen:
                seen.add(pid); uniq.append((pid, span, conf))
        return uniq[:12]

    def build(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        by_code = self.classify_docs()
        # FirmType and ScopeRule nodes used across packages.
        for ft in ["bank", "building society", "PRA-designated investment firm"]:
            fid = "firm_type:" + re.sub(r"[^A-Za-z0-9]+", "_", ft).strip("_")
            self.node(fid, "FirmType", ft, props={"scope": "banking/building societies/PRA-designated investment firms"}, status="accepted_candidate")
        for sr in ["solo basis", "consolidated basis", "sub-consolidated basis", "UK establishment where applicable"]:
            sid = "scope_rule:" + re.sub(r"[^A-Za-z0-9]+", "_", sr).strip("_")
            self.node(sid, "ScopeRule", sr, props={"scope": "general reporting basis"})
        for code, docs in sorted(by_code.items()):
            # Keep packages meaningful: require either a template/instruction/taxonomy source or an explicit item code.
            docs = list({d["source_id"]: d for d in docs}.values())
            hay = " ".join((d["title"] or "") + " " + (d["url"] or "") + " " + (d["local_path"] or "") for d in docs)
            domain = domain_for(hay, code)
            title = title_for_code(code, docs)
            first_span = self.doc_span(docs[0]["source_id"]) if docs else None
            data_node = f"data_item:{code}"
            obligation_node = f"reporting_obligation:{code}"
            oid = self.ensure_normalised_obligation(code, title, domain, first_span)
            props = {
                "data_item_code": code,
                "reporting_domain": domain,
                "title": title,
                "submission_system": self.submission_system(docs),
                "source_document_count": len(docs),
            }
            self.node(data_node, "DataItem", code, "reporting_obligation", f"data_item:{code}", props)
            self.node(obligation_node, "ReportingObligation", f"{code} reporting obligation", "reporting_obligation", oid, props)
            self.edge(obligation_node, "CONTAINS", data_node, first_span, 0.90, "deterministic_package", "candidate", "Data item package created from official reporting source documents.")
            for ft in ["bank", "building society", "PRA-designated investment firm"]:
                fid = "firm_type:" + re.sub(r"[^A-Za-z0-9]+", "_", ft).strip("_")
                self.edge(obligation_node, "APPLIES_TO", fid, first_span, 0.78, "scope_from_banking_page", "candidate", "Package is sourced from the banking, building societies and PRA-designated investment firms reporting page.")
            for sr in ["solo basis", "consolidated basis", "sub-consolidated basis"]:
                sid = "scope_rule:" + re.sub(r"[^A-Za-z0-9]+", "_", sr).strip("_")
                self.edge(obligation_node, "HAS_SCOPE_RULE", sid, first_span, 0.66, "generic_reporting_scope", "candidate", "Generic reporting-basis hook retained as a low-confidence scope candidate; source-specific scope may be in instructions and should not be treated as accepted legal applicability.")
            template_docs = [d for d in docs if d["file_type"] in {"xlsx", "xlsm", "xltx", "xls", "pdf"} and re.search(r"template|data[- ]item|annex|reporting-on|pillar3|corep|finrep|lvr", f"{d['title']} {d['url']} {d['local_path']}", re.I)]
            instr_docs = [d for d in docs if re.search(r"instruction|guidance|notes|manual|q&a|qa", f"{d['title']} {d['url']} {d['local_path']}", re.I)]
            tax_docs = [d for d in docs if re.search(r"taxonomy|dpm|sample instance|xbrl|filing manual|release note|change log", f"{d['title']} {d['url']} {d['local_path']}", re.I)]
            val_docs = [d for d in docs if re.search(r"validation|known issues", f"{d['title']} {d['url']} {d['local_path']}", re.I)]
            template_set = f"template_set:{code}"
            instruction_set = f"instruction_set:{code}"
            if template_docs:
                self.node(template_set, "TemplateSet", f"{code} template set", props={"source_document_ids": [d["source_id"] for d in template_docs]})
                self.edge(data_node, "USES_TEMPLATE", template_set, self.doc_span(template_docs[0]["source_id"]), 0.90, "deterministic_package", "candidate", "Official template/data-item source document linked to package.")
            if instr_docs:
                self.node(instruction_set, "InstructionSet", f"{code} instruction set", props={"source_document_ids": [d["source_id"] for d in instr_docs]})
                self.edge(data_node, "USES_INSTRUCTIONS", instruction_set, self.doc_span(instr_docs[0]["source_id"]), 0.90, "deterministic_package", "candidate", "Official instruction/guidance source document linked to package.")
            parsed_template_ids = []
            for d in template_docs[:8]:
                span = self.doc_span(d["source_id"])
                sd_node = f"source_document:{d['source_id']}"
                self.node(sd_node, "SourceDocument", d["title"] or d["source_id"], "source_document", d["source_id"], {"url": d["url"], "file_type": d["file_type"], "checksum_sha256": d["checksum_sha256"]}, "accepted_candidate")
                self.edge(data_node, "EVIDENCED_BY", sd_node, span, 1.0, "source_provenance", "accepted_candidate", "Package source provenance document.")
                if template_docs:
                    self.edge(template_set, "EVIDENCED_BY", sd_node, span, 1.0, "source_provenance", "accepted_candidate", "Template set source provenance document.")
                if d["file_type"] in {"xlsx", "xlsm", "xltx"}:
                    for tid in self.parse_template_doc(code, d, domain):
                        parsed_template_ids.append(tid)
                        if template_docs:
                            self.edge(template_set, "CONTAINS", tid, span, 0.92, "deterministic_xlsx_parse", "candidate", "Template parsed from official workbook.")
                            self.edge(data_node, "USES_TEMPLATE", tid, span, 0.86, "deterministic_xlsx_parse", "candidate", "Data item uses parsed official template.")
                        if instr_docs:
                            self.edge(tid, "USES_INSTRUCTIONS", instruction_set, self.doc_span(instr_docs[0]["source_id"]), 0.78, "document_pairing", "candidate", "Instruction document paired with template by data-item/package code.")
            for d in instr_docs[:8]:
                span = self.doc_span(d["source_id"])
                sd_node = f"source_document:{d['source_id']}"
                self.node(sd_node, "SourceDocument", d["title"] or d["source_id"], "source_document", d["source_id"], {"url": d["url"], "file_type": d["file_type"], "checksum_sha256": d["checksum_sha256"]}, "accepted_candidate")
                self.edge(instruction_set if instr_docs else data_node, "EVIDENCED_BY", sd_node, span, 1.0, "source_provenance", "accepted_candidate", "Instruction source provenance document.")
            for d in val_docs[:20]:
                span = self.doc_span(d["source_id"])
                vid = f"validation_rule:{code}:{d['source_id']}"
                self.conn.execute("INSERT OR REPLACE INTO validation_rule(validation_id,label,expression_text,source_id,source_span_id) VALUES (?,?,?,?,?)", (vid, d["title"] or vid, "Validation/known-issue artefact linked at source-document level; detailed rule extraction remains candidate review.", d["source_id"], span))
                self.node(vid, "ValidationRule", d["title"] or vid, "validation_rule", vid, {"source_id": d["source_id"]})
                self.edge(data_node, "HAS_VALIDATION_RULE", vid, span, 0.78, "source_classification", "candidate", "Official validation/known-issues source linked to data item package.")
            for d in tax_docs[:20]:
                span = self.doc_span(d["source_id"])
                sd_node = f"source_document:{d['source_id']}"
                self.node(sd_node, "SourceDocument", d["title"] or d["source_id"], "source_document", d["source_id"], {"url": d["url"], "file_type": d["file_type"], "checksum_sha256": d["checksum_sha256"], "source_type": "taxonomy/DPM/XBRL artefact"}, "accepted_candidate")
                self.edge(data_node, "EVIDENCED_BY", sd_node, span, 0.86, "taxonomy_source_link", "candidate", "Taxonomy/DPM/XBRL artefact linked as package evidence.")
            legal_basis = []
            for pid, span, conf in self.legal_basis_candidates(code, domain):
                pr = self.conn.execute("SELECT provision_label FROM provision WHERE provision_id=?", (pid,)).fetchone()
                if not pr:
                    continue
                self.node(pid, "Provision", pr["provision_label"], "provision", pid)
                self.edge(obligation_node, "LEGAL_BASIS", pid, span or first_span, conf, "legal_basis_candidate", "needs_review", "Candidate legal basis from existing PRA Rulebook provisions; requires legal review.")
                legal_basis.append(pid)
            self.packages[code] = DataItemPackage(
                code=code, title=title, reporting_domain=domain,
                source_document_ids=[d["source_id"] for d in docs],
                template_document_ids=[d["source_id"] for d in template_docs],
                instruction_document_ids=[d["source_id"] for d in instr_docs],
                taxonomy_document_ids=[d["source_id"] for d in tax_docs],
                validation_document_ids=[d["source_id"] for d in val_docs],
                legal_basis_node_ids=legal_basis,
            )
            self.write_package(code)
            self.conn.commit()
        self.write_reports()
        self.conn.commit()

    def node_dict(self, nid: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM graph_node WHERE node_id=?", (nid,)).fetchone()
        return dict(r) if r else None

    def evidence(self, span_id: str | None) -> dict[str, Any] | None:
        if not span_id:
            return None
        r = self.conn.execute(
            """
            SELECT s.span_id,s.source_id,s.span_type,s.page_number,s.sheet_name,s.row_number,s.column_number,s.heading_path,s.anchor,
                   s.normalised_text AS text,d.title AS source_title,d.url AS source_url,d.local_path,d.file_type,d.checksum_sha256,d.downloaded_at,d.publication_date,d.effective_from,d.parent_url
            FROM source_span s JOIN source_document d ON d.source_id=s.source_id WHERE s.span_id=?
            """, (span_id,),
        ).fetchone()
        return dict(r) if r else None

    def edge_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["evidence"] = self.evidence(d.get("evidence_span_id"))
        try:
            d["properties"] = json.loads(d.pop("properties_json") or "{}")
        except Exception:
            d["properties"] = {}
        return d

    def write_package(self, code: str) -> None:
        pkg = self.packages[code]
        package_dir = OUT / "packages"
        package_dir.mkdir(parents=True, exist_ok=True)
        data_node = f"data_item:{code}"
        obligation_node = f"reporting_obligation:{code}"
        direct_edges = [self.edge_dict(r) for r in self.conn.execute("SELECT * FROM graph_edge WHERE source_node_id IN (?,?) OR target_node_id IN (?,?) ORDER BY confidence DESC LIMIT 1000", (data_node, obligation_node, data_node, obligation_node))]
        templates = [dict(r) for r in self.conn.execute("SELECT * FROM template WHERE template_id LIKE ? OR template_id=? ORDER BY template_code,title LIMIT 500", (f"template:{code}:%", f"template:{code}"))]
        for t in templates:
            t["rows_count"] = self.conn.execute("SELECT count(*) FROM template_row WHERE template_id=?", (t["template_id"],)).fetchone()[0]
            t["columns_count"] = self.conn.execute("SELECT count(*) FROM template_column WHERE template_id=?", (t["template_id"],)).fetchone()[0]
            t["datapoints_count"] = self.conn.execute("SELECT count(*) FROM datapoint WHERE template_id=?", (t["template_id"],)).fetchone()[0]
        docs = [dict(r) for r in self.conn.execute(f"SELECT source_id,title,url,file_type,checksum_sha256,downloaded_at,publication_date,effective_from,parent_url,local_path FROM source_document WHERE source_id IN ({','.join('?' for _ in pkg.source_document_ids)}) ORDER BY title", pkg.source_document_ids)] if pkg.source_document_ids else []
        obj = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "package_type": "ReportingObligationPackage",
            "core_node_id": data_node,
            "data_item_code": code,
            "title": pkg.title,
            "reporting_domain": pkg.reporting_domain,
            "summary": {
                "source_documents": len(pkg.source_document_ids),
                "template_documents": len(pkg.template_document_ids),
                "instruction_documents": len(pkg.instruction_document_ids),
                "taxonomy_documents": len(pkg.taxonomy_document_ids),
                "validation_documents": len(pkg.validation_document_ids),
                "legal_basis_candidates": len(pkg.legal_basis_node_ids),
                "templates": len(templates),
            },
            "data_item": self.node_dict(data_node),
            "reporting_obligation": self.node_dict(obligation_node),
            "legal_basis": {"candidate_provisions": [self.node_dict(x) for x in pkg.legal_basis_node_ids], "review_required": True},
            "reporting_artefacts": {"templates": templates, "template_set": self.node_dict(f"template_set:{code}"), "instruction_set": self.node_dict(f"instruction_set:{code}")},
            "validation_rules": [dict(r) for r in self.conn.execute("SELECT * FROM validation_rule WHERE validation_id LIKE ? ORDER BY label", (f"validation_rule:{code}:%",))],
            "source_provenance": docs,
            "direct_edges": direct_edges,
            "review_workflow": {"confidence_threshold": 0.75, "candidate_edges_require_review": True, "accepted_candidate_edges_are_source-provenance_or_COR011_baseline": True},
        }
        path = package_dir / f"{code.lower().replace('/','_')}_package.json"
        path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        pkg.package_path = path.relative_to(ROOT).as_posix()

    def write_reports(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        # Package index.
        with (OUT / "package_index.csv").open("w", newline="", encoding="utf-8") as f:
            fields = list(asdict(next(iter(self.packages.values()))).keys()) if self.packages else ["code"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for p in self.packages.values():
                row = asdict(p)
                for k, v in list(row.items()):
                    if isinstance(v, list): row[k] = ";".join(v)
                w.writerow(row)
        with (OUT / "review_edges.csv").open("w", newline="", encoding="utf-8") as f:
            fields = ["edge_id","source_node_id","edge_type","target_node_id","evidence_span_id","confidence","review_status","explanation"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(self.review)
        with (OUT / "low_confidence_edges.csv").open("w", newline="", encoding="utf-8") as f:
            fields = ["edge_id","source_node_id","edge_type","target_node_id","evidence_span_id","confidence","review_status","explanation"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(self.low_conf)
        qa = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": dict(self.counts),
            "packages": len(self.packages),
            "by_domain": Counter(p.reporting_domain for p in self.packages.values()),
            "packages_without_templates": [p.code for p in self.packages.values() if not p.template_document_ids],
            "packages_without_instructions": [p.code for p in self.packages.values() if not p.instruction_document_ids],
            "packages_without_legal_basis_candidates": [p.code for p in self.packages.values() if not p.legal_basis_node_ids],
            "low_confidence_edges": len(self.low_conf),
            "review_edges": len(self.review),
        }
        qa["by_domain"] = dict(qa["by_domain"])
        (OUT / "qa_report.json").write_text(json.dumps(qa, indent=2, ensure_ascii=False), encoding="utf-8")
        readme = [
            "# All PRA/BoE reporting packages",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            "",
            "This projection extends the COR011 package pattern to other official reporting materials already ingested into `source_document` and `source_span`.",
            "It preserves the existing schema, allowed node/edge types, confidence fields and review-status workflow.",
            "",
            "## Outputs",
            "- `package_index.csv` - one row per ReportingObligationPackage.",
            "- `packages/*_package.json` - package views equivalent to the COR011 package pattern.",
            "- `review_edges.csv` - candidate/needs-review edges.",
            "- `low_confidence_edges.csv` - edges below confidence 0.75.",
            "- `qa_report.json` - package coverage and QA checks.",
            "",
            "## Evidence standard",
            "Every graph edge inserted by this pass carries an `evidence_span_id` where a source span exists. Where a source document had no parsed span, the script created a `document_anchor` source span rather than linking without evidence.",
            "",
            "## Review note",
            "Legal-basis and generic scope edges are intentionally marked `needs_review` unless directly established by source-provenance pairing. This avoids collapsing legal obligations, artefacts, instructions and data models into generic references.",
        ]
        (OUT / "README.md").write_text("\n".join(readme), encoding="utf-8")


def main() -> None:
    b = Builder()
    b.build()
    print(json.dumps({"out": str(OUT), "packages": len(b.packages), "counts": dict(b.counts), "low_confidence_edges": len(b.low_conf), "review_edges": len(b.review)}, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
