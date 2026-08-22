#!/usr/bin/env python3
"""Stage deterministic and already-resolved reference repairs.

This is the bridge between the recall ledger and materialisation.  The source
Rulebook database is opened read-only; proposals and holds are written to a
separate SQLite file.  A proposal is eligible only when its quoted text is an
exact source substring, its target is unique and present, and no self/duplicate
relationship already exists.  The companion apply step can later promote
reviewed proposals to versioned occurrence/edge rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
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


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_LEDGER = ROOT / "logs" / "reference-recall-ledger-20260731.sqlite3"
DEFAULT_STAGE = ROOT / "logs" / "reference-recall-stage-20260731.sqlite3"
STAGE_VERSION = "reference-recall-stage-v1"
AUTO_MIN_CONFIDENCE = 0.90
LLM_MIN_EXTRACTED = 0.70
LLM_MIN_RESOLVER = 0.88

STAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_run (
  run_id TEXT PRIMARY KEY,
  stage_version TEXT NOT NULL,
  source_db TEXT NOT NULL,
  ledger_db TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS staged_repair (
  proposal_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  candidate_id TEXT DEFAULT '',
  source_node_id TEXT NOT NULL,
  target_node_id TEXT DEFAULT '',
  source_node_type TEXT NOT NULL,
  target_node_type TEXT DEFAULT '',
  source_title TEXT DEFAULT '',
  target_title TEXT DEFAULT '',
  source_text_hash TEXT NOT NULL,
  span_start INTEGER,
  span_end INTEGER,
  quoted_text TEXT NOT NULL DEFAULT '',
  candidate_text TEXT NOT NULL DEFAULT '',
  citation_kind TEXT DEFAULT '',
  relationship_type TEXT NOT NULL DEFAULT 'REF',
  proposal_method TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  reason_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_staged_repair_status
  ON staged_repair(status,proposal_method,confidence DESC);
CREATE INDEX IF NOT EXISTS idx_staged_repair_source
  ON staged_repair(source_node_id,span_start,span_end);
CREATE INDEX IF NOT EXISTS idx_staged_repair_target
  ON staged_repair(target_node_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_stage(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path, timeout=60)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(STAGE_SCHEMA)
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def source_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["id"]: row
        for row in conn.execute(
            "SELECT id,node_type,title,text,url,metadata_json FROM node"
        )
    }


def stage_id(source_id: str, target_id: str, start: int | None, end: int | None, method: str, quote: str) -> str:
    return hashlib.sha1(
        f"{source_id}|{target_id}|{start}|{end}|{method}|{quote}".encode("utf-8")
    ).hexdigest()[:28]


def exact_spans(text: str, quote: str) -> list[tuple[int, int]]:
    if not quote:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(quote, start)
        if found < 0:
            return out
        out.append((found, found + len(quote)))
        start = found + 1


def relationship_for_target(target: sqlite3.Row | None) -> tuple[str, str]:
    """Return the reader relationship and graph edge type for a target.

    Glossary terms are a distinct relationship in the existing graph.  Keeping
    them as ``uses_defined_term`` means the reader can render them as DEF and
    avoids adding ordinary cross-reference edges to the definition shelf.
    Everything else discovered by this recall pass is a cross-reference.
    """
    if target is not None and target["node_type"] == "defined_term":
        return "DEF", "uses_defined_term"
    return "REF", "references"


def llm_quote_and_spans(
    text: str,
    reference_text: str,
    target_title_or_identifier: str,
    evidence_quote: str,
) -> tuple[str, list[tuple[int, int]], str]:
    """Choose a precise citation span while preserving broad LLM evidence.

    The resolver stores a human-readable evidence sentence in
    ``reference_text``.  That sentence can contain an entire definition or
    paragraph, which is unsuitable as the clickable citation span.  Prefer the
    target identifier when it occurs inside the broad evidence span, otherwise
    choose the shortest exact phrase.  The returned third value is the broad
    evidence phrase used for provenance.
    """
    broad_phrases = [phrase for phrase in (reference_text, evidence_quote) if phrase]
    broad_spans: list[tuple[int, int]] = []
    broad_quote = ""
    for phrase in broad_phrases:
        spans = exact_spans(text, phrase)
        if spans and (not broad_spans or len(phrase) > len(broad_quote)):
            broad_quote, broad_spans = phrase, spans

    target_phrases = [
        phrase
        for phrase in (target_title_or_identifier, reference_text, evidence_quote)
        if phrase
    ]
    # First try the target identifier within the broad evidence span.  This
    # keeps ``Investment firm`` rather than the entire quoted definition while
    # still disambiguating repeated identifiers outside that evidence.
    if target_title_or_identifier and broad_spans:
        target_spans = exact_spans(text, target_title_or_identifier)
        contained = [
            span for span in target_spans
            if any(span[0] >= broad[0] and span[1] <= broad[1] for broad in broad_spans)
        ]
        if contained:
            return target_title_or_identifier, contained, broad_quote

    exact_candidates: list[tuple[int, str, list[tuple[int, int]]]] = []
    for phrase in target_phrases:
        spans = exact_spans(text, phrase)
        if spans:
            exact_candidates.append((len(phrase), phrase, spans))
    if exact_candidates:
        _, phrase, spans = min(exact_candidates, key=lambda item: item[0])
        return phrase, spans, broad_quote or phrase
    fallback = reference_text or target_title_or_identifier or evidence_quote or ""
    return fallback, [], broad_quote or fallback


def metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return json_load(row["metadata_json"], {}) if isinstance(row, sqlite3.Row) else json_load(row.get("metadata_json"), {})


def context_keys(row: sqlite3.Row | dict[str, Any]) -> set[str]:
    meta = metadata(row)
    return {
        normalised(str(meta.get(key) or ""))
        for key in ("part_title", "document_title", "chapter_title", "source")
        if meta.get(key)
    }


def same_context(source: sqlite3.Row | dict[str, Any], target: sqlite3.Row | dict[str, Any], explicit: str = "") -> bool:
    source_keys = context_keys(source)
    target_keys = context_keys(target)
    explicit_norm = normalised(explicit)
    if explicit_norm:
        if any(explicit_norm == key or explicit_norm in key or key in explicit_norm for key in target_keys if key):
            return True
    return bool(source_keys & target_keys)


def node_title_number(row: sqlite3.Row | dict[str, Any]) -> str:
    meta = metadata(row)
    for key in ("rule_number", "display_number", "section_number", "paragraph_number"):
        if meta.get(key):
            return str(meta[key]).rstrip(".")
    title = str(row["title"] if isinstance(row, sqlite3.Row) else row.get("title") or "")
    return title.split(" ", 1)[0].rstrip(".")


def structural_parts(candidate_text: str) -> tuple[str, str, str]:
    match = re.search(
        r"\b(?P<label>paragraphs?|paras?|points?|subparagraphs?|articles?|sections?|regulations?|rules?|chapters?|parts?|titles?|annex(?:es)?|schedules?|templates?|tables?|forms?)\s+"
        r"(?P<identifier>[0-9A-Za-zIVXLCDM]+(?:\.[0-9A-Za-z]+)*(?:\s*\([^)]*\))?)",
        candidate_text,
        re.IGNORECASE,
    )
    if not match:
        return "", "", ""
    label = match.group("label").casefold().rstrip("s")
    identifier = re.sub(r"\s+", "", match.group("identifier")).casefold()
    tail = candidate_text[match.end() :]
    explicit = ""
    of_match = re.search(r"\b(?:of|under|in)\s+(?:the\s+)?(.+)$", tail, re.IGNORECASE)
    if of_match:
        explicit = of_match.group(1).strip(" .,;:")
    return label, identifier, explicit


def build_target_indexes(nodes: dict[str, sqlite3.Row]) -> dict[str, Any]:
    exact_title: dict[str, list[sqlite3.Row]] = defaultdict(list)
    structural: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for node in nodes.values():
        raw_title = str(node["title"] or "").strip()
        title = normalised(raw_title)
        if title:
            exact_title[title].append(node)
        match = re.match(
            r"^(article|chapter|part|annex|schedule|template|table|form|rule|paragraph|section|point|subparagraph)\s+"
            r"([0-9a-z]+(?:\.[0-9a-z]+)*(?:\([^)]*\))?)",
            raw_title,
            re.IGNORECASE,
        )
        if match:
            structural[(match.group(1).casefold(), re.sub(r"\s+", "", match.group(2)).casefold())].append(node)
    return {"exact_title": exact_title, "structural": structural}


def candidate_targets(
    source: sqlite3.Row,
    candidate: sqlite3.Row,
    nodes: dict[str, sqlite3.Row],
    indexes: dict[str, Any],
) -> tuple[list[sqlite3.Row], str, float]:
    """Resolve only exact local targets; no network/external-node creation."""
    text = candidate["candidate_text"] or ""
    kind = candidate["candidate_kind"] or ""
    label, identifier, explicit = structural_parts(text)
    source_meta = metadata(source)
    source_context = normalised(str(source_meta.get("part_title") or source_meta.get("document_title") or ""))

    if kind in {"named_document", "named_instrument"}:
        wanted = normalised(text)
        wanted_without_the = re.sub(r"^the\s+", "", wanted)
        candidates = [
            node for key in {wanted, wanted_without_the}
            for node in indexes["exact_title"].get(key, [])
            if node["id"] != source["id"]
            and node["node_type"] in {"part", "guidance_document", "legal_instrument", "external_reference", "chapter"}
        ]
        candidates = list({node["id"]: node for node in candidates}.values())
        if len(candidates) == 1:
            return candidates, "exact_named_title", 0.94
        return candidates, "exact_named_title", 0.0

    if not label:
        return [], "not_structural", 0.0

    # Article references explicitly tied to CRR are local only when the
    # already-materialised external Article node exists.  Never create one in
    # this read-only stage.
    article_match = re.fullmatch(r"article\s+(\d+[a-z]?)(?:\s*\([^)]*\))?.*", text, re.IGNORECASE)
    if label == "article":
        article = identifier.split("(", 1)[0]
        if re.search(r"\b(?:UK\s+)?CRR\b", text, re.IGNORECASE):
            external = [node for node in indexes["exact_title"].get(normalised(f"UK CRR Article {article}"), [])]
            direct = nodes.get(f"external:uk-crr:article:{article}")
            if direct is not None and direct not in external:
                external.append(direct)
            if len(external) == 1:
                return external, "existing_uk_crr_article", 0.96
        wanted = normalised(f"article {article}")
        matches = []
        article_nodes = [
            node
            for (structural_label, structural_identifier), values in indexes["structural"].items()
            if structural_label == "article" and structural_identifier.split("(", 1)[0] == article.casefold()
            for node in values
        ]
        for node in article_nodes:
            if node["id"] == source["id"] or node["node_type"] not in {"chapter", "rule"}:
                continue
            if source_context and same_context(source, node, explicit):
                matches.append(node)
        if len(matches) == 1:
            return matches, "article_same_context", 0.95
        return matches, "article_same_context", 0.0

    target_types = {
        "rule": {"rule", "guidance_paragraph", "guidance_section"},
        "paragraph": {"guidance_paragraph", "rule"},
        "section": {"guidance_section", "rule", "chapter"},
        "regulation": {"external_reference", "chapter", "rule"},
        "chapter": {"chapter"},
        "part": {"part"},
        "title": {"chapter", "part"},
        "annex": {"chapter", "rule", "external_reference"},
        "schedule": {"chapter", "rule", "external_reference"},
        "template": {"chapter", "rule", "external_reference"},
        "table": {"chapter", "rule", "external_reference"},
        "form": {"chapter", "rule", "external_reference"},
        "point": {"rule", "guidance_paragraph", "external_reference"},
        "subparagraph": {"rule", "guidance_paragraph", "external_reference"},
    }.get(label, set())
    if not target_types:
        return [], "unsupported_structure", 0.0
    matches: list[sqlite3.Row] = []
    # Structural identifiers use punctuation (for example ``2.4``) as part
    # of their identity.  The general text normalizer intentionally removes
    # punctuation, so use a structural-specific key here to stay consistent
    # with ``build_target_indexes``.
    identifier_norm = re.sub(r"\s+", "", identifier).casefold()
    indexed_nodes = indexes["structural"].get((label, identifier_norm), [])
    for node in indexed_nodes:
        if node["id"] == source["id"] or node["node_type"] not in target_types:
            continue
        if source_context and not same_context(source, node, explicit):
            continue
        matches.append(node)
    if len(matches) == 1:
        return matches, "exact_structural_title", 0.93
    return matches, "exact_structural_title", 0.0


def existing_relationship(source_conn: sqlite3.Connection, source_id: str, target_id: str) -> bool:
    return bool(source_conn.execute("SELECT 1 FROM edge WHERE from_node_id=? AND to_node_id=? LIMIT 1", (source_id, target_id)).fetchone())


def existing_occurrence_overlap(source_conn: sqlite3.Connection, source_id: str, start: int | None, end: int | None) -> bool:
    return any(overlap(start, end, row.get("span_start"), row.get("span_end")) > 0 for row in existing_occurrences(source_conn, source_id))


def insert_stage(
    stage: sqlite3.Connection,
    *,
    run_id: str,
    source: sqlite3.Row,
    target: sqlite3.Row | None,
    candidate_id: str,
    start: int | None,
    end: int | None,
    quote: str,
    candidate_text: str,
    citation_kind: str,
    method: str,
    confidence: float,
    status: str,
    reasons: list[str],
    evidence: dict[str, Any],
    relationship_type: str = "REF",
) -> None:
    target_id = target["id"] if target else ""
    proposal_id = stage_id(source["id"], target_id, start, end, method, quote)
    stage.execute(
        """
        INSERT OR REPLACE INTO staged_repair(
          proposal_id,run_id,candidate_id,source_node_id,target_node_id,source_node_type,
          target_node_type,source_title,target_title,source_text_hash,span_start,span_end,
          quoted_text,candidate_text,citation_kind,relationship_type,proposal_method,
          confidence,status,reason_json,evidence_json,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            proposal_id,
            run_id,
            candidate_id,
            source["id"],
            target_id,
            source["node_type"],
            target["node_type"] if target else "",
            source["title"] or "",
            target["title"] if target else "",
            digest(source_text(source)),
            start,
            end,
            quote,
            candidate_text,
            citation_kind,
            relationship_type,
            method,
            confidence,
            status,
            json.dumps(sorted(set(reasons)), ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            now(),
        ),
    )


