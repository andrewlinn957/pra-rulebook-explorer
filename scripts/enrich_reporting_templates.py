#!/usr/bin/env python3
"""Enrich reporting template nodes with user-facing descriptions.

The reporting graph has many template nodes whose labels are only workbook/sheet
codes. This script builds a compact evidence bundle from parsed template rows,
columns and datapoints, optionally asks a low-cost LLM for a concise explanation,
and stores the result in SQLite for the reporting API/UI to display.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"
PROMPT_VERSION = "reporting-template-enrichment-v1"
DEFAULT_MODEL = os.environ.get("PRA_REPORTING_TEMPLATE_ENRICHMENT_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4.1-nano"
API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
BATCH_DIR = ROOT / "outputs" / "reporting-template-enrichment-batches"


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reporting_template_enrichment (
          template_id TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          purpose TEXT,
          contents TEXT,
          summary TEXT,
          key_rows_json TEXT NOT NULL DEFAULT '[]',
          quality_notes TEXT,
          response_json TEXT,
          error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (template_id) REFERENCES template(template_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reporting_template_enrichment_status ON reporting_template_enrichment(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reporting_template_enrichment_prompt ON reporting_template_enrichment(prompt_version,input_hash)")
    conn.commit()


def list_template_ids(conn: sqlite3.Connection, *, q: str = "", missing_only: bool = True, limit: int | None = None) -> list[str]:
    ensure_schema(conn)
    params: list[Any] = []
    where = "WHERE n.node_type='Template'"
    if q:
        where += " AND (n.node_id LIKE ? OR n.label LIKE ? OR n.properties_json LIKE ?)"
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    if missing_only:
        where += " AND NOT EXISTS (SELECT 1 FROM reporting_template_enrichment e WHERE e.template_id=n.node_id AND e.status='ok')"
    sql = f"""
        SELECT n.node_id AS template_id
        FROM graph_node n
        {where}
        ORDER BY n.label,n.node_id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row["template_id"] for row in conn.execute(sql, params).fetchall()]


def collect_template_context(conn: sqlite3.Connection, template_id: str) -> dict[str, Any]:
    template = conn.execute(
        """
        SELECT t.template_id,t.template_code,t.title,t.annex,t.source_id,
               sd.title AS source_title,sd.url AS source_url,sd.local_path AS source_local_path,sd.file_type AS source_file_type
        FROM template t
        LEFT JOIN source_document sd ON sd.source_id=t.source_id
        WHERE t.template_id=?
        """,
        (template_id,),
    ).fetchone()
    if template:
        template_data = {
            "template_id": template["template_id"],
            "template_code": template["template_code"],
            "title": template["title"] or template["template_code"] or template_id,
            "annex": template["annex"] or "",
            "source_id": template["source_id"],
            "source_title": template["source_title"] or "",
            "source_url": template["source_url"] or "",
            "source_local_path": template["source_local_path"] or "",
            "source_file_type": template["source_file_type"] or "",
        }
    else:
        template_data = graph_template_context(conn, template_id)

    rows = conn.execute(
        """
        SELECT row_code AS code,label
        FROM template_row
        WHERE template_id=? AND COALESCE(label,'') <> ''
        ORDER BY COALESCE(row_order, 999999), row_code
        LIMIT 80
        """,
        (template_id,),
    ).fetchall()
    columns = conn.execute(
        """
        SELECT column_code AS code,label
        FROM template_column
        WHERE template_id=? AND COALESCE(label,'') <> ''
        ORDER BY COALESCE(column_order, 999999), column_code
        LIMIT 40
        """,
        (template_id,),
    ).fetchall()
    datapoints = conn.execute(
        """
        SELECT DISTINCT concept_label
        FROM datapoint
        WHERE template_id=? AND COALESCE(concept_label,'') <> ''
        ORDER BY concept_label
        LIMIT 40
        """,
        (template_id,),
    ).fetchall()

    workbook_entry: dict[str, str] = {}
    parsed_rows = [{"code": r["code"] or "", "label": r["label"] or ""} for r in rows]
    parsed_columns = [{"code": r["code"] or "", "label": r["label"] or ""} for r in columns]
    if not parsed_rows and str(template_data.get("source_file_type") or "").lower() in {"xlsx", "xlsm", "xltx"}:
        workbook_entry = lookup_workbook_index_entry(template_data.get("source_local_path") or "", template_data["title"], template_id)
        sheet = parse_template_sheet_from_workbook(template_data.get("source_local_path") or "", template_data["title"], template_id)
        parsed_rows = sheet.get("rows", [])
        parsed_columns = sheet.get("columns", [])

    return {
        "template_id": template_data["template_id"],
        "template_code": template_data["template_code"],
        "title": template_data["title"] or template_data["template_code"] or template_id,
        "annex": template_data["annex"] or "",
        "source": {
            "source_id": template_data["source_id"],
            "title": template_data["source_title"] or "",
            "url": template_data["source_url"] or "",
            "local_path": template_data["source_local_path"] or "",
            "file_type": template_data["source_file_type"] or "",
        },
        "workbook_index": workbook_entry,
        "rows": parsed_rows,
        "columns": parsed_columns,
        "datapoint_labels": [r["concept_label"] for r in datapoints if r["concept_label"]],
    }


