#!/usr/bin/env python3
"""Audit reporting graph nodes for semantic category/source mismatches.

This is aimed at failures such as a PDF instruction artefact being presented as
TemplateSet, or the same source URL being materialised as both an instruction
set and an annex SourceDocument in user-facing reporting navigation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"
BATCH_DIR = ROOT / "outputs" / "reporting-node-audit-batches"
PROMPT_VERSION = "reporting-node-audit-v1"
DEFAULT_MODEL = os.environ.get("PRA_REPORTING_NODE_AUDIT_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4.1-nano"
API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TAXONOMY_TYPES = {"xml", "xsd", "zip", "xbrl"}
LLM_NODE_TYPES = {
    "DataItem", "ReportingObligation", "Template", "TemplateSet", "InstructionSet",
    "SourceDocument", "ExternalReference", "Provision", "LegalInstrument",
    "PolicyStatement", "Concept", "ScopeRule", "FirmType", "Permission", "ValidationRule",
}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def api_headers(*, json_body: bool = False) -> dict[str, str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    headers = {"Authorization": f"Bearer {key}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reporting_node_audit (
          node_id TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          expected_category TEXT,
          source_category TEXT,
          issue_type TEXT,
          severity TEXT,
          confidence REAL,
          finding TEXT,
          recommended_action TEXT,
          duplicate_of TEXT,
          response_json TEXT,
          error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reporting_node_audit_issue ON reporting_node_audit(issue_type,severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reporting_node_audit_prompt ON reporting_node_audit(prompt_version,input_hash)")
    conn.commit()


def _json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def normalise_url(url: str) -> str:
    return re.sub(r"/$", "", re.sub(r"[?#].*$", "", str(url or "").strip().lower()))


def source_file_type(row: sqlite3.Row, props: dict[str, Any]) -> str:
    return str(row["sd_file_type"] or props.get("file_type") or props.get("source_file_type") or "").lower()


def list_audit_node_ids(conn: sqlite3.Connection, *, include_leaf_nodes: bool = False, limit: int | None = None) -> list[str]:
    leaf_types = set() if include_leaf_nodes else {"DataPoint", "TemplateRow", "TemplateColumn", "DataPointGroup"}
    rows = conn.execute(
        """
        SELECT n.node_id,n.node_type,n.properties_json,sd.file_type AS sd_file_type,sd.url AS sd_url
        FROM graph_node n
        LEFT JOIN source_document sd ON sd.source_id=n.source_pk OR sd.source_id=replace(n.node_id,'source_document:','')
        WHERE n.node_type IN ({})
        ORDER BY n.node_type,n.node_id
        """.format(",".join("?" for _ in LLM_NODE_TYPES)),
        sorted(LLM_NODE_TYPES),
    ).fetchall()
    ids: list[str] = []
    for row in rows:
        if row["node_type"] in leaf_types:
            continue
        props = _json(row["properties_json"])
        ft = source_file_type(row, props)
        hay = " ".join(str(v or "") for v in [row["sd_url"], props.get("source_url"), props.get("url"), props.get("source_local_path"), props.get("local_path")]).lower()
        if ft in TAXONOMY_TYPES or re.search(r"\.(xml|xsd|zip|xbrl)(?:[?#]|$)", hay):
            continue
        ids.append(row["node_id"])
        if limit and len(ids) >= limit:
            break
    return ids


def collect_context(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT n.node_id,n.node_type,n.label,n.source_table,n.source_pk,n.properties_json,
               sd.source_id AS sd_source_id,sd.title AS sd_title,sd.url AS sd_url,sd.local_path AS sd_local_path,sd.file_type AS sd_file_type
        FROM graph_node n
        LEFT JOIN source_document sd ON sd.source_id=n.source_pk OR sd.source_id=replace(n.node_id,'source_document:','')
        WHERE n.node_id=?
        """,
        (node_id,),
    ).fetchone()
    if not row:
        raise KeyError(node_id)
    props = _json(row["properties_json"])
    source_ids = []
    for sid in props.get("source_document_ids") or []:
        if sid and sid not in source_ids:
            source_ids.append(str(sid))
    if row["sd_source_id"] and row["sd_source_id"] not in source_ids:
        source_ids.append(row["sd_source_id"])
    source_docs = []
    if source_ids:
        docs = conn.execute(
            f"SELECT source_id,title,url,local_path,file_type FROM source_document WHERE source_id IN ({','.join('?' for _ in source_ids)})",
            source_ids,
        ).fetchall()
        for doc in docs:
            ft = str(doc["file_type"] or "").lower()
            if ft in TAXONOMY_TYPES:
                continue
            spans = conn.execute(
                """
                SELECT page_number,sheet_name,heading_path,raw_text
                FROM source_span
                WHERE source_id=? AND COALESCE(raw_text,'')<>''
                ORDER BY page_number,row_number,column_number
                LIMIT 8
                """,
                (doc["source_id"],),
            ).fetchall()
            source_docs.append({
                "source_id": doc["source_id"],
                "title": doc["title"],
                "url": doc["url"],
                "local_path": doc["local_path"],
                "file_type": ft,
                "sample_text": [re.sub(r"\s+", " ", str(s["raw_text"] or "")).strip()[:500] for s in spans],
            })
    edges = conn.execute(
        """
        SELECT edge_type,source_node_id,target_node_id,confidence,extraction_method
        FROM graph_edge
        WHERE source_node_id=? OR target_node_id=?
        ORDER BY edge_type,confidence DESC
        LIMIT 30
        """,
        (node_id, node_id),
    ).fetchall()
    return {
        "node": {
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "label": row["label"],
            "source_table": row["source_table"],
            "source_pk": row["source_pk"],
            "properties": props,
        },
        "source_documents": source_docs,
        "edges": [dict(e) for e in edges],
    }


