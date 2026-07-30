#!/usr/bin/env python3
"""Materialise instrument-aware legal references and exact citation occurrences."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db import configure_connection, ensure_indexes
from backend.rulebook_scraper.article_references import (
    extract_article_citations,
    is_non_reference_article_use,
)
from backend.rulebook_scraper.legal_references import (
    DEFAULT_INSTRUMENT_REGISTRY,
    Instrument,
    InstrumentRegistry,
    LegalCitationOccurrence,
    citation_occurrences,
    external_provision_node_id,
    fetch_official_provision,
    normalize_alias,
    provision_path_for,
)
from backend.rulebook_scraper.store import SCHEMA as LEGACY_SCHEMA


DEFAULT_DB = PROJECT_ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_AUDIT = PROJECT_ROOT / "outputs" / "legal-reference-audit.json"
DEFAULT_CACHE = PROJECT_ROOT / "backend" / "data" / "raw" / "legal-provisions"
DEFAULT_OVERRIDES = PROJECT_ROOT / "config" / "legal_reference_overrides.json"
SOURCE_NODE_TYPES = ("rule", "guidance_paragraph", "defined_term")
GENERATED_METHOD = "legal_reference_occurrence_v1"
RAW_FSMA_INSTRUMENT_RE = (
    r"(?:FSMA(?:\s*(?:2000|2023))?|"
    r"Financial\s+Services\s+and\s+Markets\s+Act(?:\s+(?:2000|2023))?)"
)
RAW_FSMA_REFERENCES_RE = (
    r"\d+[A-Za-z]*(?:\s*\([^)]*\))*"
    r"(?:\s*(?:,|and|or|to|[-–—])\s*"
    r"(?:(?:sections?|s{1,2}\.?)\s+)?"
    r"(?:\d+[A-Za-z]*(?:\s*\([^)]*\))*|(?:\s*\([^)]*\))+))*"
)
RAW_EXPLICIT_FSMA_RE = re.compile(
    rf"\b(?:sections?|s{{1,2}}\.?)\s*{RAW_FSMA_REFERENCES_RE}"
    rf"(?:\s+\d{{1,2}})?\s+(?:of\s+)?(?:the\s+)?"
    rf"(?P<instrument>{RAW_FSMA_INSTRUMENT_RE})(?:\d{{1,2}})?\b"
    rf"|\b{RAW_FSMA_REFERENCES_RE}\s+of\s+(?:the\s+)?"
    rf"(?P<bare_instrument>{RAW_FSMA_INSTRUMENT_RE})\b",
    re.I,
)
RAW_EXPLICIT_FSMA_STRUCTURE_RE = re.compile(
    rf"\b(?:"
    rf"(?:sub-?)?paragraphs?|sections?"
    rf")\s+{RAW_FSMA_REFERENCES_RE}\s+of\s+Schedule\s+[0-9A-Za-z]+"
    rf"\s+(?:to|of)\s*,?\s*(?:the\s+)?(?P<schedule_instrument>{RAW_FSMA_INSTRUMENT_RE})\b"
    rf"|\bParts?\s+[0-9A-Za-z]+(?:\s*\([^)]*\))?"
    rf"\s+of\s+Schedule\s+[0-9A-Za-z]+\s+(?:to|of)\s*,?\s*"
    rf"(?:the\s+)?(?P<schedule_part_instrument>{RAW_FSMA_INSTRUMENT_RE})\b"
    rf"|\bSchedules?\s+[0-9A-Za-z]+\s+(?:to|of)\s*,?\s*"
    rf"(?:the\s+)?(?P<schedule_only_instrument>{RAW_FSMA_INSTRUMENT_RE})\b"
    rf"|\bParts?\s+[0-9A-Za-z]+\s*,?\s*chapters?\s+[0-9A-Za-z]+"
    rf"\s+of\s+(?:the\s+)?(?P<chapter_instrument>{RAW_FSMA_INSTRUMENT_RE})\b"
    rf"|\bParts?\s+[0-9A-Za-z]+\s+of\s+(?:the\s+)?"
    rf"(?P<part_instrument>{RAW_FSMA_INSTRUMENT_RE})\b",
    re.I,
)
ARTICLE16_REPLACEMENTS = [
    {
        "category": "private companies",
        "citation": "Companies Act 2006 sections 485A to 485C and 494ZA",
        "url": "https://www.legislation.gov.uk/ukpga/2006/46/section/485A",
    },
    {
        "category": "public companies",
        "citation": "Companies Act 2006 sections 489A to 489C and 494ZA",
        "url": "https://www.legislation.gov.uk/ukpga/2006/46/section/489A",
    },
    {
        "category": "building societies",
        "citation": "Building Societies Act 1986 Schedule 11 paragraphs 3B to 3E",
        "url": "https://www.legislation.gov.uk/ukpga/1986/53/schedule/11",
    },
    {
        "category": "friendly societies",
        "citation": "Friendly Societies Act 1992 Schedule 14A paragraphs 2 to 5",
        "url": "https://www.legislation.gov.uk/ukpga/1992/40/schedule/14A",
    },
    {
        "category": "limited liability partnerships",
        "citation": "Companies Act 2006 provisions as applied by regulations 36 and 38A of the 2008 LLP Regulations",
        "url": "https://www.legislation.gov.uk/uksi/2008/1911/regulation/36",
    },
    {
        "category": "specified insurance undertakings",
        "citation": "Companies Act 2006 provisions as applied by regulation 6(1A) of the Insurance Accounts Directive Regulations 2008",
        "url": "https://www.legislation.gov.uk/uksi/2008/565/regulation/6",
    },
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    configure_connection(conn)
    conn.executescript(LEGACY_SCHEMA)
    return conn


def compact(value: str) -> str:
    return " ".join((value or "").split())


def parse_metadata(value: str) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def apply_context_override(
    occurrence: LegalCitationOccurrence,
    *,
    source: sqlite3.Row,
    registry: InstrumentRegistry,
    rules: list[dict[str, Any]],
) -> LegalCitationOccurrence:
    for rule in rules:
        source_ids = set(rule.get("source_node_ids") or ())
        if source_ids and source["id"] not in source_ids:
            continue
        title_prefix = str(rule.get("source_title_prefix") or "")
        if title_prefix and not (source["title"] or "").startswith(title_prefix):
            continue
        kinds = set(rule.get("citation_kinds") or ())
        if kinds and occurrence.kind not in kinds:
            continue
        bases = {str(value).casefold() for value in rule.get("citation_bases") or ()}
        if bases and occurrence.target.base.casefold() not in bases:
            continue
        metadata = {
            **occurrence.metadata,
            "resolution_override": {
                "reason": rule["reason"],
                "instrument_id": rule.get("instrument_id", ""),
                "target_node_id": rule.get("target_node_id", ""),
                "internal_part": rule.get("internal_part", ""),
            },
        }
        instrument_id = str(rule.get("instrument_id") or "")
        if instrument_id:
            instrument = registry.by_id[instrument_id]
            return replace(
                occurrence,
                instrument=instrument,
                instrument_evidence=f"context override: {rule['reason']}",
                provision_path=str(rule.get("provision_path") or "") or provision_path_for(
                    type(
                        "Group",
                        (),
                        {
                            "kind": occurrence.kind,
                            "schedule": occurrence.metadata.get("schedule", ""),
                        },
                    )(),
                    occurrence.target,
                    instrument,
                ),
                status="resolved",
                confidence=max(occurrence.confidence, 0.98),
                metadata=metadata,
            )
        if rule.get("target_node_id"):
            return replace(
                occurrence,
                instrument=None,
                instrument_evidence=f"context override: {rule['reason']}",
                provision_path="",
                status="resolved",
                confidence=max(occurrence.confidence, 0.98),
                metadata=metadata,
            )
        return replace(occurrence, metadata=metadata, confidence=max(occurrence.confidence, 0.98))
    return occurrence


def contextual_section_hints(
    *,
    source: sqlite3.Row,
    rules: list[dict[str, Any]],
) -> dict[str, str]:
    """Return auditable instrument hints for otherwise bare section phrases."""

    hints: dict[str, str] = {}
    for rule in rules:
        source_ids = set(rule.get("source_node_ids") or ())
        if source_ids and source["id"] not in source_ids:
            continue
        title_prefix = str(rule.get("source_title_prefix") or "")
        if title_prefix and not (source["title"] or "").startswith(title_prefix):
            continue
        kinds = set(rule.get("citation_kinds") or ())
        if kinds and "section" not in kinds:
            continue
        instrument_id = str(rule.get("instrument_id") or "")
        if instrument_id not in {"fsma", "fsma-2023"}:
            continue
        bases = {
            str(value).casefold()
            for value in (rule.get("citation_bases") or ("*",))
        }
        for base in bases:
            existing = hints.get(base)
            if existing and existing != instrument_id:
                raise ValueError(
                    f"Conflicting contextual section hints for {source['id']} "
                    f"base {base}: {existing}, {instrument_id}"
                )
            hints[base] = instrument_id
    return hints


def audit_explicit_fsma_coverage(
    *,
    rows: list[sqlite3.Row],
    extracted: list[tuple[sqlite3.Row, LegalCitationOccurrence]],
) -> tuple[int, list[dict[str, Any]]]:
    """Cross-check raw FSMA syntax independently from the citation parser."""

    spans_by_source: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for source, occurrence in extracted:
        if (
            occurrence.instrument is None
            or occurrence.instrument.instrument_id not in {"fsma", "fsma-2023"}
        ):
            continue
        group_span = occurrence.metadata.get("group_span") or {}
        spans_by_source[source["id"]].append(
            (
                int(group_span.get("start", occurrence.span_start)),
                int(group_span.get("end", occurrence.span_end)),
                occurrence.instrument.instrument_id,
            )
        )

    groups_seen = 0
    gaps: list[dict[str, Any]] = []
    for source in rows:
        text = source["text"] or ""
        matches = [
            *RAW_EXPLICIT_FSMA_RE.finditer(text),
            *RAW_EXPLICIT_FSMA_STRUCTURE_RE.finditer(text),
        ]
        for match in sorted(matches, key=lambda item: (item.start(), item.end())):
            matched_groups = match.groupdict()
            if (
                matched_groups.get("bare_instrument")
                and re.search(
                    r"\b(?:Part|Schedule|Chapter)\s*$",
                    text[max(0, match.start() - 24) : match.start()],
                    re.I,
                )
            ):
                continue
            groups_seen += 1
            instrument_text = (
                matched_groups.get("instrument")
                or matched_groups.get("bare_instrument")
                or next(
                    (
                        value
                        for name, value in matched_groups.items()
                        if name.endswith("_instrument") and value
                    ),
                    "",
                )
                or ""
            )
            expected_id = (
                "fsma-2023"
                if re.search(r"\b2023\b", instrument_text)
                else "fsma"
            )
            if any(
                start < match.end()
                and end > match.start()
                and instrument_id == expected_id
                for start, end, instrument_id in spans_by_source.get(
                    source["id"],
                    (),
                )
            ):
                continue
            gaps.append(
                {
                    "source_node_id": source["id"],
                    "source_title": source["title"],
                    "citation": compact(match.group(0)),
                    "expected_instrument_id": expected_id,
                    "context": compact(
                        text[
                            max(0, match.start() - 140) :
                            min(len(text), match.end() + 180)
                        ]
                    ),
                }
            )
    return groups_seen, gaps


def edge_id(source_id: str, target_id: str) -> str:
    return hashlib.sha1(
        f"{source_id}|{target_id}|references|{GENERATED_METHOD}".encode("utf-8")
    ).hexdigest()[:20]


def occurrence_id(occurrence: LegalCitationOccurrence) -> str:
    return str(occurrence.metadata["occurrence_id"])


def has_direct_instrument_context(
    source_text: str,
    occurrence: LegalCitationOccurrence,
    registry: InstrumentRegistry,
) -> bool:
    """True when the instrument is lexically attached to this exact citation."""

    if occurrence.instrument is None:
        return False
    group_span = occurrence.metadata.get("group_span") or {}
    start = int(group_span.get("start", occurrence.span_start))
    end = int(group_span.get("end", occurrence.span_end))
    before = (source_text or "")[max(0, start - 180) : start]
    after = (source_text or "")[end : min(len(source_text or ""), end + 240)]
    after_clause = re.split(r"[.;\n]", after, maxsplit=1)[0]
    instrument_id = occurrence.instrument.instrument_id

    for _, instrument, evidence in registry.match_aliases(after_clause):
        evidence_position = normalize_alias(after_clause).find(normalize_alias(evidence))
        if instrument.instrument_id == instrument_id and 0 <= evidence_position <= 5:
            return True
        if (
            instrument.instrument_id == instrument_id
            and re.match(r"\s*(?:of|under)\b", after, re.I)
            and 0 <= evidence_position <= 24
        ):
            return True

    before_normalized = normalize_alias(before)
    for alias in (occurrence.instrument.title, *occurrence.instrument.aliases):
        alias_normalized = normalize_alias(alias)
        if alias_normalized and before_normalized.endswith(alias_normalized):
            return True

    evidence = occurrence.instrument_evidence.split(":", 1)[-1]
    evidence_normalized = normalize_alias(evidence)
    if evidence_normalized:
        after_normalized = normalize_alias(after_clause)
        if (
            evidence_normalized in after_normalized
            and (
                after_normalized.startswith(evidence_normalized)
                or re.match(r"\s*(?:of|under)\b", after, re.I)
            )
        ):
            return True
        if before_normalized.endswith(evidence_normalized):
            return True
    return False


def cache_fetcher(cache_root: Path, instrument: Instrument, provision_path: str):
    def fetch(url: str) -> bytes:
        url_digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        cache_path = cache_root / instrument.instrument_id / (
            f"{provision_path.replace('/', '__')}__{url_digest}.source"
        )
        if cache_path.exists():
            return cache_path.read_bytes()
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                from backend.rulebook_scraper.legal_references import _fetch_bytes

                payload = _fetch_bytes(url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(payload)
                return payload
            except (HTTPError, URLError, TimeoutError) as error:
                last_error = error
                if isinstance(error, HTTPError) and error.code == 404:
                    break
                if attempt < 3:
                    time.sleep(1.0 * (2**attempt))
        assert last_error is not None
        raise last_error

    return fetch


def load_existing_article_targets(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Map existing source/Article pairs to substantive graph targets."""

    targets: dict[tuple[str, str], tuple[str, str]] = {}
    for row in conn.execute(
        """
        SELECT e.from_node_id,e.to_node_id,e.id,e.metadata_json,
               e.evidence_text,n.id AS target_id,n.title AS target_title,n.text
        FROM edge e JOIN node n ON n.id=e.to_node_id
        WHERE e.edge_type='references' AND COALESCE(n.text,'')<>''
          AND e.source_method<>?
        """,
        (GENERATED_METHOD,),
    ):
        metadata = parse_metadata(row["metadata_json"])
        article = compact(str(metadata.get("article") or "")).casefold()
        if not article:
            reference = compact(str(metadata.get("reference") or ""))
            match = extract_article_citations(reference)
            if match and match[0].tokens:
                article = match[0].tokens[0].base
        if not article:
            target_match = re.search(
                r"(?:^|:)article[:\s-]*(\d{1,3}[A-Za-z]{0,3})\b",
                f"{row['target_id']} {row['target_title'] or ''}",
                re.I,
            )
            if target_match:
                article = target_match.group(1).casefold()
        if not article:
            evidence_matches = extract_article_citations(row["evidence_text"] or "")
            if len(evidence_matches) == 1 and evidence_matches[0].tokens:
                article = evidence_matches[0].tokens[0].base.casefold()
        if article:
            targets.setdefault(
                (row["from_node_id"], article),
                (row["to_node_id"], row["id"]),
            )
    return targets