def lookup_workbook_index_entry(local_path: str, title: str, template_id: str) -> dict[str, str]:
    path = Path(local_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return {}
    candidates = {normalise_template_number(c) for c in sheet_name_candidates(title, template_id)}
    candidates.discard("")
    for row in workbook_index_rows(str(path)):
        if row.get("number") in candidates:
            return row
    return {}


@lru_cache(maxsize=64)
def workbook_index_rows(path_str: str) -> tuple[dict[str, str], ...]:
    path = Path(path_str)
    try:
        rows = read_xlsx_sheet_rows(path, ["Index", "Contents"], limit=500)
    except Exception:
        return ()
    out: list[dict[str, str]] = []
    current_group = ""
    for cells in rows:
        if len(cells) == 1 and not re.search(r"\d", cells[0]):
            current_group = clean_text(cells[0], 180)
            continue
        if len(cells) >= 3:
            number = normalise_template_number(cells[0])
            code = clean_text(cells[1], 80)
            name = clean_text(cells[2], 260)
            if number and name and (re.search(r"\d", cells[0]) or code.startswith(("F ", "C ", "P "))):
                out.append({"number": number, "code": code, "name": name, "group": current_group})
    return tuple(out)


def normalise_template_number(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^[A-Za-z_ -]+", "", text)
    text = text.replace("_", ".")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        if abs(number - round(number)) < 1e-9:
            return str(int(round(number)))
        return (f"{number:.6f}".rstrip("0").rstrip("."))
    match = re.search(r"\d+(?:\.\d+)*", text)
    return match.group(0).rstrip(".") if match else ""


def parse_template_sheet_from_workbook(local_path: str, title: str, template_id: str) -> dict[str, list[dict[str, str]]]:
    path = Path(local_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists() or path.suffix.lower() not in {".xlsx", ".xlsm", ".xltx"}:
        return {"rows": [], "columns": []}
    try:
        rows = read_xlsx_sheet_rows(path, sheet_name_candidates(title, template_id), limit=90)
    except Exception:
        return {"rows": [], "columns": []}
    labels: list[dict[str, str]] = []
    columns: list[dict[str, str]] = []
    for row in rows:
        cells = [c for c in row if c]
        if not cells:
            continue
        code = cells[0] if re.fullmatch(r"[A-Za-z]?\d{3,5}", cells[0]) else ""
        label_cells = cells[1:8] if code else cells[:8]
        label = " | ".join(label_cells).strip()
        if label and len(label) > 2:
            labels.append({"code": code, "label": label[:500]})
        for cell in cells:
            if re.fullmatch(r"[A-Za-z]?\d{3,5}", cell) and cell != code:
                columns.append({"code": cell, "label": " | ".join(cells[:8])[:300]})
        if len(labels) >= 60:
            break
    return {"rows": unique_dicts(labels, "label")[:60], "columns": unique_dicts(columns, "code")[:30]}


def sheet_name_candidates(title: str, template_id: str) -> list[str]:
    values = [title]
    tail = template_id.rsplit("_", 1)[-1]
    if tail and tail != template_id:
        values.append(tail)
    values.extend(re.findall(r"\b\d+(?:\.\d+)?\b", title))
    values.extend(re.findall(r"\b\d+(?:\.\d+)?\b", template_id))
    return unique_preserve_order(values)


def unique_dicts(rows: list[dict[str, str]], key: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        value = row.get(key, "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(row)
    return out


def read_xlsx_sheet_rows(path: Path, candidates: list[str], *, limit: int = 90) -> list[list[str]]:
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    with zipfile.ZipFile(path) as zf:
        shared = read_shared_strings(zf, ns_main)
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {rel.attrib.get("Id"): rel.attrib.get("Target", "") for rel in rels}
        candidate_lc = [c.lower() for c in candidates if c]
        selected = None
        for sheet in workbook.findall(f".//{ns_main}sheet"):
            name = sheet.attrib.get("name", "")
            if any(name.lower() == c or name.lower().endswith(c) for c in candidate_lc):
                selected = sheet
                break
        if selected is None:
            selected = workbook.find(f".//{ns_main}sheet")
        if selected is None:
            return []
        target = rid_to_target.get(selected.attrib.get(f"{ns_rel}id"), "")
        if not target:
            return []
        sheet_path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
        root = ET.fromstring(zf.read(sheet_path))
        out: list[list[str]] = []
        for row in root.findall(f".//{ns_main}row"):
            vals = [cell_value(cell, shared, ns_main) for cell in row.findall(f"{ns_main}c")]
            vals = [" ".join(v.split()) for v in vals if " ".join(v.split())]
            if vals:
                out.append(vals)
            if len(out) >= limit:
                break
        return out


def read_shared_strings(zf: zipfile.ZipFile, ns_main: str) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{ns_main}t")) for si in root.findall(f"{ns_main}si")]


def cell_value(cell: ET.Element, shared: list[str], ns_main: str) -> str:
    typ = cell.attrib.get("t")
    if typ == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{ns_main}t"))
    value = cell.find(f"{ns_main}v")
    formula = cell.find(f"{ns_main}f")
    if value is None and formula is None:
        return ""
    if typ == "s" and value is not None:
        try:
            return shared[int(value.text or "0")]
        except Exception:
            return value.text or ""
    if formula is not None and value is None:
        return "=" + (formula.text or "")
    return value.text if value is not None else ""


def graph_template_context(conn: sqlite3.Connection, template_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT n.node_id,n.label,n.properties_json,
               sd.source_id,sd.title AS source_title,sd.url AS source_url,sd.local_path AS source_local_path,sd.file_type AS source_file_type
        FROM graph_node n
        LEFT JOIN source_document sd ON sd.source_id=json_extract(n.properties_json,'$.source_id')
        WHERE n.node_id=? AND n.node_type='Template'
        """,
        (template_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown template_id: {template_id}")
    props = json.loads(row["properties_json"] or "{}")
    code = str(props.get("data_item_code") or props.get("domain") or row["label"] or template_id).split()[0]
    return {
        "template_id": row["node_id"],
        "template_code": code,
        "title": row["label"] or row["node_id"],
        "annex": props.get("annex") or "",
        "source_id": row["source_id"] or props.get("source_id") or "",
        "source_title": row["source_title"] or "",
        "source_url": row["source_url"] or "",
        "source_local_path": row["source_local_path"] or "",
        "source_file_type": row["source_file_type"] or "",
    }


def context_hash(context: dict[str, Any]) -> str:
    payload = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_fallback_enrichment(context: dict[str, Any]) -> dict[str, Any]:
    title = context.get("title") or context.get("template_code") or context.get("template_id")
    index_entry = context.get("workbook_index") or {}
    index_name = index_entry.get("name") or ""
    group_name = index_entry.get("group") or ""
    row_labels = [r.get("label", "") for r in context.get("rows", []) if r.get("label")]
    analytical_rows = [r.get("label", "") for r in context.get("rows", []) if r.get("code") and r.get("label")]
    column_labels = [c.get("label", "") for c in context.get("columns", []) if c.get("label")]
    datapoints = [x for x in context.get("datapoint_labels", []) if x]
    key_rows = unique_preserve_order([strip_reference_columns(x) for x in (analytical_rows or row_labels) if useful_row_label(x)])[:6]
    subject = index_name or title
    contents_bits = []
    if key_rows:
        contents_bits.append("Covers " + "; ".join(key_rows[:4]))
    elif row_labels:
        contents_bits.append("Rows include " + "; ".join([strip_reference_columns(x) for x in row_labels[:4]]))
    if column_labels:
        contents_bits.append("Columns include " + "; ".join([strip_reference_columns(x) for x in column_labels[:3]]))
    if not contents_bits and datapoints:
        contents_bits.append("Datapoints include " + "; ".join(datapoints[:4]))
    if not contents_bits:
        contents_bits.append("Parsed workbook metadata is limited for this sheet.")
    purpose_parts = [f"Reports {subject}"]
    if context.get("template_code"):
        purpose_parts.append(f"within {context['template_code']}")
    if group_name:
        purpose_parts.append(f"under {group_name}")
    purpose = " ".join(purpose_parts) + "."
    return {
        "purpose": purpose,
        "contents": " ".join(contents_bits),
        "summary": f"{title}: {subject}. " + (contents_bits[0] if contents_bits else "Reporting template sheet."),
        "key_rows": key_rows,
        "quality_notes": "Deterministic fallback from workbook index, parsed row labels, columns and datapoints; no LLM judgement used.",
    }


def strip_reference_columns(label: str) -> str:
    parts = [p.strip() for p in str(label).split(" | ") if p.strip()]
    if not parts:
        return ""
    # Keep the business label and drop legal/reference columns that make UI subtitles noisy.
    return parts[0]


def useful_row_label(label: str) -> bool:
    text = strip_reference_columns(label).lower()
    if len(text) < 3:
        return False
    noisy = ("reference", "annex ", "breakdown in table", "carrying amount")
    return not any(token in text for token in noisy)


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


def prompt_messages(context: dict[str, Any]) -> list[dict[str, str]]:
    compact = {
        "template_id": context["template_id"],
        "template_code": context["template_code"],
        "title": context["title"],
        "annex": context["annex"],
        "source_title": context["source"].get("title"),
        "rows": context["rows"][:45],
        "columns": context["columns"][:25],
        "datapoint_labels": context["datapoint_labels"][:25],
    }
    return [
        {
            "role": "system",
            "content": (
                "You explain UK PRA regulatory reporting templates for expert regulatory users. "
                "Use only the supplied workbook-derived evidence. Be concise and concrete. "
                "Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Summarise what this reporting template sheet does. Return JSON with keys: "
                "purpose (one sentence), contents (one sentence suitable as a UI subtitle), "
                "summary (two short sentences), key_rows (array of up to 6 important row/section labels), "
                "quality_notes (one sentence noting any evidence limits).\n\n"
                + json.dumps(compact, ensure_ascii=False)
            ),
        },
    ]


def call_llm(context: dict[str, Any], *, model: str = DEFAULT_MODEL, timeout: int = 45) -> tuple[dict[str, Any], dict[str, Any]]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    response = requests.post(
        f"{API_BASE}/chat/completions",
        headers=api_headers(json_body=True),
        json=chat_completion_body(context, model=model),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    return normalise_enrichment(parsed), payload


def api_headers(*, json_body: bool = False) -> dict[str, str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    headers = {"Authorization": f"Bearer {key}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def chat_completion_body(context: dict[str, Any], *, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": prompt_messages(context),
    }


def batch_request_line(template_id: str, context: dict[str, Any], *, model: str) -> dict[str, Any]:
    return {
        "custom_id": template_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": chat_completion_body(context, model=model),
    }


def create_batch_input_file(
    conn: sqlite3.Connection,
    *,
    output_path: Path | None = None,
    q: str = "",
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    missing_only: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    ensure_schema(conn)
    ids = list_template_ids(conn, q=q, missing_only=(missing_only and not force), limit=limit)
    if output_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = BATCH_DIR / f"reporting-template-enrichment-{stamp}.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []
    with output_path.open("w", encoding="utf-8") as fh:
        for template_id in ids:
            context = collect_template_context(conn, template_id)
            digest = context_hash(context)
            fh.write(json.dumps(batch_request_line(template_id, context, model=model), ensure_ascii=False) + "\n")
            manifest.append({"template_id": template_id, "input_hash": digest})
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps({"model": model, "prompt_version": PROMPT_VERSION, "templates": manifest}, indent=2), encoding="utf-8")
    return {"path": str(output_path), "manifest_path": str(manifest_path), "count": len(manifest), "model": model}


def submit_batch_input_file(path: Path, *, metadata: dict[str, str] | None = None, timeout: int = 60) -> dict[str, Any]:
    with path.open("rb") as fh:
        uploaded = requests.post(
            f"{API_BASE}/files",
            headers=api_headers(),
            data={"purpose": "batch"},
            files={"file": (path.name, fh, "application/jsonl")},
            timeout=timeout,
        )
    if uploaded.status_code >= 400:
        raise RuntimeError(f"File upload failed HTTP {uploaded.status_code}: {uploaded.text[:500]}")
    file_payload = uploaded.json()
    batch = requests.post(
        f"{API_BASE}/batches",
        headers=api_headers(json_body=True),
        json={
            "input_file_id": file_payload["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "metadata": metadata or {"purpose": "pra-reporting-template-enrichment", "prompt_version": PROMPT_VERSION},
        },
        timeout=timeout,
    )
    if batch.status_code >= 400:
        raise RuntimeError(f"Batch creation failed HTTP {batch.status_code}: {batch.text[:500]}")
    return {"input_file": file_payload, "batch": batch.json()}


def get_batch(batch_id: str, *, timeout: int = 30) -> dict[str, Any]:
    response = requests.get(f"{API_BASE}/batches/{batch_id}", headers=api_headers(), timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Batch status failed HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def download_file_content(file_id: str, *, timeout: int = 120) -> str:
    response = requests.get(f"{API_BASE}/files/{file_id}/content", headers=api_headers(), timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"File download failed HTTP {response.status_code}: {response.text[:500]}")
    return response.text


def import_batch_results(conn: sqlite3.Connection, *, batch_id: str = "", input_path: Path | None = None, results_path: Path | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    ensure_schema(conn)
    batch_payload: dict[str, Any] = {}
    if results_path is None:
        if not batch_id:
            raise ValueError("batch_id or results_path is required")
        batch_payload = get_batch(batch_id)
        if batch_payload.get("status") != "completed" or not batch_payload.get("output_file_id"):
            return {"status": batch_payload.get("status"), "imported": 0, "failed": 0, "batch": batch_payload}
        content = download_file_content(batch_payload["output_file_id"])
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        results_path = BATCH_DIR / f"{batch_id}-output.jsonl"
        results_path.write_text(content, encoding="utf-8")
    imported = 0
    failed = 0
    with results_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            result = json.loads(raw)
            template_id = result.get("custom_id") or ""
            if not template_id:
                failed += 1
                continue
            context = collect_template_context(conn, template_id)
            digest = context_hash(context)
            response = result.get("response") or {}
            body = response.get("body") or {}
            if response.get("status_code") == 200 and body.get("choices"):
                content = body["choices"][0]["message"]["content"]
                enrichment = normalise_enrichment(json.loads(content))
                if not enrichment.get("contents"):
                    enrichment = build_fallback_enrichment(context) | {k: v for k, v in enrichment.items() if v}
                store_enrichment(conn, template_id=template_id, model=model, prompt_version=PROMPT_VERSION, input_hash=digest, status="ok", enrichment=enrichment, response={"batch_id": batch_id, "result": result}, error="")
                imported += 1
            else:
                fallback = build_fallback_enrichment(context)
                store_enrichment(conn, template_id=template_id, model=model, prompt_version=PROMPT_VERSION, input_hash=digest, status="failed", enrichment=fallback, response={"batch_id": batch_id, "result": result}, error=json.dumps(result.get("error") or response, ensure_ascii=False)[:1000])
                failed += 1
    return {"status": batch_payload.get("status") or "local", "imported": imported, "failed": failed, "results_path": str(results_path)}


def normalise_enrichment(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "purpose": clean_text(value.get("purpose"), 260),
        "contents": clean_text(value.get("contents"), 320),
        "summary": clean_text(value.get("summary"), 520),
        "key_rows": unique_preserve_order([str(x) for x in value.get("key_rows", []) if x])[:6] if isinstance(value.get("key_rows"), list) else [],
        "quality_notes": clean_text(value.get("quality_notes"), 280),
    }


def clean_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text[: max_chars - 1] + "…" if len(text) > max_chars else text


def store_enrichment(
    conn: sqlite3.Connection,
    *,
    template_id: str,
    model: str,
    prompt_version: str,
    input_hash: str,
    status: str,
    enrichment: dict[str, Any] | None,
    response: dict[str, Any] | None,
    error: str = "",
) -> None:
    enrichment = enrichment or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO reporting_template_enrichment(
          template_id,model,prompt_version,input_hash,status,purpose,contents,summary,key_rows_json,quality_notes,response_json,error,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(template_id) DO UPDATE SET
          model=excluded.model,
          prompt_version=excluded.prompt_version,
          input_hash=excluded.input_hash,
          status=excluded.status,
          purpose=excluded.purpose,
          contents=excluded.contents,
          summary=excluded.summary,
          key_rows_json=excluded.key_rows_json,
          quality_notes=excluded.quality_notes,
          response_json=excluded.response_json,
          error=excluded.error,
          updated_at=excluded.updated_at
        """,
        (
            template_id,
            model,
            prompt_version,
            input_hash,
            status,
            enrichment.get("purpose") or "",
            enrichment.get("contents") or "",
            enrichment.get("summary") or "",
            json.dumps(enrichment.get("key_rows") or [], ensure_ascii=False),
            enrichment.get("quality_notes") or "",
            json.dumps(response or {}, ensure_ascii=False),
            error,
            now,
            now,
        ),
    )
    conn.commit()


def enrich_templates(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    missing_only: bool = True,
    no_llm: bool = False,
    force: bool = False,
    sleep_seconds: float = 0.0,
) -> dict[str, int]:
    ensure_schema(conn)
    ids = list_template_ids(conn, q=q, missing_only=(missing_only and not force), limit=limit)
    counts = {"total": len(ids), "ok": 0, "fallback": 0, "failed": 0, "skipped": 0}
    for template_id in ids:
        context = collect_template_context(conn, template_id)
        digest = context_hash(context)
        existing = conn.execute(
            "SELECT input_hash,status FROM reporting_template_enrichment WHERE template_id=? AND prompt_version=?",
            (template_id, PROMPT_VERSION),
        ).fetchone()
        if existing and existing["input_hash"] == digest and existing["status"] == "ok" and not force:
            counts["skipped"] += 1
            continue
        try:
            if no_llm:
                enrichment = build_fallback_enrichment(context)
                store_enrichment(conn, template_id=template_id, model="deterministic-fallback", prompt_version=PROMPT_VERSION, input_hash=digest, status="ok", enrichment=enrichment, response={"context_hash": digest}, error="")
                counts["fallback"] += 1
            else:
                enrichment, response = call_llm(context, model=model)
                if not enrichment.get("contents"):
                    enrichment = build_fallback_enrichment(context) | {k: v for k, v in enrichment.items() if v}
                store_enrichment(conn, template_id=template_id, model=model, prompt_version=PROMPT_VERSION, input_hash=digest, status="ok", enrichment=enrichment, response=response, error="")
                counts["ok"] += 1
        except Exception as exc:  # pragma: no cover - exercised manually against provider/network failures
            fallback = build_fallback_enrichment(context)
            store_enrichment(conn, template_id=template_id, model=model, prompt_version=PROMPT_VERSION, input_hash=digest, status="failed", enrichment=fallback, response={"context_hash": digest}, error=f"{type(exc).__name__}: {exc}")
            counts["failed"] += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich PRA reporting template nodes with user-facing summaries")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--q", default="", help="Only enrich template ids/codes/titles matching this text")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--force", action="store_true", help="Recompute existing ok enrichments")
    parser.add_argument("--all", action="store_true", help="Include templates that already have ok enrichment")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic summaries instead of the OpenAI API")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between API calls")
    parser.add_argument("--batch-create", action="store_true", help="Write an OpenAI Batch JSONL input file instead of calling the API synchronously")
    parser.add_argument("--batch-submit", type=Path, help="Upload an existing Batch JSONL file and create a 24h OpenAI Batch job")
    parser.add_argument("--batch-status", help="Fetch OpenAI Batch status by id")
    parser.add_argument("--batch-import", help="Download and import a completed OpenAI Batch by id")
    parser.add_argument("--batch-results", type=Path, help="Import a local Batch output JSONL file instead of downloading by id")
    parser.add_argument("--output", type=Path, help="Output path for --batch-create")
    args = parser.parse_args(argv)

    if args.batch_submit:
        result = submit_batch_input_file(args.batch_submit, metadata={"purpose": "pra-reporting-template-enrichment", "prompt_version": PROMPT_VERSION, "model": args.model})
        print(json.dumps(result, indent=2))
        return 0
    if args.batch_status:
        print(json.dumps(get_batch(args.batch_status), indent=2))
        return 0

    conn = connect(args.db)
    try:
        if args.batch_create:
            result = create_batch_input_file(conn, output_path=args.output, q=args.q, limit=args.limit, model=args.model, missing_only=not args.all, force=args.force)
            print(json.dumps(result, indent=2))
            return 0
        if args.batch_import or args.batch_results:
            result = import_batch_results(conn, batch_id=args.batch_import or "", results_path=args.batch_results, model=args.model)
            print(json.dumps(result, indent=2))
            return 0 if result.get("failed", 0) == 0 else 1
        counts = enrich_templates(conn, q=args.q, limit=args.limit, model=args.model, missing_only=not args.all, no_llm=args.no_llm, force=args.force, sleep_seconds=args.sleep)
    finally:
        conn.close()
    print(json.dumps({"model": args.model if not args.no_llm else "deterministic-fallback", **counts}, indent=2))
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