def context_hash(context: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prompt_messages(context: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": (
            "You audit PRA reporting graph nodes for user-facing category/source quality. "
            "Find semantic mismatches such as a PDF instruction source being represented as a template set, duplicate source URLs represented by both an abstract artefact node and an annex SourceDocument, or wrong categories. "
            "Do not classify taxonomy/XML/XSD artefacts; if only taxonomy context appears, return no_issue. "
            "Return strict JSON only with keys: expected_category, source_category, issue_type, severity, confidence, finding, recommended_action, duplicate_of. "
            "issue_type must be one of no_issue, wrong_node_type, duplicate_source, wrong_category, missing_source, stale_or_superseded, insufficient_context. "
            "severity must be one of none, low, medium, high. Categories should be one of return, template_workbook, instructions_guidance_pdf, source_document, legal_reference, validation, concept_scope, other."
        )},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)[:18000]},
    ]


def chat_body(context: dict[str, Any], *, model: str) -> dict[str, Any]:
    return {"model": model, "temperature": 0, "response_format": {"type": "json_object"}, "messages": prompt_messages(context)}


def batch_request_line(node_id: str, context: dict[str, Any], *, model: str) -> dict[str, Any]:
    return {"custom_id": node_id, "method": "POST", "url": "/v1/chat/completions", "body": chat_body(context, model=model)}


def create_batch(conn: sqlite3.Connection, *, model: str, limit: int | None, include_leaf_nodes: bool, output_path: Path | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    ids = list_audit_node_ids(conn, include_leaf_nodes=include_leaf_nodes, limit=limit)
    if output_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = BATCH_DIR / f"reporting-node-audit-{stamp}.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with output_path.open("w", encoding="utf-8") as fh:
        for node_id in ids:
            context = collect_context(conn, node_id)
            digest = context_hash(context)
            fh.write(json.dumps(batch_request_line(node_id, context, model=model), ensure_ascii=False) + "\n")
            manifest.append({"node_id": node_id, "input_hash": digest})
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps({"model": model, "prompt_version": PROMPT_VERSION, "nodes": manifest}, indent=2), encoding="utf-8")
    return {"path": str(output_path), "manifest_path": str(manifest_path), "count": len(manifest), "model": model}