def stage_llm(
    source_conn: sqlite3.Connection,
    stage: sqlite3.Connection,
    run_id: str,
    nodes: dict[str, sqlite3.Row],
    source_texts: dict[str, str],
    min_extracted: float,
    min_resolver: float,
) -> int:
    if not table_exists(source_conn, "llm_reference_resolution"):
        return 0
    count = 0
    rows = source_conn.execute(
        """
        SELECT id,source_node_id,reference_text,target_kind,target_title_or_identifier,
               target_part_or_document,evidence_quote,extracted_confidence,target_node_id,
               target_node_type,target_title,resolver_method,resolver_confidence,
               already_had_edge,added_edge_id,metadata_json
        FROM llm_reference_resolution
        WHERE coalesce(target_node_id,'')<>''
          AND coalesce(added_edge_id,'')<>''
          AND coalesce(already_had_edge,0)=0
          AND extracted_confidence>=?
          AND resolver_confidence>=?
        ORDER BY source_node_id,id
        """,
        (min_extracted, min_resolver),
    )
    for row in rows:
        source = nodes.get(row["source_node_id"])
        target = nodes.get(row["target_node_id"])
        if source is None:
            continue
        text = source_texts[source["id"]]
        # The old LLM schema stores a broad evidence sentence as well as the
        # extracted reference.  Use the shortest exact reference phrase for
        # the materialised span; retain the broad evidence only as provenance.
        quote, spans, broad_quote = llm_quote_and_spans(
            text,
            row["reference_text"] or "",
            row["target_title_or_identifier"] or "",
            row["evidence_quote"] or "",
        )
        candidate = None
        if table_exists(source_conn, "edge"):
            pass
        if not spans:
            insert_stage(
                stage,
                run_id=run_id,
                source=source,
                target=target,
                candidate_id="",
                start=None,
                end=None,
                quote=quote,
                candidate_text=row["reference_text"] or quote,
                citation_kind=row["target_kind"] or "",
                method="llm_resolved_proposed",
                confidence=min(float(row["extracted_confidence"] or 0), float(row["resolver_confidence"] or 0)),
                status="held_no_exact_quote",
                reasons=["proposed_llm_finding_has_no_exact_source_substring"],
                evidence={"resolution_id": row["id"], "resolver_method": row["resolver_method"] or "", "evidence_quote": broad_quote or row["evidence_quote"] or ""},
            )
            count += 1
            continue
        # Prefer the span represented by the recall ledger, if one exists.
        start, end = spans[0]
        if existing_relationship(source_conn, source["id"], target["id"] if target else ""):
            status, reasons = "held_duplicate_edge", ["relationship_already_exists"]
        elif target is None:
            status, reasons = "held_target_missing", ["resolved_target_node_is_missing"]
        elif target["id"] == source["id"]:
            status, reasons = "held_self_reference", ["source_and_target_are_identical"]
        elif existing_occurrence_overlap(source_conn, source["id"], start, end):
            status, reasons = "held_duplicate_occurrence", ["source_span_already_has_occurrence"]
        else:
            status, reasons = "eligible", ["llm_thresholds_and_exact_quote_pass"]
        relationship_type, _edge_type = relationship_for_target(target)
        insert_stage(
            stage,
            run_id=run_id,
            source=source,
            target=target,
            candidate_id="",
            start=start,
            end=end,
            quote=quote,
            candidate_text=row["reference_text"] or quote,
            citation_kind=row["target_kind"] or "",
            method="llm_resolved_proposed",
            confidence=min(float(row["extracted_confidence"] or 0), float(row["resolver_confidence"] or 0)),
            status=status,
            reasons=reasons,
            evidence={"resolution_id": row["id"], "resolver_method": row["resolver_method"] or "", "added_edge_id": row["added_edge_id"] or "", "evidence_quote": broad_quote or row["evidence_quote"] or ""},
            relationship_type=relationship_type,
        )
        count += 1
    return count


