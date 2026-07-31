#!/usr/bin/env python3
"""Complete the reference-recall ledger with an auditable corpus review.

The pilot used an independent reviewer model.  The rest of the corpus must
still receive an explicit outcome even when a citation has no local target.
This pass is deliberately conservative and deterministic: it validates the
live source hash/span, classifies each candidate, resolves cheap unique local
targets, and records external/unresolved or ambiguous outcomes separately.
It never writes the source Rulebook database.  When ``--stage`` is supplied,
unique validated local targets are copied to a separate materialisation stage
for the existing dry-run/apply gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reference_recall_audit import (  # noqa: E402
    connect_source,
    digest,
    generic_named_instrument_label,
    json_load,
    normalised,
    overlap,
    source_text,
)
from scripts.reference_recall_stage import (  # noqa: E402
    build_target_indexes,
    candidate_targets,
    connect_stage,
    insert_stage,
    metadata,
    relationship_for_target,
    source_rows,
    structural_parts,
)


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_LEDGER = ROOT / "logs" / "reference-recall-ledger-final-20260731.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-20260731.sqlite3"
REVIEW_VERSION = "reference-recall-corpus-review-v1"
STAGE_METHOD = "corpus_review_deterministic_v1"

REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_run (
  run_id TEXT PRIMARY KEY,
  review_version TEXT NOT NULL,
  source_db TEXT NOT NULL,
  ledger_db TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS corpus_review (
  review_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL UNIQUE,
  source_node_id TEXT NOT NULL,
  source_node_type TEXT NOT NULL,
  source_title TEXT NOT NULL DEFAULT '',
  source_text_hash TEXT NOT NULL,
  span_start INTEGER,
  span_end INTEGER,
  quoted_text TEXT NOT NULL DEFAULT '',
  candidate_text TEXT NOT NULL DEFAULT '',
  candidate_kind TEXT NOT NULL DEFAULT '',
  ledger_status TEXT NOT NULL,
  decision TEXT NOT NULL,
  target_status TEXT NOT NULL,
  target_node_id TEXT DEFAULT '',
  target_title TEXT DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  method TEXT NOT NULL,
  reason_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corpus_review_decision
  ON corpus_review(decision,target_status);
CREATE INDEX IF NOT EXISTS idx_corpus_review_source
  ON corpus_review(source_node_id,span_start,span_end);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_ledger(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect_review(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(REVIEW_SCHEMA)
    return conn


def review_id(candidate_id: str, source_hash: str, decision: str) -> str:
    return hashlib.sha1(f"{candidate_id}|{source_hash}|{decision}".encode()).hexdigest()[:28]


def exact_quote(text: str, start: int | None, end: int | None, candidate_text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if start is None or end is None or start < 0 or end <= start or end > len(text):
        return candidate_text or "", ["invalid_source_span"]
    quote = text[start:end]
    if candidate_text and quote != candidate_text:
        reasons.append("candidate_text_differs_from_live_span")
    return quote, reasons


def classify_candidate(
    row: sqlite3.Row,
    source: sqlite3.Row | None,
    text: str,
    target_candidates: list[sqlite3.Row],
    target_method: str,
    target_confidence: float,
    existing_edges: set[tuple[str, str]],
    existing_spans: dict[str, list[tuple[int | None, int | None]]],
) -> tuple[str, str, sqlite3.Row | None, float, list[str]]:
    """Return decision, target status, target, confidence and reasons."""
    if source is None:
        return "INVALID", "invalid_source", None, 0.0, ["source_node_missing"]
    kind = row["candidate_kind"] or ""
    status = row["status"] or ""
    candidate_text = row["candidate_text"] or ""
    source_id = source["id"]
    if status in {"covered_edge", "covered_occurrence", "covered_llm"}:
        return "ALREADY_COVERED", "covered", None, 1.0, ["ledger_already_has_reference_evidence"]
    if status == "classified_not_reference":
        return "NOT_REFERENCE", "not_applicable", None, 1.0, ["ledger_classified_not_reference"]
    if status == "excluded_context":
        return "NOT_REFERENCE", "excluded_context", None, 0.98, ["ledger_excluded_context"]

    if target_candidates:
        if len(target_candidates) == 1:
            target = target_candidates[0]
            if target["id"] == source_id:
                return "NOT_REFERENCE", "self_reference", None, 0.99, ["candidate_resolves_to_source"]
            if (source_id, target["id"]) in existing_edges:
                return "ALREADY_COVERED", "existing_relationship", target, 1.0, ["relationship_already_exists"]
            if any(
                overlap(row["span_start"], row["span_end"], old_start, old_end) > 0
                for old_start, old_end in existing_spans.get(source_id, [])
            ):
                return "ALREADY_COVERED", "existing_occurrence", target, 1.0, ["source_span_already_has_occurrence"]
            return "REFERENCE", "unique_local_target", target, target_confidence or 0.9, [f"unique_{target_method}"]
        return "AMBIGUOUS", "multiple_local_targets", None, 0.5, ["multiple_local_targets"]

    if kind == "named_instrument" and generic_named_instrument_label(candidate_text):
        return "NOT_REFERENCE", "generic_instrument_label", None, 0.98, ["generic_or_table_instrument_label"]

    if kind in {"legal_citation", "article_citation"}:
        # Legal/article detector output is a strong reference signal even
        # when the cited instrument is external or absent from the node set.
        return "REFERENCE", "external_or_unresolved", None, 0.9, ["legal_or_article_detector_without_unique_local_target"]

    if kind == "named_instrument":
        return "REFERENCE", "external_or_unresolved", None, 0.82, ["specific_named_instrument_without_unique_local_target"]

    if kind == "named_document":
        if normalised(candidate_text) == normalised(source["title"] or ""):
            return "NOT_REFERENCE", "source_title", None, 0.99, ["candidate_matches_source_title"]
        return "REFERENCE", "external_or_unresolved", None, 0.78, ["named_document_without_unique_local_target"]

    if kind == "structure_reference":
        label, _identifier, explicit = structural_parts(candidate_text)
        if label in {"article", "regulation", "section", "rule", "paragraph", "annex", "schedule", "template", "table", "form"} and explicit:
            return "REFERENCE", "external_or_unresolved", None, 0.78, ["explicit_structural_reference_without_unique_local_target"]
        return "AMBIGUOUS", "structural_context_required", None, 0.5, ["structural_reference_needs_context"]

    return "AMBIGUOUS", "unsupported_candidate_kind", None, 0.4, ["candidate_kind_requires_adjudication"]


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect_source(args.db)
    ledger = connect_ledger(args.ledger)
    review = connect_review(args.review_db)
    stage = connect_stage(args.stage) if args.stage else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + digest(now(), "sha1")[:8]
    review.execute(
        "INSERT INTO review_run(run_id,review_version,source_db,ledger_db,started_at) VALUES (?,?,?,?,?)",
        (run_id, REVIEW_VERSION, str(args.db), str(args.ledger), now()),
    )
    if stage is not None:
        stage.execute("DELETE FROM staged_repair")
        stage_run_id = run_id
    else:
        stage_run_id = ""

    nodes = source_rows(source_conn)
    target_indexes = build_target_indexes(nodes)
    texts = {node_id: source_text(row) for node_id, row in nodes.items()}
    existing_edges = {
        (row["from_node_id"], row["to_node_id"])
        for row in source_conn.execute("SELECT from_node_id,to_node_id FROM edge")
    }
    existing_spans: dict[str, list[tuple[int | None, int | None]]] = defaultdict(list)
    for row in source_conn.execute("SELECT source_node_id,span_start,span_end FROM reference_occurrence"):
        existing_spans[row["source_node_id"]].append((row["span_start"], row["span_end"]))

    counts: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    processed = 0
    for row in ledger.execute("SELECT * FROM reference_gap ORDER BY candidate_id"):
        source = nodes.get(row["source_node_id"])
        text = texts.get(row["source_node_id"], "")
        quote, quote_reasons = exact_quote(text, row["span_start"], row["span_end"], row["candidate_text"])
        reasons = list(quote_reasons)
        target_candidates: list[sqlite3.Row] = []
        target_method = ""
        target_confidence = 0.0
        source_hash_mismatch = bool(
            source is not None
            and row["source_text_hash"]
            and row["source_text_hash"] != digest(text)
        )
        # The structural/named resolver is cheap and works for all source
        # types. Legal citations are intentionally classified as external or
        # unresolved here; the full registry matcher is too costly for the
        # corpus tail and is already applied to the pilot/high-confidence set.
        if (
            source is not None
            and not source_hash_mismatch
            and row["candidate_kind"] not in {"legal_citation", "article_citation"}
        ):
            try:
                target_candidates, target_method, target_confidence = candidate_targets(source, row, nodes, target_indexes)
            except (KeyError, TypeError, ValueError):
                target_candidates, target_method, target_confidence = [], "resolver_error", 0.0
        if source_hash_mismatch:
            decision, target_status, target, confidence, decision_reasons = (
                "INVALID", "stale_source_hash", None, 0.0, ["source_text_hash_mismatch"]
            )
        else:
            decision, target_status, target, confidence, decision_reasons = classify_candidate(
                row, source, text, target_candidates, target_method, target_confidence,
                existing_edges, existing_spans,
            )
        reasons.extend(decision_reasons)
        evidence = {
            "scanner_version": row["scanner_version"],
            "ledger_status": row["status"],
            "detectors": json_load(row["detector_json"], []),
            "target_method": target_method,
            "candidate_target_ids": [item["id"] for item in target_candidates],
        }
        review.execute(
            """
            INSERT OR REPLACE INTO corpus_review(
              review_id,run_id,candidate_id,source_node_id,source_node_type,source_title,
              source_text_hash,span_start,span_end,quoted_text,candidate_text,candidate_kind,
              ledger_status,decision,target_status,target_node_id,target_title,confidence,
              method,reason_json,evidence_json,reviewed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id(row["candidate_id"], row["source_text_hash"], decision), run_id,
                row["candidate_id"], row["source_node_id"], row["source_node_type"], row["source_title"],
                row["source_text_hash"], row["span_start"], row["span_end"], quote,
                row["candidate_text"], row["candidate_kind"], row["status"], decision, target_status,
                target["id"] if target else "", target["title"] if target else "", float(confidence),
                REVIEW_VERSION, json.dumps(sorted(set(reasons)), ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False), now(),
            ),
        )
        counts[decision] += 1
        target_counts[target_status] += 1

        if stage is not None and decision == "REFERENCE" and target is not None and target_status == "unique_local_target":
            relationship_type, _edge_type = relationship_for_target(target)
            insert_stage(
                stage,
                run_id=stage_run_id,
                source=source,
                target=target,
                candidate_id=row["candidate_id"],
                start=row["span_start"],
                end=row["span_end"],
                quote=quote,
                candidate_text=row["candidate_text"],
                citation_kind=row["candidate_kind"],
                method=STAGE_METHOD,
                confidence=confidence,
                status="eligible",
                reasons=decision_reasons,
                evidence=evidence,
                relationship_type=relationship_type,
            )
        processed += 1

    summary = {
        "run_id": run_id,
        "review_version": REVIEW_VERSION,
        "ledger": str(args.ledger),
        "source_db": str(args.db),
        "review_db": str(args.review_db),
        "processed": processed,
        "decision_counts": dict(sorted(counts.items())),
        "target_status_counts": dict(sorted(target_counts.items())),
        "stage": str(args.stage) if args.stage else "",
    }
    review.execute("UPDATE review_run SET finished_at=?,summary_json=? WHERE run_id=?", (now(), json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id))
    review.commit()
    if stage is not None:
        stage.commit()
        stage.execute(
            "INSERT OR REPLACE INTO stage_run(run_id,stage_version,source_db,ledger_db,started_at,finished_at,summary_json) VALUES (?,?,?,?,?,?,?)",
            (stage_run_id, REVIEW_VERSION, str(args.db), str(args.ledger), now(), now(), json.dumps(summary, ensure_ascii=False)),
        )
        stage.commit()
        stage.close()
    review.close()
    source_conn.close()
    ledger.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--stage", type=Path, help="optional separate stage DB for uniquely resolved local targets")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(run(args), indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
