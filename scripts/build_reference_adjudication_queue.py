#!/usr/bin/env python3
"""Build a review queue for the residual competing local targets.

This is intentionally a queue builder, not an edge writer.  It selects only
review rows whose deterministic resolver found more than one local target,
then gives a human/LLM adjudicator the exact source span, surrounding context,
candidate target metadata, and any conservative context-only recommendation.
No recommendation is materialised automatically.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-final4-20260801.sqlite3"
DEFAULT_QUEUE = ROOT / "logs" / "reference-recall-adjudication-queue-final4-20260801.jsonl"
DEFAULT_SUMMARY = ROOT / "logs" / "reference-recall-adjudication-queue-final4-20260801.json"
QUEUE_VERSION = "reference-recall-adjudication-queue-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalised(value: str | None) -> str:
    value = (value or "").casefold().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def metadata(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        value = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def document_title(meta: dict[str, Any], row: sqlite3.Row) -> str:
    value = str(meta.get("document_title") or "").strip()
    if value:
        return value
    reader = meta.get("reader_reference_text")
    if isinstance(reader, dict) and reader.get("source_title"):
        return str(reader["source_title"]).strip()
    if row["node_type"] in {"guidance_document", "guidance_section", "guidance_paragraph"}:
        return str(meta.get("source_title") or row["title"] or "").strip()
    return ""


def target_metadata(row: sqlite3.Row) -> dict[str, Any]:
    meta = metadata(row)
    return {
        "id": row["id"],
        "node_type": row["node_type"],
        "title": row["title"] or "",
        "url": row["url"] or "",
        "part_title": str(meta.get("part_title") or ""),
        "chapter_title": str(meta.get("chapter_title") or ""),
        "document_title": document_title(meta, row),
        "rule_number": str(meta.get("rule_number") or ""),
        "paragraph_number": str(meta.get("paragraph_number") or ""),
        "section_number": str(meta.get("section_number") or ""),
        "article_number": str(meta.get("article_number") or ""),
    }


def source_context(value: str, start: int | None, end: int | None, radius: int = 280) -> str:
    if not isinstance(start, int) or not isinstance(end, int):
        return " ".join((value or "").split())[: radius * 2]
    return " ".join(value[max(0, start - radius) : min(len(value), end + radius)].split())


def recommendation(
    source: sqlite3.Row,
    quoted: str,
    targets: list[dict[str, Any]],
) -> tuple[str, str, list[str], list[dict[str, Any]]]:
    """Return a recommendation only when one target has a unique strong score."""

    source_meta = metadata(source)
    source_part = normalised(source_meta.get("part_title"))
    source_doc = normalised(document_title(source_meta, source))
    text = normalised(quoted)
    scored: list[dict[str, Any]] = []
    for target in targets:
        score = 0
        reasons: list[str] = []
        target_part = normalised(target.get("part_title"))
        target_doc = normalised(target.get("document_title"))
        target_title = normalised(target.get("title"))
        if source_part and target_part and source_part == target_part:
            # The source node's containing Part is only a weak prior: a
            # citation may deliberately point into a different Part.
            score += 3
            reasons.append("source_part_matches_target_part")
        if source_doc and target_doc and source_doc == target_doc:
            score += 3
            reasons.append("source_document_matches_target_document")
        if target_part and target_part in text:
            # Explicit ``of the X Part`` context is stronger than the source
            # node's own parent and should win when the two differ.
            score += 8
            reasons.append("citation_names_target_part")
        if target_doc and target_doc in text:
            score += 8
            reasons.append("citation_names_target_document")
        if target_title and target_title in text:
            score += 4
            reasons.append("citation_contains_target_title")
        scored.append({"target_id": target["id"], "score": score, "reasons": reasons})
    best = max((item["score"] for item in scored), default=0)
    winners = [item for item in scored if item["score"] == best and best >= 4]
    if len(winners) == 1:
        return winners[0]["target_id"], "unique_context_score", winners[0]["reasons"], scored
    return "", "requires_adjudication", [], scored


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect(args.db, readonly=True)
    review_conn = connect(args.review_db, readonly=True)
    nodes = {
        row["id"]: row
        for row in source_conn.execute(
            "SELECT id,node_type,title,text,url,metadata_json FROM node"
        )
    }
    args.queue.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    recommendation_counts: Counter[str] = Counter()
    target_count_buckets: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    rows = review_conn.execute(
        """
        SELECT * FROM corpus_review
        WHERE decision='AMBIGUOUS' AND target_status='multiple_local_targets'
        ORDER BY candidate_id
        """
    )
    for row in rows:
        evidence = json.loads(row["evidence_json"] or "{}")
        target_ids = list(dict.fromkeys(evidence.get("candidate_target_ids") or []))
        if len(target_ids) < 2:
            counts["skipped_without_competing_targets"] += 1
            continue
        source = nodes.get(row["source_node_id"])
        if source is None:
            counts["skipped_missing_source"] += 1
            continue
        value = source["text"] or source["title"] or ""
        target_rows = [nodes[target_id] for target_id in target_ids if target_id in nodes]
        targets = [target_metadata(target) for target in target_rows]
        if len(targets) < 2:
            counts["skipped_missing_target_rows"] += 1
            continue
        recommended_id, recommendation_method, recommendation_reasons, scored = recommendation(
            source, row["quoted_text"] or row["candidate_text"] or "", targets
        )
        target_count_buckets[str(len(targets))] += 1
        recommendation_counts["recommended" if recommended_id else "pending"] += 1
        counts[row["candidate_kind"] or "unknown"] += 1
        records.append(
            {
                "queue_version": QUEUE_VERSION,
                "queue_status": "pending_adjudication",
                "candidate_id": row["candidate_id"],
                "source_node_id": source["id"],
                "source_node_type": source["node_type"],
                "source_title": source["title"] or "",
                "source_url": source["url"] or "",
                "source_text_hash": row["source_text_hash"],
                "span_start": row["span_start"],
                "span_end": row["span_end"],
                "quoted_text": row["quoted_text"] or row["candidate_text"] or "",
                "context_text": source_context(value, row["span_start"], row["span_end"]),
                "candidate_kind": row["candidate_kind"] or "",
                "candidate_text": row["candidate_text"] or "",
                "targets": targets,
                "recommended_target_id": recommended_id,
                "recommendation_method": recommendation_method,
                "recommendation_reasons": recommendation_reasons,
                "target_scores": scored,
                "adjudicator_decision": "",
                "adjudicator_notes": "",
                "created_at": now(),
            }
        )
    with args.queue.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "queue_version": QUEUE_VERSION,
        "generated_at": now(),
        "db": str(args.db),
        "review_db": str(args.review_db),
        "queue": str(args.queue),
        "records": len(records),
        "candidate_kinds": dict(sorted(counts.items())),
        "recommendations": dict(sorted(recommendation_counts.items())),
        "target_count_buckets": dict(sorted(target_count_buckets.items(), key=lambda item: int(item[0]))),
        "materialized": False,
        "note": "Recommendations are advisory only; competing targets require human or LLM adjudication before materialisation.",
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    source_conn.close()
    review_conn.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), indent=2, ensure_ascii=False, sort_keys=True))