def submit_batch(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        uploaded = requests.post(f"{API_BASE}/files", headers=api_headers(), data={"purpose": "batch"}, files={"file": (path.name, fh, "application/jsonl")}, timeout=120)
    if uploaded.status_code >= 400:
        raise RuntimeError(f"File upload failed HTTP {uploaded.status_code}: {uploaded.text[:500]}")
    file_payload = uploaded.json()
    batch = requests.post(
        f"{API_BASE}/batches",
        headers=api_headers(json_body=True),
        json={"input_file_id": file_payload["id"], "endpoint": "/v1/chat/completions", "completion_window": "24h", "metadata": {"purpose": "pra-reporting-node-audit", "prompt_version": PROMPT_VERSION}},
        timeout=120,
    )
    if batch.status_code >= 400:
        raise RuntimeError(f"Batch creation failed HTTP {batch.status_code}: {batch.text[:500]}")
    return {"input_file": file_payload, "batch": batch.json()}


def get_batch(batch_id: str) -> dict[str, Any]:
    r = requests.get(f"{API_BASE}/batches/{batch_id}", headers=api_headers(), timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Batch status failed HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def download_file(file_id: str) -> str:
    r = requests.get(f"{API_BASE}/files/{file_id}/content", headers=api_headers(), timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"File download failed HTTP {r.status_code}: {r.text[:500]}")
    return r.text


def import_results(conn: sqlite3.Connection, *, batch_id: str = "", results_path: Path | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    ensure_schema(conn)
    batch = {}
    if results_path is None:
        batch = get_batch(batch_id)
        if batch.get("status") != "completed" or not batch.get("output_file_id"):
            return {"status": batch.get("status"), "imported": 0, "failed": 0, "batch": batch}
        content = download_file(batch["output_file_id"])
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        results_path = BATCH_DIR / f"{batch_id}-output.jsonl"
        results_path.write_text(content, encoding="utf-8")
    imported = failed = 0
    with results_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            result = json.loads(raw)
            node_id = result.get("custom_id") or ""
            try:
                context = collect_context(conn, node_id)
                digest = context_hash(context)
                response = result.get("response") or {}
                body = response.get("body") or {}
                if response.get("status_code") == 200 and body.get("choices"):
                    audit = json.loads(body["choices"][0]["message"]["content"])
                    conn.execute(
                        """
                        INSERT INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,expected_category,source_category,issue_type,severity,confidence,finding,recommended_action,duplicate_of,response_json,error,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                        ON CONFLICT(node_id) DO UPDATE SET model=excluded.model,prompt_version=excluded.prompt_version,input_hash=excluded.input_hash,status=excluded.status,expected_category=excluded.expected_category,source_category=excluded.source_category,issue_type=excluded.issue_type,severity=excluded.severity,confidence=excluded.confidence,finding=excluded.finding,recommended_action=excluded.recommended_action,duplicate_of=excluded.duplicate_of,response_json=excluded.response_json,error=excluded.error,updated_at=CURRENT_TIMESTAMP
                        """,
                        (node_id, model, PROMPT_VERSION, digest, "ok", audit.get("expected_category"), audit.get("source_category"), audit.get("issue_type"), audit.get("severity"), audit.get("confidence"), audit.get("finding"), audit.get("recommended_action"), audit.get("duplicate_of"), json.dumps(audit, ensure_ascii=False), None),
                    )
                    imported += 1
                else:
                    conn.execute("INSERT OR REPLACE INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,error) VALUES (?,?,?,?,?,?)", (node_id, model, PROMPT_VERSION, digest, "failed", json.dumps(result)[:1000]))
                    failed += 1
            except Exception as exc:
                failed += 1
                conn.execute("INSERT OR REPLACE INTO reporting_node_audit(node_id,model,prompt_version,input_hash,status,error) VALUES (?,?,?,?,?,?)", (node_id or "unknown", model, PROMPT_VERSION, "", "failed", str(exc)[:1000]))
    conn.commit()
    return {"status": batch.get("status", "imported_from_file"), "imported": imported, "failed": failed, "results_path": str(results_path)}


def deterministic_findings(conn: sqlite3.Connection) -> dict[str, Any]:
    # Whole-graph cheap checks for this failure class.
    pdf_template_sets = conn.execute(
        """
        SELECT n.node_id,n.label,sd.title,sd.url
        FROM graph_node n
        JOIN json_each(json_extract(n.properties_json,'$.source_document_ids')) sid
        JOIN source_document sd ON sd.source_id=sid.value
        WHERE n.node_type='TemplateSet' AND lower(sd.file_type)='pdf'
        ORDER BY n.node_id
        """
    ).fetchall()
    duplicate_urls = conn.execute(
        """
        WITH node_urls AS (
          SELECT n.node_id,n.node_type,lower(rtrim(COALESCE(sd.url,json_extract(n.properties_json,'$.source_url'),json_extract(n.properties_json,'$.url')), '/')) AS url
          FROM graph_node n
          LEFT JOIN source_document sd ON sd.source_id=n.source_pk OR sd.source_id=replace(n.node_id,'source_document:','')
        )
        SELECT url, COUNT(*) AS c, group_concat(node_type || ':' || node_id, ' | ') AS nodes
        FROM node_urls
        WHERE url LIKE 'http%' AND node_type IN ('TemplateSet','InstructionSet','SourceDocument','Template')
        GROUP BY url HAVING COUNT(*)>1
        ORDER BY c DESC,url
        """
    ).fetchall()
    return {
        "pdf_template_set_edges": [dict(r) for r in pdf_template_sets],
        "duplicate_user_facing_urls": [dict(r) for r in duplicate_urls],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--include-leaf-nodes", action="store_true")
    ap.add_argument("--batch-create", action="store_true")
    ap.add_argument("--batch-submit", type=Path)
    ap.add_argument("--batch-status")
    ap.add_argument("--batch-import")
    ap.add_argument("--results-path", type=Path)
    ap.add_argument("--deterministic", action="store_true")
    args = ap.parse_args()
    conn = connect(args.db)
    if args.deterministic:
        print(json.dumps(deterministic_findings(conn), indent=2))
        return 0
    if args.batch_create:
        print(json.dumps(create_batch(conn, model=args.model, limit=args.limit, include_leaf_nodes=args.include_leaf_nodes), indent=2))
        return 0
    if args.batch_submit:
        print(json.dumps(submit_batch(args.batch_submit), indent=2))
        return 0
    if args.batch_status:
        print(json.dumps(get_batch(args.batch_status), indent=2))
        return 0
    if args.batch_import or args.results_path:
        print(json.dumps(import_results(conn, batch_id=args.batch_import or "", results_path=args.results_path, model=args.model), indent=2))
        return 0
    ids = list_audit_node_ids(conn, include_leaf_nodes=args.include_leaf_nodes, limit=args.limit)
    print(json.dumps({"eligible_nodes": len(ids), "sample": ids[:20]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
