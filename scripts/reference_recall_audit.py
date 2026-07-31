#!/usr/bin/env python3
"""Build a resumable, read-only ledger of possible missing cross-references.

The Rulebook has several independent reference surfaces: HTML links, exact
legal citation occurrences, graph edges and the earlier LLM pass.  This script
does not change the corpus database and does not call a model.  It puts those
surfaces alongside conservative deterministic candidates in a separate SQLite
ledger so that a later reviewer pass can work from exact spans and hashes.

The ledger is deliberately an audit artefact rather than an edge writer.  A
source node is skipped on ``--resume`` only when its text hash and scanner
version are unchanged; changing a provision therefore creates a fresh set of
candidate IDs instead of silently reusing stale review decisions.
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
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_LEDGER = ROOT / "outputs" / "reference-recall-ledger.sqlite3"
DEFAULT_PILOT = ROOT / "outputs" / "reference-recall-pilot.jsonl"
SCANNER_VERSION = "reference-recall-audit-v2"
DEFAULT_MAX_CHARS = 6000
DEFAULT_CHUNK_OVERLAP = 800
SOURCE_NODE_TYPES = (
    "rule",
    "chapter",
    "part",
    "guidance_document",
    "guidance_section",
    "guidance_paragraph",
    "defined_term",
)

# Keep this grammar broad enough to find reviewer work, but do not treat the
# result as an accepted reference.  Existing exact legal extraction remains
# the higher-quality detector and is merged with these lexical candidates.
STRUCTURE_REFERENCE_RE = re.compile(
    r"\b(?P<label>paragraphs?|paras?|points?|subparagraphs?|articles?|"
    r"sections?|regulations?|rules?|chapters?|parts?|titles?|annex(?:es)?|"
    r"schedules?|templates?|tables?|forms?)\s+"
    r"(?P<references>(?:\d[A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*|[IVXLCDM]+|[A-Z])"
    r"(?:\s*\([^\n)]*\))?"
    r"(?:\s*(?:,|and|or|to|[-–—])\s*"
    r"(?:\d[A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*|[IVXLCDM]+|[A-Z])"
    r"(?:\s*\([^\n)]*\))?)*)"
    r"(?![A-Za-z0-9])"
    r"(?:\s+(?:of|under|in)\s+(?:the\s+)?[^.;\n]{1,180})?",
    re.IGNORECASE,
)

NAMED_REFERENCE_RE = re.compile(
    r"\b(?:the\s+)?(?:"
    r"PRA\s+Rulebook|UK\s+CRR|CRR|"
    r"Statutory\s+Audit\s+Regulation|Capital\s+Requirements?\s+Regulation|"
    r"Financial\s+Services\s+and\s+Markets\s+Act(?:\s+20(?:00|23))?|"
    r"FCA\s+Handbook|EBA\s+Guidelines?|"
    r"(?:Supervisory\s+Statement|Statement\s+of\s+Policy|Finalised\s+Guidance|"
    r"Dear\s+CEO|Consultation\s+Paper)\s+[A-Z]{1,5}\s*\d{1,4}(?:/\d{1,4})?|"
    r"(?:SS|PS|FG|CP)\s*\d{1,4}/\d{1,4}"
    r")\b",
    re.IGNORECASE,
)

NAMED_INSTRUMENT_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9()/'’&-]*\s+){0,8}"
    r"(?:Act|Acts|Regulation|Regulations|Order|Directive|Guidelines?|"
    r"Rules?|Handbook|Code)\b",
)

RELATIVE_STRUCTURE_REFERENCE_RE = re.compile(
    r"\b(?:the\s+)?(?:annex(?:es)?|schedules?|tables?|templates?|forms?)\s+"
    r"(?:referred\s+to|set\s+out|specified|listed)\s+"
    r"(?:in|under|at)\b[^.;\n]{0,120}",
    re.IGNORECASE,
)

BOILERPLATE_RE = re.compile(
    r"\b(?:export|download|print|content\s+loading|table\s+of\s+contents|"
    r"previous|next|back\s+to|open\s+in\s+new\s+window|share|email)\b",
    re.IGNORECASE,
)

SELF_REFERENCE_RE = re.compile(
    r"\b(?:this|that|the\s+same)\s+(?:rule|paragraph|section|article|"
    r"chapter|part|title|annex|schedule|document)\b|"
    r"\b(?:above|below|following|preceding)\b",
    re.IGNORECASE,
)

GENERIC_INSTRUMENT_LABELS = {
    "act",
    "acts",
    "code",
    "directive",
    "guideline",
    "guidelines",
    "handbook",
    "notice",
    "order",
    "regulation",
    "regulations",
    "rule",
    "rules",
}

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_run (
  run_id TEXT PRIMARY KEY,
  scanner_version TEXT NOT NULL,
  source_db TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ledger_node (
  source_node_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scanner_version TEXT NOT NULL,
  node_type TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  source_text_hash TEXT NOT NULL,
  text_length INTEGER NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',
  html_edge_count INTEGER NOT NULL DEFAULT 0,
  occurrence_count INTEGER NOT NULL DEFAULT 0,
  llm_finding_count INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  uncovered_candidate_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_gap (
  candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scanner_version TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  source_node_type TEXT NOT NULL,
  source_title TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  source_text_hash TEXT NOT NULL,
  span_start INTEGER,
  span_end INTEGER,
  candidate_text TEXT NOT NULL,
  candidate_kind TEXT NOT NULL,
  detector_json TEXT NOT NULL DEFAULT '[]',
  reason_json TEXT NOT NULL DEFAULT '[]',
  context_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reference_gap_source
  ON reference_gap(source_node_id, status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_reference_gap_status
  ON reference_gap(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_reference_gap_hash
  ON reference_gap(source_text_hash);

CREATE TABLE IF NOT EXISTS ledger_chunk (
  chunk_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  source_text_hash TEXT NOT NULL,
  chunk_start INTEGER NOT NULL,
  chunk_end INTEGER NOT NULL,
  text TEXT NOT NULL,
  overlap INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  candidate_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_chunk_source
  ON ledger_chunk(source_node_id, chunk_start);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str, algorithm: str = "sha256") -> str:
    return hashlib.new(algorithm, (value or "").encode("utf-8")).hexdigest()


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalised(value: str | None) -> str:
    value = (value or "").casefold().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def json_load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def connect_source(path: Path | str) -> sqlite3.Connection:
    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        resolved = Path(path).resolve()
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect_ledger(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(LEDGER_SCHEMA)
    return conn


def source_text(row: sqlite3.Row) -> str:
    return row["text"] or row["title"] or ""


def source_context(row: sqlite3.Row) -> dict[str, Any]:
    metadata = json_load(row["metadata_json"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    keys = (
        "part_title",
        "chapter_title",
        "document_title",
        "source",
        "rule_number",
        "paragraph_number",
        "section_number",
        "html_id",
    )
    return {key: metadata[key] for key in keys if metadata.get(key) not in (None, "")}


def context_text(value: str, start: int | None, end: int | None, radius: int = 220) -> str:
    if start is None or end is None:
        return compact(value[: radius * 2])
    return compact(value[max(0, start - radius) : min(len(value), end + radius)])


def overlap(a_start: int | None, a_end: int | None, b_start: int | None, b_end: int | None) -> int:
    if None in (a_start, a_end, b_start, b_end):
        return 0
    return max(0, min(int(a_end), int(b_end)) - max(int(a_start), int(b_start)))


def locate(value: str, quote: str) -> tuple[int, int] | None:
    quote = quote or ""
    if not quote:
        return None
    index = value.find(quote)
    if index >= 0:
        return index, index + len(quote)
    # Evidence frequently differs only in HTML whitespace.  Return no span in
    # that case: a reviewer must not be given a fabricated absolute offset.
    return None


def candidate_id(source_id: str, text_hash: str, start: int | None, end: int | None, kind: str, text: str) -> str:
    return digest(
        "|".join(
            (
                source_id,
                text_hash,
                str(start if start is not None else ""),
                str(end if end is not None else ""),
                kind,
                normalised(text),
            )
        ),
        "sha1",
    )[:24]


def _raw_candidate(kind: str, start: int, end: int, text: str, detector: str, **details: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "start": start,
        "end": end,
        "text": compact(text[start:end]),
        "detectors": [detector],
        "details": details,
    }


def build_deterministic_candidates(value: str) -> list[dict[str, Any]]:
    """Return merged lexical/legal candidate spans without judging targets."""

    # Import lazily so tests for helper functions can still run in a minimal
    # environment, and so this script uses the same detector as backfill.
    from backend.rulebook_scraper.article_references import extract_article_citations
    from backend.rulebook_scraper.legal_references import extract_legal_citation_groups

    raw: list[dict[str, Any]] = []
    for group in extract_legal_citation_groups(value):
        raw.append(
            _raw_candidate(
                "legal_citation",
                group.start,
                group.end,
                value,
                f"legal_group:{group.kind}",
                instrument=group.instrument_text,
                targets=[target.display for target in group.targets],
            )
        )
    for citation in extract_article_citations(value):
        raw.append(
            _raw_candidate(
                "article_citation",
                citation.start,
                citation.end,
                value,
                "article_detector",
                tokens=[token.full for token in citation.tokens],
            )
        )
    structure_position = 0
    while True:
        match = STRUCTURE_REFERENCE_RE.search(value, structure_position)
        if not match:
            break
        end = match.end()
        # The optional “of ...” tail is intentionally permissive so named
        # documents are kept in the same review item.  Stop it before a new
        # labelled citation (for example “... Regulation and paragraph 2”).
        next_label = re.search(
            r"\s+(?:and|or)\s+(?:paragraphs?|paras?|points?|subparagraphs?|"
            r"articles?|sections?|regulations?|rules?|chapters?|parts?|titles?|"
            r"annex(?:es)?|schedules?|templates?|tables?|forms?)\b",
            value[match.start() : end],
            re.IGNORECASE,
        )
        if next_label:
            end = match.start() + next_label.start()
        raw.append(
            _raw_candidate(
                "structure_reference",
                match.start(),
                end,
                value,
                "structure_lexical_detector",
                label=match.group("label"),
            )
        )
        # Search again from a truncated tail so adjacent labelled citations
        # are not hidden by the first permissive match.
        structure_position = max(match.start() + 1, end)
    for match in NAMED_REFERENCE_RE.finditer(value):
        raw.append(_raw_candidate("named_document", match.start(), match.end(), value, "named_document_detector"))
    for match in NAMED_INSTRUMENT_RE.finditer(value):
        raw.append(_raw_candidate("named_instrument", match.start(), match.end(), value, "named_instrument_detector"))
    for match in RELATIVE_STRUCTURE_REFERENCE_RE.finditer(value):
        raw.append(_raw_candidate("relative_structure", match.start(), match.end(), value, "relative_structure_detector"))

    # Exact duplicate spans are merged.  When one detector is a strict inner
    # span of another citation, retain the wider phrase: this is important for
    # text such as “Article 26(6) of the Statutory Audit Regulation”.
    raw.sort(key=lambda item: (item["start"], -(item["end"] - item["start"]), item["kind"]))
    merged: list[dict[str, Any]] = []
    for item in raw:
        if not item["text"]:
            continue
        replacement = None
        for index, existing in enumerate(merged):
            shared = overlap(item["start"], item["end"], existing["start"], existing["end"])
            item_length = max(1, item["end"] - item["start"])
            existing_length = max(1, existing["end"] - existing["start"])
            if shared == 0:
                continue
            # Same-start citations or a large overlap are one review item.
            if item["start"] == existing["start"] or shared / min(item_length, existing_length) >= 0.75:
                replacement = index
                break
        if replacement is None:
            merged.append(item)
            continue
        existing = merged[replacement]
        if (item["end"] - item["start"]) > (existing["end"] - existing["start"]):
            existing["start"] = item["start"]
            existing["end"] = item["end"]
            existing["text"] = item["text"]
        existing["detectors"] = sorted(set(existing["detectors"] + item["detectors"]))
        existing.setdefault("details", {}).update(item.get("details") or {})
        if existing["kind"] != "legal_citation" and item["kind"] == "legal_citation":
            existing["kind"] = item["kind"]
    return merged


def html_edges(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "edge"):
        return []
    rows = conn.execute(
        """
        SELECT id,to_node_id,edge_type,source_method,confidence,evidence_text,
               source_url,metadata_json
        FROM edge
        WHERE from_node_id=?
          AND source_method IN ('html_link','html_anchor_resolved','html_anchor_unresolved','html_glossary_link')
        ORDER BY id
        """,
        (source_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def all_edges(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "edge"):
        return []
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id,to_node_id,edge_type,source_method,confidence,evidence_text,
                   source_url,metadata_json
            FROM edge WHERE from_node_id=? ORDER BY id
            """,
            (source_id,),
        )
    ]


