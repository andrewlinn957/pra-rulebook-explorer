#!/usr/bin/env python3
"""Review unresolved structural-reference candidates with adaptive context.

The deterministic recall review intentionally leaves structural candidates
whose target depends on the surrounding provision in a separate queue.  This
module prepares and runs a two-pass LLM review for that queue without writing
the Rulebook database:

* pass one groups candidates by source node (splitting very dense sources into
  bounded requests);
* pass two retries only candidates that are missing, ambiguous, or rejected
  by validation, with a tighter span-centred pack.

Every request stores the exact source hash, candidate offsets, selected text
ranges, target catalogue, model response, and deterministic target decision.
Only the later, existing staging/materialisation gates may turn a unique
local target into a graph edge.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect

from scripts.reference_recall_audit import connect_source, digest, json_load, normalised, source_text  # noqa: E402
from scripts.reference_recall_stage import (  # noqa: E402
    build_target_indexes,
    connect_stage,
    insert_stage,
    relationship_for_target,
    structural_parts,
)


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_LEDGER = ROOT / "logs" / "reference-recall-ledger-recommended-final5-20260801.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-final5-20260801.sqlite3"
DEFAULT_OUTPUT = ROOT / "logs" / "reference-structural-context-llm-20260801.sqlite3"
DEFAULT_STAGE = ROOT / "logs" / "reference-structural-context-stage-20260801.sqlite3"
PASS_VERSION = "structural-context-llm-v1"
DEFAULT_MODEL = os.environ.get("PRA_LLM_REFERENCE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini"
DEFAULT_MAX_TOKENS = 24_000
DEFAULT_MAX_CANDIDATES = 50
DEFAULT_NEIGHBOURS = 2
CHARS_PER_TOKEN = 4

STRUCTURAL_LINE_RE = re.compile(
    r"^(?:part|chapter|annex(?:es)?|schedule|article|section|rule|paragraph|"
    r"point|subparagraph|template|table|form|title|ss\s*\d|sop\s*\d|"
    r"ps\s*\d|fg\s*\d|cp\s*\d|\d+(?:\.\d+){0,5})\b",
    re.IGNORECASE,
)
NUMBER_LINE_RE = re.compile(r"^\d+(?:\.\d+){0,5}\.?$")

CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_run (
  run_id TEXT PRIMARY KEY,
  pass_version TEXT NOT NULL,
  source_db TEXT NOT NULL,
  ledger_db TEXT NOT NULL,
  review_db TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  max_tokens INTEGER NOT NULL,
  max_candidates INTEGER NOT NULL,
  neighbour_blocks INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS context_request (
  request_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  pass_number INTEGER NOT NULL,
  source_node_id TEXT NOT NULL,
  candidate_ids_json TEXT NOT NULL,
  context_hash TEXT NOT NULL,
  context_json TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'prepared',
  model TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_request_run ON context_request(run_id,pass_number,status);
CREATE TABLE IF NOT EXISTS context_result (
  result_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  status TEXT NOT NULL,
  response_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_result_candidate ON context_result(request_id,candidate_id);
CREATE TABLE IF NOT EXISTS context_adjudication (
  candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  pass_number INTEGER NOT NULL,
  classification TEXT NOT NULL,
  target_kind TEXT NOT NULL DEFAULT '',
  target_title_or_identifier TEXT NOT NULL DEFAULT '',
  target_part_or_document TEXT NOT NULL DEFAULT '',
  evidence_quote TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  target_node_id TEXT NOT NULL DEFAULT '',
  target_title TEXT NOT NULL DEFAULT '',
  target_status TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_adjudication_status
  ON context_adjudication(decision,target_status);
"""

SYSTEM_PROMPT = """You review unresolved structural-reference candidates in PRA Rulebook text.
Return JSON only. Do not invent graph IDs or facts outside the supplied context.
For every supplied candidate_id return exactly one item. Decide whether the
candidate is a cross-reference, not a reference, or still ambiguous. If it is
a reference, identify the target kind, identifier/title, and explicit Part or
document context. Use the target catalogue when it contains a matching local
provision, but do not assume a catalogue entry is correct without textual
support. Evidence quotes must be exact words from the supplied source text.
References to the current source node itself are not cross-references.
"""

USER_PROMPT = """Review these structural-reference candidates.

Return exactly:
{{"items":[{{"candidate_id":"...","classification":"reference|not_reference|ambiguous",
"target_kind":"rule|part|chapter|article|section|paragraph|annex|schedule|template|table|form|guidance|external|unknown",
"target_title_or_identifier":"","target_part_or_document":"","evidence_quote":"",
"reason":"","confidence":0.0}}]}}

Context pack (JSON):
{context_json}
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def approx_tokens(value: str) -> int:
    return max(1, math.ceil(len(value) / CHARS_PER_TOKEN))


def connect_output(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path, timeout=120)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(CONTEXT_SCHEMA)
    return conn


def row_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row) if isinstance(row, sqlite3.Row) else dict(row)


def metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    raw = row["metadata_json"] if isinstance(row, sqlite3.Row) else row.get("metadata_json")
    value = json_load(raw, {})
    return value if isinstance(value, dict) else {}


def prompt_metadata(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Keep useful hierarchy metadata without embedding scraper payloads."""
    compact: dict[str, Any] = {}
    omitted: list[str] = []
    reader = value.get("reader_reference_text")
    for key, item in value.items():
        if key == "reader_reference_text" and isinstance(item, dict):
            compact[key] = {
                nested_key: item.get(nested_key)
                for nested_key in ("method", "source_title", "source_url", "source_document_id", "source_node_ids")
                if item.get(nested_key) not in (None, "")
            }
            continue
        if key in {"pdf_extraction", "raw_html", "raw_text", "html", "content"}:
            omitted.append(key)
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            text = item
            if isinstance(text, str) and len(text) > 600:
                text = text[:600] + "…"
            compact[key] = text
        else:
            omitted.append(key)
    if reader is not None and not isinstance(reader, dict):
        omitted.append("reader_reference_text")
    return compact, sorted(set(omitted))


