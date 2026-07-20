#!/usr/bin/env python3
"""Cheap semantic enrichment for the normalized reporting catalogue via Batch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.db import connect
from backend.app.migrations import apply_migrations


DB_PATH = ROOT / "backend/data/rulebook.sqlite3"
BATCH_DIR = ROOT / "outputs/reporting-catalog-enrichment-batches"
API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("PRA_REPORTING_CATALOG_MODEL", "gpt-5-nano")
PROMPT_VERSION = "reporting-catalog-v2"


def validate_return_description(description: str, *, expected_estate: str) -> str:
    """Reject prose that contradicts the authoritative catalogue estate.

    The official table position determines whether an entry is a supervisory
    return or a Pillar 3 disclosure.  Model-written prose may enrich that
    classification, but must never override it.
    """
    value = " ".join(str(description or "").split())[:900]
    if not value:
        raise ValueError("empty return_description")
    mentions_disclosure = bool(re.search(r"\bpillar\s*3\b|\bdisclos(?:ure|ures|ed|ing)\b", value, re.I))
    if expected_estate == "supervisory_reporting" and mentions_disclosure:
        raise ValueError("supervisory return description mentions Pillar 3/disclosure")
    if expected_estate == "pillar3_disclosure" and not mentions_disclosure:
        raise ValueError("Pillar 3 description does not identify a disclosure")
    return value


def headers(*, json_body: bool = False) -> dict[str, str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    result = {"Authorization": f"Bearer {key}"}
    if json_body:
        result["Content-Type"] = "application/json"
    return result


def context(conn: sqlite3.Connection, return_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM reporting_return_catalog WHERE return_id=?", (return_id,)).fetchone()
    artifacts = []
    for artifact in conn.execute(
        """
        SELECT a.*,ra.relationship
        FROM reporting_return_artifact ra JOIN reporting_artifact a ON a.artifact_id=ra.artifact_id
        WHERE ra.return_id=? ORDER BY ra.display_order
        """,
        (return_id,),
    ):
        excerpts = []
        if artifact["source_id"]:
            excerpts = [
                " ".join((span[0] or "").split())[:1600]
                for span in conn.execute(
                    """
                    SELECT COALESCE(normalised_text,raw_text) FROM source_span
                    WHERE source_id=? AND COALESCE(normalised_text,raw_text,'')<>''
                    ORDER BY COALESCE(page_number,0),COALESCE(row_number,0) LIMIT 3
                    """,
                    (artifact["source_id"],),
                )
            ]
        try:
            sheets = json.loads(artifact["sheet_names_json"] or "[]")
        except json.JSONDecodeError:
            sheets = []
        artifacts.append(
            {
                "relationship": artifact["relationship"],
                "title": artifact["display_title"],
                "file_type": artifact["file_type"],
                "sheet_names": sheets[:60],
                "text_excerpts": excerpts,
            }
        )
    return {
        "return_id": row["return_id"],
        "return_code": row["return_code"],
        "official_name": row["name"],
        "estate": row["estate"],
        "family": row["family"],
        "effective_period": row["effective_text"],
        "artifacts": artifacts,
    }


def request_line(item: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "custom_id": item["return_id"],
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write precise, plain-English descriptions of UK PRA regulatory reporting materials. "
                        "Use only the supplied official names, workbook sheets and extracted instruction text. "
                        "The supplied estate is authoritative and must never be reclassified: supervisory_reporting "
                        "means a regulatory return and its description must not mention Pillar 3 or disclosure; "
                        "pillar3_disclosure means a Pillar 3 disclosure. Do not infer reporting obligations, scope "
                        "or frequency not present in the evidence. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Return JSON with: return_description (one or two sentences explaining what information the return or disclosure covers), "
                        "template_description (one sentence explaining the supplied form/workbook contents), and "
                        "instructions_description (one sentence explaining what the instruction material covers). "
                        "Use the official return code and distinguish Pillar 3 disclosures from regulatory returns.\n\n"
                        + json.dumps(item, ensure_ascii=False)
                    ),
                },
            ],
        },
    }


def create_file(conn: sqlite3.Connection, *, model: str, output: Path | None = None) -> dict[str, Any]:
    apply_migrations(conn)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output or BATCH_DIR / f"reporting-catalog-{stamp}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    ids = [row[0] for row in conn.execute("SELECT return_id FROM reporting_return_catalog ORDER BY return_id")]
    hashes = {}
    total_chars = 0
    with output.open("w", encoding="utf-8") as handle:
        for return_id in ids:
            item = context(conn, return_id)
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True)
            hashes[return_id] = hashlib.sha256(encoded.encode()).hexdigest()
            line = json.dumps(request_line(item, model), ensure_ascii=False)
            total_chars += len(line)
            handle.write(line + "\n")
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps({"model": model, "prompt_version": PROMPT_VERSION, "input_hashes": hashes}, indent=2),
        encoding="utf-8",
    )
    return {
        "path": str(output),
        "manifest": str(manifest),
        "requests": len(ids),
        "characters": total_chars,
        "estimated_input_tokens": round(total_chars / 4),
        "model": model,
    }


def submit(path: Path, model: str) -> dict[str, Any]:
    with path.open("rb") as handle:
        uploaded = requests.post(
            f"{API_BASE}/files",
            headers=headers(),
            data={"purpose": "batch"},
            files={"file": (path.name, handle, "application/jsonl")},
            timeout=120,
        )
    if uploaded.status_code >= 400:
        raise RuntimeError(f"File upload failed HTTP {uploaded.status_code}: {uploaded.text[:1000]}")
    batch = requests.post(
        f"{API_BASE}/batches",
        headers=headers(json_body=True),
        json={
            "input_file_id": uploaded.json()["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "metadata": {"purpose": "pra-reporting-catalog", "prompt_version": PROMPT_VERSION, "model": model},
        },
        timeout=60,
    )
    if batch.status_code >= 400:
        raise RuntimeError(f"Batch creation failed HTTP {batch.status_code}: {batch.text[:1000]}")
    return batch.json()


def batch_status(batch_id: str) -> dict[str, Any]:
    response = requests.get(f"{API_BASE}/batches/{batch_id}", headers=headers(), timeout=60)
    response.raise_for_status()
    return response.json()


def file_content(file_id: str) -> str:
    response = requests.get(f"{API_BASE}/files/{file_id}/content", headers=headers(), timeout=120)
    response.raise_for_status()
    return response.text


def import_results(conn: sqlite3.Connection, *, batch_id: str, manifest_path: Path) -> dict[str, Any]:
    payload = batch_status(batch_id)
    if payload.get("status") != "completed" or not payload.get("output_file_id"):
        return {"status": payload.get("status"), "imported": 0, "request_counts": payload.get("request_counts")}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = BATCH_DIR / f"{batch_id}-output.jsonl"
    output.write_text(file_content(payload["output_file_id"]), encoding="utf-8")
    imported = failed = 0
    for raw in output.read_text(encoding="utf-8").splitlines():
        result = json.loads(raw)
        return_id = result.get("custom_id", "")
        response = result.get("response") or {}
        body = response.get("body") or {}
        try:
            value = json.loads(body["choices"][0]["message"]["content"])
            catalog_row = conn.execute(
                "SELECT estate FROM reporting_return_catalog WHERE return_id=?", (return_id,)
            ).fetchone()
            if not catalog_row:
                raise ValueError("unknown return_id")
            return_description = validate_return_description(
                value.get("return_description") or "", expected_estate=catalog_row["estate"]
            )
            template_description = " ".join(str(value.get("template_description") or "").split())[:700]
            instructions_description = " ".join(str(value.get("instructions_description") or "").split())[:700]
            conn.execute("UPDATE reporting_return_catalog SET description=?,updated_at=CURRENT_TIMESTAMP WHERE return_id=?", (return_description, return_id))
            conn.execute(
                """
                UPDATE reporting_artifact SET description=?,updated_at=CURRENT_TIMESTAMP
                WHERE artifact_id IN (SELECT artifact_id FROM reporting_return_artifact WHERE return_id=? AND relationship='template')
                """,
                (template_description, return_id),
            )
            conn.execute(
                """
                UPDATE reporting_artifact SET description=?,updated_at=CURRENT_TIMESTAMP
                WHERE artifact_id IN (SELECT artifact_id FROM reporting_return_artifact WHERE return_id=? AND relationship='instructions')
                """,
                (instructions_description, return_id),
            )
            conn.execute(
                """
                INSERT INTO reporting_catalog_enrichment(return_id,model,prompt_version,input_hash,status,response_json,error,updated_at)
                VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(return_id) DO UPDATE SET model=excluded.model,prompt_version=excluded.prompt_version,
                  input_hash=excluded.input_hash,status=excluded.status,response_json=excluded.response_json,error='',updated_at=CURRENT_TIMESTAMP
                """,
                (return_id, manifest["model"], manifest["prompt_version"], manifest["input_hashes"].get(return_id, ""), "ok", json.dumps(result), ""),
            )
            imported += 1
        except Exception as exc:
            failed += 1
            if return_id:
                conn.execute(
                    """
                    INSERT INTO reporting_catalog_enrichment(return_id,model,prompt_version,input_hash,status,response_json,error,updated_at)
                    VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(return_id) DO UPDATE SET status='failed',response_json=excluded.response_json,error=excluded.error,updated_at=CURRENT_TIMESTAMP
                    """,
                    (return_id, manifest["model"], manifest["prompt_version"], manifest["input_hashes"].get(return_id, ""), "failed", json.dumps(result), str(exc)[:800]),
                )
    conn.commit()
    return {"status": "completed", "imported": imported, "failed": failed, "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--submit", type=Path)
    parser.add_argument("--status")
    parser.add_argument("--import-batch")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.submit:
        print(json.dumps(submit(args.submit, args.model), indent=2))
        return
    if args.status:
        print(json.dumps(batch_status(args.status), indent=2))
        return
    conn = connect(DB_PATH)
    try:
        if args.import_batch:
            if not args.manifest:
                parser.error("--manifest is required with --import-batch")
            result = import_results(conn, batch_id=args.import_batch, manifest_path=args.manifest)
        else:
            result = create_file(conn, model=args.model, output=args.output)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