def existing_occurrences(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "reference_occurrence"):
        return []
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT occurrence_id,target_node_id,edge_id,relationship_type,citation_kind,
                   citation_text,group_text,span_start,span_end,status,source_method,
                   confidence,context_text,metadata_json
            FROM reference_occurrence WHERE source_node_id=? ORDER BY span_start,span_end,occurrence_id
            """,
            (source_id,),
        )
    ]


def existing_llm_findings(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if table_exists(conn, "llm_reference_extraction"):
        row = conn.execute(
            "SELECT status,response_json,text_hash,prompt_version FROM llm_reference_extraction WHERE node_id=?",
            (source_id,),
        ).fetchone()
        if row:
            payload = json_load(row["response_json"], {})
            refs = payload.get("references", []) if isinstance(payload, dict) else []
            for index, reference in enumerate(refs if isinstance(refs, list) else []):
                if isinstance(reference, dict):
                    findings.append(
                        {
                            "kind": "extraction",
                            "index": index,
                            "status": row["status"],
                            "text_hash": row["text_hash"],
                            "prompt_version": row["prompt_version"],
                            **reference,
                        }
                    )
    if table_exists(conn, "llm_reference_resolution"):
        for row in conn.execute(
            """
            SELECT id,ref_index,reference_text,target_kind,target_title_or_identifier,
                   target_part_or_document,evidence_quote,extracted_confidence,target_node_id,
                   target_node_type,target_title,resolver_method,resolver_confidence,
                   already_had_edge,added_edge_id,metadata_json
            FROM llm_reference_resolution WHERE source_node_id=? ORDER BY ref_index,id
            """,
            (source_id,),
        ):
            findings.append({"kind": "resolution", **dict(row)})
    return findings


def edge_span(edge: dict[str, Any], value: str) -> tuple[int, int] | None:
    metadata = json_load(edge.get("metadata_json"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    source_span = metadata.get("source_span")
    if isinstance(source_span, dict) and source_span.get("start") is not None and source_span.get("end") is not None:
        return int(source_span["start"]), int(source_span["end"])
    return locate(value, edge.get("evidence_text") or "")


def evidence_for_candidate(
    candidate: dict[str, Any],
    value: str,
    occurrences: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    llm_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    start, end = candidate["start"], candidate["end"]
    occurrence_hits: list[dict[str, Any]] = []
    for occurrence in occurrences:
        hit = overlap(start, end, occurrence.get("span_start"), occurrence.get("span_end"))
        text_hit = normalised(candidate["text"]) in {
            normalised(occurrence.get("citation_text")),
            normalised(occurrence.get("group_text")),
        }
        if hit or (text_hit and candidate["text"]):
            occurrence_hits.append(
                {
                    "id": occurrence["occurrence_id"],
                    "status": occurrence.get("status", ""),
                    "target_node_id": occurrence.get("target_node_id") or "",
                    "span": [occurrence.get("span_start"), occurrence.get("span_end")],
                    "source_method": occurrence.get("source_method", ""),
                }
            )
    edge_hits: list[dict[str, Any]] = []
    for edge in edges:
        span = edge_span(edge, value)
        hit = overlap(start, end, *(span or (None, None)))
        evidence = normalised(edge.get("evidence_text"))
        text_hit = bool(evidence and (evidence in normalised(candidate["text"]) or normalised(candidate["text"]) in evidence))
        if hit or text_hit:
            edge_hits.append(
                {
                    "id": edge["id"],
                    "target_node_id": edge.get("to_node_id") or "",
                    "source_method": edge.get("source_method", ""),
                    "edge_type": edge.get("edge_type", ""),
                    "span": list(span) if span else None,
                }
            )
    llm_hits: list[dict[str, Any]] = []
    candidate_norm = normalised(candidate["text"])
    for finding in llm_findings:
        quote = finding.get("evidence_quote") or finding.get("quoted_text") or ""
        ref_text = finding.get("reference_text") or finding.get("target_title_or_identifier") or ""
        quote_norm = normalised(quote)
        ref_norm = normalised(ref_text)
        text_hit = bool(
            candidate_norm
            and ((quote_norm and (quote_norm in candidate_norm or candidate_norm in quote_norm))
                 or (ref_norm and (ref_norm in candidate_norm or candidate_norm in ref_norm)))
        )
        span_hit = False
        for phrase in (quote, ref_text):
            found = locate(value, phrase)
            if found and overlap(start, end, *found):
                span_hit = True
                break
        if text_hit or span_hit:
            llm_hits.append(
                {
                    "id": finding.get("id") or f"extraction:{finding.get('index', '')}",
                    "kind": finding.get("kind", ""),
                    "reference_text": ref_text,
                    "evidence_quote": quote,
                    "target_node_id": finding.get("target_node_id") or "",
                    "resolver_method": finding.get("resolver_method") or "",
                    "resolver_confidence": finding.get("resolver_confidence"),
                    "extracted_confidence": finding.get("extracted_confidence") or finding.get("confidence"),
                    "already_had_edge": finding.get("already_had_edge"),
                    "added_edge_id": finding.get("added_edge_id") or "",
                }
            )
    return {"occurrences": occurrence_hits, "edges": edge_hits, "llm": llm_hits}


def generic_named_instrument_label(value: str) -> bool:
    """Identify detector fragments that are not a named instrument.

    The broad instrument regex intentionally catches phrases such as
    ``Rules`` or ``Regulations Regulations 2024``. Those fragments mostly
    come from PDF tables, headings and ordinary prose; treating them as review
    candidates overwhelms the substantive queue. Specific instruments that
    include a year, number or distinctive multi-word title remain candidates.
    """
    text = normalised(value)
    if not text:
        return False
    if text in GENERIC_INSTRUMENT_LABELS:
        return True
    if text in {
        "this code",
        "the code",
        "prudential regulation",
        "the prudential regulation",
        "act reference",
        "notice description act",
    }:
        return True
    words = text.split()
    return len(words) >= 2 and len(set(words)) == 1 and words[0] in GENERIC_INSTRUMENT_LABELS


def classify_candidate(candidate: dict[str, Any], value: str, evidence: dict[str, Any], title: str, node_type: str, max_chars: int) -> tuple[str, int, list[str]]:
    start, end = candidate["start"], candidate["end"]
    reasons: list[str] = []
    candidate_context = context_text(value, start, end)
    if BOILERPLATE_RE.search(candidate_context) and node_type in {"chapter", "part", "guidance_document", "guidance_section"}:
        reasons.append("boilerplate_or_navigation_context")
    if SELF_REFERENCE_RE.search(candidate["text"]):
        reasons.append("relative_or_self_reference_language")
    if normalised(candidate["text"]) == normalised(title) and title:
        reasons.append("candidate_matches_source_title")
    if candidate["kind"] == "named_instrument" and generic_named_instrument_label(candidate["text"]):
        reasons.append("generic_or_table_instrument_label")

    if any(item.get("status") == "not_reference" for item in evidence["occurrences"]):
        status = "classified_not_reference"
        priority = 0
        reasons.append("existing_occurrence_classifies_not_reference")
    elif reasons and all(reason in {"boilerplate_or_navigation_context", "relative_or_self_reference_language", "candidate_matches_source_title", "generic_or_table_instrument_label"} for reason in reasons):
        status = "excluded_context"
        priority = 5
    elif evidence["occurrences"]:
        status = "covered_occurrence"
        priority = 20
        reasons.append("existing_reference_occurrence")
    elif evidence["edges"]:
        status = "covered_edge"
        priority = 25
        reasons.append("existing_graph_edge")
    elif evidence["llm"]:
        resolved = [item for item in evidence["llm"] if item.get("target_node_id") or item.get("added_edge_id") or item.get("already_had_edge")]
        status = "covered_llm" if resolved else "llm_unresolved"
        priority = 45 if resolved else 70
        reasons.append("previous_llm_finding")
    elif start is not None and start >= max_chars:
        status = "tail_unreviewed"
        priority = 100
        reasons.append("outside_existing_llm_prefix")
    else:
        status = "needs_review"
        priority = 80
        reasons.append("deterministic_candidate_without_existing_surface")

    if candidate["kind"] in {"legal_citation", "article_citation"}:
        priority += 8
    if candidate["kind"] in {"named_document", "named_instrument"}:
        priority += 5
    if start is None or end is None:
        priority = max(priority, 60)
        reasons.append("html_evidence_has_no_exact_text_span")
    return status, min(priority, 100), sorted(set(reasons))


def merge_html_candidates(candidates: list[dict[str, Any]], value: str, edges: list[dict[str, Any]]) -> None:
    """Add one candidate for every HTML reference surface not found lexically."""
    for edge in edges:
        evidence = edge.get("evidence_text") or ""
        span = edge_span(edge, value)
        start, end = span if span else (None, None)
        if span and any(overlap(start, end, item["start"], item["end"]) > 0 for item in candidates):
            # Existing lexical candidates will still record the HTML edge in
            # their evidence.  Avoid a duplicate work item for the same text.
            continue
        text = compact(evidence) or compact(json_load(edge.get("metadata_json"), {}).get("href", ""))
        if not text:
            continue
        candidates.append(
            {
                "kind": "html_link",
                "start": start,
                "end": end,
                "text": text,
                "detectors": [f"html_edge:{edge.get('source_method', '')}"],
                "details": {"edge_id": edge.get("id"), "target_node_id": edge.get("to_node_id") or ""},
            }
        )


def make_chunks(source_id: str, text_hash: str, value: str, candidates: list[dict[str, Any]], max_chars: int, overlap_chars: int) -> list[dict[str, Any]]:
    if len(value) <= max_chars:
        return []
    chunks: list[dict[str, Any]] = []
    step = max(1, max_chars - overlap_chars)
    start = 0
    while start < len(value):
        end = min(len(value), start + max_chars)
        ids = []
        priority = 0
        for item in candidates:
            if overlap(item.get("start"), item.get("end"), start, end) > 0:
                ids.append(item["candidate_id"])
                priority = max(priority, int(item.get("priority", 0)))
        reason = "tail" if end > max_chars else "prefix_or_overlap"
        if ids:
            reason += ":candidate"
        chunk_id = digest(f"{source_id}|{text_hash}|{start}|{end}", "sha1")[:24]
        chunks.append(
            {
                "chunk_id": chunk_id,
                "source_node_id": source_id,
                "source_text_hash": text_hash,
                "chunk_start": start,
                "chunk_end": end,
                "text": value[start:end],
                "overlap": overlap_chars if start else 0,
                "reason": reason,
                "priority": priority,
                "candidate_ids": ids,
            }
        )
        if end >= len(value):
            break
        start += step
    return chunks


def source_rows(conn: sqlite3.Connection, node_types: Iterable[str], limit: int | None) -> list[sqlite3.Row]:
    node_types = tuple(node_types)
    placeholders = ",".join("?" for _ in node_types)
    sql = f"""
        SELECT id,node_type,stable_key,title,text,url,metadata_json
        FROM node
        WHERE node_type IN ({placeholders})
          AND (COALESCE(text,'')<>'' OR COALESCE(title,'')<>'')
        ORDER BY CASE node_type
          WHEN 'rule' THEN 1
          WHEN 'guidance_paragraph' THEN 2
          WHEN 'defined_term' THEN 3
          WHEN 'guidance_section' THEN 4
          ELSE 5 END,
          title,id
    """
    if limit:
        sql += " LIMIT ?"
        return conn.execute(sql, (*node_types, limit)).fetchall()
    return conn.execute(sql, node_types).fetchall()


def existing_node_hash(ledger: sqlite3.Connection, source_id: str, text_hash: str) -> bool:
    row = ledger.execute(
        "SELECT source_text_hash,scanner_version FROM ledger_node WHERE source_node_id=?",
        (source_id,),
    ).fetchone()
    return bool(row and row["source_text_hash"] == text_hash and row["scanner_version"] == SCANNER_VERSION)


def delete_node_ledger(ledger: sqlite3.Connection, source_id: str) -> None:
    ledger.execute("DELETE FROM reference_gap WHERE source_node_id=?", (source_id,))
    ledger.execute("DELETE FROM ledger_chunk WHERE source_node_id=?", (source_id,))
    ledger.execute("DELETE FROM ledger_node WHERE source_node_id=?", (source_id,))


def scan_node(source: sqlite3.Row, source_conn: sqlite3.Connection, run_id: str, max_chars: int, chunk_overlap: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    value = source_text(source)
    text_hash = digest(value)
    occurrences = existing_occurrences(source_conn, source["id"])
    edges = all_edges(source_conn, source["id"])
    html = html_edges(source_conn, source["id"])
    llm = existing_llm_findings(source_conn, source["id"])
    candidates = build_deterministic_candidates(value)
    merge_html_candidates(candidates, value, html)

    # A second pass preserves exact HTML/edge evidence on lexical candidates.
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        evidence = evidence_for_candidate(candidate, value, occurrences, edges, llm)
        status, priority, reasons = classify_candidate(
            candidate, value, evidence, source["title"] or "", source["node_type"], max_chars
        )
        cid = candidate_id(
            source["id"], text_hash, candidate.get("start"), candidate.get("end"), candidate["kind"], candidate["text"]
        )
        candidate["candidate_id"] = cid
        candidate["priority"] = priority
        candidate["status"] = status
        candidate["reasons"] = reasons
        candidate["evidence"] = evidence
        candidate["context"] = context_text(value, candidate.get("start"), candidate.get("end"))
        existing = by_id.get(cid)
        if existing is None:
            by_id[cid] = candidate
            continue
        # Multiple HTML edges can expose the same visible anchor text.  They
        # are one review item, but all target evidence must remain available.
        existing["detectors"] = sorted(set(existing.get("detectors", []) + candidate.get("detectors", [])))
        existing["reasons"] = sorted(set(existing.get("reasons", []) + candidate.get("reasons", [])))
        existing["priority"] = max(existing.get("priority", 0), candidate.get("priority", 0))
        status_rank = {
            "needs_review": 10,
            "tail_unreviewed": 20,
            "llm_unresolved": 30,
            "covered_edge": 40,
            "covered_occurrence": 50,
            "covered_llm": 60,
            "classified_not_reference": 70,
            "excluded_context": 80,
        }
        if status_rank.get(candidate["status"], 0) > status_rank.get(existing["status"], 0):
            existing["status"] = candidate["status"]
        for evidence_key in ("occurrences", "edges", "llm"):
            seen = {json.dumps(item, sort_keys=True) for item in existing["evidence"].get(evidence_key, [])}
            for item in candidate["evidence"].get(evidence_key, []):
                encoded = json.dumps(item, sort_keys=True)
                if encoded not in seen:
                    existing["evidence"].setdefault(evidence_key, []).append(item)
                    seen.add(encoded)
    rows = list(by_id.values())

    uncovered = sum(1 for row in rows if row["status"] in {"needs_review", "tail_unreviewed", "llm_unresolved"})
    node_record = {
        "source_node_id": source["id"],
        "run_id": run_id,
        "scanner_version": SCANNER_VERSION,
        "node_type": source["node_type"],
        "title": source["title"] or "",
        "url": source["url"] or "",
        "source_text_hash": text_hash,
        "text_length": len(value),
        "context": source_context(source),
        "html_edge_count": len(html),
        "occurrence_count": len(occurrences),
        "llm_finding_count": len(llm),
        "candidate_count": len(rows),
        "uncovered_candidate_count": uncovered,
        "status": "scanned",
    }
    chunks = make_chunks(source["id"], text_hash, value, rows, max_chars, chunk_overlap)
    return node_record, rows, chunks


def write_node(ledger: sqlite3.Connection, node_record: dict[str, Any], candidates: list[dict[str, Any]], chunks: list[dict[str, Any]], created_at: str) -> None:
    source_id = node_record["source_node_id"]
    delete_node_ledger(ledger, source_id)
    ledger.execute(
        """
        INSERT INTO ledger_node(
          source_node_id,run_id,scanner_version,node_type,title,url,source_text_hash,
          text_length,context_json,html_edge_count,occurrence_count,llm_finding_count,
          candidate_count,uncovered_candidate_count,status,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_id,
            node_record["run_id"],
            node_record["scanner_version"],
            node_record["node_type"],
            node_record["title"],
            node_record["url"],
            node_record["source_text_hash"],
            node_record["text_length"],
            json.dumps(node_record["context"], ensure_ascii=False, sort_keys=True),
            node_record["html_edge_count"],
            node_record["occurrence_count"],
            node_record["llm_finding_count"],
            node_record["candidate_count"],
            node_record["uncovered_candidate_count"],
            node_record["status"],
            created_at,
        ),
    )
    for row in candidates:
        ledger.execute(
            """
            INSERT INTO reference_gap(
              candidate_id,run_id,scanner_version,source_node_id,source_node_type,
              source_title,source_url,source_text_hash,span_start,span_end,candidate_text,
              candidate_kind,detector_json,reason_json,context_text,status,priority,
              evidence_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["candidate_id"],
                node_record["run_id"],
                SCANNER_VERSION,
                source_id,
                node_record["node_type"],
                node_record["title"],
                node_record["url"],
                node_record["source_text_hash"],
                row.get("start"),
                row.get("end"),
                row["text"],
                row["kind"],
                json.dumps(row.get("detectors", []), ensure_ascii=False),
                json.dumps(row.get("reasons", []), ensure_ascii=False),
                row.get("context", ""),
                row["status"],
                row["priority"],
                json.dumps(row.get("evidence", {}), ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
    for chunk in chunks:
        ledger.execute(
            """
            INSERT INTO ledger_chunk(
              chunk_id,run_id,source_node_id,source_text_hash,chunk_start,chunk_end,
              text,overlap,reason,priority,candidate_ids_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                chunk["chunk_id"],
                node_record["run_id"],
                source_id,
                chunk["source_text_hash"],
                chunk["chunk_start"],
                chunk["chunk_end"],
                chunk["text"],
                chunk["overlap"],
                chunk["reason"],
                chunk["priority"],
                json.dumps(chunk["candidate_ids"], ensure_ascii=False),
                created_at,
            ),
        )


