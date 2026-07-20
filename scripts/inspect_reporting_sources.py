#!/usr/bin/env python3
"""Inspect local reporting source files for deterministic classification hints.

Inspection facts are stored in ``source_document_inspection`` only. They are not
written to graph nodes and they do not change graph node types.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect as connect_db

DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"

SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_document_inspection (
          source_id TEXT PRIMARY KEY,
          inspection_method TEXT NOT NULL,
          extracted_title TEXT,
          extracted_summary TEXT,
          first_page_text TEXT,
          workbook_sheets_json TEXT,
          taxonomy_manifest_json TEXT,
          lineage_json TEXT,
          classification_hint TEXT,
          confidence REAL NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def resolve_path(root: Path, local_path: str | None) -> Path | None:
    if not local_path:
        return None
    path = Path(local_path)
    if path.is_absolute():
        return path
    candidate = root / path
    if candidate.exists():
        return candidate
    project_candidate = ROOT / path
    if project_candidate.exists():
        return project_candidate
    return candidate


def workbook_sheet_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("xl/workbook.xml")
    except (KeyError, OSError, zipfile.BadZipFile):
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    return [
        str(sheet.attrib.get("name") or "").strip()
        for sheet in root.findall(f".//{SHEET_NS}sheet")
        if str(sheet.attrib.get("name") or "").strip()
    ]


def inspect_pdf(path: Path) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on local environment
        return {
            "inspection_method": "pypdf_unavailable",
            "first_page_text": "",
            "extracted_summary": str(exc),
            "classification_hint": "pdf_document",
            "confidence": 0.2,
        }
    try:
        reader = PdfReader(str(path))
        pages = reader.pages[:3]
        text = "\n".join((page.extract_text() or "") for page in pages)
    except Exception as exc:
        return {
            "inspection_method": "pypdf_error",
            "first_page_text": "",
            "extracted_summary": str(exc),
            "classification_hint": "pdf_document",
            "confidence": 0.2,
        }
    lower = text.lower()
    if re.search(r"\b(policy statement|consultation paper|supervisory statement|statement of policy)\b", lower):
        hint = "policy_pdf"
        confidence = 0.78
    elif re.search(r"\b(instruction|instructions|guidance|notes)\b", lower):
        hint = "instruction_pdf"
        confidence = 0.8
    elif re.search(r"\b(template|templates|data item|return form)\b", lower):
        hint = "template_pdf"
        confidence = 0.75
    else:
        hint = "pdf_document"
        confidence = 0.55 if text.strip() else 0.3
    return {
        "inspection_method": "pypdf_first_3_pages",
        "first_page_text": text[:12000],
        "extracted_summary": "Extracted first three PDF pages for classification.",
        "classification_hint": hint,
        "confidence": confidence,
    }


def inspect_workbook(path: Path) -> dict[str, Any]:
    sheets = workbook_sheet_names(path)
    haystack = " ".join(sheets).lower()
    has_return_code = bool(re.search(r"\b(pra\d{3}|fsa\d{3}|cor\s*\d{3}|c\s*\d{2,3}[._ ]\d{2})\b", haystack, re.I))
    hint = "template_workbook" if sheets else "other_source"
    confidence = 0.82 if has_return_code else 0.68 if sheets else 0.25
    return {
        "inspection_method": "xlsx_zip_workbook_xml",
        "workbook_sheets_json": json.dumps(sheets, sort_keys=True),
        "extracted_summary": f"Workbook sheet count: {len(sheets)}.",
        "classification_hint": hint,
        "confidence": confidence,
    }


def inspect_taxonomy_zip(path: Path, source_id: str) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted(zf.namelist())
    except (OSError, zipfile.BadZipFile):
        names = []
    manifest = {
        "files": names[:500],
        "file_count": len(names),
        "taxonomy_package_files": [name for name in names if name.lower().endswith("taxonomypackage.xml")],
        "entry_point_candidates": [
            name
            for name in names
            if name.lower().endswith((".xsd", ".xml"))
            and ("entry" in name.lower() or "module" in name.lower() or "taxonomy" in name.lower())
        ][:100],
    }
    return {
        "inspection_method": "zip_taxonomy_manifest",
        "taxonomy_manifest_json": json.dumps(manifest, sort_keys=True),
        "lineage_json": json.dumps({"source_id": source_id, "package_path": str(path)}, sort_keys=True),
        "extracted_summary": f"Taxonomy ZIP file count: {len(names)}.",
        "classification_hint": "taxonomy_package",
        "confidence": 0.88 if names else 0.35,
    }


def inspect_row(row: sqlite3.Row, root: Path) -> dict[str, Any] | None:
    file_type = (row["file_type"] or "").lower().strip()
    path = resolve_path(root, row["local_path"])
    if not path or not path.exists():
        return None
    base = {
        "source_id": row["source_id"],
        "extracted_title": row["title"],
        "lineage_json": json.dumps(
            {
                "source_id": row["source_id"],
                "url": row["url"],
                "parent_url": row["parent_url"],
                "local_path": row["local_path"],
            },
            sort_keys=True,
        ),
    }
    if file_type == "pdf":
        return {**base, **inspect_pdf(path)}
    if file_type in {"xlsx", "xlsm", "xltx"}:
        return {**base, **inspect_workbook(path)}
    if file_type == "zip":
        return {**base, **inspect_taxonomy_zip(path, row["source_id"])}
    return None


def upsert_inspection(conn: sqlite3.Connection, inspection: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO source_document_inspection(
          source_id,inspection_method,extracted_title,extracted_summary,first_page_text,
          workbook_sheets_json,taxonomy_manifest_json,lineage_json,classification_hint,
          confidence,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
          inspection_method=excluded.inspection_method,
          extracted_title=excluded.extracted_title,
          extracted_summary=excluded.extracted_summary,
          first_page_text=excluded.first_page_text,
          workbook_sheets_json=excluded.workbook_sheets_json,
          taxonomy_manifest_json=excluded.taxonomy_manifest_json,
          lineage_json=excluded.lineage_json,
          classification_hint=excluded.classification_hint,
          confidence=excluded.confidence,
          updated_at=excluded.updated_at
        """,
        (
            inspection["source_id"],
            inspection["inspection_method"],
            inspection.get("extracted_title"),
            inspection.get("extracted_summary"),
            inspection.get("first_page_text"),
            inspection.get("workbook_sheets_json"),
            inspection.get("taxonomy_manifest_json"),
            inspection.get("lineage_json"),
            inspection.get("classification_hint"),
            float(inspection.get("confidence") or 0.0),
            now,
        ),
    )


def inspect_reporting_sources(db_path: Path = DB_PATH, *, root: Path = ROOT, apply: bool = False) -> dict[str, Any]:
    conn = connect_db(db_path)
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT source_id,title,url,local_path,file_type,parent_url,checksum_sha256
        FROM source_document
        WHERE file_type IN ('pdf','xlsx','xlsm','xltx','zip')
        ORDER BY source_id
        """
    ).fetchall()
    inspections = [inspection for row in rows if (inspection := inspect_row(row, root))]
    if apply:
        for inspection in inspections:
            upsert_inspection(conn, inspection)
        conn.commit()
    by_hint: dict[str, int] = {}
    for inspection in inspections:
        hint = inspection.get("classification_hint") or "unknown"
        by_hint[hint] = by_hint.get(hint, 0) + 1
    conn.close()
    return {
        "status": "applied" if apply else "dry_run",
        "candidates": len(rows),
        "inspected": len(inspections),
        "by_hint": by_hint,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    print(json.dumps(inspect_reporting_sources(args.db, root=args.root, apply=args.apply), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
