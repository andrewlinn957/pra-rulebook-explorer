#!/usr/bin/env python3
"""Stage registry-backed legal citations in aggregate Rulebook nodes.

The main legal backfill intentionally scans atomic rules, guidance paragraphs,
and definitions.  Aggregate Parts/Chapters/Guidance documents still contain
real legal citations, however, and the recall ledger exposes them as
unresolved. This targeted pass scans only those residual source nodes,
resolves each candidate in a bounded local snippet, and stages only citations
whose target node already exists. It never fetches or creates a legal node.
Bounded snippets are important because the detector has backtracking-heavy
expressions and a full scan of every 300k-character Part is effectively
quadratic.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from dataclasses import replace
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
from scripts.backfill_legal_references import (  # noqa: E402
    apply_context_override,
    contextual_section_hints,
)
from scripts.reference_recall_audit import connect_source, digest, source_text  # noqa: E402
from scripts.reference_recall_stage import connect_stage, insert_stage  # noqa: E402


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-recommended-final-20260731.sqlite3"
DEFAULT_STAGE = ROOT / "logs" / "reference-recall-recommended-order-20260731.sqlite3"
DEFAULT_REGISTRY = ROOT / "config" / "legal_instruments.json"
DEFAULT_OVERRIDES = ROOT / "config" / "legal_reference_overrides.json"
DEFAULT_AUDIT = ROOT / "logs" / "reference-recall-legal-context-stage-20260731.json"
METHOD = "corpus_legal_context_v1"
DEFAULT_WINDOW_PADDING = 900
DEFAULT_MAX_WINDOW = 18_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_rows(conn: sqlite3.Connection, source_ids: set[str]) -> dict[str, sqlite3.Row]:
    if not source_ids:
        return {}
    placeholders = ",".join("?" for _ in source_ids)
    return {
        row["id"]: row
        for row in conn.execute(
            f"SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id IN ({placeholders})",
            sorted(source_ids),
        )
    }


def existing_state(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], dict[str, list[tuple[int, int]]]]:
    edges = {(row["from_node_id"], row["to_node_id"]) for row in conn.execute("SELECT from_node_id,to_node_id FROM edge")}
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in conn.execute("SELECT source_node_id,span_start,span_end FROM reference_occurrence WHERE status='materialized'"):
        if row["span_start"] is not None and row["span_end"] is not None:
            spans[row["source_node_id"]].append((int(row["span_start"]), int(row["span_end"])))
    return edges, spans


def local_article_targets(conn: sqlite3.Connection) -> dict[str, str]:
    """Index unique substantive Article titles already present in the graph.

    The legal registry represents an article as an external provision, but
    UK-CRR articles are already first-class Rulebook nodes.  Aggregate CRR
    text therefore needs the same internal target selection as the atomic
    legal backfill, including paragraph-qualified titles such as
    ``Article 277(3)``.
    """

    candidates: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT id,node_type,title
        FROM node
        WHERE node_type IN ('chapter','rule','external_reference')
          AND COALESCE(text,'')<>''
          AND (title LIKE 'Article %' OR title LIKE 'UK CRR Article %')
        """
    ):
        title = " ".join(str(row["title"] or "").split()).casefold()
        if title.startswith("uk crr "):
            title = title[7:]
        if not title.startswith("article "):
            continue
        # Prefer a rule node for a qualified paragraph and a chapter/external
        # node for an unqualified article when the corpus has both.
        score = 0 if row["node_type"] == "external_reference" else 1 if row["node_type"] == "chapter" else 2
        candidates[title].append((score, row["id"]))
    result: dict[str, str] = {}
    for title, values in candidates.items():
        best_score = min(score for score, _ in values)
        best = sorted(node_id for score, node_id in values if score == best_score)
        if len(best) == 1:
            result[title] = best[0]
    return result


def overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and a_end > b_start


