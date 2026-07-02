#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from llm_reference_pass import (  # noqa: E402
    DB,
    EXTRACT_NODE_TYPES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    USER_TEMPLATE,
    _parse_model_json_output,
    connect,
    load_nodes,
    node_context,
    now,
)

RUNS = ROOT / "logs" / "llm-reference-api-batches"
API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.4-mini"


def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return key


def request(method: str, path: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {api_key()}"
    resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=kwargs.pop("timeout", 120), **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:2000]}")
    return resp


def run_dir(name: str | None = None) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    if name:
        return RUNS / name
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def command_prepare(args: argparse.Namespace) -> None:
    rd = run_dir(args.name)
    conn = connect(args.db)
    node_types = args.node_type or list(EXTRACT_NODE_TYPES)
    rows = load_nodes(conn, node_types=node_types, limit=args.limit, only_missing=not args.rerun, max_chars=args.max_chars)
    input_path = rd / "input.jsonl"
    manifest = {
        "created_at": now(),
        "model": args.model,
        "prompt_version": PROMPT_VERSION,
        "max_chars": args.max_chars,
        "node_count": len(rows),
        "input_file": str(input_path),
        "status": "prepared",
    }
    with input_path.open("w", encoding="utf-8") as f:
        for item in rows:
            r = item["row"]
            prompt = USER_TEMPLATE.format(
                id=r["id"],
                node_type=r["node_type"],
                title=r["title"] or "",
                url=r["url"] or "",
                context=node_context(r),
                text=item["text"],
            )
            body = {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            rec = {
                "custom_id": r["id"],
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": body,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (rd / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(rd), "node_count": len(rows), "bytes": input_path.stat().st_size}, indent=2))


def command_submit(args: argparse.Namespace) -> None:
    rd = Path(args.run_dir)
    input_path = rd / "input.jsonl"
    if not input_path.exists():
        raise RuntimeError(f"Missing {input_path}")
    with input_path.open("rb") as f:
        file_resp = request(
            "POST",
            "/files",
            files={"file": ("input.jsonl", f, "application/jsonl")},
            data={"purpose": "batch"},
            timeout=300,
        ).json()
    batch_resp = request(
        "POST",
        "/batches",
        json={
            "input_file_id": file_resp["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
            "metadata": {"project": "pra-rulebook-explorer", "run_dir": rd.name},
        },
    ).json()
    manifest_path = rd / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"status": "submitted", "file": file_resp, "batch": batch_resp, "submitted_at": now()})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(rd), "file_id": file_resp["id"], "batch_id": batch_resp["id"], "status": batch_resp.get("status")}, indent=2))


def command_status(args: argparse.Namespace) -> None:
    rd = Path(args.run_dir)
    manifest_path = rd / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch_id = args.batch_id or manifest.get("batch", {}).get("id")
    if not batch_id:
        raise RuntimeError("No batch id found")
    batch = request("GET", f"/batches/{batch_id}").json()
    manifest["batch"] = batch
    manifest["status_checked_at"] = now()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(batch, indent=2))


def download_file(file_id: str, dest: Path) -> None:
    resp = request("GET", f"/files/{file_id}/content", stream=True, timeout=300)
    with dest.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def command_import(args: argparse.Namespace) -> None:
    rd = Path(args.run_dir)
    manifest_path = rd / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch = manifest.get("batch", {})
    if batch.get("status") != "completed":
        batch_id = args.batch_id or batch.get("id")
        if not batch_id:
            raise RuntimeError("No batch id found")
        batch = request("GET", f"/batches/{batch_id}").json()
        manifest["batch"] = batch
    if batch.get("status") != "completed":
        raise RuntimeError(f"Batch is not completed: {batch.get('status')}")
    output_file_id = batch.get("output_file_id")
    if not output_file_id:
        raise RuntimeError("Completed batch has no output_file_id")
    output_path = rd / "output.jsonl"
    if not output_path.exists() or args.redownload:
        download_file(output_file_id, output_path)
    model = manifest.get("model") or DEFAULT_MODEL
    conn = connect(args.db)
    current_hash = {
        item["row"]["id"]: item["text_hash"]
        for item in load_nodes(conn, node_types=list(EXTRACT_NODE_TYPES), limit=None, only_missing=False, max_chars=int(manifest.get("max_chars") or 6000))
    }
    ok = errors = 0
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            node_id = rec["custom_id"]
            response = rec.get("response") or {}
            err_obj = rec.get("error") or response.get("error")
            if err_obj:
                status, parsed, error = "error", {}, json.dumps(err_obj, ensure_ascii=False)
                errors += 1
            else:
                body = response.get("body") or {}
                content = body["choices"][0]["message"]["content"]
                parsed = _parse_model_json_output(content)
                status, error = "ok", ""
                ok += 1
            conn.execute(
                """
                INSERT INTO llm_reference_extraction (node_id,model,prompt_version,text_hash,status,response_json,error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET model=excluded.model,prompt_version=excluded.prompt_version,
                  text_hash=excluded.text_hash,status=excluded.status,response_json=excluded.response_json,
                  error=excluded.error,updated_at=excluded.updated_at
                """,
                (node_id, model, PROMPT_VERSION, current_hash.get(node_id, "batch-api"), status, json.dumps(parsed or {}, ensure_ascii=False), error, now(), now()),
            )
    conn.commit()
    manifest.update({"status": "imported", "imported_at": now(), "import_ok": ok, "import_errors": errors, "output_file": str(output_path)})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"ok": ok, "errors": errors, "output": str(output_path)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare/submit/import OpenAI Batch API runs for PRA reference extraction.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-chars", type=int, default=6000)
    p.add_argument("--limit", type=int)
    p.add_argument("--rerun", action="store_true")
    p.add_argument("--node-type", action="append")
    p.add_argument("--name")
    p.set_defaults(func=command_prepare)
    p = sub.add_parser("submit")
    p.add_argument("run_dir")
    p.set_defaults(func=command_submit)
    p = sub.add_parser("status")
    p.add_argument("run_dir")
    p.add_argument("--batch-id")
    p.set_defaults(func=command_status)
    p = sub.add_parser("import")
    p.add_argument("run_dir")
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--batch-id")
    p.add_argument("--redownload", action="store_true")
    p.set_defaults(func=command_import)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
