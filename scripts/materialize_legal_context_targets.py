#!/usr/bin/env python3
"""Fetch and stage missing non-CRR targets from residual legal citations.

The regular legal backfill deliberately operates on atomic Rulebook nodes. This
companion pass works from the authoritative residual review and the bounded
aggregate resolver, discovers registry-backed targets that are absent from the
database, fetches their official text into the normal cache, inserts the
official target nodes, and stages exact source occurrences for the guarded
reference materializer.

It never guesses an instrument: only citations that the existing legal
registry resolves in local source context are considered. UK-CRR citations are
reported separately because they should resolve to existing Rulebook Article
nodes rather than create new external targets.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rulebook_scraper.legal_references import (  # noqa: E402
    DEFAULT_INSTRUMENT_REGISTRY,
    Instrument,
    InstrumentRegistry,
    citation_occurrences,
    external_provision_node_id,
    fetch_official_provision,
)
from scripts.backfill_legal_references import (  # noqa: E402
    apply_context_override,
    cache_fetcher,
    connect as connect_writable,
    contextual_section_hints,
    materialize_target,
)
from scripts.reference_recall_audit import connect_source, source_text  # noqa: E402
from scripts.reference_recall_stage import connect_stage, insert_stage  # noqa: E402
from scripts.stage_legal_context_references import (  # noqa: E402
    existing_state,
    local_article_targets,
    overlap,
    shifted_occurrence,
)


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-recommended-final3-20260731.sqlite3"
DEFAULT_STAGE = ROOT / "logs" / "reference-recall-recommended-order-20260731.sqlite3"
DEFAULT_REGISTRY = ROOT / "config" / "legal_instruments.json"
DEFAULT_OVERRIDES = ROOT / "config" / "legal_reference_overrides.json"
DEFAULT_CACHE = ROOT / "backend" / "data" / "raw" / "legal-provisions"
DEFAULT_AUDIT = ROOT / "logs" / "reference-recall-legal-context-targets-20260801.json"
METHOD = "corpus_legal_official_v1"


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


def target_id_for(occurrence, article_targets: dict[str, str]) -> str:
    """Return the canonical local ID where one exists, else registry ID."""

    if occurrence.kind == "article" and occurrence.instrument.instrument_id == "uk-crr":
        local = article_targets.get(f"article {occurrence.target.display}".casefold())
        if local is None:
            # Keep the subsection in the occurrence metadata when the corpus
            # only has the substantive base Article node.
            base = occurrence.target.display.split("(", 1)[0]
            local = article_targets.get(f"article {base}".casefold())
        if local:
            return local
    return external_provision_node_id(occurrence.instrument, occurrence.provision_path)


def residual_records(
    *,
    db: Path,
    review_db: Path,
    registry_path: Path,
    overrides_path: Path,
    padding: int,
    max_window_chars: int,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, Any]]:
    source_conn = connect_source(db)
    review_conn = connect(f"file:{review_db.resolve()}?mode=ro", uri=True)
    registry = InstrumentRegistry.load(registry_path)
    rules = json.loads(overrides_path.read_text(encoding="utf-8"))["rules"]

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
    aggregate_types = {"part", "chapter", "guidance_document", "guidance_section"}
    nodes = {
        node_id: row
        for node_id, row in nodes.items()
        if row["node_type"] in aggregate_types
    }
    _edges, spans = existing_state(source_conn)
    article_targets = local_article_targets(source_conn)
    records: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, int, int]] = set()
    counts: Counter[str] = Counter()

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
        try:
            hints = contextual_section_hints(source=source, rules=rules)
        except Exception as exc:
            counts[f"hint_error:{type(exc).__name__}"] += 1
            continue
        for start, end, candidate_id in intervals:
            context_padding = min(
                padding,
                max(0, (max_window_chars - (end - start)) // 2),
            )
            window_start = max(0, start - context_padding)
            window_end = min(len(value), end + context_padding)
            try:
                occurrences = citation_occurrences(
                    source_node_id=source_id,
                    value=value[window_start:window_end],
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
                    continue
                if not overlap(occurrence.span_start, occurrence.span_end, start, end):
                    continue
                target_id = target_id_for(occurrence, article_targets)
                if source_conn.execute("SELECT 1 FROM node WHERE id=?", (target_id,)).fetchone():
                    counts["target_already_exists"] += 1
                    continue
                key = (occurrence.instrument.instrument_id, occurrence.provision_path)
                dedupe = (source_id, target_id, occurrence.span_start, occurrence.span_end)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                record = {
                    "source_node_id": source_id,
                    "source_title": source["title"] or "",
                    "candidate_id": candidate_id,
                    "target_node_id": target_id,
                    "span_start": occurrence.span_start,
                    "span_end": occurrence.span_end,
                    "quoted_text": value[occurrence.span_start:occurrence.span_end],
                    "citation_text": occurrence.citation_text,
                    "group_text": occurrence.group_text,
                    "citation_kind": occurrence.kind,
                    "confidence": max(0.94, float(occurrence.confidence or 0.0)),
                    "instrument_id": occurrence.instrument.instrument_id,
                    "instrument_title": occurrence.instrument.title,
                    "instrument_details": asdict(occurrence.instrument),
                    "provision_path": occurrence.provision_path,
                    "instrument_evidence": occurrence.instrument_evidence,
                    "metadata": occurrence.metadata,
                }
                records[key].append(record)
                counts["missing_target_occurrence"] += 1
                counts[f"missing_instrument:{occurrence.instrument.instrument_id}"] += 1
                if occurrence.instrument.instrument_id == "uk-crr":
                    counts["missing_uk_crr"] += 1
                else:
                    counts["missing_non_crr"] += 1

    source_conn.close()
    review_conn.close()
    summary = {
        "candidate_sources": len(candidate_intervals),
        "aggregate_sources": len(nodes),
        "candidate_intervals": sum(len(items) for items in candidate_intervals.values()),
        "unique_missing_targets": len(records),
        "missing_occurrences": sum(len(items) for items in records.values()),
        "counts": dict(sorted(counts.items())),
        "generated_at": now(),
    }
    return records, summary


def fetch_targets(
    records: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    registry: InstrumentRegistry,
    cache_root: Path,
    workers: int,
) -> tuple[dict[tuple[str, str], Any], dict[tuple[str, str], str]]:
    results: dict[tuple[str, str], Any] = {}
    errors: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {}
        for key in records:
            instrument_id, provision_path = key
            instrument = registry.by_id.get(instrument_id)
            if instrument is None:
                instrument = Instrument(**records[key][0]["instrument_details"])
            futures[
                executor.submit(
                    fetch_official_provision,
                    instrument,
                    provision_path,
                    fetcher=cache_fetcher(cache_root, instrument, provision_path),
                )
            ] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"
    return results, errors


def apply_targets_and_stage(
    *,
    db: Path,
    stage_path: Path,
    registry_path: Path,
    records: dict[tuple[str, str], list[dict[str, Any]]],
    fetched: dict[tuple[str, str], Any],
    run_id: str,
) -> tuple[int, int]:
    registry = InstrumentRegistry.load(registry_path)
    conn = connect_writable(db)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for (instrument_id, provision_path), official in fetched.items():
            instrument = registry.by_id.get(instrument_id)
            if instrument is None:
                instrument = Instrument(
                    **records[(instrument_id, provision_path)][0]["instrument_details"]
                )
            materialize_target(
                conn,
                instrument=instrument,
                provision_path=provision_path,
                official=official,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    stage = connect_stage(stage_path)
    stage.execute("DELETE FROM staged_repair WHERE proposal_method=?", (METHOD,))
    staged = 0
    for key, target_records in records.items():
        if key not in fetched:
            continue
        target_id = target_records[0]["target_node_id"]
        target = conn.execute(
            "SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id=?",
            (target_id,),
        ).fetchone()
        if target is None:
            continue
        for record in target_records:
            source = conn.execute(
                "SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id=?",
                (record["source_node_id"],),
            ).fetchone()
            if source is None:
                continue
            insert_stage(
                stage,
                run_id=run_id,
                source=source,
                target=target,
                candidate_id=record["candidate_id"],
                start=record["span_start"],
                end=record["span_end"],
                quote=record["quoted_text"],
                candidate_text=record["citation_text"],
                citation_kind=record["citation_kind"],
                method=METHOD,
                confidence=record["confidence"],
                status="eligible",
                reasons=["official_registry_target_fetched_for_residual_aggregate_citation"],
                evidence={
                    "instrument_id": record["instrument_id"],
                    "instrument_title": record["instrument_title"],
                    "provision_path": record["provision_path"],
                    "instrument_evidence": record["instrument_evidence"],
                    "group_text": record["group_text"],
                    "resolver": "legal_registry_aggregate_official_target",
                },
            )
            staged += 1
    stage.commit()
    stage.close()
    conn.close()
    return len(fetched), staged


def run(args: argparse.Namespace) -> dict[str, Any]:
    records, summary = residual_records(
        db=args.db,
        review_db=args.review_db,
        registry_path=args.instrument_registry,
        overrides_path=args.overrides,
        padding=args.padding,
        max_window_chars=args.max_window_chars,
    )
    summary.update(
        method=METHOD,
        db=str(args.db),
        review_db=str(args.review_db),
        padding=args.padding,
        max_window_chars=args.max_window_chars,
        unique_missing_non_crr_targets=sum(
            1 for instrument_id, _ in records if instrument_id != "uk-crr"
        ),
        unique_missing_uk_crr_targets=sum(
            1 for instrument_id, _ in records if instrument_id == "uk-crr"
        ),
        missing_target_breakdown={
            f"{instrument_id}:{provision_path}": {
                "occurrences": len(values),
                "sample_citations": sorted(
                    {str(item["citation_text"]) for item in values}
                )[:5],
                "sample_sources": sorted(
                    {str(item["source_title"]) for item in values}
                )[:5],
            }
            for (instrument_id, provision_path), values in sorted(records.items())
        },
    )
    if args.apply:
        registry = InstrumentRegistry.load(args.instrument_registry)
        non_crr_records = {
            key: values
            for key, values in records.items()
            if key[0] != "uk-crr"
        }
        fetched, errors = fetch_targets(
            non_crr_records,
            registry=registry,
            cache_root=args.cache_root,
            workers=args.fetch_workers,
        )
        fetched_count, staged_count = apply_targets_and_stage(
            db=args.db,
            stage_path=args.stage,
            registry_path=args.instrument_registry,
            records=non_crr_records,
            fetched=fetched,
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-legal-official",
        )
        summary.update(
            fetched_targets=fetched_count,
            fetch_errors=len(errors),
            staged_occurrences=staged_count,
            fetch_error_details={f"{key[0]}:{key[1]}": value for key, value in sorted(errors.items())},
        )
    if args.audit:
        args.audit.parent.mkdir(parents=True, exist_ok=True)
        args.audit.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        summary["audit"] = str(args.audit)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--instrument-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--padding", type=int, default=220)
    parser.add_argument("--max-window-chars", type=int, default=18_000)
    parser.add_argument("--fetch-workers", type=int, default=8)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Fetch official targets, insert them, and stage exact occurrences.",
    )
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), indent=2, ensure_ascii=False, sort_keys=True))

try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect
