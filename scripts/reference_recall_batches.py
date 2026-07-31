#!/usr/bin/env python3
"""Prepare independent reviewer prompts from a reference-recall pilot.

This is the Phase 3 boundary: it only writes a JSONL request file and a
manifest.  It never writes graph edges or updates the corpus SQLite database.
The same input can be submitted through the existing OpenAI Batch API helper
or handed to an independent reviewer agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PILOT = ROOT / "logs" / "reference-recall-pilot-20260731.jsonl"
DEFAULT_OUT = ROOT / "logs" / "reference-recall-review-batches"
PROMPT_VERSION = "reference-recall-review-v1"
DEFAULT_MODEL = "gpt-4.1-mini"

SYSTEM_PROMPT = """You review one PRA Rulebook provision text for cross-reference recall.
Return JSON only and do not infer a target from outside the supplied text.
Find references that a human reader would understand as pointing to another
Rulebook provision, Part, Article, Chapter, guidance document, definition,
annex, table, template, form, legal instrument, statute, regulation, directive,
policy statement or external regulatory source. Include oddly formatted or
non-linked references. Do not treat a current provision's own number/title,
list numbering, or generic phrases such as 'this rule', 'this Part' or 'the
firm' as a cross-reference unless the wording identifies a distinct target.
Use exact source text for quoted_text. The absolute offsets refer to the full
source text, not a zero-based position within the supplied chunk. Do not return
references outside the supplied chunk. Existing candidate hints are leads only;
confirm them from the text and report omissions as well.
"""

USER_TEMPLATE = """Review this independent provision/chunk.

source_node_id: {source_node_id}
source_node_type: {source_node_type}
source_title: {source_title}
source_url: {source_url}
source_text_hash: {source_text_hash}
chunk_start: {chunk_start}
chunk_end: {chunk_end}

Already detected candidate hints (not authoritative):
{candidate_hints}

Text in this chunk:
{text}

Return exactly:
{{
  "source_node_id": "{source_node_id}",
  "source_text_hash": "{source_text_hash}",
  "chunk_start": {chunk_start},
  "chunk_end": {chunk_end},
  "findings": [
    {{
      "span_start": 0,
      "span_end": 0,
      "quoted_text": "exact text",
      "target_hint": "identifier/title as written, or empty",
      "target_kind": "rule|part|chapter|article|paragraph|annex|table|template|definition|guidance|statute|regulation|external|unknown",
      "decision": "REFERENCE|NOT_REFERENCE|AMBIGUOUS",
      "confidence": 0.0
    }}
  ]
}}
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_id(source_node_id: str, source_hash: str, chunk_start: int, chunk_end: int) -> str:
    return hashlib.sha1(
        f"{source_node_id}|{source_hash}|{chunk_start}|{chunk_end}|{PROMPT_VERSION}".encode("utf-8")
    ).hexdigest()[:24]


def load_pilot(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("pilot rows must be JSON objects")
                rows.append(value)
    return rows


def candidate_hints(row: dict[str, Any], max_items: int = 30) -> list[dict[str, Any]]:
    hints = []
    for candidate in (row.get("candidates") or [])[:max_items]:
        hints.append(
            {
                "span_start": candidate.get("span_start"),
                "span_end": candidate.get("span_end"),
                "candidate_text": candidate.get("candidate_text", ""),
                "candidate_kind": candidate.get("candidate_kind", ""),
                "status": candidate.get("status", ""),
            }
        )
    return hints


def make_request(row: dict[str, Any], model: str) -> dict[str, Any]:
    source_node_id = str(row.get("source_node_id") or "")
    source_hash = str(row.get("source_text_hash") or "")
    chunk_start = int(row.get("chunk_start") or 0)
    chunk_end = int(row.get("chunk_end") or chunk_start + len(row.get("text") or ""))
    prompt = USER_TEMPLATE.format(
        source_node_id=source_node_id,
        source_node_type=row.get("source_node_type", ""),
        source_title=row.get("source_title", ""),
        source_url=row.get("source_url", ""),
        source_text_hash=source_hash,
        chunk_start=chunk_start,
        chunk_end=chunk_end,
        candidate_hints=json.dumps(candidate_hints(row), ensure_ascii=False),
        text=row.get("text", ""),
    )
    return {
        "custom_id": request_id(source_node_id, source_hash, chunk_start, chunk_end),
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
    }


def prepare(pilot: Path, output_dir: Path, model: str, limit: int | None, batch_size: int) -> dict[str, Any]:
    rows = load_pilot(pilot)
    if limit:
        rows = rows[:limit]
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    requests = [make_request(row, model) for row in rows]
    batch_paths: list[str] = []
    for index in range(0, len(requests), batch_size):
        batch_number = index // batch_size + 1
        path = output_dir / f"batch-{batch_number:04d}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for request in requests[index : index + batch_size]:
                handle.write(json.dumps(request, ensure_ascii=False) + "\n")
        batch_paths.append(str(path))
    manifest = {
        "run_id": run_id,
        "created_at": now(),
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "pilot": str(pilot),
        "request_count": len(requests),
        "batch_size": batch_size,
        "batch_paths": batch_paths,
        "status": "prepared_read_only",
        "source_node_ids": [row.get("source_node_id") for row in rows],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=50)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(prepare(args.pilot, args.output_dir, args.model, args.limit, args.batch_size), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