def candidate_windows(
    intervals: list[tuple[int, int, str]],
    *,
    text_length: int,
    padding: int,
    max_chars: int,
) -> list[tuple[int, int]]:
    """Build bounded resolver windows around residual candidate spans.

    ``citation_occurrences`` is deliberately a whole-value extractor, but a
    few aggregate Rulebook nodes contain hundreds of thousands of characters.
    Passing those values repeatedly makes a source-level scan effectively
    quadratic.  Windows retain enough surrounding text for instrument
    resolution while putting a hard bound on each regex pass.  Candidate
    intervals are grouped until the bound would be exceeded, so every window
    still contains at least one complete residual candidate.
    """

    if not intervals or text_length <= 0:
        return []
    ordered = sorted(
        (
            max(0, int(start)),
            min(text_length, int(end)),
            candidate_id,
        )
        for start, end, candidate_id in intervals
        if end is not None and start is not None and int(end) > int(start)
    )
    if not ordered:
        return []
    max_chars = max(2_000, int(max_chars))
    padding = max(0, int(padding))
    windows: list[tuple[int, int]] = []
    window_start = max(0, ordered[0][0] - padding)
    window_end = min(text_length, ordered[0][1] + padding)
    for start, end, _ in ordered[1:]:
        expanded_start = max(0, start - padding)
        expanded_end = min(text_length, end + padding)
        if expanded_end - window_start <= max_chars:
            window_end = max(window_end, expanded_end)
            continue
        windows.append((window_start, window_end))
        window_start = expanded_start
        window_end = expanded_end
    windows.append((window_start, window_end))
    return windows


