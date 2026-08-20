"""Shared helpers for turning extracted citations into occurrence rows.

Graph edges describe a source/target relationship.  A reference occurrence is
the separate lexical fact that a citation appears at one exact source span.
Policy extraction is allowed to produce one row for a distinct citation, so
this module expands that row back to every matching parsed span before the
row reaches the database.
"""

from __future__ import annotations

import re
from typing import Any

from .legal_references import InstrumentRegistry, LegalCitationOccurrence, citation_occurrences


_KIND_ALIASES = {
    "articles": "article",
    "arts": "article",
    "art": "article",
    "sections": "section",
    "sec": "section",
    "regs": "regulation",
    "regulations": "regulation",
    "reg": "regulation",
    "paragraphs": "paragraph",
    "paras": "paragraph",
    "points": "point",
    "subparagraphs": "subparagraph",
    "rules": "rule",
    "schedule paragraph": "schedule_paragraph",
    "schedule part": "schedule_part",
}

_IDENTIFIER_RE = re.compile(
    r"\b(?:articles?|arts?\.?|sections?|s\.?|regulations?|regs?\.?|"
    r"paragraphs?|paras?\.?|points?|subparagraphs?|rules?)\s*"
    r"(?P<identifier>[0-9][0-9A-Za-z]*(?:\s*\.\s*[0-9A-Za-z-]+)?"
    r"(?:\s*\([^)]*\))*)",
    re.I,
)


def _kind(value: Any) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    return _KIND_ALIASES.get(value, value.replace(" ", "_"))


def _identifier_key(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _policy_identifier_keys(row: dict[str, Any] | Any) -> set[str]:
    values = (
        row.get("target_title_or_identifier", "")
        if isinstance(row, dict)
        else row["target_title_or_identifier"]
    ), (
        row.get("reference_text", "")
        if isinstance(row, dict)
        else row["reference_text"]
    )
    keys: set[str] = set()
    for value in values:
        for match in _IDENTIFIER_RE.finditer(str(value or "")):
            keys.add(_identifier_key(match.group("identifier")))
    return keys


def _policy_instrument_ids(
    row: dict[str, Any] | Any,
    registry: InstrumentRegistry,
) -> set[str]:
    values = (
        row.get("target_part_or_document", "")
        if isinstance(row, dict)
        else row["target_part_or_document"]
    ), (
        row.get("reference_text", "")
        if isinstance(row, dict)
        else row["reference_text"]
    )
    ids: set[str] = set()
    for value in values:
        for _position, instrument, _alias in registry.match_aliases(str(value or ""), strict=True):
            ids.add(instrument.instrument_id)
    return ids


def policy_citation_occurrences(
    *,
    source_node_id: str,
    source_text: str,
    source_title: str,
    row: dict[str, Any] | Any,
    registry: InstrumentRegistry,
    parsed: list[LegalCitationOccurrence] | None = None,
) -> list[LegalCitationOccurrence]:
    """Expand one policy citation row into all matching parsed spans.

    The match is deliberately based on legal kind and provision identifier,
    rather than the complete evidence quote.  The latter often contains the
    following list marker, such as ``; (ii)``, and is not the lexical citation
    itself.  Instrument ids are used when the policy row names one explicitly.
    """

    identifiers = _policy_identifier_keys(row)
    if not identifiers:
        return []
    expected_kind = _kind(
        row.get("target_kind", "") if isinstance(row, dict) else row["target_kind"]
    )
    expected_instruments = _policy_instrument_ids(row, registry)
    if parsed is None:
        parsed = citation_occurrences(
            source_node_id=source_node_id,
            value=source_text,
            registry=registry,
            source_title=source_title,
        )
    matches = [
        occurrence
        for occurrence in parsed
        if _kind(occurrence.kind) == expected_kind
        and _identifier_key(occurrence.target.display) in identifiers
        and (
            not expected_instruments
            or occurrence.instrument is None
            or occurrence.instrument.instrument_id in expected_instruments
        )
    ]
    return sorted(
        {occurrence.metadata["occurrence_id"]: occurrence for occurrence in matches}.values(),
        key=lambda occurrence: (occurrence.span_start, occurrence.span_end),
    )


__all__ = ["policy_citation_occurrences"]