def node_descriptor(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    meta = metadata(row)
    return {
        "id": row["id"],
        "node_type": row["node_type"],
        "title": row["title"] or "",
        "url": row["url"] or "",
        "stable_key": row["stable_key"] or "",
        "part_title": meta.get("part_title") or "",
        "chapter_title": meta.get("chapter_title") or "",
        "document_title": meta.get("document_title") or meta.get("source_title") or "",
        "source": meta.get("source") or "",
    }


def candidate_key(row: sqlite3.Row | dict[str, Any]) -> str:
    return str(row["candidate_id"])


def canonical_structural_label(value: str) -> str:
    value = (value or "").casefold().strip()
    return {
        "annexe": "annex",
        "regulation": "regulation",
        "section": "section",
        "paragraph": "paragraph",
        "article": "article",
        "rule": "rule",
        "part": "part",
        "chapter": "chapter",
        "schedule": "schedule",
        "template": "template",
        "table": "table",
        "form": "form",
        "point": "point",
        "subparagraph": "subparagraph",
    }.get(value, value)


def source_child_ids(source: sqlite3.Row, conn: sqlite3.Connection) -> list[str]:
    ids: list[str] = []
    meta = metadata(source)
    reader = meta.get("reader_reference_text")
    if isinstance(reader, dict) and isinstance(reader.get("source_node_ids"), list):
        ids.extend(str(value) for value in reader["source_node_ids"] if value)
    for row in conn.execute(
        "SELECT to_node_id FROM edge WHERE from_node_id=? AND edge_type='contains' ORDER BY to_node_id",
        (source["id"],),
    ):
        ids.append(row[0])
    return list(dict.fromkeys(ids))


def block_ranges(text: str) -> list[tuple[int, int]]:
    """Split aggregate text at headings, numbered provision lines, or blanks."""
    if not text:
        return [(0, 0)]
    boundaries = {0, len(text)}
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            boundaries.add(offset + len(line))
        elif STRUCTURAL_LINE_RE.match(stripped) or NUMBER_LINE_RE.fullmatch(stripped):
            boundaries.add(offset)
        offset += len(line)
    ordered = sorted(boundaries)
    ranges: list[tuple[int, int]] = []
    for start, end in zip(ordered, ordered[1:]):
        if end > start:
            ranges.append((start, end))
    # A source with no useful line boundaries is still packable as one block.
    return ranges or [(0, len(text))]


def block_index(ranges: list[tuple[int, int]], position: int) -> int:
    for index, (start, end) in enumerate(ranges):
        if start <= position < end:
            return index
    return max(0, len(ranges) - 1)


def ranges_for_candidates(
    text: str,
    candidates: list[sqlite3.Row | dict[str, Any]],
    *,
    max_tokens: int,
    neighbour_blocks: int,
) -> list[tuple[int, int]]:
    """Return merged text ranges containing all candidate spans.

    Candidate blocks are mandatory. Neighbour blocks are added only while the
    context remains under the cap; this means every exact citation is retained
    even for unusually dense or widely separated aggregate documents.
    """
    ranges = block_ranges(text)
    wanted: set[int] = set()
    for candidate in candidates:
        start = candidate["span_start"]
        end = candidate["span_end"]
        if start is None:
            continue
        wanted.add(block_index(ranges, int(start)))
        if end is not None and end > start:
            wanted.add(block_index(ranges, int(end) - 1))
    if not wanted:
        wanted = {0}

    selected = set(wanted)
    # Prioritise context around the outermost required blocks, then fill gaps.
    for distance in range(1, max(0, neighbour_blocks) + 1):
        for index in sorted(wanted):
            for neighbour in (index - distance, index + distance):
                if 0 <= neighbour < len(ranges):
                    trial = sorted(selected | {neighbour})
                    candidate_text = "\n".join(text[start:end] for start, end in _merge_ranges([ranges[i] for i in trial]))
                    if approx_tokens(candidate_text) <= max_tokens:
                        selected.add(neighbour)
    merged = _merge_ranges([ranges[index] for index in sorted(selected)])
    if sum(approx_tokens(text[start:end]) for start, end in merged) <= max_tokens:
        return merged

    # If a single block is very large, centre a bounded excerpt on each
    # candidate. This path still includes every candidate's exact span.
    max_chars = max_tokens * CHARS_PER_TOKEN
    pieces: list[tuple[int, int]] = []
    per_candidate = max(800, max_chars // max(1, len(candidates)))
    for candidate in candidates:
        start = int(candidate["span_start"] or 0)
        end = int(candidate["span_end"] or start)
        centre = (start + end) // 2
        left = max(0, centre - per_candidate // 2)
        right = min(len(text), max(end, left + per_candidate))
        left = max(0, right - per_candidate)
        pieces.append((left, right))
    return _merge_ranges(pieces)


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(start), int(end)) for start, end in ranges if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def load_queue(review_path: Path, ledger_path: Path) -> list[dict[str, Any]]:
    review = connect(f"file:{review_path.resolve()}?mode=ro", uri=True)
    ledger = connect(f"file:{ledger_path.resolve()}?mode=ro", uri=True)
    try:
        gaps = {row["candidate_id"]: row for row in ledger.execute("SELECT * FROM reference_gap")}
        out: list[dict[str, Any]] = []
        for row in review.execute(
            """
            SELECT candidate_id,source_node_id,source_node_type,source_title,
                   source_text_hash,span_start,span_end,quoted_text,candidate_text,
                   candidate_kind,ledger_status,decision,target_status,evidence_json
            FROM corpus_review
            WHERE decision='AMBIGUOUS' AND target_status='structural_context_required'
            ORDER BY source_node_id,span_start,candidate_id
            """
        ):
            item = dict(row)
            gap = gaps.get(row["candidate_id"])
            if gap:
                item.update({
                    "source_url": gap["source_url"],
                    "detector_json": gap["detector_json"],
                    "reason_json": gap["reason_json"],
                    "context_text": gap["context_text"],
                    "gap_status": gap["status"],
                })
            else:
                item.update({"source_url": "", "detector_json": "[]", "reason_json": "[]", "context_text": "", "gap_status": ""})
            out.append(item)
        return out
    finally:
        review.close()
        ledger.close()


def target_catalog(
    candidate: dict[str, Any],
    indexes: dict[str, Any],
    nodes: dict[str, sqlite3.Row],
    source: sqlite3.Row,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    label, identifier, explicit = structural_parts(candidate.get("candidate_text") or "")
    label = canonical_structural_label(label)
    candidates: dict[str, sqlite3.Row] = {}
    if label and identifier:
        for node in indexes.get("structural", {}).get((label, identifier), []):
            candidates[node["id"]] = node
    wanted = normalised(candidate.get("candidate_text") or "")
    if wanted:
        for node in indexes.get("exact_title", {}).get(wanted, []):
            candidates[node["id"]] = node
    source_meta_full = metadata(source)
    source_meta, omitted_metadata = prompt_metadata(source_meta_full)
    source_context = normalised(str(source_meta.get("part_title") or source_meta.get("document_title") or ""))
    ordered = list(candidates.values())
    if source_context:
        ordered.sort(key=lambda node: (0 if source_context in normalised(json.dumps(metadata(node), ensure_ascii=False)) else 1, node["id"]))
    return [node_descriptor(node) for node in ordered[:limit] if node["id"] != source["id"]]


def make_context_pack(
    source: sqlite3.Row,
    candidates: list[dict[str, Any]],
    nodes: dict[str, sqlite3.Row],
    indexes: dict[str, Any],
    source_conn: sqlite3.Connection,
    *,
    max_tokens: int,
    neighbour_blocks: int,
    pass_number: int,
) -> dict[str, Any]:
    text = source_text(source)
    source_meta_full = metadata(source)
    source_meta, omitted_metadata = prompt_metadata(source_meta_full)
    child_ids = source_child_ids(source, source_conn)
    children = [node_descriptor(nodes[node_id]) for node_id in child_ids if node_id in nodes]
    candidate_payload = []
    catalogue: dict[str, list[dict[str, Any]]] = {}
    catalogue_entries = 0
    max_catalogue_entries = 120
    for candidate in candidates:
        candidate_payload.append({
            "candidate_id": candidate_key(candidate),
            "candidate_text": candidate.get("candidate_text") or "",
            "candidate_kind": candidate.get("candidate_kind") or "",
            "span_start": candidate.get("span_start"),
            "span_end": candidate.get("span_end"),
            "quoted_text": candidate.get("quoted_text") or "",
        })
        remaining = max(0, max_catalogue_entries - catalogue_entries)
        entries = target_catalog(candidate, indexes, nodes, source, limit=min(6, remaining)) if remaining else []
        catalogue[candidate_key(candidate)] = entries
        catalogue_entries += len(entries)
    # Reserve budget for metadata, candidate descriptors, and the target
    # catalogue before deciding whether the complete source text fits.
    overhead = {
        "pack_version": PASS_VERSION,
        "pass_number": pass_number,
        "source": node_descriptor(source),
        "source_metadata": source_meta,
        "children": children[:32],
        "candidates": candidate_payload,
        "target_catalog": catalogue,
    }
    overhead_tokens = approx_tokens(json.dumps(overhead, ensure_ascii=False))
    text_budget = max(1_000, max_tokens - overhead_tokens - 100)
    full_source = approx_tokens(text) <= text_budget and pass_number == 1
    ranges = [(0, len(text))] if full_source else ranges_for_candidates(
        text, candidates, max_tokens=text_budget, neighbour_blocks=neighbour_blocks
    )
    segments = [
        {"start": start, "end": end, "text": text[start:end]}
        for start, end in ranges
    ]
    selected_text = "\n".join(segment["text"] for segment in segments)
    pack = {
        "pack_version": PASS_VERSION,
        "pass_number": pass_number,
        "source": node_descriptor(source),
        "source_metadata": source_meta,
        "source_metadata_hash": sha1(json.dumps(source_meta_full, ensure_ascii=False, sort_keys=True)),
        "source_metadata_omitted": omitted_metadata,
        "source_text_hash": digest(text),
        "full_source_included": full_source,
        "text_length": len(text),
        "segments": segments,
        "children": children[:32],
        "candidates": candidate_payload,
        "target_catalog": catalogue,
        "provenance": {
            "selected_ranges": [[start, end] for start, end in ranges],
            "neighbour_blocks": neighbour_blocks,
            "approx_text_tokens": approx_tokens(selected_text),
            "approx_overhead_tokens": overhead_tokens,
            "approx_tokens": approx_tokens(selected_text) + overhead_tokens,
        },
    }
    return pack


def request_id(run_id: str, pass_number: int, source_id: str, candidate_ids: list[str]) -> str:
    return sha1("|".join([run_id, str(pass_number), source_id, *candidate_ids]))[:28]


def partition_candidates(
    candidates: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[list[dict[str, Any]]]:
    return [candidates[start:start + max_candidates] for start in range(0, len(candidates), max_candidates)]


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    queue = load_queue(args.review_db, args.ledger)
    if args.limit:
        queue = queue[: args.limit]
    source_conn = connect_source(args.db)
    nodes = {row["id"]: row for row in source_conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node")}
    indexes = build_target_indexes(nodes)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in queue:
        grouped[candidate["source_node_id"]].append(candidate)
    output = connect_output(args.output)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + sha1(now())[:8]
    output.execute(
        "INSERT INTO context_run(run_id,pass_version,source_db,ledger_db,review_db,model,max_tokens,max_candidates,neighbour_blocks,started_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, PASS_VERSION, str(args.db), str(args.ledger), str(args.review_db), args.model, args.max_tokens, args.max_candidates, args.neighbour_blocks, now()),
    )
    requests = 0
    source_count = 0
    missing = 0
    for source_id, candidates in grouped.items():
        source = nodes.get(source_id)
        if source is None:
            missing += len(candidates)
            continue
        source_count += 1
        for chunk in partition_candidates(candidates, max_candidates=args.max_candidates):
            pack = make_context_pack(
                source, chunk, nodes, indexes, source_conn,
                max_tokens=args.max_tokens, neighbour_blocks=args.neighbour_blocks,
                pass_number=1,
            )
            prompt = USER_PROMPT.format(context_json=json.dumps(pack, ensure_ascii=False))
            ids = [candidate_key(item) for item in chunk]
            rid = request_id(run_id, 1, source_id, ids)
            timestamp = now()
            output.execute(
                "INSERT OR REPLACE INTO context_request(request_id,run_id,pass_number,source_node_id,candidate_ids_json,context_hash,context_json,prompt_text,status,model,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, run_id, 1, source_id, json.dumps(ids), sha1(json.dumps(pack, ensure_ascii=False, sort_keys=True)), json.dumps(pack, ensure_ascii=False), prompt, "prepared", args.model, "", timestamp, timestamp),
            )
            requests += 1
    summary = {
        "run_id": run_id,
        "pass_version": PASS_VERSION,
        "queue_candidates": len(queue),
        "source_nodes": source_count,
        "requests": requests,
        "missing_source_candidates": missing,
        "full_source_requests": output.execute("SELECT COUNT(*) FROM context_request WHERE run_id=? AND json_extract(context_json,'$.full_source_included')=1", (run_id,)).fetchone()[0],
        "excerpt_requests": output.execute("SELECT COUNT(*) FROM context_request WHERE run_id=? AND json_extract(context_json,'$.full_source_included')=0", (run_id,)).fetchone()[0],
        "approx_context_tokens": output.execute("SELECT SUM(json_extract(context_json,'$.provenance.approx_tokens')) FROM context_request WHERE run_id=?", (run_id,)).fetchone()[0] or 0,
    }
    output.execute("UPDATE context_run SET finished_at=?,summary_json=? WHERE run_id=?", (now(), json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id))
    output.commit()
    output.close()
    source_conn.close()
    return summary


def prepare_retry(args: argparse.Namespace) -> dict[str, Any]:
    """Prepare the individual second-pass requests for unresolved outcomes."""
    output = connect_output(args.output)
    run = output.execute("SELECT * FROM context_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not run:
        raise RuntimeError("Prepare pass one before preparing retries")
    queue = {item["candidate_id"]: item for item in load_queue(args.review_db, args.ledger)}
    retry_ids = {
        row["candidate_id"]
        for row in output.execute(
            """
            SELECT candidate_id
            FROM context_adjudication
            WHERE run_id=? AND (decision='AMBIGUOUS' OR target_status='llm_ambiguous')
            """,
            (run["run_id"],),
        )
    }
    for request in output.execute(
        "SELECT * FROM context_request WHERE run_id=? AND pass_number=1 AND status='error'",
        (run["run_id"],),
    ):
        retry_ids.update(json.loads(request["candidate_ids_json"]))
    for request in output.execute(
        "SELECT * FROM context_request WHERE run_id=? AND pass_number=1 AND status='ok'",
        (run["run_id"],),
    ):
        expected = set(json.loads(request["candidate_ids_json"]))
        returned = {
            row["candidate_id"]
            for row in output.execute("SELECT candidate_id FROM context_result WHERE request_id=? AND status='ok'", (request["request_id"],))
        }
        retry_ids.update(expected - returned)
    source_conn = connect_source(args.db)
    nodes = {row["id"]: row for row in source_conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node")}
    indexes = build_target_indexes(nodes)
    made = 0
    skipped = 0
    for candidate_id in sorted(retry_ids):
        candidate = queue.get(candidate_id)
        if candidate is None:
            skipped += 1
            continue
        source = nodes.get(candidate["source_node_id"])
        if source is None:
            skipped += 1
            continue
        # A retry is deliberately one candidate per request. The surrounding
        # range can therefore grow without consuming budget on unrelated
        # citations in the same aggregate document.
        pack = make_context_pack(
            source, [candidate], nodes, indexes, source_conn,
            max_tokens=args.max_tokens, neighbour_blocks=args.neighbour_blocks,
            pass_number=2,
        )
        prompt = USER_PROMPT.format(context_json=json.dumps(pack, ensure_ascii=False))
        rid = request_id(run["run_id"], 2, source["id"], [candidate_id])
        timestamp = now()
        output.execute(
            "INSERT OR REPLACE INTO context_request(request_id,run_id,pass_number,source_node_id,candidate_ids_json,context_hash,context_json,prompt_text,status,model,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, run["run_id"], 2, source["id"], json.dumps([candidate_id]), sha1(json.dumps(pack, ensure_ascii=False, sort_keys=True)), json.dumps(pack, ensure_ascii=False), prompt, "prepared", run["model"], "", timestamp, timestamp),
        )
        made += 1
    summary = json.loads(run["summary_json"] or "{}")
    summary.update({"retry_candidates": len(retry_ids), "retry_requests": made, "retry_missing": skipped})
    output.execute("UPDATE context_run SET summary_json=? WHERE run_id=?", (json.dumps(summary, ensure_ascii=False, sort_keys=True), run["run_id"]))
    output.commit()
    output.close()
    source_conn.close()
    return {"run_id": run["run_id"], "retry_candidates": len(retry_ids), "retry_requests": made, "retry_missing": skipped}


def parse_json_output(content: str) -> dict[str, Any]:
    content = (content or "").strip()
    # ``codex exec --json`` emits JSONL events. Use the final agent message as
    # the model payload before falling back to ordinary JSON/wrapper parsing.
    for line in reversed(content.splitlines()):
        try:
            event = json.loads(line)
        except Exception:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            return parse_json_output(item["text"])
        if isinstance(event, dict) and event.get("type") == "agent_message" and isinstance(event.get("text"), str):
            return parse_json_output(event["text"])
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            for key in ("text", "content", "output", "message", "response"):
                if isinstance(parsed.get(key), str):
                    return parse_json_output(parsed[key])
            outputs = parsed.get("outputs")
            if isinstance(outputs, list):
                for output in outputs:
                    if isinstance(output, dict) and isinstance(output.get("text"), str):
                        return parse_json_output(output["text"])
            if isinstance(parsed.get("items"), list):
                return parsed
    except Exception:
        pass
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError(f"No JSON object found: {content[:500]}")
    parsed = json.loads(match.group(0))
    if isinstance(parsed, dict) and isinstance(parsed.get("outputs"), list):
        for output in parsed["outputs"]:
            if isinstance(output, dict) and isinstance(output.get("text"), str):
                return parse_json_output(output["text"])
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        raise ValueError(f"Expected an items array: {content[:500]}")
    return parsed


def call_openai(model: str, prompt: str, timeout: int = 180) -> dict[str, Any]:
    import requests
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    # GPT-5-family endpoints currently reject an explicit temperature value;
    # older chat-completions models accept deterministic temperature=0.
    if not str(model).casefold().startswith("gpt-5"):
        payload["temperature"] = 0
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {response.status_code}: {response.text[:1200]}")
    return parse_json_output(response.json()["choices"][0]["message"]["content"])


def call_openclaw(model: str, prompt: str, thinking: str = "low", timeout: int = 240) -> dict[str, Any]:
    command = [
        "openclaw", "infer", "model", "run", "--gateway", "--model", model,
        "--json", "--prompt", prompt,
    ]
    if thinking:
        command.extend(["--thinking", thinking])
    proc = subprocess.run(
        command,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    output = (proc.stdout or "").strip()
    if proc.returncode != 0 or output.startswith(("Error:", "GatewayClientRequestError")):
        raise RuntimeError(output[:1200])
    return parse_json_output(output)


def call_codex(model: str, prompt: str, thinking: str = "low", timeout: int = 300) -> dict[str, Any]:
    """Call the local Codex CLI directly, bypassing the OpenClaw gateway."""
    command = [
        "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "read-only", "-m", model,
        "-c", f"model_reasoning_effort={thinking or 'low'}", "-",
    ]
    proc = subprocess.run(
        command,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(output[:1600])
    return parse_json_output(output)


def call_gemini(model: str, prompt: str, timeout: int = 180) -> dict[str, Any]:
    """Call the configured Gemini REST endpoint directly."""
    import requests

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        endpoint,
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:1200]}")
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:1200]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    return parse_json_output(text)


def run_one(request: sqlite3.Row, backend: str, model: str, thinking: str) -> tuple[str, str, dict[str, Any] | None, str]:
    try:
        if backend == "openclaw":
            parsed = call_openclaw(model, request["prompt_text"], thinking=thinking)
        elif backend == "codex":
            parsed = call_codex(model, request["prompt_text"], thinking=thinking)
        elif backend == "gemini":
            parsed = call_gemini(model, request["prompt_text"])
        else:
            parsed = call_openai(model, request["prompt_text"])
        return request["request_id"], "ok", parsed, ""
    except Exception as exc:
        return request["request_id"], "error", None, str(exc)


def normalise_result_items(parsed: dict[str, Any], expected_ids: list[str]) -> list[dict[str, Any]]:
    """Attach model items to the request candidates without trusting IDs blindly.

    Long random candidate IDs are easy for a model to mistype.  A request is
    safe to repair when it has exactly one item per expected candidate and the
    only discrepancy is an unknown ID paired with one missing expected ID.
    In that case the item already carries the classification/evidence for the
    sole missing candidate, so replace only its identifier.  Do not infer
    mappings for partial, duplicated, or otherwise malformed responses; those
    remain visible to adjudication as missing model items.
    """
    raw_items = parsed.get("items", []) if isinstance(parsed, dict) else []
    items = [dict(item) for item in raw_items if isinstance(item, dict) and item.get("candidate_id")]
    expected = list(dict.fromkeys(str(value) for value in expected_ids))
    expected_set = set(expected)
    actual_ids = [str(item["candidate_id"]) for item in items]
    actual_set = set(actual_ids)
    missing = [candidate_id for candidate_id in expected if candidate_id not in actual_set]
    unknown = [item for item in items if str(item["candidate_id"]) not in expected_set]
    if len(items) == len(expected) and len(missing) == 1 and len(unknown) == 1:
        unknown[0]["candidate_id"] = missing[0]
    return items


def run_requests(args: argparse.Namespace) -> dict[str, Any]:
    conn = connect_output(args.output)
    run = conn.execute("SELECT * FROM context_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not run:
        raise RuntimeError("No prepared context run found")
    rows = conn.execute("SELECT * FROM context_request WHERE run_id=? AND status IN ('prepared','error') ORDER BY pass_number,request_id", (run["run_id"],)).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    request_by_id = {row["request_id"]: row for row in rows}
    done = Counter()
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures_map = [pool.submit(run_one, row, args.backend, args.model or run["model"], args.thinking) for row in rows]
        for future in futures.as_completed(futures_map):
            rid, status, parsed, error = future.result()
            timestamp = now()
            conn.execute("UPDATE context_request SET status=?,model=?,error=?,updated_at=? WHERE request_id=?", (status, args.model or run["model"], error, timestamp, rid))
            if status == "ok" and parsed is not None:
                request = request_by_id[rid]
                expected_ids = json.loads(request["candidate_ids_json"])
                for item in normalise_result_items(parsed, expected_ids):
                    result_id = sha1(f"{rid}|{item['candidate_id']}")[:28]
                    conn.execute("INSERT OR REPLACE INTO context_result(result_id,request_id,candidate_id,status,response_json,error,created_at) VALUES (?,?,?,?,?,?,?)", (result_id, rid, item["candidate_id"], "ok", json.dumps(item, ensure_ascii=False), "", timestamp))
            done[status] += 1
            conn.commit()
    summary = {"run_id": run["run_id"], "processed_requests": len(rows), "status_counts": dict(sorted(done.items()))}
    conn.close()
    return summary


def quote_in_source(text: str, quote: str) -> bool:
    if not quote:
        return False
    if quote in text:
        return True
    compact = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    return compact(quote) in compact(text)


def resolve_target(item: dict[str, Any], candidate: dict[str, Any], source: sqlite3.Row, indexes: dict[str, Any], nodes: dict[str, sqlite3.Row]) -> tuple[sqlite3.Row | None, str, list[str]]:
    reasons: list[str] = []
    if item.get("classification") != "reference":
        return None, "", reasons
    label = canonical_structural_label(normalised(str(item.get("target_kind") or "")).rstrip("s"))
    identifier_text = str(item.get("target_title_or_identifier") or "").strip()
    target_part = str(item.get("target_part_or_document") or "").strip()
    if not identifier_text:
        return None, "missing_target_identifier", ["llm_missing_target_identifier"]
    exact = normalised(identifier_text)
    exact_matches = list(indexes.get("exact_title", {}).get(exact, []))
    if len(exact_matches) == 1:
        target = exact_matches[0]
        if target["id"] == source["id"]:
            return None, "self_reference", ["llm_target_is_source"]
        return target, "exact_llm_title", ["unique_exact_llm_title"]
    structural_match = re.search(r"^(?:article|chapter|part|annex|schedule|template|table|form|rule|paragraph|section|point|subparagraph)?\s*([0-9A-Za-zIVXLCDM]+(?:\.[0-9A-Za-z]+)*)", identifier_text, re.I)
    if structural_match and label:
        identifier = re.sub(r"\s+", "", structural_match.group(1)).casefold()
        values = indexes.get("structural", {}).get((label, identifier), [])
        filtered = [node for node in values if node["id"] != source["id"]]
        if target_part:
            wanted = normalised(target_part)
            filtered = [node for node in filtered if wanted in normalised(json.dumps(metadata(node), ensure_ascii=False)) or wanted in normalised(node["title"] or "")]
        if len(filtered) == 1:
            return filtered[0], "llm_structural_context", ["unique_llm_structural_target"]
        if len(filtered) > 1:
            reasons.append("multiple_llm_structural_targets")
        else:
            reasons.append("no_llm_structural_target")
    return None, "ambiguous_llm_target", reasons or ["llm_target_not_resolved"]


def adjudicate(args: argparse.Namespace) -> dict[str, Any]:
    conn = connect_output(args.output)
    source_conn = connect_source(args.db)
    nodes = {row["id"]: row for row in source_conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node")}
    indexes = build_target_indexes(nodes)
    run = conn.execute("SELECT * FROM context_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not run:
        raise RuntimeError("No context run found")
    queue = {item["candidate_id"]: item for item in load_queue(args.review_db, args.ledger)}
    requests = conn.execute("SELECT * FROM context_request WHERE run_id=? AND status='ok' ORDER BY pass_number,request_id", (run["run_id"],)).fetchall()
    counts = Counter()
    for request in requests:
        pack = json.loads(request["context_json"])
        source = nodes.get(request["source_node_id"])
        if source is None:
            continue
        source_text_value = source_text(source)
        expected = set(json.loads(request["candidate_ids_json"]))
        results = {row["candidate_id"]: json.loads(row["response_json"]) for row in conn.execute("SELECT * FROM context_result WHERE request_id=? AND status='ok'", (request["request_id"],))}
        for candidate_id in expected:
            candidate = queue.get(candidate_id)
            item = results.get(candidate_id)
            if candidate is None:
                continue
            reasons: list[str] = []
            target = None
            target_status = "llm_ambiguous"
            decision = "AMBIGUOUS"
            classification = str(item.get("classification") or "ambiguous") if item else "ambiguous"
            if item is None:
                reasons.append("missing_model_item")
            elif classification == "not_reference":
                decision, target_status = "NOT_REFERENCE", "not_applicable"
                reasons.append("llm_classified_not_reference")
            elif classification == "reference":
                quote = str(item.get("evidence_quote") or "")
                if not quote_in_source(source_text_value, quote):
                    decision, target_status = "AMBIGUOUS", "invalid_evidence"
                    reasons.append("evidence_quote_not_in_source")
                else:
                    target, target_method, target_reasons = resolve_target(item, candidate, source, indexes, nodes)
                    reasons.extend(target_reasons)
                    if target is not None:
                        decision, target_status = "REFERENCE", "unique_local_target"
                    else:
                        decision, target_status = "REFERENCE", "external_or_unresolved"
                        if target_method == "ambiguous_llm_target":
                            target_status = "llm_ambiguous"
            else:
                reasons.append("llm_left_ambiguous")
            confidence = float(item.get("confidence") or 0) if item else 0.0
            evidence = {"pack_hash": request["context_hash"], "selected_ranges": pack.get("provenance", {}).get("selected_ranges", []), "full_source_included": pack.get("full_source_included", False), "model_item": item or {}, "source_text_hash": digest(source_text_value)}
            conn.execute(
                "INSERT OR REPLACE INTO context_adjudication(candidate_id,run_id,request_id,pass_number,classification,target_kind,target_title_or_identifier,target_part_or_document,evidence_quote,confidence,target_node_id,target_title,target_status,decision,reason_json,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, run["run_id"], request["request_id"], request["pass_number"], classification, str((item or {}).get("target_kind") or ""), str((item or {}).get("target_title_or_identifier") or ""), str((item or {}).get("target_part_or_document") or ""), str((item or {}).get("evidence_quote") or ""), confidence, target["id"] if target else "", target["title"] if target else "", target_status, decision, json.dumps(sorted(set(reasons))), json.dumps(evidence, ensure_ascii=False), now()),
            )
            counts[(decision, target_status)] += 1
    # ``context_adjudication`` is keyed by candidate, so pass two replaces the
    # pass-one row for retried candidates.  Report both the number of model
    # result rows processed and the final per-candidate outcome; otherwise the
    # run summary can misleadingly add both passes together.
    final_counts = Counter(
        (row["decision"], row["target_status"])
        for row in conn.execute("SELECT decision,target_status FROM context_adjudication WHERE run_id=?", (run["run_id"],))
    )
    summary = {
        "run_id": run["run_id"],
        "adjudicated": sum(final_counts.values()),
        "processed_results": sum(counts.values()),
        "counts": {f"{decision}/{status}": count for (decision, status), count in sorted(final_counts.items())},
    }
    conn.execute("UPDATE context_run SET summary_json=? WHERE run_id=?", (json.dumps(summary, ensure_ascii=False, sort_keys=True), run["run_id"]))
    conn.commit()
    conn.close()
    source_conn.close()
    return summary


def stage(args: argparse.Namespace) -> dict[str, Any]:
    """Copy only validated unique local targets into the existing stage DB."""
    output = connect_output(args.output)
    source_conn = connect_source(args.db)
    stage_conn = connect_stage(args.stage)
    run = output.execute("SELECT * FROM context_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not run:
        raise RuntimeError("No context run found")
    nodes = {row["id"]: row for row in source_conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node")}
    queue = {item["candidate_id"]: item for item in load_queue(args.review_db, args.ledger)}
    stage_conn.execute("DELETE FROM staged_repair WHERE proposal_method=?", (PASS_VERSION,))
    staged = 0
    held = 0
    for row in output.execute(
        "SELECT * FROM context_adjudication WHERE run_id=? AND decision='REFERENCE' AND target_status='unique_local_target' ORDER BY candidate_id",
        (run["run_id"],),
    ):
        candidate = queue.get(row["candidate_id"])
        source = nodes.get(candidate["source_node_id"]) if candidate else None
        target = nodes.get(row["target_node_id"])
        if candidate is None or source is None or target is None:
            held += 1
            continue
        # The stage schema's quoted_text must match the stored absolute span;
        # keep the model's broader evidence quote in evidence_json instead.
        quote = candidate.get("quoted_text") or candidate.get("candidate_text") or ""
        text = source_text(source)
        if not quote_in_source(text, quote):
            held += 1
            continue
        confidence = float(row["confidence"] or 0)
        if confidence < args.min_confidence:
            held += 1
            continue
        evidence = json.loads(row["evidence_json"] or "{}")
        evidence["context_run_id"] = run["run_id"]
        evidence["adjudication_id"] = row["candidate_id"]
        relationship_type, _edge_type = relationship_for_target(target)
        insert_stage(
            stage_conn,
            run_id=run["run_id"],
            source=source,
            target=target,
            candidate_id=row["candidate_id"],
            start=candidate.get("span_start"),
            end=candidate.get("span_end"),
            quote=quote,
            candidate_text=candidate.get("candidate_text") or "",
            citation_kind=candidate.get("candidate_kind") or "structure_reference",
            method=PASS_VERSION,
            confidence=confidence,
            status="eligible",
            reasons=json.loads(row["reason_json"] or "[]"),
            evidence=evidence,
            relationship_type=relationship_type,
        )
        staged += 1
    stage_conn.commit()
    stage_conn.close()
    source_conn.close()
    output.close()
    return {"run_id": run["run_id"], "staged": staged, "held": held, "stage": str(args.stage)}


def stats(args: argparse.Namespace) -> dict[str, Any]:
    conn = connect_output(args.output)
    run = conn.execute("SELECT * FROM context_run ORDER BY started_at DESC LIMIT 1").fetchone()
    if not run:
        return {"runs": 0}
    summary = json.loads(run["summary_json"] or "{}")
    summary.update({
        "run_id": run["run_id"],
        "request_status": {row["status"]: row["count"] for row in conn.execute("SELECT status,COUNT(*) count FROM context_request WHERE run_id=? GROUP BY status", (run["run_id"],))},
        "adjudication_status": {f"{row['decision']}/{row['target_status']}": row["count"] for row in conn.execute("SELECT decision,target_status,COUNT(*) count FROM context_adjudication WHERE run_id=? GROUP BY decision,target_status", (run["run_id"],))},
    })
    conn.close()
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    p.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--model", default=DEFAULT_MODEL)
    prep.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    prep.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    prep.add_argument("--neighbour-blocks", type=int, default=DEFAULT_NEIGHBOURS)
    prep.add_argument("--limit", type=int)
    prep.set_defaults(func=prepare)
    retry = sub.add_parser("prepare-retry")
    retry.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    retry.add_argument("--neighbour-blocks", type=int, default=DEFAULT_NEIGHBOURS + 2)
    retry.set_defaults(func=prepare_retry)
    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--backend", choices=["openai", "openclaw", "codex", "gemini"], default=os.environ.get("PRA_LLM_REFERENCE_BACKEND", "openai"))
    run_cmd.add_argument("--model", default="")
    run_cmd.add_argument("--workers", type=int, default=4)
    run_cmd.add_argument("--thinking", choices=["off", "minimal", "low", "medium", "high", "xhigh", "adaptive"], default="low")
    run_cmd.add_argument("--limit", type=int)
    run_cmd.set_defaults(func=run_requests)
    adj = sub.add_parser("adjudicate")
    adj.set_defaults(func=adjudicate)
    stage_cmd = sub.add_parser("stage")
    stage_cmd.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    stage_cmd.add_argument("--min-confidence", type=float, default=0.65)
    stage_cmd.set_defaults(func=stage)
    st = sub.add_parser("stats")
    st.set_defaults(func=stats)
    return p


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(args.func(args), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()