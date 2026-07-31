#!/usr/bin/env python3
"""Validate and locally adjudicate reviewer JSONL without graph writes.

The importer accepts complete or partial reviewer batches.  It records every
finding, including invalid, negative and unresolved findings, in a separate
review SQLite database.  A finding is only marked ``eligible_reviewed`` when
the live source hash, exact absolute span, unique local target, reviewer
decision/confidence and duplicate/self-reference checks all pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rulebook_scraper.legal_references import (  # noqa: E402
    DEFAULT_INSTRUMENT_REGISTRY,
    InstrumentRegistry,
    citation_occurrences,
    external_provision_node_id,
)
from scripts.reference_recall_audit import (  # noqa: E402
    connect_source,
    digest,
    existing_occurrences,
    json_load,
    normalised,
    overlap,
    source_text,
)
from scripts.reference_recall_batches import load_pilot, request_id  # noqa: E402
from scripts.reference_recall_stage import (  # noqa: E402
    build_target_indexes,
    candidate_targets,
    context_keys,
    exact_spans,
    existing_relationship,
    same_context,
)
from scripts.validate_reference_recall_reviews import (  # noqa: E402
    DECISIONS,
    parse_content,
    validate_finding,
)


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REVIEW_DB = ROOT / "logs" / "reference-recall-reviews-20260731.sqlite3"
REVIEW_VERSION = "reference-recall-review-import-v1"
MIN_REVIEW_CONFIDENCE = 0.90

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_run (
  run_id TEXT PRIMARY KEY,
  review_version TEXT NOT NULL,
  pilot_path TEXT NOT NULL,
  responses_path TEXT NOT NULL,
  source_db TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS review_finding (
  finding_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  custom_id TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  source_node_type TEXT DEFAULT '',
  source_title TEXT DEFAULT '',
  source_text_hash TEXT NOT NULL,
  span_start INTEGER,
  span_end INTEGER,
  quoted_text TEXT NOT NULL DEFAULT '',
  target_hint TEXT DEFAULT '',
  target_kind TEXT DEFAULT '',
  decision TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  validation_json TEXT NOT NULL DEFAULT '[]',
  candidate_target_ids_json TEXT NOT NULL DEFAULT '[]',
  target_node_id TEXT DEFAULT '',
  target_node_type TEXT DEFAULT '',
  target_title TEXT DEFAULT '',
  resolution_method TEXT DEFAULT '',
  resolution_confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  reason_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_finding_status
  ON review_finding(status,decision,confidence DESC);
CREATE INDEX IF NOT EXISTS idx_review_finding_source
  ON review_finding(source_node_id,span_start,span_end);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: object, length: int = 28) -> str:
    return hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:length]


def connect_review(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def metadata(row: sqlite3.Row) -> dict[str, Any]:
    return json_load(row["metadata_json"], {})


def source_nodes(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["id"]: row
        for row in conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node")
    }


def target_kind_to_candidate_kind(target_kind: str) -> str:
    value = (target_kind or "").casefold()
    if value in {"article", "section", "regulation", "statute", "directive", "legal_instrument"}:
        return "legal_citation"
    if value in {"part", "chapter", "annex", "table", "template", "form", "rule", "paragraph", "subparagraph", "point"}:
        return "structure_reference"
    if value in {"guidance", "guidance_document", "policy_statement", "consultation"}:
        return "named_document"
    if value in {"definition", "defined_term"}:
        return "defined_term"
    return "named_document"


def defined_term_candidates(nodes: dict[str, sqlite3.Row], hint: str) -> list[sqlite3.Row]:
    wanted = normalised(hint).strip(" '‘’\"")
    if not wanted:
        return []
    exact = [
        node for node in nodes.values()
        if node["node_type"] == "defined_term" and normalised(node["title"]) == wanted
    ]
    if exact:
        return exact
    return [
        node for node in nodes.values()
        if node["node_type"] == "defined_term"
        and (normalised(node["title"]) in wanted or wanted in normalised(node["title"]))
    ]


def legal_candidates(
    source: sqlite3.Row,
    value: str,
    finding: dict[str, Any],
    registry: InstrumentRegistry,
    nodes: dict[str, sqlite3.Row],
) -> tuple[list[sqlite3.Row], str, float]:
    start, end = finding.get("span_start"), finding.get("span_end")
    try:
        occurrences = citation_occurrences(
            source_node_id=source["id"],
            value=value,
            registry=registry,
            source_title=source["title"] or "",
        )
    except Exception:
        occurrences = []
    matches: list[sqlite3.Row] = []
    for occurrence in occurrences:
        if start is not None and end is not None and overlap(start, end, occurrence.span_start, occurrence.span_end) == 0:
            continue
        if not occurrence.instrument or not occurrence.provision_path:
            continue
        target = nodes.get(external_provision_node_id(occurrence.instrument, occurrence.provision_path))
        if target is not None:
            matches.append(target)
    unique = list({target["id"]: target for target in matches}.values())
    return unique, "legal_registry_overlap", max((float(item.confidence or 0) for item in occurrences), default=0.0)


def resolve_target(
    source: sqlite3.Row,
    finding: dict[str, Any],
    nodes: dict[str, sqlite3.Row],
    indexes: dict[str, Any],
    registry: InstrumentRegistry,
) -> tuple[list[sqlite3.Row], str, float]:
    hint = str(finding.get("target_hint") or "").strip()
    quote = str(finding.get("quoted_text") or "")
    candidate_kind = target_kind_to_candidate_kind(str(finding.get("target_kind") or ""))
    value = quote or hint
    if candidate_kind == "legal_citation":
        candidates, method, confidence = legal_candidates(source, source_text(source), finding, registry, nodes)
        if candidates:
            return candidates, method, confidence
    if candidate_kind == "defined_term":
        candidates = defined_term_candidates(nodes, hint or quote)
        if len(candidates) == 1:
            return candidates, "exact_defined_term_title", 0.94
        return candidates, "defined_term_title", 0.0
    synthetic = {
        "candidate_text": value,
        "candidate_kind": candidate_kind,
    }
    candidates, method, confidence = candidate_targets(source, synthetic, nodes, indexes)
    if candidates:
        return candidates, method, confidence
    # Named guidance/title hints are often longer than the exact title.  A
    # second pass looks for the distinctive code (SS/SoP/CP/PS) in titles.
    hint_norm = normalised(hint)
    code_match = re.search(r"\b(?:SS|SoP|CP|PS)\s*\d+\s*/\s*\d{2}\b", hint, re.IGNORECASE)
    if hint_norm and code_match:
        code = normalised(code_match.group(0))
        code_candidates = [
            node for node in nodes.values()
            if node["node_type"] == "guidance_document"
            and code in normalised(node["title"])
        ]
        if len(code_candidates) == 1:
            return code_candidates, "guidance_code_title", 0.90
    return [], method, confidence


def existing_occurrence(source_conn: sqlite3.Connection, source_id: str, start: int, end: int) -> sqlite3.Row | None:
    for row in source_conn.execute(
        "SELECT occurrence_id,target_node_id,edge_id,span_start,span_end FROM reference_occurrence WHERE source_node_id=? AND status='materialized'",
        (source_id,),
    ):
        if overlap(start, end, row["span_start"], row["span_end"]) > 0:
            return row
    return None


def import_reviews(args: argparse.Namespace) -> dict[str, Any]:
    pilot_rows = load_pilot(args.pilot)
    expected: dict[str, dict[str, Any]] = {}
    for row in pilot_rows:
        custom_id = request_id(
            str(row.get("source_node_id") or ""),
            str(row.get("source_text_hash") or ""),
            int(row.get("chunk_start") or 0),
            int(row.get("chunk_end") or 0),
        )
        expected[custom_id] = row

    source_conn = connect_source(args.db)
    nodes = source_nodes(source_conn)
    indexes = build_target_indexes(nodes)
    registry = InstrumentRegistry.load(args.instrument_registry)
    review = connect_review(args.review_db)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + stable_id(args.responses, now(), length=8)
    review.execute(
        "INSERT INTO review_run(run_id,review_version,pilot_path,responses_path,source_db,started_at) VALUES (?,?,?,?,?,?)",
        (run_id, REVIEW_VERSION, str(args.pilot), str(args.responses), str(args.db), now()),
    )

    seen: set[str] = set()
    status_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    invalid_records = 0
    findings_count = 0
    with args.responses.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                custom_id = str(record.get("custom_id") or "")
                row = expected.get(custom_id)
                if row is None:
                    invalid_records += 1
                    continue
                seen.add(custom_id)
                parsed = parse_content(record)
                if parsed.get("source_node_id") not in (None, row.get("source_node_id")):
                    invalid_records += 1
                if parsed.get("source_text_hash") not in (None, row.get("source_text_hash")):
                    invalid_records += 1
                findings = parsed.get("findings")
                if not isinstance(findings, list):
                    invalid_records += 1
                    continue
                source = nodes.get(str(row.get("source_node_id") or ""))
                live_hash_ok = source is not None and digest(source_text(source)) == str(row.get("source_text_hash") or "")
                for index, finding in enumerate(findings):
                    findings_count += 1
                    if not isinstance(finding, dict):
                        continue
                    decision = str(finding.get("decision") or "")
                    decision_counts[decision] += 1
                    errors = validate_finding(finding, row)
                    if not live_hash_ok:
                        errors.append("live_source_hash_mismatch")
                    finding_id = stable_id(run_id, custom_id, index, json.dumps(finding, sort_keys=True), length=28)
                    status = "invalid"
                    reasons = list(errors)
                    candidates: list[sqlite3.Row] = []
                    method = ""
                    resolution_confidence = 0.0
                    target = None
                    occurrence = None
                    if not errors and source is not None and decision in DECISIONS:
                        candidates, method, resolution_confidence = resolve_target(source, finding, nodes, indexes, registry)
                        if decision == "NOT_REFERENCE":
                            status, reasons = "not_reference", ["reviewer_classified_not_reference"]
                        elif decision == "AMBIGUOUS":
                            status, reasons = "ambiguous", ["reviewer_classified_ambiguous"]
                        elif len(candidates) != 1:
                            status = "unresolved_target" if not candidates else "ambiguous_target"
                            reasons = ["reviewer_reference_has_no_unique_local_target"]
                        else:
                            target = candidates[0]
                            start, end = int(finding["span_start"]), int(finding["span_end"])
                            if target["id"] == source["id"]:
                                status, reasons = "self_reference", ["source_and_target_are_identical"]
                            elif existing_relationship(source_conn, source["id"], target["id"]):
                                status, reasons = "duplicate_edge", ["relationship_already_exists"]
                            elif (occurrence := existing_occurrence(source_conn, source["id"], start, end)) is not None:
                                status, reasons = "duplicate_occurrence", ["source_span_already_has_occurrence"]
                            elif float(finding.get("confidence") or 0) < args.min_confidence or resolution_confidence < args.min_resolution_confidence:
                                status, reasons = "low_confidence", ["reviewer_or_resolver_confidence_below_threshold"]
                            else:
                                status, reasons = "eligible_reviewed", ["exact_span_unique_target_and_review_thresholds_pass"]
                    target_ids = [candidate["id"] for candidate in candidates]
                    review.execute(
                        """
                        INSERT OR REPLACE INTO review_finding(
                          finding_id,run_id,custom_id,source_node_id,source_node_type,
                          source_title,source_text_hash,span_start,span_end,quoted_text,
                          target_hint,target_kind,decision,confidence,validation_json,
                          candidate_target_ids_json,target_node_id,target_node_type,
                          target_title,resolution_method,resolution_confidence,status,
                          reason_json,metadata_json,created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            finding_id,
                            run_id,
                            custom_id,
                            row.get("source_node_id") or "",
                            row.get("source_node_type") or "",
                            row.get("source_title") or "",
                            row.get("source_text_hash") or "",
                            finding.get("span_start"),
                            finding.get("span_end"),
                            finding.get("quoted_text") or "",
                            finding.get("target_hint") or "",
                            finding.get("target_kind") or "",
                            decision,
                            float(finding.get("confidence") or 0),
                            json.dumps(errors, ensure_ascii=False),
                            json.dumps(target_ids, ensure_ascii=False),
                            target["id"] if target else "",
                            target["node_type"] if target else "",
                            target["title"] if target else "",
                            method,
                            resolution_confidence,
                            status,
                            json.dumps(sorted(set(reasons)), ensure_ascii=False),
                            json.dumps({"line": line_number, "occurrence_id": occurrence["occurrence_id"] if occurrence else ""}, ensure_ascii=False),
                            now(),
                        ),
                    )
                    status_counts[status] += 1
            except Exception as exc:
                invalid_records += 1
                status_counts["invalid_record"] += 1

    summary = {
        "run_id": run_id,
        "review_version": REVIEW_VERSION,
        "pilot_requests": len(expected),
        "received_requests": len(seen),
        "missing_requests": len(set(expected) - seen),
        "findings": findings_count,
        "invalid_records": invalid_records,
        "decisions": dict(sorted(decision_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "review_db": str(args.review_db),
        "source_db": str(args.db),
    }
    review.execute(
        "UPDATE review_run SET finished_at=?,summary_json=? WHERE run_id=?",
        (now(), json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
    )
    review.commit()
    review.close()
    source_conn.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW_DB)
    parser.add_argument("--instrument-registry", type=Path, default=DEFAULT_INSTRUMENT_REGISTRY)
    parser.add_argument("--min-confidence", type=float, default=MIN_REVIEW_CONFIDENCE)
    parser.add_argument("--min-resolution-confidence", type=float, default=0.90)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(import_reviews(args), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