def write_pilot(
    ledger: sqlite3.Connection,
    source_conn: sqlite3.Connection,
    path: Path,
    size: int,
    max_chars: int,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ledger.execute(
        """
        SELECT candidate_id,source_node_id,source_node_type,source_title,source_url,
               source_text_hash,span_start,span_end,candidate_text,candidate_kind,
               detector_json,reason_json,context_text,status,priority,evidence_json
        FROM reference_gap
        WHERE status IN ('needs_review','tail_unreviewed','llm_unresolved')
        ORDER BY CASE source_node_type
          WHEN 'rule' THEN 1
          WHEN 'guidance_paragraph' THEN 2
          WHEN 'defined_term' THEN 3
          WHEN 'guidance_section' THEN 4
          WHEN 'guidance_document' THEN 5
          WHEN 'chapter' THEN 6
          WHEN 'part' THEN 7 ELSE 8 END,
          CASE candidate_kind
            WHEN 'legal_citation' THEN 1
            WHEN 'article_citation' THEN 2
            WHEN 'named_document' THEN 3
            WHEN 'named_instrument' THEN 4
            WHEN 'relative_structure' THEN 5
            ELSE 6 END,
          priority DESC,source_title,span_start,candidate_id
        """,
    ).fetchall()
    # A pilot item is a provision/chunk, not an individual candidate.  This
    # prevents a single aggregate Chapter from consuming the whole first
    # batch and gives the reviewer the surrounding text needed for recall.
    by_source: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_source.setdefault(row["source_node_id"], []).append(row)
    source_order = list(by_source)
    with path.open("w", encoding="utf-8") as handle:
        emitted = 0
        for source_id in source_order:
            if emitted >= size:
                break
            candidate_rows = by_source[source_id]
            primary = candidate_rows[0]
            source = source_conn.execute(
                "SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id=?",
                (source_id,),
            ).fetchone()
            if source is None:
                continue
            full_text = source_text(source)
            chunk_start, chunk_end, review_text = 0, len(full_text), full_text
            if len(full_text) > max_chars:
                span_start = primary["span_start"]
                chunk = None
                if span_start is not None:
                    chunk = ledger.execute(
                        """
                        SELECT chunk_start,chunk_end,text FROM ledger_chunk
                        WHERE source_node_id=? AND chunk_start<=? AND chunk_end>? 
                        ORDER BY CASE WHEN reason LIKE 'tail%' THEN 0 ELSE 1 END,
                                 priority DESC,chunk_start
                        LIMIT 1
                        """,
                        (source_id, span_start, span_start),
                    ).fetchone()
                if chunk is None:
                    chunk = ledger.execute(
                        """
                        SELECT chunk_start,chunk_end,text FROM ledger_chunk
                        WHERE source_node_id=?
                        ORDER BY priority DESC,chunk_start DESC LIMIT 1
                        """,
                        (source_id,),
                    ).fetchone()
                if chunk is not None:
                    chunk_start, chunk_end, review_text = chunk["chunk_start"], chunk["chunk_end"], chunk["text"]
            review_candidates = []
            for row in candidate_rows:
                if len(review_candidates) >= 30:
                    break
                if row["span_start"] is not None and overlap(row["span_start"], row["span_end"], chunk_start, chunk_end) == 0:
                    continue
                review_candidates.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "span_start": row["span_start"],
                        "span_end": row["span_end"],
                        "candidate_text": row["candidate_text"],
                        "candidate_kind": row["candidate_kind"],
                        "status": row["status"],
                        "priority": row["priority"],
                        "detectors": json_load(row["detector_json"], []),
                        "reasons": json_load(row["reason_json"], []),
                    }
                )
            record = {
                "source_node_id": source["id"],
                "source_node_type": source["node_type"],
                "source_title": source["title"] or "",
                "source_url": source["url"] or "",
                "source_text_hash": digest(full_text),
                "context": source_context(source),
                "chunk_start": chunk_start,
                "chunk_end": chunk_end,
                "text": review_text,
                "candidates": review_candidates,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            emitted += 1
    return emitted


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect_source(args.db)
    ledger = connect_ledger(args.ledger)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{digest(now(), 'sha1')[:8]}"
    started = now()
    options = {
        "node_types": args.node_type or list(SOURCE_NODE_TYPES),
        "limit": args.limit,
        "max_chars": args.max_chars,
        "chunk_overlap": args.chunk_overlap,
        "resume": bool(args.resume),
    }
    ledger.execute(
        "INSERT INTO ledger_run(run_id,scanner_version,source_db,started_at,options_json) VALUES (?,?,?,?,?)",
        (run_id, SCANNER_VERSION, str(Path(args.db)), started, json.dumps(options, sort_keys=True)),
    )
    ledger.commit()

    counts = Counter()
    rows = source_rows(source_conn, args.node_type or SOURCE_NODE_TYPES, args.limit)
    total = len(rows)
    for index, source in enumerate(rows, 1):
        value = source_text(source)
        text_hash = digest(value)
        if args.resume and existing_node_hash(ledger, source["id"], text_hash):
            counts["skipped_unchanged"] += 1
            continue
        node_record, candidates, chunks = scan_node(
            source, source_conn, run_id, args.max_chars, args.chunk_overlap
        )
        write_node(ledger, node_record, chunks=chunks, candidates=candidates, created_at=started)
        counts["scanned"] += 1
        counts["candidates"] += len(candidates)
        counts["uncovered_candidates"] += node_record["uncovered_candidate_count"]
        counts["chunks"] += len(chunks)
        for candidate in candidates:
            counts[f"status:{candidate['status']}"] += 1
        if index % 100 == 0:
            ledger.commit()
            print(f"scanned {index}/{total}", file=sys.stderr)
    ledger.commit()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "scanner_version": SCANNER_VERSION,
        "source_db": str(Path(args.db)),
        "ledger": str(Path(args.ledger)),
        "source_nodes": total,
        **dict(counts),
    }
    if args.pilot_output:
        pilot_count = write_pilot(
            ledger,
            source_conn,
            Path(args.pilot_output),
            args.pilot_size,
            args.max_chars,
        )
        ledger.commit()
        summary["pilot_output"] = str(Path(args.pilot_output))
        summary["pilot_items"] = pilot_count
    finished = now()
    ledger.execute(
        "UPDATE ledger_run SET finished_at=?,summary_json=? WHERE run_id=?",
        (finished, json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
    )
    ledger.commit()
    source_conn.close()
    ledger.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--node-type", action="append", choices=SOURCE_NODE_TYPES)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--resume", action="store_true", help="skip nodes with the same source hash and scanner version")
    parser.add_argument("--pilot-output", type=Path, default=None)
    parser.add_argument("--pilot-size", type=int, default=200)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
