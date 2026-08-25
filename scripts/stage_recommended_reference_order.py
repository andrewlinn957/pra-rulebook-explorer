#!/usr/bin/env python3
"""Stage the first two guarded passes in the unresolved-reference work order.

The corpus review and the CRR resolver are intentionally read-only inputs.  A
separate stage database receives proposals for:

* exact PRA document-code mentions (SS/SoP/LSS/PS/FG/CP) with a unique local
  guidance-document target;
* exact generic legal-document labels whose glossary definition is unique; and
* exact CRR resolver spans whose post-resolver target is unique for that span.

No Rulebook rows are written here.  The existing materializer can be run in
dry-run mode first and, with ``--allow-existing-edge``, can add occurrences to
edges that the CRR resolver has already materialised.
"""

from __future__ import annotations

import argparse
import json
import re
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
    normalised,
    source_text,
)
from scripts.reference_recall_stage import (  # noqa: E402
    connect_stage,
    insert_stage,
    relationship_for_target,
)


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-final-20260731.sqlite3"
DEFAULT_CRR_AUDIT = ROOT / "logs" / "reference-recall-crr-resolution-audit-20260731.json"
DEFAULT_STAGE = ROOT / "logs" / "reference-recall-recommended-order-20260731.sqlite3"
DEFAULT_AUDIT = ROOT / "logs" / "reference-recall-recommended-order-stage-20260731.json"
METHOD_CODE = "corpus_guidance_code_alias_v1"
METHOD_TERM = "corpus_defined_term_alias_v1"
METHOD_CRR = "corpus_crr_occurrence_v1"
METHOD_DOCUMENT = "corpus_generic_document_label_v1"

# Include document families present in the corpus as well as the PS/FG/CP
# families which often have no local document node and are therefore held.
DOCUMENT_CODE_RE = re.compile(
    r"\b(?:SS|SoP|LSS|PS|FG|CP)\s*[0-9]{1,3}\s*/\s*[0-9]{2}\b",
    re.IGNORECASE,
)

GENERIC_TERM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("CRR", r"\b(?:the\s+)?CRR\b"),
    ("Solvency II Directive", r"\bSolvency\s+II\s+Directive\b"),
    ("PRA Handbook", r"\bPRA\s+Handbook\b"),
    ("FCA Handbook", r"\bFCA\s+Handbook\b"),
    (
        "Capital Requirements Regulations",
        r"\bCapital\s+Requirements?\s+Regulations?\b",
    ),
)

# These labels are meaningful references, but they are not provisions.  Only
# link one when the corpus has an exact document/defined-term node; otherwise
# record an explicit external hold so the unresolved ledger does not invite a
# guessed link to an arbitrary Rulebook provision.
GENERIC_DOCUMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "PRA Rulebook Rules",
        r"\b(?:the\s+)?PRA\s+Rulebook\s+Rules\b",
    ),
    (
        "PRA Rulebook",
        r"\b(?:the\s+)?PRA\s+Rulebook\b",
    ),
    (
        "EBA Guidelines",
        r"\b(?:the\s+)?EBA\s+Guidelines?\b",
    ),
    (
        "Financial Services and Markets Act 2000",
        r"\b(?:the\s+)?Financial\s+Services\s+and\s+Markets\s+Act\s+2000\b",
    ),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_nodes(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["id"]: row
        for row in conn.execute(
            "SELECT id,node_type,title,text,url,metadata_json FROM node"
        )
    }


def code_key(value: str) -> str:
    match = DOCUMENT_CODE_RE.search(value or "")
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(0)).upper()