def normalized_part(value: str) -> str:
    return " ".join(
        "".join(character if character.isalnum() else " " for character in (value or "").casefold()).split()
    )


def load_article_node_indexes(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Index substantive UK CRR and same-Part Article nodes by base number."""

    uk_crr: dict[str, tuple[int, str]] = {}
    internal: dict[tuple[str, str], tuple[int, str]] = {}
    pattern = re.compile(
        r"^(?:UK\s+CRR\s+)?Article\s+(\d{1,3}[A-Za-z]{0,3})\b",
        re.I,
    )
    for row in conn.execute(
        """
        SELECT id,node_type,title,text,url,metadata_json
        FROM node
        WHERE node_type IN ('chapter','rule','external_reference')
          AND COALESCE(text,'')<>''
          AND (title LIKE 'Article %' OR title LIKE 'UK CRR Article %')
        """
    ):
        match = pattern.match(row["title"] or "")
        if not match:
            continue
        article = match.group(1).casefold()
        metadata = parse_metadata(row["metadata_json"])
        part = compact(str(metadata.get("part_title") or ""))
        score = (
            0
            if row["id"] == f"external:uk-crr:article:{article}"
            else 1
            if row["node_type"] == "chapter"
            else 2
        )
        if (
            row["id"].startswith("external:uk-crr:")
            or "(crr)" in part.casefold()
            or "-crr/" in (row["url"] or "").casefold()
        ):
            existing = uk_crr.get(article)
            if existing is None or score < existing[0]:
                uk_crr[article] = (score, row["id"])
        if part:
            key = (normalized_part(part), article)
            existing = internal.get(key)
            if existing is None or score < existing[0]:
                internal[key] = (score, row["id"])
    return (
        {article: node_id for article, (_, node_id) in uk_crr.items()},
        {key: node_id for key, (_, node_id) in internal.items()},
    )


def source_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in SOURCE_NODE_TYPES)
    return conn.execute(
        f"""
        SELECT id,node_type,title,text,url,metadata_json
        FROM node
        WHERE node_type IN ({placeholders}) AND COALESCE(text,'')<>''
        ORDER BY id
        """,
        SOURCE_NODE_TYPES,
    ).fetchall()


def materialize_target(
    conn: sqlite3.Connection,
    *,
    instrument: Instrument,
    provision_path: str,
    official,
) -> str:
    node_id = external_provision_node_id(instrument, provision_path)
    metadata = {
        "instrument_id": instrument.instrument_id,
        "instrument_title": instrument.title,
        "legislation_type": instrument.legislation_type,
        "legislation_year": instrument.year,
        "legislation_number": instrument.number,
        "provision_path": provision_path,
        "official_source_url": official.url,
        "official_data_url": official.data_url,
        "official_document_title": official.document_title,
        "content_hash": official.content_hash,
        "retrieved_at": official.retrieved_at,
        "evidence_status": "official_source_text",
        "extraction_run_id": GENERATED_METHOD,
    }
    if (
        instrument.instrument_id == "statutory-audit-regulation"
        and provision_path in {"article/16", "article/16/8"}
    ):
        metadata["applicability_note"] = (
            "The PRA Rulebook definition of Statutory Audit Regulation provides "
            "category-specific UK enactment substitutions for Article 16."
        )
        metadata["related_provisions"] = ARTICLE16_REPLACEMENTS
    conn.execute(
        """
        INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          node_type=excluded.node_type,
          stable_key=excluded.stable_key,
          title=excluded.title,
          text=excluded.text,
          url=excluded.url,
          metadata_json=excluded.metadata_json
        """,
        (
            node_id,
            "external_reference",
            node_id,
            official.title,
            official.text,
            official.url,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    return node_id


def materialize_edge(
    conn: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    target_id: str,
    occurrence: LegalCitationOccurrence,
) -> str:
    identifier = edge_id(source["id"], target_id)
    metadata = {
        "reference": occurrence.citation_text,
        "reference_group": occurrence.group_text,
        "instrument_id": occurrence.instrument.instrument_id,
        "instrument_title": occurrence.instrument.title,
        "provision_path": occurrence.provision_path,
        "scope": "external_legal_provision",
        "official_source_url": occurrence.instrument.provision_url(
            occurrence.provision_path
        ),
        "source_span": {
            "start": occurrence.span_start,
            "end": occurrence.span_end,
        },
        "group_id": occurrence.group_id,
        "evidence_status": "direct_text",
        "extraction_run_id": GENERATED_METHOD,
    }
    conn.execute(
        """
        INSERT INTO edge(
          id,from_node_id,to_node_id,edge_type,source_method,confidence,
          evidence_text,source_url,metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          confidence=excluded.confidence,
          evidence_text=excluded.evidence_text,
          source_url=excluded.source_url,
          metadata_json=excluded.metadata_json
        """,
        (
            identifier,
            source["id"],
            target_id,
            "references",
            GENERATED_METHOD,
            occurrence.confidence,
            compact(
                (source["text"] or "")[
                    max(0, occurrence.span_start - 180) : occurrence.span_end + 220
                ]
            ),
            source["url"] or "",
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    return identifier


def materialize_existing_target_edge(
    conn: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    target_id: str,
    occurrence: LegalCitationOccurrence,
) -> str:
    identifier = edge_id(source["id"], target_id)
    metadata = {
        "reference": occurrence.citation_text,
        "reference_group": occurrence.group_text,
        "instrument_id": occurrence.instrument.instrument_id
        if occurrence.instrument
        else "",
        "provision_path": occurrence.provision_path,
        "source_span": {
            "start": occurrence.span_start,
            "end": occurrence.span_end,
        },
        "group_id": occurrence.group_id,
        "scope": "existing_substantive_provision",
        "evidence_status": "direct_text",
        "extraction_run_id": GENERATED_METHOD,
    }
    conn.execute(
        """
        INSERT INTO edge(
          id,from_node_id,to_node_id,edge_type,source_method,confidence,
          evidence_text,source_url,metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          confidence=excluded.confidence,
          evidence_text=excluded.evidence_text,
          source_url=excluded.source_url,
          metadata_json=excluded.metadata_json
        """,
        (
            identifier,
            source["id"],
            target_id,
            "references",
            GENERATED_METHOD,
            max(occurrence.confidence, 0.94),
            compact(
                (source["text"] or "")[
                    max(0, occurrence.span_start - 180) : occurrence.span_end + 220
                ]
            ),
            source["url"] or "",
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    return identifier


def upsert_occurrence(
    conn: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    occurrence: LegalCitationOccurrence,
    status: str,
    target_id: str = "",
    target_edge_id: str = "",
    issue: str = "",
) -> None:
    metadata = {
        **occurrence.metadata,
        "instrument_evidence": occurrence.instrument_evidence,
        "issue": issue,
    }
    conn.execute(
        """
        INSERT INTO reference_occurrence(
          occurrence_id,group_id,source_node_id,target_node_id,edge_id,
          relationship_type,citation_kind,citation_text,group_text,instrument_id,
          provision_path,qualifier,span_start,span_end,status,source_method,
          confidence,context_text,metadata_json,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(occurrence_id) DO UPDATE SET
          group_id=excluded.group_id,
          source_node_id=excluded.source_node_id,
          target_node_id=excluded.target_node_id,
          edge_id=excluded.edge_id,
          relationship_type=excluded.relationship_type,
          citation_kind=excluded.citation_kind,
          citation_text=excluded.citation_text,
          group_text=excluded.group_text,
          instrument_id=excluded.instrument_id,
          provision_path=excluded.provision_path,
          qualifier=excluded.qualifier,
          span_start=excluded.span_start,
          span_end=excluded.span_end,
          status=excluded.status,
          source_method=excluded.source_method,
          confidence=excluded.confidence,
          context_text=excluded.context_text,
          metadata_json=excluded.metadata_json,
          updated_at=CURRENT_TIMESTAMP
        """,
        (
            occurrence_id(occurrence),
            occurrence.group_id,
            source["id"],
            target_id or None,
            target_edge_id or None,
            "REF",
            occurrence.kind,
            occurrence.citation_text,
            occurrence.group_text,
            occurrence.instrument.instrument_id if occurrence.instrument else None,
            occurrence.provision_path or None,
            "".join(f"({part})" for part in occurrence.target.qualifiers),
            occurrence.span_start,
            occurrence.span_end,
            status,
            GENERATED_METHOD,
            occurrence.confidence,
            compact(
                (source["text"] or "")[
                    max(0, occurrence.span_start - 180) : occurrence.span_end + 220
                ]
            ),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )


def audit_and_apply(
    conn: sqlite3.Connection,
    *,
    registry_path: Path,
    overrides_path: Path,
    cache_root: Path,
    apply: bool,
    fetch_workers: int,
) -> dict[str, Any]:
    registry = InstrumentRegistry.load(registry_path)
    override_rules = json.loads(overrides_path.read_text(encoding="utf-8"))["rules"]
    rows = source_rows(conn)
    existing_article_targets = load_existing_article_targets(conn)
    uk_crr_nodes, internal_article_nodes = load_article_node_indexes(conn)
    extracted: list[tuple[sqlite3.Row, LegalCitationOccurrence]] = []
    non_references: list[tuple[sqlite3.Row, LegalCitationOccurrence]] = []
    for source in rows:
        text = source["text"] or ""
        lexical_non_references = {
            (citation.start, citation.end)
            for citation in extract_article_citations(text)
            if is_non_reference_article_use(text, citation)
        }
        for occurrence in citation_occurrences(
            source_node_id=source["id"],
            value=text,
            registry=registry,
            source_title=source["title"] or "",
            contextual_instrument_hints=contextual_section_hints(
                source=source,
                rules=override_rules,
            ),
        ):
            occurrence = apply_context_override(
                occurrence,
                source=source,
                registry=registry,
                rules=override_rules,
            )
            if any(
                occurrence.span_start >= start and occurrence.span_end <= end
                for start, end in lexical_non_references
            ):
                non_references.append((source, occurrence))
            else:
                extracted.append((source, occurrence))

    raw_fsma_groups, fsma_coverage_gaps = audit_explicit_fsma_coverage(
        rows=rows,
        extracted=extracted,
    )

    # Existing UK CRR/internal Article targets remain authoritative; this pass
    # adds occurrence rows so repeated citations no longer collapse in the UI.
    resolved_existing: dict[str, tuple[str, str]] = {}
    official_candidates: dict[
        tuple[str, str], tuple[Instrument, str]
    ] = {}
    unresolved: list[tuple[sqlite3.Row, LegalCitationOccurrence, str]] = []
    for source, occurrence in extracted:
        resolution_override = occurrence.metadata.get("resolution_override") or {}
        target_override = str(resolution_override.get("target_node_id") or "")
        if target_override:
            resolved_existing[occurrence_id(occurrence)] = (target_override, "")
            continue
        article_key = (source["id"], occurrence.target.base.casefold())
        existing_article = (
            existing_article_targets.get(article_key)
            if occurrence.kind == "article"
            else None
        )
        if (
            occurrence.instrument
            and existing_article
            and not resolution_override.get("instrument_id")
            and not has_direct_instrument_context(
                source["text"] or "",
                occurrence,
                registry,
            )
        ):
            resolved_existing[occurrence_id(occurrence)] = existing_article
            continue
        if occurrence.instrument and occurrence.instrument.instrument_id == "uk-crr":
            existing = existing_article_targets.get(article_key)
            if not existing and occurrence.target.base.casefold() in uk_crr_nodes:
                existing = (uk_crr_nodes[occurrence.target.base.casefold()], "")
            if existing:
                resolved_existing[occurrence_id(occurrence)] = existing
            else:
                official_candidates[
                    (occurrence.instrument.instrument_id, occurrence.provision_path)
                ] = (occurrence.instrument, occurrence.provision_path)
            continue
        if occurrence.instrument:
            official_candidates[
                (occurrence.instrument.instrument_id, occurrence.provision_path)
            ] = (occurrence.instrument, occurrence.provision_path)
            continue
        existing = (
            existing_article_targets.get(article_key)
            if occurrence.kind == "article"
            else None
        )
        if not existing and occurrence.kind == "article":
            source_metadata = parse_metadata(source["metadata_json"])
            part_key = normalized_part(str(source_metadata.get("part_title") or ""))
            internal_target = internal_article_nodes.get(
                (part_key, occurrence.target.base.casefold())
            )
            if internal_target:
                existing = (internal_target, "")
            if not existing:
                override_part = normalized_part(
                    str(resolution_override.get("internal_part") or "")
                )
                internal_target = internal_article_nodes.get(
                    (override_part, occurrence.target.base.casefold())
                )
                if internal_target:
                    existing = (internal_target, "")
            elif (
                "(crr)" in str(source_metadata.get("part_title") or "").casefold()
                or "-crr/" in (source["url"] or "").casefold()
            ):
                uk_crr_target = uk_crr_nodes.get(occurrence.target.base.casefold())
                if uk_crr_target:
                    existing = (uk_crr_target, "")
        if existing and occurrence.kind == "article":
            resolved_existing[occurrence_id(occurrence)] = existing
        else:
            unresolved.append(
                (
                    source,
                    occurrence,
                    "No canonical instrument or existing internal Article target",
                )
            )

    official_results: dict[tuple[str, str], Any] = {}
    official_errors: dict[tuple[str, str], str] = {}
    if apply:
        with ThreadPoolExecutor(max_workers=max(1, fetch_workers)) as executor:
            futures = {
                executor.submit(
                    fetch_official_provision,
                    instrument,
                    provision_path,
                    fetcher=cache_fetcher(cache_root, instrument, provision_path),
                ): key
                for key, (instrument, provision_path) in official_candidates.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    official_results[key] = future.result()
                except Exception as error:  # recorded precisely in the audit
                    official_errors[key] = f"{type(error).__name__}: {error}"

    fetch_failures = [
        (
            source,
            occurrence,
            official_errors[
                (occurrence.instrument.instrument_id, occurrence.provision_path)
            ],
        )
        for source, occurrence in extracted
        if occurrence.instrument
        and occurrence_id(occurrence) not in resolved_existing
        and (
            occurrence.instrument.instrument_id,
            occurrence.provision_path,
        ) in official_errors
    ]
    all_unresolved = [*unresolved, *fetch_failures]
    # Never replace a previously valid materialisation with a partial run.  All
    # resolution and official-source fetch gates must pass before the generated
    # edge/occurrence layer is touched.
    apply_succeeded = apply and not all_unresolved and not fsma_coverage_gaps
    materialized = 0
    occurrence_rows = []
    if apply_succeeded:
        conn.execute(
            "DELETE FROM edge WHERE source_method=?",
            (GENERATED_METHOD,),
        )
        conn.execute(
            "DELETE FROM reference_occurrence WHERE source_method=?",
            (GENERATED_METHOD,),
        )
        for source, occurrence in non_references:
            upsert_occurrence(
                conn,
                source=source,
                occurrence=occurrence,
                status="not_reference",
                issue="Lexicalised Article term",
            )
        for source, occurrence in extracted:
            occ_id = occurrence_id(occurrence)
            existing = resolved_existing.get(occ_id)
            if existing:
                target_id, existing_edge_id = existing
                if not existing_edge_id:
                    existing_edge_id = materialize_existing_target_edge(
                        conn,
                        source=source,
                        target_id=target_id,
                        occurrence=occurrence,
                    )
                upsert_occurrence(
                    conn,
                    source=source,
                    occurrence=occurrence,
                    status="materialized",
                    target_id=target_id,
                    target_edge_id=existing_edge_id,
                )
                materialized += 1
                continue
            if not occurrence.instrument:
                upsert_occurrence(
                    conn,
                    source=source,
                    occurrence=occurrence,
                    status=occurrence.status
                    if occurrence.status in {"ambiguous", "unresolved"}
                    else "unresolved",
                    issue="No canonical instrument or existing internal Article target",
                )
                continue
            key = (occurrence.instrument.instrument_id, occurrence.provision_path)
            official = official_results.get(key)
            if not official:
                upsert_occurrence(
                    conn,
                    source=source,
                    occurrence=occurrence,
                    status="unresolved",
                    issue=official_errors.get(key, "Official source was not fetched in dry-run mode"),
                )
                continue
            target_id = materialize_target(
                conn,
                instrument=occurrence.instrument,
                provision_path=occurrence.provision_path,
                official=official,
            )
            target_edge_id = materialize_edge(
                conn,
                source=source,
                target_id=target_id,
                occurrence=occurrence,
            )
            upsert_occurrence(
                conn,
                source=source,
                occurrence=occurrence,
                status="materialized",
                target_id=target_id,
                target_edge_id=target_edge_id,
            )
            materialized += 1
        conn.commit()
        ensure_indexes(conn)

    status_counts = Counter()
    if apply_succeeded:
        status_counts.update(
            dict(
                conn.execute(
                    """
                    SELECT status,COUNT(*) FROM reference_occurrence
                    WHERE source_method=? GROUP BY status
                    """,
                    (GENERATED_METHOD,),
                ).fetchall()
            )
        )
    else:
        status_counts["not_reference"] = len(non_references)
        status_counts["materializable_official"] = (
            len(extracted) - len(resolved_existing) - len(all_unresolved)
        )
        status_counts["materializable_existing"] = len(resolved_existing)
        status_counts["unresolved"] = len(all_unresolved)

    unresolved_rows = []
    if apply_succeeded:
        for row in conn.execute(
            """
            SELECT occurrence_id,source_node_id,citation_text,group_text,
                   instrument_id,provision_path,status,context_text,metadata_json
            FROM reference_occurrence
            WHERE source_method=? AND status IN ('unresolved','ambiguous')
            ORDER BY source_node_id,span_start
            """,
            (GENERATED_METHOD,),
        ):
            item = dict(row)
            item["metadata"] = parse_metadata(item.pop("metadata_json"))
            unresolved_rows.append(item)
    else:
        unresolved_rows = [
            {
                "source_node_id": source["id"],
                "source_title": source["title"],
                "citation": occurrence.citation_text,
                "group": occurrence.group_text,
                "instrument_id": occurrence.instrument.instrument_id
                if occurrence.instrument
                else "",
                "provision_path": occurrence.provision_path,
                "issue": issue,
            }
            for source, occurrence, issue in all_unresolved
        ]

    instrument_counts = Counter(
        occurrence.instrument.instrument_id
        for _, occurrence in extracted
        if occurrence.instrument
    )
    fsma_text_gaps: list[dict[str, Any]] = []
    if apply_succeeded:
        fsma_text_gaps = [
            dict(row)
            for row in conn.execute(
                """
                SELECT ro.occurrence_id,ro.source_node_id,ro.instrument_id,
                       ro.provision_path,ro.target_node_id,
                       COALESCE(n.title,'') AS target_title,
                       COALESCE(n.url,'') AS target_url,
                       LENGTH(TRIM(COALESCE(n.text,''))) AS text_length
                FROM reference_occurrence ro
                LEFT JOIN node n ON n.id=ro.target_node_id
                WHERE ro.source_method=?
                  AND ro.instrument_id IN ('fsma','fsma-2023')
                  AND (
                    ro.target_node_id IS NULL
                    OR LENGTH(TRIM(COALESCE(n.text,'')))=0
                    OR LENGTH(TRIM(COALESCE(n.url,'')))=0
                  )
                ORDER BY ro.source_node_id,ro.span_start
                """,
                (GENERATED_METHOD,),
            )
        ]
    summary = {
        "apply_requested": apply,
        "applied": apply_succeeded,
        "source_nodes_scanned": len(rows),
        "citation_occurrences": len(extracted) + len(non_references),
        "genuine_citation_occurrences": len(extracted),
        "not_reference_occurrences": len(non_references),
        "unique_official_targets": len(official_candidates),
        "official_targets_fetched": len(official_results),
        "official_target_fetch_errors": len(official_errors),
        "materialized_occurrences": materialized if apply_succeeded else None,
        "status_counts": dict(sorted(status_counts.items())),
        "unresolved_genuine_references": (
            status_counts["unresolved"] + status_counts["ambiguous"]
            if apply_succeeded
            else len(all_unresolved) + len(fsma_coverage_gaps)
        ),
        "raw_explicit_fsma_groups": raw_fsma_groups,
        "uncovered_explicit_fsma_groups": len(fsma_coverage_gaps),
        "fsma_occurrences": instrument_counts["fsma"],
        "fsma_2023_occurrences": instrument_counts["fsma-2023"],
        "fsma_occurrences_missing_source_text": len(fsma_text_gaps)
        if apply_succeeded
        else None,
        "by_instrument": dict(sorted(instrument_counts.items())),
    }
    return {
        "summary": summary,
        "official_fetch_errors": [
            {
                "instrument_id": instrument_id,
                "provision_path": provision_path,
                "error": error,
            }
            for (instrument_id, provision_path), error in sorted(official_errors.items())
        ],
        "unresolved": unresolved_rows,
        "fsma_coverage_gaps": fsma_coverage_gaps,
        "fsma_source_text_gaps": fsma_text_gaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--instrument-registry",
        type=Path,
        default=DEFAULT_INSTRUMENT_REGISTRY,
    )
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--fetch-workers", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = connect(args.db)
    result = audit_and_apply(
        conn,
        registry_path=args.instrument_registry,
        overrides_path=args.overrides,
        cache_root=args.cache,
        apply=args.apply,
        fetch_workers=args.fetch_workers,
    )
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    return 2 if (
        result["summary"]["unresolved_genuine_references"]
        or result["summary"]["uncovered_explicit_fsma_groups"]
        or result["summary"]["fsma_occurrences_missing_source_text"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
