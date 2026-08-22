#!/usr/bin/env python3
"""Stage conservative structural-reference resolutions.

Structural detectors deliberately over-collect labels such as ``rule 6`` and
``Part 1``.  This pass resolves only when the live source span and the source
hierarchy identify one local target (or a small, fully resolved range):

* rule/chapter/annex targets use the source Part context;
* guidance paragraphs/sections use the source document context; and
* an exact local title (for example ``Annex XXVIII``) may stand alone when it
  is unique.

UI boilerplate and ambiguous historical duplicates are held.  The source DB
  is read-only; proposals go through the normal stage/materializer gate.
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

from scripts.reference_recall_audit import connect_source, digest, normalised, source_text  # noqa: E402
from scripts.reference_recall_stage import connect_stage, insert_stage, relationship_for_target  # noqa: E402
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REVIEW = ROOT / "logs" / "reference-recall-corpus-review-recommended-20260731.sqlite3"
DEFAULT_STAGE = ROOT / "logs" / "reference-recall-recommended-order-20260731.sqlite3"
DEFAULT_AUDIT = ROOT / "logs" / "reference-recall-structural-context-stage-20260731.json"
METHOD = "corpus_structural_context_v1"

STRUCTURE_RE = re.compile(
    r"\b(?P<label>paragraphs?|paras?|points?|subparagraphs?|articles?|"
    r"sections?|regulations?|rules?|chapters?|parts?|titles?|annex(?:es)?|"
    r"schedules?|templates?|tables?|forms?)\s+"
    r"(?P<references>[0-9A-Za-zIVXLCDM]+(?:\.[0-9A-Za-z]+)*(?:\s*\([^)]*\))?"
    r"(?:\s*(?:,|and|or|to|[-–—])\s*"
    r"[0-9A-Za-zIVXLCDM]+(?:\.[0-9A-Za-z]+)*(?:\s*\([^)]*\))?){0,4})",
    re.IGNORECASE,
)
BASE_RE = re.compile(r"^[0-9A-Za-zIVXLCDM]+(?:\.[0-9A-Za-z]+)*", re.IGNORECASE)
SEPARATOR_RE = re.compile(r"\s*(?:,|and|or|to|[-–—])\s*", re.IGNORECASE)
STRUCTURAL_LABEL_START_RE = re.compile(
    r"\s+(?=(?:paragraphs?|paras?|points?|subparagraphs?|articles?|"
    r"sections?|regulations?|rules?|chapters?|parts?|titles?|annex(?:es)?|"
    r"schedules?|templates?|tables?|forms?)\s+"
    r"[0-9A-Za-zIVXLCDM])",
    re.IGNORECASE,
)
DANGLING_CONNECTOR_RE = re.compile(r"\s+(?:of|under|in|and|or|to)\s*[\]})},;:.]*$", re.IGNORECASE)

BOILERPLATE_RE = re.compile(
    r"(?:legal\s+instruments\s+that\s+change\s+this\s+(?:rule|article)|"
    r"export\s+article\s+as|past\s+version|content\s+loading|"
    r"table\s+of\s+contents|open\s+in\s+new\s+window)",
    re.IGNORECASE,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_nodes(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["id"]: row for row in conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node")}


def metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def source_context(source: sqlite3.Row) -> tuple[str, str]:
    meta = metadata(source)
    # Aggregate guidance documents contain their child paragraphs in one node
    # and historically did not carry ``document_title`` at the aggregate
    # level.  The node title is the authoritative document label in that case;
    # without this fallback every ``paragraph N`` inside the document was
    # treated as context-free even though matching child nodes are indexed.
    document_title = str(meta.get("document_title") or "").strip()
    if not document_title and source["node_type"] in {"guidance_document", "guidance_section", "guidance_paragraph"}:
        reader_meta = meta.get("reader_reference_text")
        if isinstance(reader_meta, dict):
            document_title = str(reader_meta.get("source_title") or "").strip()
        document_title = document_title or str(meta.get("source_title") or source["title"] or "").strip()
    return (
        str(meta.get("part_title") or "").strip(),
        document_title,
    )


def title_key(value: str) -> str:
    return normalised(value).replace(" ", "")


def base_identifier(value: str) -> str:
    value = re.sub(r"\s+", "", value or "")
    value = value.split("(", 1)[0]
    return value.casefold()


def parse_structural(candidate_text: str) -> tuple[str, list[str]]:
    match = STRUCTURE_RE.search(candidate_text or "")
    if not match:
        return "", []
    label = match.group("label").casefold().rstrip("s")
    refs = match.group("references")
    # Keep only bases.  Parenthesised qualifiers are part of the citation span
    # but do not change the provision node identity.
    pieces = SEPARATOR_RE.split(refs)
    identifiers: list[str] = []
    for piece in pieces:
        found = BASE_RE.match(piece.strip())
        if found:
            ident = base_identifier(found.group(0))
            if ident and ident not in identifiers:
                identifiers.append(ident)
    return label, identifiers


def source_span(source: sqlite3.Row, start: Any, end: Any) -> tuple[str, int, int] | None:
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    value = source_text(source)
    if start < 0 or end <= start or end > len(value):
        return None
    return value[start:end], start, end


def repair_structural_span(live: str) -> tuple[str, int, int]:
    """Trim detector tails while preserving the complete first reference.

    The lexical detector intentionally keeps context, but PDF text often
    concatenates the next ``Rule``/``Table``/``Paragraph`` label into the same
    candidate.  Use the bounded structural grammar for the clickable span and
    retain a short ``of Part``/``of Schedule`` tail when present.
    """

    match = STRUCTURE_RE.search(live or "")
    if not match:
        return live, 0, len(live)
    start = match.start()
    end = match.end()
    boundary = None
    for possible in STRUCTURAL_LABEL_START_RE.finditer(live, end):
        prefix = live[end : possible.start()]
        # A label following ``of the ... Part`` is part of the source
        # document title. A new label after a Rulebook/Guidelines title is a
        # concatenated second citation and should start a new span.
        if re.search(r"\b(?:rulebook|guidelines?|handbook)\b", prefix, re.IGNORECASE):
            boundary = possible
            break
    if boundary:
        end = boundary.start()
    else:
        tail = live[end:]
        if re.match(r"\s+(?:of|under|in)\b", tail, re.IGNORECASE):
            stop = re.search(r"[.;\n]|\)\s+(?=[a-z])", tail)
            end += stop.start() if stop else min(len(tail), 180)
        else:
            punctuation = re.search(r"[.;\n]", tail)
            if punctuation:
                end = min(end + punctuation.start(), end + 220)
    end = min(len(live), max(end, match.end()))
    repaired = live[start:end].rstrip()
    while True:
        trimmed = DANGLING_CONNECTOR_RE.sub("", repaired).rstrip()
        if trimmed == repaired:
            break
        repaired = trimmed
    return repaired, start, start + len(repaired)


def boilerplate(source_value: str, start: int, end: int) -> bool:
    before = source_value[max(0, start - 220) : start]
    # Only inspect the immediate preceding UI block.  A valid citation in a
    # substantive sentence may occur elsewhere in a long paragraph containing
    # the word “print”.
    return bool(BOILERPLATE_RE.search(before))


def exact_title_targets(
    indexes: dict[str, Any],
    label: str,
    identifier: str,
    source: sqlite3.Row,
) -> list[sqlite3.Row]:
    wanted = title_key(f"{label} {identifier}")
    return [node for node in indexes["exact"].get(wanted, []) if node["id"] != source["id"]]


def context_targets(
    indexes: dict[str, Any],
    label: str,
    identifier: str,
    source: sqlite3.Row,
    live: str,
) -> list[sqlite3.Row]:
    part_title, document_title = source_context(source)
    ident = base_identifier(identifier)
    context = document_title if label in {"paragraph", "section"} else part_title
    matches = list(indexes["context"].get((label, normalised(context), ident), []))
    if label in {"table", "form", "template"}:
        matches = list(indexes["exact"].get(title_key(f"{label} {identifier}"), []))
    if label == "part" and part_title:
        # Numeric/roman Part labels are meaningful only when a complete title
        # appears immediately before the citation.  Do not guess among the
        # many same-number Parts in the Rulebook.
        return []
    return matches


def build_indexes(nodes: dict[str, sqlite3.Row]) -> dict[str, Any]:
    exact: dict[str, list[sqlite3.Row]] = defaultdict(list)
    context: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    allowed_exact = {"chapter", "guidance_section", "guidance_paragraph", "external_reference", "rule", "part"}
    for node in nodes.values():
        if node["node_type"] in allowed_exact:
            exact[title_key(node["title"] or "")].append(node)
        meta = metadata(node)
        part = normalised(str(meta.get("part_title") or ""))
        doc = normalised(str(meta.get("document_title") or ""))
        if node["node_type"] == "rule":
            value = str(meta.get("rule_number") or node["title"] or "")
            context[("rule", part, base_identifier(value))].append(node)
        elif node["node_type"] == "chapter":
            value = str(meta.get("chapter_number") or "")
            if value:
                context[("chapter", part, base_identifier(value))].append(node)
            article = str(meta.get("article_number") or node["title"] or "")
            annex_match = re.search(r"\bannex\s+([0-9A-Za-zIVXLCDM]+)", article, re.IGNORECASE)
            if annex_match:
                context[("annex", part, base_identifier(annex_match.group(1)))].append(node)
        elif node["node_type"] in {"guidance_paragraph", "guidance_section"}:
            label = "paragraph" if node["node_type"] == "guidance_paragraph" else "section"
            value = str(meta.get("paragraph_number") or meta.get("section_number") or "")
            if value:
                context[(label, doc, base_identifier(value))].append(node)
    return {"exact": dict(exact), "context": dict(context)}


def enrich_part_reference(label: str, identifier: str, value: str, start: int) -> tuple[str, str] | None:
    if label != "part":
        return None
    prefix = value[max(0, start - 80) : start]
    match = re.search(r"\b(Annex\s+[IVXLCDM]+)\s*$", prefix, re.IGNORECASE)
    if match:
        return "annex", f"{match.group(1)} Part {identifier}"
    return None


def existing_state(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], dict[str, list[tuple[int, int]]]]:
    edges = {(row["from_node_id"], row["to_node_id"]) for row in conn.execute("SELECT from_node_id,to_node_id FROM edge")}
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in conn.execute("SELECT source_node_id,span_start,span_end FROM reference_occurrence WHERE status='materialized'"):
        if row["span_start"] is not None and row["span_end"] is not None:
            spans[row["source_node_id"]].append((int(row["span_start"]), int(row["span_end"])))
    return edges, spans


def overlaps(start: int, end: int, old_start: int, old_end: int) -> bool:
    return start < old_end and end > old_start


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect_source(args.db)
    review_conn = connect(f"file:{args.review_db.resolve()}?mode=ro", uri=True)
    stage = connect_stage(args.stage)
    stage.execute("DELETE FROM staged_repair WHERE proposal_method=?", (METHOD,))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-structural"
    nodes = row_nodes(source_conn)
    indexes = build_indexes(nodes)
    edges, spans = existing_state(source_conn)
    counts: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    rows = review_conn.execute(
        """
        SELECT candidate_id,source_node_id,source_text_hash,span_start,span_end,
               quoted_text,candidate_text,candidate_kind,decision,target_status
        FROM corpus_review
        WHERE candidate_kind='structure_reference'
          AND decision IN ('AMBIGUOUS','REFERENCE')
        ORDER BY candidate_id
        """
    )
    seen: set[tuple[str, int, int, str]] = set()
    for candidate in rows:
        source = nodes.get(candidate["source_node_id"])
        if source is None:
            counts["source_missing"] += 1
            continue
        segment = source_span(source, candidate["span_start"], candidate["span_end"])
        if segment is None:
            counts["span_invalid"] += 1
            continue
        live, original_start, original_end = segment
        live, start_delta, repaired_length = repair_structural_span(live)
        start = original_start + start_delta
        end = original_start + repaired_length
        label, identifiers = parse_structural(live)
        if not label or not identifiers:
            counts["unsupported_shape"] += 1
            continue
        if label in {"article", "regulation", "section", "schedule", "title"}:
            # Legal-instrument resolution owns these labels; do not create
            # duplicate local structure links from the lexical detector.
            counts["deferred_to_legal_registry"] += 1
            continue
        if boilerplate(source_text(source), start, end):
            counts["excluded_boilerplate"] += 1
            continue
        # A Part reference preceded immediately by “Annex III” is a composite
        # target in the reporting hierarchy, not an unqualified Part number.
        enriched = enrich_part_reference(label, identifiers[0], source_text(source), start)
        if enriched:
            enriched_label, enriched_title = enriched
            candidates = [
                node
                for node in indexes["exact"].get(title_key(enriched_title), [])
                if node["id"] != source["id"]
            ]
            identifier_targets = [(enriched_label, identifiers[0], candidates)]
        else:
            identifier_targets = [
                (
                    label,
                    ident,
                    context_targets(indexes, label, ident, source, live)
                    or exact_title_targets(indexes, label, ident, source),
                )
                for ident in identifiers
            ]
        # A range is eligible only when every member has exactly one target.
        if any(len(targets) != 1 for _label, _ident, targets in identifier_targets):
            counts["held_ambiguous"] += 1
            target_counts["multiple_or_missing_targets"] += 1
            continue
        for resolved_label, ident, targets in identifier_targets:
            target = targets[0]
            key = (source["id"], start, end, target["id"])
            if key in seen:
                continue
            seen.add(key)
            if target["id"] == source["id"]:
                status, reasons = "held_self_reference", ["source_and_target_are_identical"]
            elif any(overlaps(start, end, old_start, old_end) for old_start, old_end in spans.get(source["id"], [])):
                status, reasons = "held_duplicate_occurrence", ["source_span_already_has_materialized_occurrence"]
            else:
                status, reasons = "eligible", ["unique_structural_target_in_source_context"]
                if (source["id"], target["id"]) in edges:
                    reasons = ["unique_structural_target_reuses_existing_edge"]
            insert_stage(
                stage,
                run_id=run_id,
                source=source,
                target=target,
                candidate_id=candidate["candidate_id"],
                start=start,
                end=end,
                quote=live,
                candidate_text=candidate["candidate_text"] or live,
                citation_kind="structure_reference",
                method=METHOD,
                confidence=0.96 if enriched else 0.93,
                status=status,
                reasons=reasons,
                evidence={
                    "label": resolved_label,
                    "identifier": ident,
                    "candidate_decision": candidate["decision"],
                    "candidate_target_status": candidate["target_status"],
                    "source_text_hash_from_review": candidate["source_text_hash"],
                    "resolver": "source_context_and_exact_hierarchy",
                    "composite_target": target["title"] if enriched else "",
                },
            )
            counts["eligible" if status == "eligible" else status] += 1
            target_counts[resolved_label] += 1
    stage.commit()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "method": METHOD,
        "db": str(args.db),
        "review_db": str(args.review_db),
        "source_nodes": len(nodes),
        "candidate_counts": dict(sorted(counts.items())),
        "resolved_by_label": dict(sorted(target_counts.items())),
        "staged_rows": stage.execute("SELECT COUNT(*) FROM staged_repair WHERE proposal_method=?", (METHOD,)).fetchone()[0],
        "eligible_rows": stage.execute("SELECT COUNT(*) FROM staged_repair WHERE proposal_method=? AND status='eligible'", (METHOD,)).fetchone()[0],
        "status_counts": {
            row["status"]: row["n"]
            for row in stage.execute("SELECT status,COUNT(*) n FROM staged_repair WHERE proposal_method=? GROUP BY status", (METHOD,))
        },
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
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), indent=2, ensure_ascii=False, sort_keys=True))