def guidance_code_targets(nodes: dict[str, sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    targets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for node in nodes.values():
        if node["node_type"] != "guidance_document":
            continue
        key = code_key(node["title"] or "")
        if key:
            targets[key].append(node)
    return dict(targets)


def existing_edges(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (row["from_node_id"], row["to_node_id"])
        for row in conn.execute("SELECT from_node_id,to_node_id FROM edge")
    }


def existing_spans(conn: sqlite3.Connection) -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT source_node_id,span_start,span_end FROM reference_occurrence "
        "WHERE status='materialized'"
    ):
        if row["span_start"] is not None and row["span_end"] is not None:
            spans[row["source_node_id"]].append((int(row["span_start"]), int(row["span_end"])))
    return spans


def overlaps(start: int, end: int, old_start: int, old_end: int) -> bool:
    return start < old_end and end > old_start


def exact_candidate_segment(
    source: sqlite3.Row, start: int | None, end: int | None, candidate_text: str
) -> tuple[str, int, int] | None:
    value = source_text(source)
    if start is None or end is None:
        return None
    start, end = int(start), int(end)
    if not (0 <= start <= end <= len(value)):
        return None
    live = value[start:end]
    # The review ledger intentionally retains the live span hash.  A compacted
    # candidate can differ in whitespace, but the live source segment remains
    # authoritative for materialisation.
    if not live and candidate_text:
        return None
    return live, start, end


def status_for(
    source: sqlite3.Row,
    target: sqlite3.Row,
    start: int,
    end: int,
    edges: set[tuple[str, str]],
    spans: dict[str, list[tuple[int, int]]],
) -> tuple[str, list[str]]:
    if source["id"] == target["id"]:
        return "held_self_reference", ["source_and_target_are_identical"]
    if any(overlaps(start, end, old_start, old_end) for old_start, old_end in spans.get(source["id"], [])):
        return "held_duplicate_occurrence", ["source_span_already_has_materialized_occurrence"]
    if (source["id"], target["id"]) in edges:
        return "eligible", ["exact_target_and_new_occurrence_reuses_existing_edge"]
    return "eligible", ["exact_target_and_span_passed_unique_alias_checks"]


def stage_code_aliases(
    *,
    source_conn: sqlite3.Connection,
    review_conn: sqlite3.Connection,
    stage: sqlite3.Connection,
    run_id: str,
    nodes: dict[str, sqlite3.Row],
    edges: set[tuple[str, str]],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, int]:
    targets = guidance_code_targets(nodes)
    counts: Counter[str] = Counter()
    rows = review_conn.execute(
        """
        SELECT candidate_id,source_node_id,span_start,span_end,candidate_text,candidate_kind,
               source_text_hash,source_title
        FROM corpus_review
        WHERE decision='REFERENCE' AND target_status='external_or_unresolved'
        ORDER BY candidate_id
        """
    )
    seen: set[tuple[str, str, int, int]] = set()
    for candidate in rows:
        source = nodes.get(candidate["source_node_id"])
        if source is None:
            counts["source_missing"] += 1
            continue
        segment = exact_candidate_segment(
            source, candidate["span_start"], candidate["span_end"], candidate["candidate_text"]
        )
        if segment is None:
            counts["span_invalid"] += 1
            continue
        live, base_start, _base_end = segment
        for match in DOCUMENT_CODE_RE.finditer(live):
            key = code_key(match.group(0))
            match_start = base_start + match.start()
            match_end = base_start + match.end()
            target_rows = targets.get(key, [])
            unique_target_ids = {row["id"] for row in target_rows}
            target = target_rows[0] if len(unique_target_ids) == 1 else None
            dedupe_key = (source["id"], key, match_start, match_end)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if target is None:
                status = "held_unresolved"
                reasons = [
                    "no_local_guidance_document_for_code"
                    if not target_rows
                    else "multiple_local_guidance_documents_for_code"
                ]
                evidence = {
                    "code": key,
                    "matching_target_ids": sorted(unique_target_ids),
                    "candidate_id": candidate["candidate_id"],
                }
                insert_stage(
                    stage,
                    run_id=run_id,
                    source=source,
                    target=None,
                    candidate_id=candidate["candidate_id"],
                    start=match_start,
                    end=match_end,
                    quote=match.group(0),
                    candidate_text=candidate["candidate_text"] or match.group(0),
                    citation_kind="named_document",
                    method=METHOD_CODE,
                    confidence=0.0,
                    status=status,
                    reasons=reasons,
                    evidence=evidence,
                )
                counts["held"] += 1
                continue
            status, reasons = status_for(source, target, match_start, match_end, edges, spans)
            insert_stage(
                stage,
                run_id=run_id,
                source=source,
                target=target,
                candidate_id=candidate["candidate_id"],
                start=match_start,
                end=match_end,
                quote=match.group(0),
                candidate_text=candidate["candidate_text"] or match.group(0),
                citation_kind="named_document",
                method=METHOD_CODE,
                confidence=0.96,
                status=status,
                reasons=reasons,
                evidence={
                    "code": key,
                    "candidate_id": candidate["candidate_id"],
                    "source_text_hash_from_review": candidate["source_text_hash"],
                    "resolver": "unique_guidance_document_code_prefix",
                },
            )
            counts["eligible" if status == "eligible" else status] += 1
    counts["unique_codes"] = len(targets)
    counts["duplicate_codes"] = sum(1 for values in targets.values() if len({v["id"] for v in values}) > 1)
    return dict(counts)


def stage_generic_terms(
    *,
    review_conn: sqlite3.Connection,
    stage: sqlite3.Connection,
    run_id: str,
    nodes: dict[str, sqlite3.Row],
    edges: set[tuple[str, str]],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, int]:
    by_title = defaultdict(list)
    for node in nodes.values():
        if node["node_type"] == "defined_term":
            by_title[normalised(node["title"] or "")].append(node)
    aliases = {
        normalised("CRR"): "CRR",
        normalised("the CRR"): "CRR",
        normalised("Solvency II Directive"): "Solvency II Directive",
        normalised("PRA Handbook"): "PRA Handbook",
        normalised("FCA Handbook"): "FCA Handbook",
        normalised("Capital Requirements Regulation"): "Capital Requirements Regulations",
        normalised("Capital Requirements Regulations"): "Capital Requirements Regulations",
    }
    patterns = [(canonical, re.compile(pattern, re.IGNORECASE)) for canonical, pattern in GENERIC_TERM_PATTERNS]
    counts: Counter[str] = Counter()
    rows = review_conn.execute(
        """
        SELECT candidate_id,source_node_id,span_start,span_end,candidate_text,candidate_kind,
               source_text_hash
        FROM corpus_review
        WHERE decision='REFERENCE' AND target_status='external_or_unresolved'
        ORDER BY candidate_id
        """
    )
    seen: set[tuple[str, str, int, int]] = set()
    for candidate in rows:
        source = nodes.get(candidate["source_node_id"])
        if source is None:
            counts["source_missing"] += 1
            continue
        segment = exact_candidate_segment(source, candidate["span_start"], candidate["span_end"], candidate["candidate_text"])
        if segment is None:
            counts["span_invalid"] += 1
            continue
        live, base_start, _ = segment
        for canonical, pattern in patterns:
            for match in pattern.finditer(live):
                alias = normalised(match.group(0))
                if alias not in aliases or aliases[alias] != canonical:
                    continue
                start, end = base_start + match.start(), base_start + match.end()
                key = (source["id"], canonical, start, end)
                if key in seen:
                    continue
                seen.add(key)
                target_rows = by_title.get(normalised(canonical), [])
                target = target_rows[0] if len({row["id"] for row in target_rows}) == 1 else None
                if target is None:
                    counts["held"] += 1
                    continue
                status, reasons = status_for(source, target, start, end, edges, spans)
                insert_stage(
                    stage,
                    run_id=run_id,
                    source=source,
                    target=target,
                    candidate_id=candidate["candidate_id"],
                    start=start,
                    end=end,
                    quote=match.group(0),
                    candidate_text=candidate["candidate_text"] or match.group(0),
                    citation_kind="defined_term",
                    method=METHOD_TERM,
                    confidence=0.94,
                    status=status,
                    reasons=reasons,
                    evidence={
                        "canonical_title": canonical,
                        "candidate_id": candidate["candidate_id"],
                        "source_text_hash_from_review": candidate["source_text_hash"],
                        "resolver": "exact_generic_defined_term_alias",
                    },
                    relationship_type="DEF",
                )
                counts["eligible" if status == "eligible" else status] += 1
    counts["alias_titles"] = len(by_title)
    return dict(counts)


def stage_generic_document_labels(
    *,
    review_conn: sqlite3.Connection,
    stage: sqlite3.Connection,
    run_id: str,
    nodes: dict[str, sqlite3.Row],
    edges: set[tuple[str, str]],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, int]:
    """Classify generic document labels without inventing provision targets.

    A label is linked only to a unique exact document/defined-term node.  The
    current corpus has no document-level PRA Rulebook, EBA Guidelines, or
    FSMA node, so those occurrences are deliberately staged as
    ``held_unresolved`` with an explicit reason.  This keeps them visible in
    the audit while preventing a misleading link to a similarly titled rule.
    """

    target_types = {"defined_term", "external_reference", "legal_instrument", "guidance_document"}
    by_title: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for node in nodes.values():
        if node["node_type"] in target_types:
            by_title[normalised(node["title"] or "")].append(node)
    patterns = [
        (canonical, re.compile(pattern, re.IGNORECASE))
        for canonical, pattern in GENERIC_DOCUMENT_PATTERNS
    ]
    counts: Counter[str] = Counter()
    rows = review_conn.execute(
        """
        SELECT candidate_id,source_node_id,span_start,span_end,candidate_text,
               candidate_kind,source_text_hash
        FROM corpus_review
        WHERE decision='REFERENCE' AND target_status='external_or_unresolved'
        ORDER BY candidate_id
        """
    )
    seen: set[tuple[str, str, int, int]] = set()
    for candidate in rows:
        source = nodes.get(candidate["source_node_id"])
        if source is None:
            counts["source_missing"] += 1
            continue
        segment = exact_candidate_segment(
            source, candidate["span_start"], candidate["span_end"], candidate["candidate_text"]
        )
        if segment is None:
            counts["span_invalid"] += 1
            continue
        live, base_start, _ = segment
        found: list[tuple[int, int, str, str]] = []
        for canonical, pattern in patterns:
            for match in pattern.finditer(live):
                found.append((match.start(), match.end(), canonical, match.group(0)))
        # Prefer the longest label at an overlapping span (``PRA Rulebook
        # Rules`` contains the shorter ``PRA Rulebook`` label).
        found.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
        accepted: list[tuple[int, int]] = []
        for rel_start, rel_end, canonical, matched in found:
            if any(overlaps(rel_start, rel_end, old_start, old_end) for old_start, old_end in accepted):
                continue
            accepted.append((rel_start, rel_end))
            start, end = base_start + rel_start, base_start + rel_end
            key = (source["id"], canonical, start, end)
            if key in seen:
                continue
            seen.add(key)
            target_rows = by_title.get(normalised(canonical), [])
            target_ids = {row["id"] for row in target_rows}
            target = target_rows[0] if len(target_ids) == 1 else None
            if target is None:
                reason = (
                    "no_local_document_target_for_generic_label"
                    if not target_rows
                    else "multiple_local_document_targets_for_generic_label"
                )
                insert_stage(
                    stage,
                    run_id=run_id,
                    source=source,
                    target=None,
                    candidate_id=candidate["candidate_id"],
                    start=start,
                    end=end,
                    quote=matched,
                    candidate_text=candidate["candidate_text"] or matched,
                    citation_kind="named_document",
                    method=METHOD_DOCUMENT,
                    confidence=0.0,
                    status="held_unresolved",
                    reasons=[reason],
                    evidence={
                        "canonical_label": canonical,
                        "matching_target_ids": sorted(target_ids),
                        "candidate_id": candidate["candidate_id"],
                        "resolver": "exact_document_or_defined_term_title_only",
                    },
                )
                counts["held_unresolved"] += 1
                continue
            status, reasons = status_for(source, target, start, end, edges, spans)
            relationship = "DEF" if target["node_type"] == "defined_term" else "REF"
            insert_stage(
                stage,
                run_id=run_id,
                source=source,
                target=target,
                candidate_id=candidate["candidate_id"],
                start=start,
                end=end,
                quote=matched,
                candidate_text=candidate["candidate_text"] or matched,
                citation_kind="named_document",
                method=METHOD_DOCUMENT,
                confidence=0.94,
                status=status,
                reasons=reasons,
                evidence={
                    "canonical_label": canonical,
                    "matching_target_ids": sorted(target_ids),
                    "candidate_id": candidate["candidate_id"],
                    "resolver": "exact_document_or_defined_term_title_only",
                },
                relationship_type=relationship,
            )
            counts["eligible" if status == "eligible" else status] += 1
    counts["target_titles"] = len(by_title)
    return dict(counts)


def stage_crr_occurrences(
    *,
    crr_audit: Path,
    stage: sqlite3.Connection,
    run_id: str,
    nodes: dict[str, sqlite3.Row],
    edges: set[tuple[str, str]],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, int]:
    data = json.loads(crr_audit.read_text(encoding="utf-8"))
    counts: Counter[str] = Counter()
    grouped: dict[tuple[str, int, int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for occurrence in data.get("occurrences", []):
        source_id = occurrence.get("source_node_id") or ""
        start, end = occurrence.get("span_start"), occurrence.get("span_end")
        citation = occurrence.get("citation") or ""
        if source_id and isinstance(start, int) and isinstance(end, int):
            for resolution in occurrence.get("resolutions", []):
                if resolution.get("target_id") and not resolution.get("already_linked"):
                    grouped[(source_id, start, end, citation)][resolution["target_id"]] = {
                        "occurrence": occurrence,
                        "resolution": resolution,
                    }
    for (source_id, start, end, citation), choices in sorted(grouped.items()):
        source = nodes.get(source_id)
        if source is None:
            counts["source_missing"] += 1
            continue
        text = source_text(source)
        if not (0 <= start <= end <= len(text)) or not text[start:end]:
            counts["span_invalid"] += 1
            continue
        if len(choices) != 1:
            counts["held_multiple_targets"] += 1
            continue
        target_id, payload = next(iter(choices.items()))
        target = nodes.get(target_id)
        if target is None:
            counts["target_missing"] += 1
            continue
        resolution = payload["resolution"]
        status, reasons = status_for(source, target, start, end, edges, spans)
        insert_stage(
            stage,
            run_id=run_id,
            source=source,
            target=target,
            candidate_id="",
            start=start,
            end=end,
            quote=text[start:end],
            candidate_text=citation,
            citation_kind="article_citation",
            method=METHOD_CRR,
            confidence=0.99 if str(resolution.get("classification", "")).startswith("uk_crr") else 0.96,
            status=status,
            reasons=reasons,
            evidence={
                "classification": resolution.get("classification", ""),
                "classification_evidence": resolution.get("classification_evidence", ""),
                "crr_target_kind": resolution.get("target_kind", ""),
                "audit_target_title": resolution.get("target_title", ""),
                "audit_edge_id": resolution.get("edge_id", ""),
                "resolver": "backfill_uk_crr_article_references",
            },
        )
        counts["eligible" if status == "eligible" else status] += 1
    counts["unique_spans"] = len(grouped)
    counts["single_target_spans"] = sum(1 for choices in grouped.values() if len(choices) == 1)
    return dict(counts)


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect_source(args.db)
    review_conn = connect(Path(args.review_db), readonly=True)
    stage = connect_stage(args.stage)
    # Reruns replace only this work-order's proposals, leaving any separately
    # reviewed stage methods intact.
    methods = (METHOD_CODE, METHOD_TERM, METHOD_CRR, METHOD_DOCUMENT)
    method_placeholders = ",".join("?" for _ in methods)
    stage.execute(
        f"DELETE FROM staged_repair WHERE proposal_method IN ({method_placeholders})", methods
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-recommended"
    stage.execute(
        "INSERT OR REPLACE INTO stage_run(run_id,stage_version,source_db,ledger_db,started_at) VALUES (?,?,?,?,?)",
        (run_id, "recommended-reference-order-v1", str(args.db), str(args.review_db), now()),
    )
    nodes = row_nodes(source_conn)
    edges = existing_edges(source_conn)
    spans = existing_spans(source_conn)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "source_db": str(args.db),
        "review_db": str(args.review_db),
        "crr_audit": str(args.crr_audit),
        "source_nodes": len(nodes),
        "source_digest": digest("|".join(sorted(nodes)), "sha256"),
        "code_aliases": stage_code_aliases(
            source_conn=source_conn,
            review_conn=review_conn,
            stage=stage,
            run_id=run_id,
            nodes=nodes,
            edges=edges,
            spans=spans,
        ),
        "generic_terms": stage_generic_terms(
            review_conn=review_conn,
            stage=stage,
            run_id=run_id,
            nodes=nodes,
            edges=edges,
            spans=spans,
        ),
        "generic_document_labels": stage_generic_document_labels(
            review_conn=review_conn,
            stage=stage,
            run_id=run_id,
            nodes=nodes,
            edges=edges,
            spans=spans,
        ),
        "crr_occurrences": stage_crr_occurrences(
            crr_audit=args.crr_audit,
            stage=stage,
            run_id=run_id,
            nodes=nodes,
            edges=edges,
            spans=spans,
        ),
    }
    stage.commit()
    summary.update(
        staged_rows=stage.execute(
            f"SELECT COUNT(*) FROM staged_repair WHERE proposal_method IN ({method_placeholders})", methods
        ).fetchone()[0],
        eligible_rows=stage.execute(
            f"SELECT COUNT(*) FROM staged_repair WHERE proposal_method IN ({method_placeholders}) AND status='eligible'", methods
        ).fetchone()[0],
        status_counts={
            row["status"]: row["n"]
            for row in stage.execute(
                f"SELECT status,COUNT(*) n FROM staged_repair WHERE proposal_method IN ({method_placeholders}) GROUP BY status",
                methods,
            )
        },
        method_counts={
            row["proposal_method"]: row["n"]
            for row in stage.execute(
                f"SELECT proposal_method,COUNT(*) n FROM staged_repair WHERE proposal_method IN ({method_placeholders}) GROUP BY proposal_method",
                methods,
            )
        },
        generated_at=now(),
    )
    stage.execute(
        "UPDATE stage_run SET finished_at=?,summary_json=? WHERE run_id=?",
        (now(), json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
    )
    stage.commit()
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
    parser.add_argument("--crr-audit", type=Path, default=DEFAULT_CRR_AUDIT)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), indent=2, ensure_ascii=False, sort_keys=True))

try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect
