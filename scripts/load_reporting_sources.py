#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "backend/data/rulebook.sqlite3"
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
RAW_DIR = PROJECT_ROOT / "backend/data/raw/reporting-sources/cor011-lcr-final"
MANIFEST_PATH = RAW_DIR / "source_manifest.csv"
OUTPUT_DIR = RAW_DIR / "parsed-load"
EXTRACT_DIR = RAW_DIR / "extracted"

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

TABLE_DELETE_ORDER = [
    "graph_edge", "graph_node", "validation_rule", "calculation_rule", "permission", "concept",
    "instruction", "datapoint", "template_column", "template_row", "template",
    "reporting_obligation", "provision", "rulebook_part", "source_span", "source_document",
]

TARGET_TEMPLATE_CODES = {"C72.00", "C73.00", "C74.00", "C75.01", "C76.00"}
TEMPLATE_ALIASES = {
    "C 72.00": "C72.00", "C_72.00": "C72.00", "C72": "C72.00",
    "C 73.00": "C73.00", "C_73.00": "C73.00", "C73": "C73.00",
    "C 74.00": "C74.00", "C_74.00": "C74.00", "C74": "C74.00",
    "C 75.01": "C75.01", "C_75.01": "C75.01", "C75.01": "C75.01",
    "C 76.00": "C76.00", "C_76.00": "C76.00", "C76": "C76.00",
}
RELEVANT_TEXT = re.compile(r"\b(COR011|C\s*7[2-6]\.0[01]|C7[2-6]\.0[01]|Annex\s+XXIV|Annex\s+XXV|liquidity coverage|LCR|Article\s+415|Article\s+430|PRA110|COREP liquidity|liquid assets|outflows|inflows|collateral swaps|calculations?)\b", re.I)
ARTICLE_LABEL = re.compile(r"\b(Article\s+\d+[A-Z]?(?:\(\d+\))?(?:\([a-z]\))?|Annex\s+[IVXLCDM]+|Chapter\s+\d+[A-Z]?)\b", re.I)
TEMPLATE_CODE_RE = re.compile(r"\bC\s*([0-9]{2})\.([0-9]{2})\b", re.I)
ROW_CODE_RE = re.compile(r"^(?:r)?\s*([0-9]{3,4})$", re.I)
COL_CODE_RE = re.compile(r"^(?:c)?\s*([0-9]{3,4})$", re.I)

@dataclass
class Counters:
    source_documents: int = 0
    source_spans: int = 0
    zip_entries: int = 0
    rulebook_parts: int = 0
    provisions: int = 0
    obligations: int = 0
    templates: int = 0
    rows: int = 0
    columns: int = 0
    datapoints: int = 0
    instructions: int = 0
    concepts: int = 0
    permissions: int = 0
    calculations: int = 0
    validations: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    errors: int = 0
    uncertain: int = 0