def stage_deterministic(
    source_conn: sqlite3.Connection,
    ledger: sqlite3.Connection,
    stage: sqlite3.Connection,
    run_id: str,
    nodes: dict[str, sqlite3.Row],
    registry_path: Path,
) -> int:
    registry = InstrumentRegistry.load(registry_path)
    source_texts = {node_id: source_text(row) for node_id, row in nodes.items()}
    # These checks used to issue a full occurrence/edge query for every
    # candidate.  The corpus-wide tail contains tens of thousands of
    # candidates, so cache the immutable source relationships once per run.
    existing_spans: dict[str, list[tuple[int | None, int | None]]] = defaultdict(list)
    for row in source_conn.execute(
        "SELECT source_node_id,span_start,span_end FROM reference_occurrence"
    ):
        existing_spans[row["source_node_id"]].append((row["span_start"], row["span_end"]))
    existing_edges = {
        (row["from_node_id"], row["to_node_id"])
        for row in source_conn.execute("SELECT from_node_id,to_node_id FROM edge")
    }
    candidate_rows = ledger.execute(
        """
        SELECT candidate_id,source_node_id,source_node_type,source_title,source_url,
               source_text_hash,span_start,span_end,candidate_text,candidate_kind,
               detector_json,reason_json,context_text,status,priority,evidence_json
        FROM reference_gap
        WHERE status IN ('needs_review','tail_unreviewed','llm_unresolved')
          AND candidate_kind IN ('legal_citation','article_citation','structure_reference','named_document','named_instrument','relative_structure')
          AND source_node_type IN ('rule','chapter','part','guidance_document','guidance_section','guidance_paragraph','defined_term')
        ORDER BY priority DESC,candidate_id
        """
    ).fetchall()
    staged = 0
    legal_cache: dict[str, list[Any]] = {}
    target_indexes = build_target_indexes(nodes)
    for candidate in candidate_rows:
        source = nodes.get(candidate["source_node_id"])
        if source is None:
            continue
        value = source_texts[source["id"]]
        start, end = candidate["span_start"], candidate["span_end"]
        quote = value[start:end] if start is not None and end is not None else candidate["candidate_text"]
        # Existing occurrence/edge evidence is checked again against the live
        # DB because the ledger may have been generated before another repair.
        if any(overlap(start, end, old_start, old_end) > 0 for old_start, old_end in existing_spans.get(source["id"], [])):
            continue

        proposals: list[tuple[sqlite3.Row | None, str, float, list[str], dict[str, Any]]] = []
        if candidate["candidate_kind"] in {"legal_citation", "article_citation"}:
            if source["id"] not in legal_cache:
                try:
                    legal_cache[source["id"]] = citation_occurrences(
                        source_node_id=source["id"],
                        value=value,
                        registry=registry,
                        source_title=source["title"] or "",
                    )
                except Exception as exc:
                    legal_cache[source["id"]] = []
                    proposals.append((None, "deterministic_legal", 0.0, [f"legal_detector_error:{type(exc).__name__}"], {}))
            occurrences = legal_cache.get(source["id"], [])
            for occurrence in occurrences:
                if overlap(start, end, occurrence.span_start, occurrence.span_end) == 0:
                    continue
                if not occurrence.instrument or not occurrence.provision_path:
                    continue
                target_id = external_provision_node_id(occurrence.instrument, occurrence.provision_path)
                target = nodes.get(target_id)
                proposals.append((target, "deterministic_legal", float(occurrence.confidence or 0), ["legal_registry_resolution"], {"occurrence_id": occurrence.metadata.get("occurrence_id"), "instrument_id": occurrence.instrument.instrument_id, "provision_path": occurrence.provision_path}))
        else:
            targets, method, confidence = candidate_targets(source, candidate, nodes, target_indexes)
            if len(targets) == 1:
                proposals.append((targets[0], f"deterministic_{method}", confidence, ["unique_local_target"], {"candidate_kind": candidate["candidate_kind"], "candidate_text": candidate["candidate_text"]}))
            elif len(targets) > 1:
                proposals.append((None, f"deterministic_{method}", 0.0, ["multiple_local_targets"], {"target_ids": [row["id"] for row in targets]}))

        if not proposals:
            continue
        for target, method, confidence, reasons, evidence in proposals:
            if target is None:
                insert_stage(stage, run_id=run_id, source=source, target=None, candidate_id=candidate["candidate_id"], start=start, end=end, quote=quote, candidate_text=candidate["candidate_text"], citation_kind=candidate["candidate_kind"], method=method, confidence=confidence, status="held_unresolved", reasons=reasons, evidence=evidence)
                staged += 1
                continue
            if target["id"] == source["id"]:
                status, hold_reasons = "held_self_reference", ["source_and_target_are_identical"]
            elif (source["id"], target["id"]) in existing_edges:
                status, hold_reasons = "held_duplicate_edge", ["relationship_already_exists"]
            elif confidence < AUTO_MIN_CONFIDENCE:
                status, hold_reasons = "held_low_confidence", ["detector_confidence_below_stage_threshold"]
            else:
                status, hold_reasons = "eligible", reasons
            relationship_type, _edge_type = relationship_for_target(target)
            insert_stage(stage, run_id=run_id, source=source, target=target, candidate_id=candidate["candidate_id"], start=start, end=end, quote=quote, candidate_text=candidate["candidate_text"], citation_kind=candidate["candidate_kind"], method=method, confidence=confidence, status=status, reasons=hold_reasons, evidence=evidence, relationship_type=relationship_type)
            staged += 1
    return staged


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_conn = connect_source(args.db)
    ledger = connect(args.ledger)
    stage = connect_stage(args.stage)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + digest(now(), "sha1")[:8]
    stage.execute("INSERT INTO stage_run(run_id,stage_version,source_db,ledger_db,started_at) VALUES (?,?,?,?,?)", (run_id, STAGE_VERSION, str(args.db), str(args.ledger), now()))
    stage.execute("DELETE FROM staged_repair")
    nodes = source_rows(source_conn)
    source_texts = {node_id: source_text(row) for node_id, row in nodes.items()}
    llm_count = stage_llm(source_conn, stage, run_id, nodes, source_texts, args.min_extracted_confidence, args.min_resolver_confidence)
    deterministic_count = stage_deterministic(source_conn, ledger, stage, run_id, nodes, args.instrument_registry)
    stage.commit()
    summary = {
        "run_id": run_id,
        "stage_version": STAGE_VERSION,
        "llm_proposals_considered": llm_count,
        "deterministic_candidates_considered": deterministic_count,
        "staged_rows": stage.execute("SELECT COUNT(*) FROM staged_repair").fetchone()[0],
        "status_counts": {row["status"]: row["n"] for row in stage.execute("SELECT status,COUNT(*) n FROM staged_repair GROUP BY status")},
        "method_counts": {row["proposal_method"]: row["n"] for row in stage.execute("SELECT proposal_method,COUNT(*) n FROM staged_repair GROUP BY proposal_method")},
        "eligible_rows": stage.execute("SELECT COUNT(*) FROM staged_repair WHERE status='eligible'").fetchone()[0],
        "stage": str(args.stage),
    }
    stage.execute("UPDATE stage_run SET finished_at=?,summary_json=? WHERE run_id=?", (now(), json.dumps(summary, ensure_ascii=False), run_id))
    stage.commit()
    source_conn.close()
    ledger.close()
    stage.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--instrument-registry", type=Path, default=DEFAULT_INSTRUMENT_REGISTRY)
    parser.add_argument("--min-extracted-confidence", type=float, default=LLM_MIN_EXTRACTED)
    parser.add_argument("--min-resolver-confidence", type=float, default=LLM_MIN_RESOLVER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(run(args), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect
