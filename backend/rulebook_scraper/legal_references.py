"""Instrument-aware extraction and official-source loading for legal citations.

The graph edge is intentionally not the unit of extraction.  A source provision
can cite the same target more than once, and coordinated ranges can cite several
targets through one lexical span.  ``LegalCitationOccurrence`` therefore keeps
the exact source span and group identity independently from any eventual edge.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .article_references import compact, extract_article_citations


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INSTRUMENT_REGISTRY = PROJECT_ROOT / "config" / "legal_instruments.json"
LEGISLATION_NS = "http://www.legislation.gov.uk/namespaces/legislation"
DC_NS = "http://purl.org/dc/elements/1.1/"

EU_REGULATION_RE = re.compile(
    r"(?P<label>(?:Commission\s+)?(?:(?:Delegated|Implementing)\s+)?Regulation)"
    r"\s*\(?(?:EU|EC)\)?\s*(?:No\.?\s*)?"
    r"(?P<first>\d{1,4})\s*/\s*(?P<second>\d{2,4})",
    re.I,
)
EU_DIRECTIVE_RE = re.compile(
    r"(?:Directive\s*(?:\((?:EU|EC)\))?\s*)?"
    r"(?P<year>\d{4})\s*/\s*(?P<number>\d+)"
    r"(?:\s*/\s*(?:EU|EC))?",
    re.I,
)
EU_SUFFIX_REGULATION_RE = re.compile(
    r"\b(?P<number>\d{1,4})\s*/\s*(?P<year>\d{4})\s*/\s*(?:EU|EC)\b",
    re.I,
)
SI_NUMBER_RE = re.compile(
    r"\bS\.?\s*I\.?\s*(?P<year>\d{4})\s*/\s*(?P<number>\d+)\b",
    re.I,
)
ORDER_NUMBER_RE = re.compile(
    r"\b(?P<year>\d{4})\s*/\s*(?P<number>\d{3,4})\b"
)
SCHEDULE_REFERENCE_RE = re.compile(
    r"\b(?P<prefix>paragraphs?)\s+"
    r"(?P<references>"
    r"\d+(?:\s*\([^)]*\))*"
    r"(?:\s*(?:,|and|or|to|[-–—])\s*"
    r"\d+(?:\s*\([^)]*\))*)*"
    r")\s+of\s+Schedule\s+(?P<schedule>[0-9A-Za-z]+)\s+to\s+"
    r"(?P<instrument>[^.;\]\n]{3,220}?"
    r"(?:Act|Regulations?|Order)\s+\d{4}"
    r"(?:\s*\((?:S\.?\s*I\.?\s*)?\d{4}/\d+\))?)",
    re.I,
)
GENERIC_PROVISION_RE = re.compile(
    r"\b(?P<prefix>sections?|regulations?)\s+"
    r"(?P<references>"
    r"\d+[A-Za-z]*(?:\s*\([^)]*\))*"
    r"(?:\s*(?:,|and|or|to|[-–—])\s*"
    r"\d+[A-Za-z]*(?:\s*\([^)]*\))*)*"
    r")\s+(?:of|under)\s+(?:the\s+)?"
    r"(?P<instrument>[^.;\]\n]{3,220}?"
    r"(?:Act|Regulations?|Order)(?:\s+\d{4})?"
    r"(?:\s*\((?:S\.?\s*I\.?\s*)?\d{4}/\d+\))?)",
    re.I,
)
FSMA_INSTRUMENT_PATTERN = (
    r"(?:"
    r"FSMA(?:\s*(?:2000|2023))?(?=\W|$)"
    r"|FSMA(?=\d{1,2}\b)"
    r"|Financial\s+Services\s+and\s+Markets\s+Act(?:\s+(?:2000|2023))?(?=\W|$)"
    r")"
)
FSMA_NUMBER_REFERENCE_PATTERN = (
    r"\d+[A-Za-z]*(?:\s*\([^)]*\))*"
    r"(?:\s*(?:,|and|or|to|[-–—])\s*"
    r"(?:(?:sections?|s{1,2}\.?)\s+)?"
    r"(?:\d+[A-Za-z]*(?:\s*\([^)]*\))*|(?:\s*\([^)]*\))+))*"
)
FSMA_SECTION_RE = re.compile(
    rf"\b(?P<prefix>sections?|s{{1,2}}\.?)\s*"
    rf"(?P<references>{FSMA_NUMBER_REFERENCE_PATTERN})"
    rf"(?:\s+\d{{1,2}})?"
    rf"\s+(?:of\s+)?(?:the\s+)?"
    rf"(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
FSMA_BARE_NUMBER_RE = re.compile(
    rf"\b(?P<references>{FSMA_NUMBER_REFERENCE_PATTERN})"
    rf"(?:\s+\d{{1,2}})?"
    rf"\s+of\s+(?:the\s+)?"
    rf"(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
FSMA_SCHEDULE_PROVISION_RE = re.compile(
    rf"\b(?P<prefix>(?:sub-?)?paragraphs?|sections?)\s+"
    rf"(?P<references>{FSMA_NUMBER_REFERENCE_PATTERN})"
    r"\s+of\s+Schedule\s+(?P<schedule>[0-9A-Za-z]+)\s+"
    rf"(?:to|of)\s*,?\s*(?:the\s+)?(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
FSMA_SCHEDULE_PART_RE = re.compile(
    r"\b(?P<prefix>Parts?)\s+"
    r"(?P<references>[0-9A-Za-z]+)"
    r"(?:\s*\([^)]*\))?"
    r"\s+of\s+Schedule\s+(?P<schedule>[0-9A-Za-z]+)\s+"
    rf"(?:to|of)\s*,?\s*(?:the\s+)?(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
FSMA_SCHEDULE_RE = re.compile(
    r"\b(?P<prefix>Schedules?)\s+"
    r"(?P<references>[0-9A-Za-z]+)"
    rf"\s+(?:to|of)\s*,?\s*(?:the\s+)?(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
FSMA_PART_CHAPTER_RE = re.compile(
    r"\b(?P<prefix>Parts?)\s+"
    r"(?P<part>[0-9A-Za-z]+)\s*,?\s*"
    r"chapters?\s+(?P<references>[0-9A-Za-z]+)"
    rf"\s+of\s+(?:the\s+)?(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
FSMA_PART_RE = re.compile(
    r"\b(?P<prefix>Parts?)\s+"
    r"(?P<references>[0-9A-Za-z]+)"
    rf"\s+of\s+(?:the\s+)?(?P<instrument>{FSMA_INSTRUMENT_PATTERN})",
    re.I,
)
BARE_SECTION_RE = re.compile(
    rf"\b(?P<prefix>sections?\s+|ss\.?\s+|s\.?\s*)"
    rf"(?P<references>{FSMA_NUMBER_REFERENCE_PATTERN})",
    re.I,
)
INTERNAL_SECTION_TAIL_RE = re.compile(
    r"^\s+(?:(?:of|under|in)\s+)?"
    r"(?:this|that|the|these|those)\s+"
    r"(?:Part|Chapter|Section|rule|rules|paragraph|document|statement)\b"
    r"|^\s+(?:above|below)\b",
    re.I,
)
NUMBER_TOKEN_RE = re.compile(
    r"(?P<base>\d+[A-Za-z]*)(?P<qualifiers>(?:\s*\([^)]*\))*)",
    re.I,
)
QUALIFIER_TOKEN_RE = re.compile(r"(?P<qualifiers>(?:\s*\([^)]*\))+)", re.I)
SECTION_PREFIX_RE = re.compile(r"(?:sections?|s{1,2}\.?)\s*", re.I)
SEPARATOR_RE = re.compile(r"\s*(?P<separator>,|and|or|to|[-–—])\s*", re.I)


def normalize_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def qualifier_parts(value: str) -> tuple[str, ...]:
    dotted = re.match(r"\s*\.\s*([0-9][0-9A-Za-z-]*)", value or "", re.I)
    parts = [
        compact(part)
        for part in re.findall(r"\(([^)]*)\)", value or "")
    ]
    if dotted:
        parts.insert(0, compact(dotted.group(1)))
    return tuple(
        part
        for part in parts
        if re.fullmatch(r"(?:\d+[A-Za-z]*|[A-Za-z]|[ivxlcdm]{2,})", part, re.I)
        and part.casefold() != "part"
    )


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    title: str
    legislation_type: str
    year: int
    number: int
    aliases: tuple[str, ...] = ()
    official_url: str = ""
    data_url: str = ""
    source_format: str = ""
    provision_sources: dict[str, dict[str, str]] = field(
        default_factory=dict,
        compare=False,
    )

    @property
    def base_url(self) -> str:
        return (
            f"https://www.legislation.gov.uk/"
            f"{self.legislation_type}/{self.year}/{self.number}"
        )

    def provision_url(self, provision_path: str) -> str:
        mapped = self.provision_sources.get(provision_path, {})
        if mapped.get("official_url"):
            return mapped["official_url"]
        if self.official_url:
            return self.official_url
        return f"{self.base_url}/{provision_path}"


@dataclass(frozen=True)
class CitationTarget:
    base: str
    qualifiers: tuple[str, ...]
    span_start: int
    span_end: int
    synthetic: bool = False

    @property
    def display(self) -> str:
        return self.base + "".join(f"({part})" for part in self.qualifiers)


@dataclass
class LegalCitationGroup:
    kind: str
    prefix: str
    text: str
    start: int
    end: int
    targets: list[CitationTarget]
    instrument_text: str = ""
    schedule: str = ""
    part: str = ""
    separators: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InstrumentResolution:
    instrument: Instrument | None
    evidence: str
    status: str


@dataclass(frozen=True)
class LegalCitationOccurrence:
    group_id: str
    kind: str
    citation_text: str
    group_text: str
    span_start: int
    span_end: int
    target: CitationTarget
    instrument: Instrument | None
    instrument_evidence: str
    provision_path: str
    status: str
    confidence: float
    metadata: dict


@dataclass(frozen=True)
class OfficialProvision:
    title: str
    text: str
    url: str
    data_url: str
    content_hash: str
    retrieved_at: str
    document_title: str


class InstrumentRegistry:
    def __init__(self, instruments: Iterable[Instrument]):
        self.instruments = tuple(instruments)
        self.by_id = {instrument.instrument_id: instrument for instrument in self.instruments}
        self._aliases: list[tuple[str, Instrument, str]] = []
        for instrument in self.instruments:
            for alias in (instrument.title, *instrument.aliases):
                normalized = normalize_alias(alias)
                if normalized:
                    self._aliases.append((normalized, instrument, alias))
        self._aliases.sort(key=lambda item: len(item[0]), reverse=True)

    @classmethod
    def load(cls, path: Path = DEFAULT_INSTRUMENT_REGISTRY) -> "InstrumentRegistry":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            Instrument(
                instrument_id=item["id"],
                title=item["title"],
                legislation_type=item["legislation_type"],
                year=int(item["year"]),
                number=int(item["number"]),
                aliases=tuple(item.get("aliases") or ()),
                official_url=str(item.get("official_url") or ""),
                data_url=str(item.get("data_url") or ""),
                source_format=str(item.get("source_format") or ""),
                provision_sources={
                    str(path): {
                        str(key): str(value)
                        for key, value in details.items()
                    }
                    for path, details in (item.get("provision_sources") or {}).items()
                },
            )
            for item in payload["instruments"]
        )

    def match_aliases(self, value: str) -> list[tuple[int, Instrument, str]]:
        normalized = normalize_alias(value)
        matches: list[tuple[int, Instrument, str]] = []
        for alias, instrument, original in self._aliases:
            match = re.search(rf"(?:^|\s){re.escape(alias)}(?:$|\s)", normalized)
            if match:
                matches.append((match.start(), instrument, original))
        return sorted(matches, key=lambda item: (item[0], -len(item[2])))

    def from_legislation_identity(
        self,
        *,
        legislation_type: str,
        year: int,
        number: int,
        title: str,
    ) -> Instrument:
        for instrument in self.instruments:
            if (
                instrument.legislation_type == legislation_type
                and instrument.year == year
                and instrument.number == number
            ):
                return instrument
        return Instrument(
            instrument_id=f"{legislation_type}-{year}-{number}",
            title=compact(title) or f"{legislation_type.upper()} {year}/{number}",
            legislation_type=legislation_type,
            year=year,
            number=number,
            aliases=(compact(title),) if compact(title) else (),
        )


def extract_legal_citation_groups(value: str) -> list[LegalCitationGroup]:
    """Extract Article, Schedule-paragraph, section and regulation groups."""

    text = value or ""
    groups: list[LegalCitationGroup] = []
    occupied: list[tuple[int, int]] = []

    for match in SCHEDULE_REFERENCE_RE.finditer(text):
        targets, separators = _parse_number_targets(
            match.group("references"),
            offset=match.start("references"),
        )
        groups.append(
            LegalCitationGroup(
                kind="schedule_paragraph",
                prefix=match.group("prefix"),
                text=compact(match.group(0)),
                start=match.start(),
                end=match.end(),
                targets=targets,
                instrument_text=compact(match.group("instrument")),
                schedule=compact(match.group("schedule")),
                separators=separators,
            )
        )
        occupied.append((match.start(), match.end()))

    for matcher, kind in (
        (FSMA_SCHEDULE_PROVISION_RE, "schedule_paragraph"),
        (FSMA_SCHEDULE_PART_RE, "schedule_part"),
        (FSMA_SCHEDULE_RE, "schedule"),
        (FSMA_PART_CHAPTER_RE, "part_chapter"),
        (FSMA_PART_RE, "part"),
    ):
        for match in matcher.finditer(text):
            if _overlaps(match.start(), match.end(), occupied):
                continue
            targets, separators = (
                _parse_structural_targets(
                    match.group("references"),
                    offset=match.start("references"),
                )
                if kind in {"part", "schedule_part", "schedule"}
                else _parse_number_targets(
                    match.group("references"),
                    offset=match.start("references"),
                )
            )
            if not targets:
                continue
            groups.append(
                LegalCitationGroup(
                    kind=kind,
                    prefix=match.group("prefix"),
                    text=compact(match.group(0)),
                    start=match.start(),
                    end=match.end(),
                    targets=targets,
                    instrument_text=compact(match.group("instrument")),
                    schedule=compact(match.groupdict().get("schedule") or ""),
                    part=compact(match.groupdict().get("part") or ""),
                    separators=separators,
                )
            )
            occupied.append((match.start(), match.end()))

    for matcher, synthetic_prefix in (
        (FSMA_SECTION_RE, False),
        (FSMA_BARE_NUMBER_RE, True),
    ):
        for match in matcher.finditer(text):
            if _overlaps(match.start(), match.end(), occupied):
                continue
            if synthetic_prefix and re.search(
                r"\b(?:Part|Schedule|Chapter)\s*$",
                text[max(0, match.start() - 24) : match.start()],
                re.I,
            ):
                continue
            targets, separators = _parse_number_targets(
                match.group("references"),
                offset=match.start("references"),
            )
            if not targets:
                continue
            targets = _normalize_fsma_ocr_targets(targets)
            prefix = "section" if synthetic_prefix else match.group("prefix")
            groups.append(
                LegalCitationGroup(
                    kind="section",
                    prefix=prefix,
                    text=compact(match.group(0)),
                    start=match.start(),
                    end=match.end(),
                    targets=targets,
                    instrument_text=compact(match.group("instrument")),
                    separators=separators,
                )
            )
            occupied.append((match.start(), match.end()))

    for match in GENERIC_PROVISION_RE.finditer(text):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        targets, separators = _parse_number_targets(
            match.group("references"),
            offset=match.start("references"),
        )
        prefix = match.group("prefix")
        groups.append(
            LegalCitationGroup(
                kind="section" if prefix.casefold().startswith("section") else "regulation",
                prefix=prefix,
                text=compact(match.group(0)),
                start=match.start(),
                end=match.end(),
                targets=targets,
                instrument_text=compact(match.group("instrument")),
                separators=separators,
            )
        )
        occupied.append((match.start(), match.end()))

    for citation in extract_article_citations(text):
        if _overlaps(citation.start, citation.end, occupied):
            continue
        targets = [
            CitationTarget(
                base=token.base,
                qualifiers=qualifier_parts(token.paragraphs),
                span_start=token.start,
                span_end=token.end,
            )
            for token in citation.tokens
        ]
        targets = _expand_target_ranges(targets, list(citation.separators))
        groups.append(
            LegalCitationGroup(
                kind="article",
                prefix=citation.prefix,
                text=citation.text,
                start=citation.start,
                end=citation.end,
                targets=targets,
                separators=list(citation.separators),
            )
        )
    return sorted(groups, key=lambda group: (group.start, group.end))


def resolve_group_instrument(
    value: str,
    group: LegalCitationGroup,
    registry: InstrumentRegistry,
    *,
    source_title: str = "",
) -> InstrumentResolution:
    """Resolve the named instrument without defaulting external law to UK CRR."""

    text = value or ""
    if group.instrument_text:
        direct = _resolve_instrument_text(group.instrument_text, registry)
        if direct.instrument:
            return direct

    after = text[group.end : min(len(text), group.end + 320)]
    # Definitions and guidance paragraphs often introduce the governing
    # instrument once, several thousand characters before later bare
    # citations. Search the complete atomic source, while still preferring the
    # nearest preceding declaration.
    before = text[: group.start]
    shorthand = normalize_alias(group.instrument_text)
    near_before = before[max(0, len(before) - 120) :]

    # Some drafting omits “of”, for example “article 14(2) Credit Unions
    # (Northern Ireland) Order 1985”. A title beginning immediately after the
    # citation is nevertheless direct evidence and must beat an earlier Act.
    leading_after = after[:220]
    leading_matches = registry.match_aliases(leading_after)
    leading_normalized = normalize_alias(leading_after)
    for _, instrument, evidence in leading_matches:
        evidence_normalized = normalize_alias(evidence)
        if (
            leading_normalized.startswith(evidence_normalized)
            or leading_normalized.startswith(f"the {evidence_normalized}")
        ):
            return InstrumentResolution(
                instrument,
                f"following citation: {evidence}",
                "resolved",
            )

    default_zones = (
        (near_before, "preceding citation"),
        (before, "preceding citation"),
        (after, "following citation"),
    )
    # “Article X of [instrument]” is stronger than a nearby instrument mention
    # before the citation. Conversely, “CRD Article X” is stronger than a later
    # unrelated citation in the same sentence.
    zones = (
        ((after, "following citation"), *default_zones)
        if re.match(r"\s*(?:of|under)\b", after, re.I)
        and shorthand not in {"act", "the act", "that act", "same act", "1986 act"}
        else default_zones
    )
    for zone, label in zones:
        resolved = _nearest_instrument(
            zone,
            registry,
            prefer_last=label.startswith("preceding"),
        )
        if resolved.instrument:
            return InstrumentResolution(
                resolved.instrument,
                f"{label}: {resolved.evidence}",
                "resolved",
            )

    # Bare citations in a compact definition or guidance paragraph often rely
    # on one instrument declaration elsewhere in the same atomic source text.
    source_matches = _all_instruments(f"{source_title}\n{text}", registry)
    unique = {instrument.instrument_id: (instrument, evidence) for _, instrument, evidence in source_matches}
    if len(unique) == 1:
        instrument, evidence = next(iter(unique.values()))
        return InstrumentResolution(
            instrument,
            f"sole instrument in source: {evidence}",
            "resolved",
        )
    if shorthand in {"act", "the act", "that act", "same act", "1986 act"}:
        act_matches = {
            instrument.instrument_id: (instrument, evidence)
            for _, instrument, evidence in source_matches
            if instrument.legislation_type in {"ukpga", "asp", "anaw"}
        }
        if len(act_matches) == 1:
            instrument, evidence = next(iter(act_matches.values()))
            return InstrumentResolution(
                instrument,
                f"sole Act in source: {evidence}",
                "resolved",
            )
    if shorthand in {"order", "the order", "that order", "same order"}:
        order_matches = {
            instrument.instrument_id: (instrument, evidence)
            for _, instrument, evidence in source_matches
            if instrument.legislation_type in {"uksi", "nisi"}
            and "order" in instrument.title.casefold()
        }
        if len(order_matches) == 1:
            instrument, evidence = next(iter(order_matches.values()))
            return InstrumentResolution(
                instrument,
                f"sole Order in source: {evidence}",
                "resolved",
            )
    if len(unique) > 1:
        return InstrumentResolution(
            None,
            ", ".join(sorted(unique)),
            "ambiguous",
        )
    return InstrumentResolution(None, "", "unresolved")


def citation_occurrences(
    *,
    source_node_id: str,
    value: str,
    registry: InstrumentRegistry,
    source_title: str = "",
    contextual_instrument_hints: dict[str, str] | None = None,
) -> list[LegalCitationOccurrence]:
    occurrences: list[LegalCitationOccurrence] = []
    groups = extract_legal_citation_groups(value)
    groups.extend(
        _contextual_fsma_section_groups(
            value,
            groups,
            registry,
            source_title=source_title,
            instrument_hints=contextual_instrument_hints or {},
        )
    )
    for group in sorted(groups, key=lambda item: (item.start, item.end)):
        resolution = resolve_group_instrument(
            value,
            group,
            registry,
            source_title=source_title,
        )
        group_id = hashlib.sha1(
            f"{source_node_id}|{group.start}|{group.end}|{group.text}".encode("utf-8")
        ).hexdigest()[:24]
        for index, target in enumerate(group.targets):
            provision_path = (
                provision_path_for(group, target, resolution.instrument)
                if resolution.instrument
                else ""
            )
            occurrence_id = hashlib.sha1(
                f"{group_id}|{index}|{target.display}|{provision_path}".encode("utf-8")
            ).hexdigest()[:24]
            occurrences.append(
                LegalCitationOccurrence(
                    group_id=group_id,
                    kind=group.kind,
                    citation_text=_citation_text(group, target),
                    group_text=group.text,
                    span_start=target.span_start,
                    span_end=target.span_end,
                    target=target,
                    instrument=resolution.instrument,
                    instrument_evidence=resolution.evidence,
                    provision_path=provision_path,
                    status=resolution.status,
                    confidence=0.99 if group.instrument_text else 0.96 if resolution.instrument else 0.0,
                    metadata={
                        "occurrence_id": occurrence_id,
                        "synthetic_range_member": target.synthetic,
                        "group_span": {"start": group.start, "end": group.end},
                        "schedule": group.schedule,
                        "part": group.part,
                    },
                )
            )
    return occurrences


def _citation_text(
    group: LegalCitationGroup,
    target: CitationTarget,
) -> str:
    if group.kind == "part_chapter":
        return f"Part {group.part}, Chapter {target.display}"
    if group.kind == "schedule_part":
        return f"Part {target.display} of Schedule {group.schedule}"
    if group.kind == "schedule":
        return f"Schedule {target.display}"
    return compact(f"{group.prefix} {target.display}")


def _contextual_fsma_section_groups(
    value: str,
    existing_groups: list[LegalCitationGroup],
    registry: InstrumentRegistry,
    *,
    source_title: str = "",
    instrument_hints: dict[str, str] | None = None,
) -> list[LegalCitationGroup]:
    """Recover bare section citations only when their context resolves to FSMA.

    A global bare ``section 3`` grammar would turn Rulebook structure labels
    into external-law links.  This second pass is deliberately constrained:
    the candidate must not be an internal structural reference, must not
    overlap a stronger citation, and must resolve to one of the two registered
    Financial Services and Markets Acts from the surrounding atomic source.
    """

    text = value or ""
    hints = instrument_hints or {}
    occupied = [(group.start, group.end) for group in existing_groups]
    explicit_instruments_by_base: dict[str, set[str]] = {}
    for group in existing_groups:
        resolution = resolve_group_instrument(
            text,
            group,
            registry,
            source_title=source_title,
        )
        if (
            resolution.instrument is None
            or resolution.instrument.instrument_id not in {"fsma", "fsma-2023"}
        ):
            continue
        for target in group.targets:
            explicit_instruments_by_base.setdefault(target.base.casefold(), set()).add(
                resolution.instrument.instrument_id
            )
    recovered: list[LegalCitationGroup] = []
    for match in BARE_SECTION_RE.finditer(text):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        tail = text[match.end() : min(len(text), match.end() + 80)]
        if INTERNAL_SECTION_TAIL_RE.match(tail):
            continue
        targets, separators = _parse_number_targets(
            match.group("references"),
            offset=match.start("references"),
        )
        if not targets:
            continue
        candidate = LegalCitationGroup(
            kind="section",
            prefix=match.group("prefix"),
            text=compact(match.group(0)),
            start=match.start(),
            end=match.end(),
            targets=targets,
            separators=separators,
        )
        hinted_ids = {
            hints.get(target.base.casefold()) or hints.get("*") or ""
            for target in targets
        } - {""}
        if len(hinted_ids) == 1:
            hinted_id = next(iter(hinted_ids))
            if hinted_id in {"fsma", "fsma-2023"}:
                candidate.instrument_text = registry.by_id[hinted_id].title
        resolution = resolve_group_instrument(
            text,
            candidate,
            registry,
            source_title=source_title,
        )
        local_before = text[max(0, candidate.start - 180) : candidate.start]
        local_after = text[candidate.end : min(len(text), candidate.end + 180)]
        fuzzy_fsma_after = re.search(
            r"\bof\s+(FSMA(?:\s*(?:2000|2023))?)\b",
            local_after,
            re.I,
        )
        if fuzzy_fsma_after and fuzzy_fsma_after.start() <= 100:
            fuzzy_resolution = _resolve_instrument_text(
                fuzzy_fsma_after.group(1),
                registry,
            )
            if fuzzy_resolution.instrument:
                candidate.instrument_text = fuzzy_resolution.instrument.title
                resolution = InstrumentResolution(
                    fuzzy_resolution.instrument,
                    f"table-layout following instrument: {fuzzy_fsma_after.group(1)}",
                    "resolved",
                )
        local_matches: list[tuple[int, Instrument, str]] = []
        normalized_before_length = len(normalize_alias(local_before))
        for position, instrument, evidence in registry.match_aliases(local_before):
            local_matches.append(
                (
                    normalized_before_length
                    - position
                    - len(normalize_alias(evidence)),
                    instrument,
                    evidence,
                )
            )
        for position, instrument, evidence in registry.match_aliases(local_after):
            local_matches.append((position, instrument, evidence))
        if local_matches:
            distance, local_instrument, evidence = min(
                local_matches,
                key=lambda item: (item[0], -len(item[2])),
            )
            if (
                distance <= 120
                and local_instrument.instrument_id in {"fsma", "fsma-2023"}
            ):
                candidate.instrument_text = local_instrument.title
                resolution = InstrumentResolution(
                    local_instrument,
                    f"nearest local instrument: {evidence}",
                    "resolved",
                )
        inherited_ids = {
            instrument_id
            for target in targets
            for instrument_id in explicit_instruments_by_base.get(
                target.base.casefold(),
                set(),
            )
        }
        if (
            resolution.instrument is None
            or resolution.instrument.instrument_id not in {"fsma", "fsma-2023"}
        ) and len(inherited_ids) == 1:
            instrument_id = next(iter(inherited_ids))
            candidate.instrument_text = registry.by_id[instrument_id].title
            resolution = InstrumentResolution(
                registry.by_id[instrument_id],
                "same section explicitly attributed to FSMA in source",
                "resolved",
            )
        if (
            resolution.instrument is None
            or resolution.instrument.instrument_id not in {"fsma", "fsma-2023"}
        ):
            continue
        recovered.append(candidate)
        occupied.append((candidate.start, candidate.end))
    return recovered


def provision_path_for(
    group: LegalCitationGroup,
    target: CitationTarget,
    instrument: Instrument | None,
) -> str:
    suffix = "/".join((target.base, *target.qualifiers))
    if group.kind == "schedule_paragraph":
        return f"schedule/{group.schedule}/paragraph/{suffix}"
    if group.kind == "schedule_part":
        return f"schedule/{group.schedule}/part/{suffix}"
    if group.kind == "schedule":
        return f"schedule/{suffix}"
    if group.kind == "part":
        part = target.base
        if instrument and instrument.instrument_id == "fsma" and part.isdigit():
            part = _to_roman(int(part))
        return f"part/{part}" + (
            "/" + "/".join(target.qualifiers)
            if target.qualifiers
            else ""
        )
    if group.kind == "part_chapter":
        part = group.part
        if instrument and instrument.instrument_id == "fsma" and part.isdigit():
            part = _to_roman(int(part))
        return f"part/{part}/chapter/{suffix}"
    if group.kind == "section":
        return f"section/{suffix}"
    if group.kind == "regulation":
        return f"regulation/{suffix}"
    if group.kind == "article":
        return f"article/{suffix}"
    raise ValueError(f"unsupported citation kind: {group.kind}")


def external_provision_node_id(instrument: Instrument, provision_path: str) -> str:
    return f"external:legislation:{instrument.instrument_id}:{provision_path.replace('/', ':')}"


def fetch_official_provision(
    instrument: Instrument,
    provision_path: str,
    *,
    fetcher: Callable[[str], bytes] | None = None,
) -> OfficialProvision:
    fetch = fetcher or _fetch_bytes
    if instrument.source_format == "eurlex-cellar-html":
        return _fetch_cellar_annex_provision(
            instrument,
            provision_path,
            fetch=fetch,
        )
    if instrument.source_format == "lloyds-consolidated-pdf":
        return _fetch_lloyds_provision(
            instrument,
            provision_path,
            fetch=fetch,
        )
    if provision_path in instrument.provision_sources:
        return _fetch_mapped_legislation_provision(
            instrument,
            provision_path,
            source=instrument.provision_sources[provision_path],
            fetch=fetch,
        )

    requested_url = instrument.provision_url(provision_path)
    parts = provision_path.split("/")
    minimum_parts = (
        4
        if parts[:1] == ["schedule"]
        and len(parts) >= 3
        and parts[2] in {"paragraph", "part"}
        else 2
    )
    candidates = [
        "/".join(parts[:length])
        for length in range(len(parts), minimum_parts - 1, -1)
    ]
    last_error: Exception | None = None
    root = None
    target = None
    source_url = requested_url
    data_url = f"{requested_url}/data.xml"
    for candidate_path in candidates:
        candidate_url = f"{instrument.base_url}/{candidate_path}"
        candidate_data_url = f"{candidate_url}/data.xml"
        try:
            payload = fetch(candidate_data_url)
            candidate_root = ElementTree.fromstring(payload)
            target_uri = candidate_url.replace("https://", "http://", 1).rstrip("/")
            candidate_target = _find_provision_element(
                candidate_root,
                target_uri=target_uri,
                provision_path=candidate_path,
            )
            if candidate_target is None:
                raise ValueError(f"official XML did not contain {target_uri}")
            root = candidate_root
            target = candidate_target
            source_url = candidate_url
            data_url = candidate_data_url
            break
        except Exception as error:
            last_error = error
    if root is None or target is None:
        # Older Acts can use regnal-year canonical URIs, and newly inserted
        # provisions do not always expose a dedicated /data.xml endpoint.
        # Search the official instrument XML by provision-path suffix.
        try:
            payload = fetch(f"{instrument.base_url}/data.xml")
            candidate_root = ElementTree.fromstring(payload)
            for candidate_path in candidates:
                candidate_target = _find_provision_element(
                    candidate_root,
                    target_uri="",
                    provision_path=candidate_path,
                )
                if candidate_target is not None:
                    root = candidate_root
                    target = candidate_target
                    data_url = f"{instrument.base_url}/data.xml"
                    source_url = (
                        candidate_target.attrib.get("DocumentURI") or requested_url
                    ).replace("http://", "https://", 1)
                    break
        except Exception as error:
            last_error = error
    if root is None or target is None:
        assert last_error is not None
        raise last_error
    rendered = render_legislation_element(target)
    if not rendered:
        raise ValueError(f"official XML provision {target_uri} had no substantive text")
    document_title_node = root.find(f".//{{{DC_NS}}}title")
    document_title = (
        compact(" ".join(document_title_node.itertext()))
        if document_title_node is not None
        else instrument.title
    )
    citation_label = _display_provision_path(provision_path)
    title = f"{instrument.title} — {citation_label}"
    text = f"{title}\n\n{rendered}"
    return OfficialProvision(
        title=title,
        text=text,
        url=requested_url,
        data_url=data_url,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        document_title=document_title,
    )


def _official_provision(
    *,
    instrument: Instrument,
    provision_path: str,
    rendered: str,
    url: str,
    data_url: str,
    document_title: str,
) -> OfficialProvision:
    citation_label = _display_provision_path(provision_path)
    title = f"{instrument.title} — {citation_label}"
    text = f"{title}\n\n{rendered}"
    return OfficialProvision(
        title=title,
        text=text,
        url=url,
        data_url=data_url,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        document_title=document_title,
    )


def _fetch_mapped_legislation_provision(
    instrument: Instrument,
    provision_path: str,
    *,
    source: dict[str, str],
    fetch: Callable[[str], bytes],
) -> OfficialProvision:
    source_path = source.get("provision_path", "")
    official_url = source.get("official_url", "")
    data_url = source.get("data_url", "")
    if not source_path or not official_url or not data_url:
        raise ValueError(
            f"{instrument.instrument_id} {provision_path} has incomplete mapped-source metadata"
        )
    root = ElementTree.fromstring(fetch(data_url))
    target = _find_provision_element(
        root,
        target_uri=official_url.replace("https://", "http://", 1),
        provision_path=source_path,
    )
    if target is None:
        raise ValueError(f"official XML did not contain mapped source {official_url}")
    rendered = render_legislation_element(target)
    if not rendered:
        raise ValueError(f"mapped official XML provision {official_url} had no substantive text")
    document_title_node = root.find(f".//{{{DC_NS}}}title")
    document_title = (
        compact(" ".join(document_title_node.itertext()))
        if document_title_node is not None
        else source.get("document_title", instrument.title)
    )
    return _official_provision(
        instrument=instrument,
        provision_path=provision_path,
        rendered=rendered,
        url=official_url,
        data_url=data_url,
        document_title=document_title,
    )


class _ParagraphHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._paragraph_stack: list[list[str]] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() == "p":
            self._paragraph_stack.append([])

    def handle_data(self, data: str) -> None:
        if self._paragraph_stack:
            self._paragraph_stack[-1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "p" or not self._paragraph_stack:
            return
        value = compact(" ".join(self._paragraph_stack.pop()))
        if value:
            self.paragraphs.append(value)


def _fetch_cellar_annex_provision(
    instrument: Instrument,
    provision_path: str,
    *,
    fetch: Callable[[str], bytes],
) -> OfficialProvision:
    match = re.fullmatch(r"article/(\d+)(?:/(\d+))?", provision_path, re.I)
    if not match:
        raise ValueError(
            f"{instrument.instrument_id} supports Annex Article paths, got {provision_path}"
        )
    if not instrument.data_url or not instrument.official_url:
        raise ValueError(f"{instrument.instrument_id} is missing official source metadata")

    parser = _ParagraphHTMLParser()
    parser.feed(fetch(instrument.data_url).decode("utf-8", errors="replace"))
    paragraphs = parser.paragraphs
    annex_index = max(
        (index for index, value in enumerate(paragraphs) if value.casefold() == "annex"),
        default=-1,
    )
    if annex_index < 0:
        raise ValueError("official Cellar document did not contain the Annex")

    article = match.group(1)
    heading = f"article {article}"
    try:
        start = next(
            index
            for index in range(annex_index + 1, len(paragraphs))
            if paragraphs[index].casefold() == heading
        )
    except StopIteration as error:
        raise ValueError(f"official Cellar Annex did not contain Article {article}") from error
    end = next(
        (
            index
            for index in range(start + 1, len(paragraphs))
            if re.fullmatch(r"article\s+\d+[a-z]?", paragraphs[index], re.I)
        ),
        len(paragraphs),
    )
    body = paragraphs[start + 1 : end]
    paragraph = match.group(2)
    if paragraph:
        prefix = re.compile(rf"^{re.escape(paragraph)}\.\s")
        selected = [value for value in body if prefix.match(value)]
        if not selected:
            raise ValueError(
                f"official Cellar Annex Article {article} did not contain paragraph {paragraph}"
            )
        body = selected
    rendered = "\n".join(body)
    if not rendered:
        raise ValueError(f"official Cellar Annex Article {article} had no substantive text")
    return _official_provision(
        instrument=instrument,
        provision_path=provision_path,
        rendered=rendered,
        url=instrument.official_url,
        data_url=instrument.data_url,
        document_title=(
            "Commission Recommendation 2003/361/EC concerning the definition "
            "of micro, small and medium-sized enterprises"
        ),
    )


def _fetch_lloyds_provision(
    instrument: Instrument,
    provision_path: str,
    *,
    fetch: Callable[[str], bytes],
) -> OfficialProvision:
    match = re.fullmatch(r"section/(\d+)", provision_path, re.I)
    if not match:
        raise ValueError(
            f"{instrument.instrument_id} supports numbered section paths, got {provision_path}"
        )
    if not instrument.data_url or not instrument.official_url:
        raise ValueError(f"{instrument.instrument_id} is missing official source metadata")

    from pypdf import PdfReader

    pdf = PdfReader(BytesIO(fetch(instrument.data_url)))
    document_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    section = int(match.group(1))
    start_match = re.search(
        rf"(?m)^.*?\b{section}\.\s*[—-]",
        document_text,
    )
    if not start_match:
        raise ValueError(f"official Lloyd's PDF did not contain section {section}")
    end_match = re.search(
        rf"(?m)^.*?\b{section + 1}\.\s*[—-]",
        document_text[start_match.end() :],
    )
    end = (
        start_match.end() + end_match.start()
        if end_match
        else len(document_text)
    )
    lines = [
        compact(line)
        for line in document_text[start_match.start() : end].splitlines()
    ]
    rendered = "\n".join(line for line in lines if line and not line.isdigit())
    rendered = rendered.replace("mana gement", "management")
    if not rendered:
        raise ValueError(f"official Lloyd's PDF section {section} had no substantive text")
    return _official_provision(
        instrument=instrument,
        provision_path=provision_path,
        rendered=rendered,
        url=instrument.official_url,
        data_url=instrument.data_url,
        document_title="Lloyd's Act 1982 (as amended through 2008)",
    )


def render_legislation_element(element: ElementTree.Element) -> str:
    block_tags = {
        "P1para", "P2", "P3", "P4", "P5", "ListItem", "Para", "Tabular", "tr",
    }
    chunks: list[str] = []

    def visit(node: ElementTree.Element) -> None:
        local_name = node.tag.rsplit("}", 1)[-1]
        if local_name in {"Commentaries", "Commentary"}:
            return
        if local_name in block_tags:
            chunks.append("\n")
        if node.text:
            chunks.append(f" {node.text} ")
        for child in node:
            visit(child)
            if child.tail:
                chunks.append(f" {child.tail} ")
        if local_name in block_tags:
            chunks.append("\n")

    visit(element)
    lines = [compact(line) for line in "".join(chunks).splitlines()]
    return "\n".join(line for line in lines if line)


def _find_provision_element(
    root: ElementTree.Element,
    *,
    target_uri: str,
    provision_path: str,
) -> ElementTree.Element | None:
    exact = target_uri.rstrip("/")
    suffix = f"/{provision_path}".rstrip("/").casefold()
    candidates: list[tuple[int, ElementTree.Element]] = []
    for element in root.iter():
        uri = (element.attrib.get("DocumentURI") or "").rstrip("/")
        if exact and uri == exact:
            return element
        normalized_uri = re.sub(
            r"/(?:made|enacted|prospective|\d{4}-\d{2}-\d{2})$",
            "",
            uri,
            flags=re.I,
        )
        if normalized_uri.casefold().endswith(suffix):
            candidates.append((len(normalized_uri), element))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _fetch_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "PRA-Rulebook-Explorer/1.0 legal-reference-source-loader",
            "Accept": "application/xml,text/html,application/pdf,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def _resolve_instrument_text(
    value: str,
    registry: InstrumentRegistry,
) -> InstrumentResolution:
    candidates = _all_instruments(value, registry)
    if candidates:
        si_identity = SI_NUMBER_RE.search(value)
        if si_identity:
            year = int(si_identity.group("year"))
            number = int(si_identity.group("number"))
            matching = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate[1].legislation_type == "uksi"
                    and candidate[1].year == year
                    and candidate[1].number == number
                ),
                None,
            )
            if matching:
                _, instrument, evidence = matching
                return InstrumentResolution(instrument, evidence, "resolved")
        explicit_eu = [
            candidate
            for candidate in candidates
            if candidate[2].casefold().startswith(("regulation", "commission", "directive"))
            and any(character.isdigit() for character in candidate[2])
        ]
        if explicit_eu:
            _, instrument, evidence = max(explicit_eu, key=lambda item: len(item[2]))
            return InstrumentResolution(instrument, evidence, "resolved")
        # The instrument phrase begins immediately after “of” or “under”.
        # Prefer its first recognised identity; later instrument names often
        # occur in trailing explanatory text and are not the citation target.
        _, instrument, evidence = min(candidates, key=lambda item: (item[0], -len(item[2])))
        return InstrumentResolution(instrument, evidence, "resolved")
    return InstrumentResolution(None, "", "unresolved")


def _nearest_instrument(
    value: str,
    registry: InstrumentRegistry,
    *,
    prefer_last: bool = False,
) -> InstrumentResolution:
    candidates = _all_instruments(value, registry)
    if not candidates:
        return InstrumentResolution(None, "", "unresolved")
    position, instrument, evidence = candidates[-1] if prefer_last else candidates[0]
    del position
    return InstrumentResolution(instrument, evidence, "resolved")


def _all_instruments(
    value: str,
    registry: InstrumentRegistry,
) -> list[tuple[int, Instrument, str]]:
    candidates: list[tuple[int, Instrument, str]] = []
    for match in EU_REGULATION_RE.finditer(value):
        first = int(match.group("first"))
        second = int(match.group("second"))
        year, number = (first, second) if first >= 1900 else (second, first)
        evidence = compact(match.group(0))
        candidates.append(
            (
                match.start(),
                registry.from_legislation_identity(
                    legislation_type="eur",
                    year=year,
                    number=number,
                    title=evidence,
                ),
                evidence,
            )
        )
    for match in EU_SUFFIX_REGULATION_RE.finditer(value):
        prefix = value[max(0, match.start() - 180) : match.start()]
        if not re.search(r"\bRegulation\b", prefix, re.I):
            continue
        year = int(match.group("year"))
        number = int(match.group("number"))
        evidence = compact(match.group(0))
        candidates.append(
            (
                match.start(),
                registry.from_legislation_identity(
                    legislation_type="eur",
                    year=year,
                    number=number,
                    title=evidence,
                ),
                evidence,
            )
        )
    for match in re.finditer(r"\bDirective\b[^.;\n]{0,50}", value, re.I):
        identity = EU_DIRECTIVE_RE.search(match.group(0))
        if not identity:
            continue
        year = int(identity.group("year"))
        number = int(identity.group("number"))
        evidence = compact(identity.group(0))
        candidates.append(
            (
                match.start() + identity.start(),
                registry.from_legislation_identity(
                    legislation_type="eudr",
                    year=year,
                    number=number,
                    title=evidence,
                ),
                evidence,
            )
        )
    for match in SI_NUMBER_RE.finditer(value):
        year = int(match.group("year"))
        number = int(match.group("number"))
        evidence = compact(match.group(0))
        title_start = max(0, match.start() - 180)
        title = compact(value[title_start : match.end()])
        candidates.append(
            (
                match.start(),
                registry.from_legislation_identity(
                    legislation_type="uksi",
                    year=year,
                    number=number,
                    title=title,
                ),
                evidence,
            )
        )
    for position, instrument, evidence in registry.match_aliases(value):
        candidates.append((position, instrument, evidence))
    deduped: dict[tuple[int, str], tuple[int, Instrument, str]] = {}
    for candidate in candidates:
        key = (candidate[0], candidate[1].instrument_id)
        existing = deduped.get(key)
        if existing is None or len(candidate[2]) > len(existing[2]):
            deduped[key] = candidate
    return sorted(deduped.values(), key=lambda item: (item[0], -len(item[2])))


def _parse_number_targets(
    value: str,
    *,
    offset: int,
) -> tuple[list[CitationTarget], list[str]]:
    targets: list[CitationTarget] = []
    separators: list[str] = []
    cursor = 0
    prefix_match = SECTION_PREFIX_RE.match(value, cursor)
    if prefix_match:
        cursor = prefix_match.end()
    token_match = NUMBER_TOKEN_RE.match(value, cursor)
    if not token_match:
        return targets, separators
    targets.append(_target_from_match(token_match, offset))
    cursor = token_match.end()
    while cursor < len(value):
        separator_match = SEPARATOR_RE.match(value, cursor)
        if not separator_match:
            break
        next_cursor = separator_match.end()
        prefix_match = SECTION_PREFIX_RE.match(value, next_cursor)
        if prefix_match:
            next_cursor = prefix_match.end()
        token_match = NUMBER_TOKEN_RE.match(value, next_cursor)
        if token_match:
            target = _target_from_match(token_match, offset)
        else:
            qualifier_match = QUALIFIER_TOKEN_RE.match(value, next_cursor)
            if not qualifier_match:
                break
            previous = targets[-1]
            continuation = qualifier_parts(qualifier_match.group("qualifiers"))
            if not continuation:
                break
            target = CitationTarget(
                base=previous.base,
                qualifiers=(
                    previous.qualifiers[:-1] + continuation
                    if previous.qualifiers
                    else continuation
                ),
                span_start=offset + qualifier_match.start(),
                span_end=offset + qualifier_match.end(),
            )
            token_match = qualifier_match
        separators.append(compact(separator_match.group("separator")).casefold())
        targets.append(target)
        cursor = token_match.end()
    return _expand_target_ranges(targets, separators), separators


def _parse_structural_targets(
    value: str,
    *,
    offset: int,
) -> tuple[list[CitationTarget], list[str]]:
    match = re.match(r"[0-9A-Za-z]+", value)
    if not match:
        return [], []
    return [
        CitationTarget(
            base=compact(match.group(0)),
            qualifiers=(),
            span_start=offset + match.start(),
            span_end=offset + match.end(),
        )
    ], []


def _to_roman(value: int) -> str:
    if value <= 0 or value > 3999:
        return str(value)
    numerals = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    remainder = value
    output: list[str] = []
    for amount, numeral in numerals:
        while remainder >= amount:
            output.append(numeral)
            remainder -= amount
    return "".join(output)


def _target_from_match(match: re.Match[str], offset: int) -> CitationTarget:
    return CitationTarget(
        base=compact(match.group("base")),
        qualifiers=qualifier_parts(match.group("qualifiers")),
        span_start=offset + match.start(),
        span_end=offset + match.end(),
    )


def _normalize_fsma_ocr_targets(
    targets: list[CitationTarget],
) -> list[CitationTarget]:
    """Split footnote digits fused onto impossible FSMA section numbers."""

    normalized: list[CitationTarget] = []
    for target in targets:
        if target.qualifiers or not target.base.isdigit() or int(target.base) <= 500:
            normalized.append(target)
            continue
        split_base = ""
        for split_at in range(len(target.base) - 1, 0, -1):
            candidate = target.base[:split_at]
            footnote = target.base[split_at:]
            if (
                1 <= int(candidate) <= 500
                and 1 <= len(footnote) <= 2
                and 1 <= int(footnote) <= 99
            ):
                split_base = candidate
                break
        normalized.append(
            CitationTarget(
                base=split_base or target.base,
                qualifiers=target.qualifiers,
                span_start=target.span_start,
                span_end=(
                    target.span_start + len(split_base)
                    if split_base
                    else target.span_end
                ),
                synthetic=target.synthetic,
            )
        )
    return normalized


def _expand_target_ranges(
    targets: list[CitationTarget],
    separators: list[str],
) -> list[CitationTarget]:
    if not targets:
        return []
    expanded = [targets[0]]
    for index, (separator, target) in enumerate(zip(separators, targets[1:]), start=1):
        previous = targets[index - 1]
        if separator not in {"to", "-", "–", "—"}:
            expanded.append(target)
            continue
        if not previous.base.isdigit() or not target.base.isdigit():
            expanded.append(target)
            continue
        start = int(previous.base)
        end = int(target.base)
        if end <= start or end - start > 500:
            expanded.append(target)
            continue
        range_start = previous.span_start
        range_end = target.span_end
        expanded.extend(
            CitationTarget(
                base=str(number),
                qualifiers=(),
                span_start=range_start,
                span_end=range_end,
                synthetic=True,
            )
            for number in range(start + 1, end)
        )
        expanded.append(target)
    return expanded


def _display_provision_path(provision_path: str) -> str:
    parts = provision_path.split("/")
    if parts[:1] == ["article"]:
        return f"Article {parts[1]}" + "".join(f"({part})" for part in parts[2:])
    if parts[:1] == ["section"]:
        return f"Section {parts[1]}" + "".join(f"({part})" for part in parts[2:])
    if parts[:1] == ["regulation"]:
        return f"Regulation {parts[1]}" + "".join(f"({part})" for part in parts[2:])
    if len(parts) >= 5 and parts[0] == "schedule" and parts[2] == "paragraph":
        return (
            f"Schedule {parts[1]} paragraph {parts[3]}"
            + "".join(f"({part})" for part in parts[4:])
        )
    if len(parts) >= 4 and parts[0] == "schedule" and parts[2] == "part":
        return f"Schedule {parts[1]} Part {parts[3]}"
    if len(parts) == 2 and parts[0] == "schedule":
        return f"Schedule {parts[1]}"
    if len(parts) >= 2 and parts[0] == "part":
        if len(parts) >= 4 and parts[2] == "chapter":
            return f"Part {parts[1]} Chapter {parts[3]}"
        return f"Part {parts[1]}" + "".join(f"({part})" for part in parts[2:])
    return provision_path.replace("/", " ")


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)