class Loader:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.c = Counters()
        self.errors: list[dict[str, str]] = []
        self.unresolved: list[dict[str, str]] = []
        self.template_summary: dict[str, dict[str, Any]] = {}
        self.datapoint_rows: list[dict[str, Any]] = []
        self.inserted_docs: set[str] = set()
        self.inserted_spans: set[str] = set()
        self.inserted_nodes: set[str] = set()
        self.inserted_edges: set[str] = set()

    def stable(self, prefix: str, *parts: Any) -> str:
        raw = "|".join("" if p is None else str(p) for p in parts)
        h = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]
        return f"{prefix}:{h}"

    def apply_schema_and_clear(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        for table in TABLE_DELETE_ORDER:
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def add_error(self, stage: str, identifier: str, message: str) -> None:
        self.c.errors += 1
        self.errors.append({"stage": stage, "identifier": identifier, "message": message})

    def add_unresolved(self, reference_type: str, reference_text: str, source_id: str, span_id: str | None, notes: str) -> None:
        self.c.uncertain += 1
        self.unresolved.append({
            "reference_type": reference_type,
            "reference_text": reference_text,
            "source_id": source_id,
            "span_id": span_id or "",
            "notes": notes,
        })

    def load_manifest_docs(self) -> list[dict[str, str]]:
        rows = list(csv.DictReader(MANIFEST_PATH.open(newline="", encoding="utf-8")))
        for r in rows:
            source_id = r["source_id"]
            self.conn.execute(
                """
                INSERT OR REPLACE INTO source_document
                (source_id,title,url,local_path,file_type,checksum_sha256,downloaded_at,publication_date,
                 effective_from,effective_to,parent_url,source_status,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_id, r.get("title"), r.get("url"), r.get("local_path"), r.get("file_type"),
                    r.get("checksum_sha256"), r.get("downloaded_at"), r.get("publication_date") or None,
                    r.get("effective_date") or None, None, r.get("parent_url"), "downloaded", r.get("notes"),
                ),
            )
            self.inserted_docs.add(source_id)
        self.conn.commit()
        self.c.source_documents += len(rows)
        return rows

    def span(self, source_id: str, span_type: str, raw_text: str | None, *, page_number=None, sheet_name=None,
             row_number=None, column_number=None, heading_path=None, anchor=None, start_offset=None, end_offset=None) -> str:
        sid = self.stable("span", source_id, span_type, page_number, sheet_name, row_number, column_number, anchor, start_offset, raw_text[:80] if raw_text else "")
        if sid in self.inserted_spans:
            return sid
        norm = re.sub(r"\s+", " ", raw_text or "").strip() if raw_text else None
        self.conn.execute(
            """
            INSERT OR IGNORE INTO source_span
            (span_id,source_id,span_type,page_number,sheet_name,row_number,column_number,heading_path,anchor,
             raw_text,normalised_text,start_offset,end_offset)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (sid, source_id, span_type, page_number, sheet_name, row_number, column_number, heading_path, anchor,
             raw_text, norm, start_offset, end_offset),
        )
        self.inserted_spans.add(sid)
        self.c.source_spans += 1
        return sid

    def parse_html(self, r: dict[str, str], path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(text, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else r.get("title")
        self.span(r["source_id"], "html_document", title or "", anchor="document")
        headings: list[str] = []
        for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
            val = el.get_text(" ", strip=True)
            if not val:
                continue
            tag = el.name.lower()
            anchor = el.get("id") or el.find_parent(id=True).get("id") if el.find_parent(id=True) else el.get("id")
            if tag.startswith("h"):
                level = int(tag[1])
                headings = headings[: level - 1] + [val]
                self.span(r["source_id"], "heading", val, heading_path=" > ".join(headings), anchor=anchor)
            else:
                stype = "provision" if ARTICLE_LABEL.search(val) else "paragraph"
                self.span(r["source_id"], stype, val, heading_path=" > ".join(headings), anchor=anchor)

    def parse_pdf(self, r: dict[str, str], path: Path) -> None:
        try:
            reader = PdfReader(str(path))
            for idx, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception as e:
                    self.add_error("pdf_page_extract", r["source_id"], f"page {idx}: {e}")
                    page_text = ""
                page_span = self.span(r["source_id"], "pdf_page", page_text, page_number=idx)
                paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=\.)\s{2,}", page_text) if p.strip()]
                for n, para in enumerate(paras, start=1):
                    if len(para) < 20:
                        continue
                    self.span(r["source_id"], "pdf_paragraph", para, page_number=idx, anchor=f"p{n}")
        except Exception as e:
            self.add_error("pdf_extract", r["source_id"], str(e))

    def xlsx_shared_strings(self, z: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        strings=[]
        for si in root.findall(f"{NS_MAIN}si"):
            strings.append("".join(t.text or "" for t in si.iter(f"{NS_MAIN}t")))
        return strings

    def xlsx_sheets(self, z: zipfile.ZipFile) -> list[tuple[str, str]]:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall(f"{PKG_REL}Relationship")}
        out=[]
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
            try: return shared[int(v.text or "0")]
            except Exception: return v.text or ""
        if f is not None:
            return "=" + (f.text or "")
        return v.text or ""

    def parse_xlsx_cells(self, source_id: str, path: Path, emit_spans: bool = True) -> dict[str, list[dict[str, Any]]]:
        sheets_data: dict[str, list[dict[str, Any]]] = {}
        with zipfile.ZipFile(path) as z:
            shared = self.xlsx_shared_strings(z)
            self.span(source_id, "xlsx_workbook", path.name, anchor="workbook") if emit_spans else None
            for sheet_name, target in self.xlsx_sheets(z):
                if target not in z.namelist():
                    continue
                self.span(source_id, "xlsx_sheet", sheet_name, sheet_name=sheet_name, anchor=sheet_name) if emit_spans else None
                root = ET.fromstring(z.read(target))
                rows=[]
                for row in root.findall(f".//{NS_MAIN}sheetData/{NS_MAIN}row"):
                    rnum = int(float(row.attrib.get("r", "0") or 0))
                    vals=[]
                    for c in row.findall(f"{NS_MAIN}c"):
                        ref = c.attrib.get("r", "")
                        val = self.cell_value(c, shared)
                        if val != "":
                            col_letters = re.sub(r"\d", "", ref)
                            vals.append({"ref": ref, "col": col_letters, "value": val})
                            if emit_spans:
                                col_num = self.col_num(col_letters)
                                self.span(source_id, "xlsx_cell", val, sheet_name=sheet_name, row_number=rnum, column_number=col_num, anchor=ref)
                    if vals:
                        row_text = " | ".join(v["value"] for v in vals)
                        if emit_spans:
                            self.span(source_id, "xlsx_row", row_text, sheet_name=sheet_name, row_number=rnum)
                        rows.append({"row": rnum, "cells": vals, "text": row_text})
                sheets_data[sheet_name] = rows
        return sheets_data

    def col_num(self, letters: str) -> int | None:
        if not letters:
            return None
        n=0
        for ch in letters.upper():
            if 'A' <= ch <= 'Z': n = n*26 + ord(ch)-64
        return n or None

    def parse_xlsx(self, r: dict[str, str], path: Path) -> dict[str, list[dict[str, Any]]]:
        try:
            return self.parse_xlsx_cells(r["source_id"], path, True)
        except Exception as e:
            self.add_error("xlsx_extract", r["source_id"], str(e))
            return {}

    def add_extracted_doc(self, parent: dict[str, str], entry_name: str, out_path: Path, data: bytes) -> str:
        checksum = hashlib.sha256(data).hexdigest()
        sid = self.stable("source", parent["source_id"], entry_name, checksum)
        rel_path = out_path.relative_to(PROJECT_ROOT).as_posix()
        ext = Path(entry_name).suffix.lower().lstrip(".") or "binary"
        title = Path(entry_name).name
        self.conn.execute(
            """
            INSERT OR REPLACE INTO source_document
            (source_id,title,url,local_path,file_type,checksum_sha256,downloaded_at,parent_url,source_status,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (sid, title, parent["url"] + "#" + entry_name, rel_path, ext, checksum,
             datetime.now(timezone.utc).isoformat(), parent["url"], "extracted", f"Extracted from ZIP source_id={parent['source_id']}"),
        )
        if sid not in self.inserted_docs:
            self.inserted_docs.add(sid); self.c.source_documents += 1
        self.span(parent["source_id"], "zip_entry", entry_name, anchor=entry_name)
        return sid

    def parse_zip(self, r: dict[str, str], path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                self.span(r["source_id"], "zip_manifest", "\n".join(names), anchor="zip_manifest")
                base = EXTRACT_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", r["source_id"])
                for name in names:
                    try:
                        data = z.read(name)
                    except Exception as e:
                        self.add_error("zip_entry_read", r["source_id"], f"{name}: {e}")
                        continue
                    out = base / name
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists() or hashlib.sha256(out.read_bytes()).digest() != hashlib.sha256(data).digest():
                        out.write_bytes(data)
                    child_sid = self.add_extracted_doc(r, name, out, data)
                    self.c.zip_entries += 1
                    suffix = Path(name).suffix.lower()
                    # Keep ZIP handling auditable but bounded: preserve/extract every entry and
                    # create a source_document row, but do not explode full taxonomy packages
                    # into multi-GB text spans. Only small text-like validation artefacts get a
                    # compact text span when they contain in-scope terms.
                    if suffix in {".xml", ".xsd", ".xbrl", ".txt", ".csv", ".json"} and len(data) <= 250_000:
                        txt = data.decode("utf-8", errors="replace")
                        if RELEVANT_TEXT.search(txt):
                            self.span(child_sid, "archive_text_extract", txt[:50_000], anchor="text_extract")
        except Exception as e:
            self.add_error("zip_extract", r["source_id"], str(e))

    def chunk_text(self, text: str, size: int) -> list[str]:
        return [text[i:i+size] for i in range(0, len(text), size)] if text else []

    def parse_sources(self, rows: list[dict[str, str]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        xlsx_cache = {}
        for r in rows:
            p = PROJECT_ROOT / r["local_path"]
            if not p.exists():
                self.add_error("missing_file", r["source_id"], str(p)); continue
            ft = r.get("file_type", "").lower()
            print(f"parse {ft} {r['source_id']} {p.name}", flush=True)
            if ft == "html": self.parse_html(r, p)
            elif ft == "pdf": self.parse_pdf(r, p)
            elif ft == "xlsx": xlsx_cache[r["source_id"]] = self.parse_xlsx(r, p)
            elif ft == "zip": self.parse_zip(r, p)
            self.conn.commit()
        self.conn.commit()
        return xlsx_cache

    def load_existing_rulebook(self) -> None:
        wanted_parts = [
            ("part:ReportingCRR", "Reporting (CRR)"),
            ("part:LCR", "Liquidity Coverage Ratio (CRR)"),
            ("part:LiquidityCRR", "Liquidity (CRR)"),
            ("part:RegulatoryReporting", "Regulatory Reporting"),
        ]
        for part_id, title in wanted_parts:
            row = self.conn.execute("SELECT * FROM node WHERE node_type='part' AND title=? ORDER BY url DESC LIMIT 1", (title,)).fetchone()
            if not row:
                self.add_unresolved("rulebook_part", title, "existing-db", None, "Part not found in existing node table")
                continue
            self.conn.execute("INSERT OR REPLACE INTO rulebook_part(part_id,title,url,effective_from) VALUES (?,?,?,?)", (part_id, row["title"], row["url"], self.extract_date(row["url"])))
            self.c.rulebook_parts += 1
            descendants = self.conn.execute(
                """
                WITH RECURSIVE d(id, depth) AS (
                  SELECT to_node_id, 1 FROM edge WHERE from_node_id=? AND edge_type='contains'
                  UNION ALL
                  SELECT e.to_node_id, d.depth+1 FROM edge e JOIN d ON e.from_node_id=d.id
                  WHERE e.edge_type='contains' AND d.depth < 8
                )
                SELECT DISTINCT n.* FROM d JOIN node n ON n.id=d.id
                WHERE n.node_type IN ('rule','chapter')
                ORDER BY n.stable_key
                """,
                (row["id"],),
            ).fetchall()
            for n in descendants:
                text = (n["text"] or "").strip()
                ttl = n["title"] or ""
                if title == "Regulatory Reporting" and not RELEVANT_TEXT.search(ttl + " " + text):
                    continue
                if title == "Liquidity (CRR)" and not (re.search(r"Article\s+415|Chapter\s+4|liquidity", ttl + " " + text, re.I)):
                    continue
                label_match = ARTICLE_LABEL.search(ttl) or ARTICLE_LABEL.search(text)
                label = label_match.group(1) if label_match else ttl[:80]
                pid = self.provision_id(part_id, label, n["url"], n["stable_key"])
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO provision
                    (provision_id,part_id,provision_label,provision_type,heading_path,text,effective_from,effective_to,source_span_id)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (pid, part_id, label, n["node_type"], ttl, text, self.extract_date(n["url"]), None, None),
                )
                self.c.provisions += 1
                if re.search(r"COR011|C\s*7[2-6]|liquidity coverage|liquidity", ttl + " " + text, re.I):
                    oid = self.stable("obligation", pid)
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO reporting_obligation
                        (obligation_id,data_item_code,title,domain,frequency,reporting_horizon_days,effective_from,source_span_id)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (oid, "COR011", ttl[:240], "liquidity", None, None, self.extract_date(n["url"]), None),
                    )
                    self.c.obligations += 1

    def extract_date(self, value: str | None) -> str | None:
        if not value: return None
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", value)
        return m.group(1) if m else None

    def provision_id(self, part_id: str, label: str, url: str, stable_key: str) -> str:
        key = re.sub(r"[^A-Za-z0-9]+", ".", label).strip(".") or hashlib.sha1(stable_key.encode()).hexdigest()[:8]
        return f"provision:{part_id.split(':',1)[1]}.{key}"

    def find_annex_source(self, rows: list[dict[str, str]], annex: str, file_type: str | None = None) -> dict[str, str] | None:
        terms = [annex.lower(), annex.lower().replace(" ", "-")]
        for r in rows:
            hay = (r.get("title", "") + " " + r.get("url", "") + " " + r.get("local_path", "")).lower()
            if file_type and r.get("file_type") != file_type: continue
            if any(t in hay for t in terms): return r
        return None

    def normalise_template_code(self, text: str) -> str | None:
        m = TEMPLATE_CODE_RE.search(text or "")
        if m:
            code = f"C{m.group(1)}.{m.group(2)}".upper()
            if code in TARGET_TEMPLATE_CODES: return code
        t = (text or "").strip().upper().replace("_", " ")
        return TEMPLATE_ALIASES.get(t)

    def parse_annex_xxiv(self, rows: list[dict[str, str]], xlsx_cache: dict[str, Any]) -> None:
        # Prefer the COREP liquidity workbook: this is Annex XXIV templates.
        # Use the current BoE COREP liquidity workbook as the canonical Annex XXIV
        # template source. Consultation annex copies are retained as source documents
        # and spans, but should not overwrite the current template objects.
        candidates = [r for r in rows if r.get("file_type") == "xlsx" and "corep-liquidity" in r.get("local_path", "")]
        if not candidates:
            self.add_unresolved("annex_xxiv", "Annex XXIV workbook", "manifest", None, "No XLSX candidate found")
            return
        for r in candidates:
            data = xlsx_cache.get(r["source_id"])
            if data is None:
                data = self.parse_xlsx_cells(r["source_id"], PROJECT_ROOT / r["local_path"], False)
            for sheet, sheet_rows in data.items():
                sheet_text = " ".join(x["text"] for x in sheet_rows[:15])
                code = self.normalise_template_code(sheet) or self.normalise_template_code(sheet_text)
                if not code and "perimeter" in (sheet + " " + sheet_text).lower():
                    code = "PERIMETER_OF_CONSOLIDATION"
                if not code:
                    continue
                template_id = f"template:{code}"
                title = self.template_title(code, sheet_text or sheet)
                self.conn.execute(
                    "INSERT OR REPLACE INTO template(template_id,template_code,title,annex,source_id) VALUES (?,?,?,?,?)",
                    (template_id, code, title, "Annex XXIV", r["source_id"]),
                )
                self.c.templates += 1
                col_headers = self.infer_headers(sheet_rows, column=True)
                row_count=0; col_count=0; dp_count=0
                for col_code, label in col_headers.items():
                    cid=f"column:{code}:{col_code}"
                    self.conn.execute("INSERT OR REPLACE INTO template_column(column_id,template_id,column_code,column_order,label,unit_type) VALUES (?,?,?,?,?,?)",
                                      (cid, template_id, col_code, int(col_code) if col_code.isdigit() else None, label, None))
                    self.c.columns += 1; col_count += 1
                for rr in sheet_rows:
                    first_vals=[c["value"] for c in rr["cells"][:3]]
                    row_code=None
                    for v in first_vals:
                        m=ROW_CODE_RE.match(str(v).strip())
                        if m: row_code=m.group(1); break
                    if not row_code: continue
                    label=" | ".join(v for v in first_vals if not ROW_CODE_RE.match(str(v).strip()))[:500]
                    rid=f"row:{code}:{row_code}"
                    self.conn.execute("INSERT OR REPLACE INTO template_row(row_id,template_id,row_code,row_order,label) VALUES (?,?,?,?,?)",
                                      (rid, template_id, row_code, int(row_code), label))
                    self.c.rows += 1; row_count += 1
                    for col_code in col_headers or {"000": "value"}:
                        did=f"datapoint:{code}:r{row_code}:c{col_code}"
                        cid=f"column:{code}:{col_code}"
                        self.conn.execute("INSERT OR REPLACE INTO datapoint(datapoint_id,template_id,row_id,column_id,data_type,unit_type,concept_label) VALUES (?,?,?,?,?,?,?)",
                                          (did, template_id, rid, cid if col_headers else None, "monetary_or_decimal", None, label or title))
                        self.c.datapoints += 1; dp_count += 1
                        self.datapoint_rows.append({"datapoint_id": did, "template_code": code, "row_code": row_code, "column_code": col_code, "concept_label": label or title})
                self.template_summary[code] = {"template_id": template_id, "template_code": code, "title": title, "annex": "Annex XXIV", "source_id": r["source_id"], "rows": row_count, "columns": col_count, "datapoints": dp_count, "notes": "deterministic XLSX parse"}

    def infer_headers(self, sheet_rows: list[dict[str, Any]], column: bool = True) -> dict[str, str]:
        headers={}
        for rr in sheet_rows[:25]:
            for cell in rr["cells"]:
                v=str(cell["value"]).strip()
                m=COL_CODE_RE.match(v)
                if m:
                    code=m.group(1)
                    # use neighbouring row text as label, conservative
                    headers.setdefault(code, rr["text"][:300])
        return headers

    def template_title(self, code: str, context: str) -> str:
        titles={
            "C72.00":"Liquid assets",
            "C73.00":"Outflows",
            "C74.00":"Inflows",
            "C75.01":"Collateral swaps",
            "C76.00":"Calculations",
            "PERIMETER_OF_CONSOLIDATION":"Perimeter of consolidation",
        }
        return titles.get(code, context[:200] or code)

    def parse_annex_xxv_instructions(self, rows: list[dict[str, str]]) -> None:
        candidates=[r for r in rows if r.get("file_type") == "pdf" and ("corep-liquidity-instructions" in r.get("local_path", "") or "Annex XXV" in r.get("title", ""))]
        if not candidates:
            self.add_unresolved("annex_xxv", "Annex XXV instructions", "manifest", None, "No PDF candidate found")
            return
        for r in candidates:
            spans=self.conn.execute("SELECT span_id, normalised_text FROM source_span WHERE source_id=? AND span_type='pdf_paragraph'", (r["source_id"],)).fetchall()
            current_template=None
            for sp in spans:
                text=sp["normalised_text"] or ""
                code=self.normalise_template_code(text)
                if code: current_template=code
                if len(text) < 30: continue
                if not (current_template or RELEVANT_TEXT.search(text)):
                    continue
                applies_type="template" if current_template else "source_span"
                applies_id=f"template:{current_template}" if current_template else sp["span_id"]
                iid=self.stable("instruction", r["source_id"], sp["span_id"], current_template)
                self.conn.execute("INSERT OR REPLACE INTO instruction(instruction_id,instruction_set,applies_to_type,applies_to_id,text,source_span_id) VALUES (?,?,?,?,?,?)",
                                  (iid, "Annex XXV", applies_type, applies_id, text, sp["span_id"]))
                self.c.instructions += 1
                if current_template and not self.conn.execute("SELECT 1 FROM template WHERE template_id=?", (f"template:{current_template}",)).fetchone():
                    self.add_unresolved("instruction_target", current_template, r["source_id"], sp["span_id"], "Instruction references template not parsed from Annex XXIV workbook")

    def parse_validation_rules(self) -> None:
        val_docs = self.conn.execute("SELECT source_id,local_path,title,file_type FROM source_document WHERE lower(title || ' ' || local_path) LIKE '%validation%' AND file_type IN ('xlsx','xml','xbrl','txt','csv')").fetchall()
        for d in val_docs:
            sid=d["source_id"]
            if d["file_type"] == "xlsx":
                try:
                    data=self.parse_xlsx_cells(sid, PROJECT_ROOT / d["local_path"], False)
                    for sheet, rows in data.items():
                        for rr in rows:
                            txt=rr["text"]
                            if RELEVANT_TEXT.search(txt) or "C_7" in txt or "C 7" in txt:
                                vid=self.stable("validation", sid, sheet, rr["row"], txt[:120])
                                self.conn.execute("INSERT OR REPLACE INTO validation_rule(validation_id,label,expression_text,source_id) VALUES (?,?,?,?)", (vid, f"{d['title']} {sheet} row {rr['row']}", txt, sid))
                                self.c.validations += 1
                except Exception as e:
                    self.add_error("validation_xlsx", sid, str(e))
            else:
                spans=self.conn.execute("SELECT span_id, normalised_text FROM source_span WHERE source_id=? AND span_type='archive_text_extract'", (sid,)).fetchall()
                for sp in spans:
                    txt=sp["normalised_text"] or ""
                    if RELEVANT_TEXT.search(txt):
                        vid=self.stable("validation", sid, sp["span_id"])
                        self.conn.execute("INSERT OR REPLACE INTO validation_rule(validation_id,label,expression_text,source_id,source_span_id) VALUES (?,?,?,?,?)", (vid, d["title"], txt[:4000], sid, sp["span_id"]))
                        self.c.validations += 1

    def seed_concepts_permissions_calculations_graph(self) -> None:
        concepts=[
            ("concept:LiquidityCoverageRatio", "metric", "Liquidity Coverage Ratio", "Ratio of liquidity buffer to net liquidity outflows."),
            ("concept:LiquidityBuffer", "measure", "Liquidity Buffer", "Stock of eligible liquid assets for LCR purposes."),
            ("concept:NetLiquidityOutflows", "measure", "Net Liquidity Outflows", "LCR denominator after inflows and outflows treatment."),
        ]
        for row in concepts:
            self.conn.execute("INSERT OR REPLACE INTO concept(concept_id,concept_type,label,description) VALUES (?,?,?,?)", row)
            self.c.concepts += 1
        perms=[("permission:LCR.Article17.4", "Liquidity buffer composition requirements", "Article 17(4)"), ("permission:LCR.Article33", "Inflows cap permission", "Article 33"), ("permission:LCR.Article34", "Higher intra-group inflow rate permission", "Article 34")]
        for pid,label,prov_label in perms:
            prov=self.conn.execute("SELECT provision_id FROM provision WHERE part_id='part:LCR' AND provision_label LIKE ? LIMIT 1", (f"%{prov_label}%",)).fetchone()
            self.conn.execute("INSERT OR REPLACE INTO permission(permission_id,label,provision_id,permission_type,description) VALUES (?,?,?,?,?)", (pid,label, prov[0] if prov else None, "PRA permission", label))
            self.c.permissions += 1
            if not prov: self.add_unresolved("permission_provision", prov_label, "existing-db", None, "Seed permission could not be linked to provision")
        # calculation rows from C76.00 datapoints
        for dp in self.conn.execute("SELECT datapoint_id,concept_label FROM datapoint WHERE template_id='template:C76.00'").fetchall():
            cid=self.stable("calculation", dp["datapoint_id"])
            self.conn.execute("INSERT OR REPLACE INTO calculation_rule(calculation_id,label,expression_text) VALUES (?,?,?)", (cid, dp["concept_label"], "Derived from C76.00 template row; formula not inferred in this deterministic pass"))
            self.c.calculations += 1
        # graph projection for key objects only
        for table, pk, typ, label in self.graph_sources():
            self.conn.execute("INSERT OR REPLACE INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,review_status) VALUES (?,?,?,?,?,?,?)", (pk, typ, label, table, pk, "{}", "unreviewed"))
            self.inserted_nodes.add(pk); self.c.graph_nodes += 1
        # conservative deterministic edges
        for tpl in self.conn.execute("SELECT template_id FROM template").fetchall():
            self.edge(f"edge:{tpl['template_id']}:reports:COR011", tpl["template_id"], "data_item:COR011", "reports_in")
        for dp in self.conn.execute("SELECT datapoint_id,template_id FROM datapoint").fetchall():
            self.edge(f"edge:{dp['datapoint_id']}:part_of:{dp['template_id']}", dp["datapoint_id"], dp["template_id"], "part_of_template")

    def graph_sources(self):
        yield ("reporting_obligation", "data_item:COR011", "data_item", "COR011")
        for r in self.conn.execute("SELECT template_id,title FROM template"): yield ("template", r["template_id"], "template", r["title"])
        for r in self.conn.execute("SELECT datapoint_id,concept_label FROM datapoint"): yield ("datapoint", r["datapoint_id"], "datapoint", r["concept_label"] or r["datapoint_id"])
        for r in self.conn.execute("SELECT concept_id,label FROM concept"): yield ("concept", r["concept_id"], "concept", r["label"])
        for r in self.conn.execute("SELECT permission_id,label FROM permission"): yield ("permission", r["permission_id"], "permission", r["label"])
        for r in self.conn.execute("SELECT provision_id,provision_label FROM provision WHERE provision_label LIKE '%Article 4%' OR provision_label LIKE '%Article 430%' OR provision_label LIKE '%Annex XXV%' OR provision_label LIKE '%Annex XXIV%' LIMIT 100"): yield ("provision", r["provision_id"], "provision", r["provision_label"])

    def edge(self, eid, src, tgt, typ):
        if src not in self.inserted_nodes or tgt not in self.inserted_nodes:
            self.add_unresolved("graph_edge", f"{src} -[{typ}]-> {tgt}", "graph_projection", None, "Missing projected source or target node")
            return
        self.conn.execute("INSERT OR REPLACE INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,confidence,extraction_method,review_status) VALUES (?,?,?,?,?,?,?)", (eid, src, tgt, typ, 1.0, "deterministic_loader", "unreviewed"))
        self.c.graph_edges += 1

    def write_outputs(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUTPUT_DIR / "unresolved_references.csv").open("w", newline="", encoding="utf-8") as f:
            fields=["reference_type","reference_text","source_id","span_id","notes"]
            w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(self.unresolved)
        with (OUTPUT_DIR / "templates_summary.csv").open("w", newline="", encoding="utf-8") as f:
            fields=["template_id","template_code","title","annex","source_id","rows","columns","datapoints","notes"]
            w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(self.template_summary.values())
        with (OUTPUT_DIR / "datapoints_summary.csv").open("w", newline="", encoding="utf-8") as f:
            fields=["datapoint_id","template_code","row_code","column_code","concept_label"]
            w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(self.datapoint_rows)
        with (OUTPUT_DIR / "parsing_errors.json").open("w", encoding="utf-8") as f:
            json.dump(self.errors, f, indent=2, ensure_ascii=False)
        report = [
            "# COR011 / LCR reporting parse report", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
            "## Scope", "COR011 only, Reporting (CRR), relevant Regulatory Reporting, Liquidity Coverage Ratio (CRR), relevant Liquidity (CRR), Annex XXIV/XXV, taxonomy/DPM/validation materials.", "",
            "## Counts", "",
        ]
        for k,v in self.c.__dict__.items(): report.append(f"- {k}: {v}")
        report += ["", "## Outputs", "", f"- unresolved_references.csv: `{OUTPUT_DIR/'unresolved_references.csv'}`", f"- templates_summary.csv: `{OUTPUT_DIR/'templates_summary.csv'}`", f"- datapoints_summary.csv: `{OUTPUT_DIR/'datapoints_summary.csv'}`", f"- parsing_errors.json: `{OUTPUT_DIR/'parsing_errors.json'}`", "", "## Notes", "", "- No LLM was used.", "- PDF extraction used pypdf text extraction only; OCR repair was not required in this run.", "- XLSX parsing used raw workbook XML, not openpyxl.", "- Graph edges are conservative projections only; no legal relationship inference was performed."]
        (OUTPUT_DIR / "parse_report.md").write_text("\n".join(report), encoding="utf-8")

    def run(self) -> None:
        self.apply_schema_and_clear()
        rows = self.load_manifest_docs()
        xlsx_cache = self.parse_sources(rows)
        self.load_existing_rulebook()
        self.parse_annex_xxiv(rows, xlsx_cache)
        self.parse_annex_xxv_instructions(rows)
        self.parse_validation_rules()
        self.seed_concepts_permissions_calculations_graph()
        self.conn.commit()
        self.write_outputs()
        print(json.dumps(self.c.__dict__, indent=2))

if __name__ == "__main__":
    Loader().run()