def shifted_occurrence(occurrence, offset: int):
    """Translate resolver offsets from a window back to source coordinates."""

    metadata = dict(occurrence.metadata or {})
    group_span = metadata.get("group_span")
    if isinstance(group_span, dict):
        metadata["group_span"] = {
            **group_span,
            "start": int(group_span.get("start", occurrence.span_start)) + offset,
            "end": int(group_span.get("end", occurrence.span_end)) + offset,
        }
    return replace(
        occurrence,
        span_start=int(occurrence.span_start) + offset,
        span_end=int(occurrence.span_end) + offset,
        metadata=metadata,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect_source(args.db)
    review_conn = sqlite3.connect(f"file:{args.review_db.resolve()}?mode=ro", uri=True)
    review_conn.row_factory = sqlite3.Row
    stage = connect_stage(args.stage)
    stage.execute("DELETE FROM staged_repair WHERE proposal_method=?", (METHOD,))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-legal-context"
    registry = InstrumentRegistry.load(args.instrument_registry)
    rules = json.loads(args.overrides.read_text(encoding="utf-8"))["rules"]

    candidate_intervals: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for row in review_conn.execute(
        """
        SELECT candidate_id,source_node_id,span_start,span_end,candidate_kind
        FROM corpus_review
        WHERE decision='REFERENCE' AND target_status='external_or_unresolved'
          AND candidate_kind IN ('legal_citation','article_citation')
        """
    ):
        if row["span_start"] is not None and row["span_end"] is not None:
            candidate_intervals[row["source_node_id"]].append(
                (int(row["span_start"]), int(row["span_end"]), row["candidate_id"])
            )
    nodes = source_rows(source_conn, set(candidate_intervals))
    # The principal legal backfill already covers these atomic types.  Keep
    # this pass aggregate-only so every staged proposal has clear provenance.
    aggregate_types = {"part", "chapter", "guidance_document", "guidance_section"}
    nodes = {node_id: row for node_id, row in nodes.items() if row["node_type"] in aggregate_types}
    edges, spans = existing_state(source_conn)
    article_targets = local_article_targets(source_conn)
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str, int, int]] = set()
    target_cache: dict[str, sqlite3.Row | None] = {}
    for source_id, source in nodes.items():
        value = source_text(source)
        intervals = [
            interval
            for interval in candidate_intervals.get(source_id, [])
            if not any(
                overlap(interval[0], interval[1], old_start, old_end)
                for old_start, old_end in spans.get(source_id, [])
            )
        ]
        counts["already_materialized_candidates"] += (
            len(candidate_intervals.get(source_id, [])) - len(intervals)
        )
        # Resolve each residual candidate in a short local snippet.  This is
        # intentionally narrower than ``candidate_windows``: the legal
        # detector has a few backtracking-heavy expressions, so scanning the
        # unrelated text between two candidates can dominate runtime on large
        # CRR Parts.  The candidate span itself remains the only span eligible
        # for staging; surrounding text is resolver context only.
        snippets = [
            (
                max(
                    0,
                    start
                    - min(
                        args.window_padding,
                        max(0, (args.max_window_chars - (end - start)) // 2),
                    ),
                ),
                min(
                    len(value),
                    end
                    + min(
                        args.window_padding,
                        max(0, (args.max_window_chars - (end - start)) // 2),
                    ),
                ),
                start,
                end,
                candidate_id,
            )
            for start, end, candidate_id in intervals
            if end > start
        ]
        counts["windows"] += len(snippets)
        try:
            hints = contextual_section_hints(source=source, rules=rules)
        except Exception as exc:
            counts[f"hint_error:{type(exc).__name__}"] += 1
            continue
        for window_start, window_end, candidate_start, candidate_end, candidate_id in snippets:
            window_value = value[window_start:window_end]
            try:
                occurrences = citation_occurrences(
                    source_node_id=source_id,
                    value=window_value,
                    registry=registry,
                    source_title=source["title"] or "",
                    contextual_instrument_hints=hints,
                )
                occurrences = [
                    apply_context_override(o, source=source, registry=registry, rules=rules)
                    for o in occurrences
                ]
            except Exception as exc:
                counts[f"detector_error:{type(exc).__name__}"] += 1
                continue
            for raw_occurrence in occurrences:
                occurrence = shifted_occurrence(raw_occurrence, window_start)
                if not occurrence.instrument or not occurrence.provision_path:
                    counts["unresolved_or_non_reference"] += 1
                    continue
                if not overlap(
                    occurrence.span_start,
                    occurrence.span_end,
                    candidate_start,
                    candidate_end,
                ):
                    continue
                target_id = external_provision_node_id(occurrence.instrument, occurrence.provision_path)
                if occurrence.kind == "article" and occurrence.instrument.instrument_id == "uk-crr":
                    local_title = f"article {occurrence.target.display}".casefold()
                    target_id = article_targets.get(local_title, target_id)
                if target_id not in target_cache:
                    target_cache[target_id] = source_conn.execute(
                        "SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id=?",
                        (target_id,),
                    ).fetchone()
                target = target_cache[target_id]
                if target is None:
                    counts["target_missing"] += 1
                    continue
                key = (source_id, target_id, occurrence.span_start, occurrence.span_end)
                if key in seen:
                    continue
                seen.add(key)
                if source_id == target_id:
                    counts["self_reference"] += 1
                    continue
                if any(
                    overlap(occurrence.span_start, occurrence.span_end, old_start, old_end)
                    for old_start, old_end in spans.get(source_id, [])
                ):
                    counts["duplicate_occurrence"] += 1
                    continue
                insert_stage(
                    stage,
                    run_id=run_id,
                    source=source,
                    target=target,
                    candidate_id=candidate_id,
                    start=occurrence.span_start,
                    end=occurrence.span_end,
                    quote=value[occurrence.span_start:occurrence.span_end],
                    candidate_text=occurrence.citation_text,
                    citation_kind=occurrence.kind,
                    method=METHOD,
                    confidence=max(0.94, float(occurrence.confidence or 0.0)),
                    status="eligible",
                    reasons=["registry_resolved_aggregate_source_citation_window"],
                    evidence={
                        "instrument_id": occurrence.instrument.instrument_id,
                        "instrument_title": occurrence.instrument.title,
                        "provision_path": occurrence.provision_path,
                        "instrument_evidence": occurrence.instrument_evidence,
                        "group_text": occurrence.group_text,
                        "source_text_hash": digest(value),
                        "resolver": "legal_registry_aggregate_context_window",
                        "window": {"start": window_start, "end": window_end},
                    },
                )
                counts["eligible"] += 1
    stage.commit()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "method": METHOD,
        "source_db": str(args.db),
        "review_db": str(args.review_db),
        "aggregate_source_nodes": len(nodes),
        "residual_candidate_sources": len(candidate_intervals),
        "candidate_intervals": sum(len(v) for v in candidate_intervals.values()),
        "window_padding": args.window_padding,
        "max_window_chars": args.max_window_chars,
        "counts": dict(sorted(counts.items())),
        "staged_rows": stage.execute("SELECT COUNT(*) FROM staged_repair WHERE proposal_method=?", (METHOD,)).fetchone()[0],
        "eligible_rows": stage.execute("SELECT COUNT(*) FROM staged_repair WHERE proposal_method=? AND status='eligible'", (METHOD,)).fetchone()[0],
        "generated_at": now(),
    }
    if args.audit:
        args.audit.parent.mkdir(parents=True, exist_ok=True)
        args.audit.write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        summary["audit"] = str(args.audit)
    source_conn.close()
    review_conn.close()
    stage.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--instrument-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--window-padding",
        type=int,
        default=DEFAULT_WINDOW_PADDING,
        help="Characters of local source context around each residual candidate.",
    )
    parser.add_argument(
        "--max-window-chars",
        type=int,
        default=DEFAULT_MAX_WINDOW,
        help="Hard maximum size of each resolver window.",
    )
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), indent=2, ensure_ascii=False, sort_keys=True))
