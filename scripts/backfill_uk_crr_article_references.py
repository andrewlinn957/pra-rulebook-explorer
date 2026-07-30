#!/usr/bin/env python3
"""Audit and materialise missing numbered Article references in the PRA corpus.

The script scans atomic Rulebook and guidance nodes, accounts for every
``Article``/``Articles`` citation, classifies its legal instrument, resolves
same-Part references to existing PRA nodes, and resolves UK CRR references to
the latest revised text published by legislation.gov.uk.

Dry-run is the default.  ``--apply`` writes canonical nodes and edges.  The JSON
audit retains every occurrence, including citations classified as another
instrument or requiring review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import DEFAULT_DB, connect, ensure_indexes
from backend.rulebook_scraper.article_references import (
    ArticleCitation,
    OfficialArticle,
    citation_context,
    compact,
    expand_citation_articles,
    explicit_instrument,
    extract_article_citations,
    is_non_reference_article_use,
    load_official_uk_crr_articles,
    normalized_article,
)


DEFAULT_SOURCE_XML = ROOT / "backend/data/raw/uk-crr/regulation.xml"
DEFAULT_AUDIT = ROOT / "outputs/uk-crr-article-reference-audit.json"
DEFAULT_OVERRIDES = ROOT / "config/article_reference_classification_overrides.json"
SOURCE_NODE_TYPES = ("rule", "guidance_paragraph", "defined_term")
STRUCTURAL_NODE_TYPES = ("chapter", "rule")
GENERATED_EDGE_METHOD = "regex_uk_crr_article_reference_v2"
INTERNAL_EDGE_METHOD = "regex_internal_article_reference_v2"
CRR_PART_RE = re.compile(r"\(CRR\)\s*$", re.I)
ARTICLE_TITLE_RE = re.compile(
    r"^Article\s+(?P<base>\d{1,3}[A-Za-z]{0,3})"
    r"(?P<paragraphs>(?:\s*\(\s*[0-9A-Za-zivxlcdm.-]+\s*\))*)"
    r"(?=\s|$|[:—-])",
    re.I,
)
NUMBERED_RULE_TITLE_RE = re.compile(
    r"^(?P<base>\d{1,3})\.(?P<subrule>\d+[A-Za-z]?)"
    r"(?P<paragraphs>(?:\s*\(\s*[0-9A-Za-zivxlcdm.-]+\s*\))*)"
    r"(?=\s|$|[:—-])",
    re.I,
)
CRR_LABEL_RE = re.compile(
    r"\b(?:UK\s+)?CRR\b|Capital\s+Requirements?\s+Regulation"
    r"|Regulation\s*\(EU\)\s*(?:No\.?\s*)?(?:575\s*/\s*2013|2013\s*/\s*575)",
    re.I,
)
OTHER_LABEL_RE = re.compile(
    r"\b(?:Directive|Order|Act|BRRD|CRD|MiFID|MiFIR|EMIR|CSDR|"
    r"Statutory\s+Audit\s+Regulation|Commission\s+Recommendation|"
    r"Regulation\s*\((?:EU|EC)\)(?!\s*(?:No\.?\s*)?(?:575\s*/\s*2013|2013\s*/\s*575)))",
    re.I,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(value: str) -> str:
    value = (value or "").casefold().replace("–", "-").replace("—", "-")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def make_edge_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def parse_metadata(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_classification_overrides(
    path: Path,
) -> dict[tuple[str, str], tuple[str, str]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for item in payload.get("overrides", []):
        source_id = compact(str(item.get("source_node_id") or ""))
        article = normalized_article(str(item.get("article") or ""))
        classification = compact(str(item.get("classification") or "")).casefold()
        if not source_id or not article or classification not in {"uk_crr", "other", "internal"}:
            raise ValueError(f"Invalid Article classification override: {item!r}")
        evidence = compact(
            " — ".join(
                value
                for value in [
                    str(item.get("instrument") or ""),
                    str(item.get("rationale") or ""),
                ]
                if value
            )
        )
        result[(source_id, article)] = (classification, evidence)
    return result


@dataclass(frozen=True)
class InternalTarget:
    node_id: str
    node_type: str
    title: str
    text_chars: int
    part: str
    base: str
    full: str


@dataclass
class Resolution:
    article: str
    cited_token: str
    classification: str
    classification_evidence: str
    target_id: str = ""
    target_title: str = ""
    target_url: str = ""
    target_kind: str = ""
    source_text_chars: int = 0
    already_linked: bool = False
    edge_id: str = ""
    applied: bool = False
    issue: str = ""


def source_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in SOURCE_NODE_TYPES)
    return conn.execute(
        f"""
        SELECT id,node_type,title,text,url,metadata_json
        FROM node
        WHERE node_type IN ({placeholders})
          AND instr(lower(coalesce(text,'')),'art') > 0
        ORDER BY node_type,title,id
        """,
        SOURCE_NODE_TYPES,
    ).fetchall()


def internal_article_index(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], list[InternalTarget]]:
    index: dict[tuple[str, str], list[InternalTarget]] = defaultdict(list)
    placeholders = ",".join("?" for _ in STRUCTURAL_NODE_TYPES)
    for row in conn.execute(
        f"""
        SELECT id,node_type,title,text,metadata_json
        FROM node
        WHERE node_type IN ({placeholders})
          AND (
            lower(title) LIKE 'article %'
            OR (node_type='rule' AND title GLOB '[0-9]*.[0-9]*')
          )
        """,
        STRUCTURAL_NODE_TYPES,
    ):
        match = ARTICLE_TITLE_RE.match(compact(row["title"]))
        numbered_rule = NUMBERED_RULE_TITLE_RE.match(compact(row["title"]))
        if not match and not numbered_rule:
            continue
        metadata = parse_metadata(row["metadata_json"])
        part = compact(metadata.get("part_title") or "")
        if not part:
            continue
        if match:
            base = normalized_article(match.group("base"))
            paragraphs = re.sub(r"\s+", "", match.group("paragraphs") or "").lower()
            full = f"{base}{paragraphs}"
        else:
            base = normalized_article(numbered_rule.group("base"))
            paragraphs = re.sub(
                r"\s+",
                "",
                numbered_rule.group("paragraphs") or "",
            ).lower()
            full = f"{base}.{numbered_rule.group('subrule').lower()}{paragraphs}"
        index[(norm(part), base)].append(
            InternalTarget(
                node_id=row["id"],
                node_type=row["node_type"],
                title=row["title"],
                text_chars=len(compact(row["text"] or "")),
                part=part,
                base=base,
                full=full,
            )
        )
    for values in index.values():
        aggregate_chars = sum(
            target.text_chars
            for target in values
            if target.node_type == "rule" and target.text_chars
        )
        if aggregate_chars:
            values[:] = [
                replace(target, text_chars=aggregate_chars)
                if target.node_type == "chapter"
                else target
                for target in values
            ]
        values[:] = [
            replace(target, text_chars=len("[Deleted]"))
            if not target.text_chars and "[deleted]" in target.title.casefold()
            else target
            for target in values
        ]
        values.sort(
            key=lambda target: (
                target.node_type != "chapter",
                -target.text_chars,
                target.title,
                target.node_id,
            )
        )
    return index


def choose_internal_target(
    index: dict[tuple[str, str], list[InternalTarget]],
    *,
    source_id: str,
    target_part: str,
    article: str,
    cited_token: str,
) -> InternalTarget | None:
    candidates = [
        target
        for target in index.get((norm(target_part), article), [])
        if target.node_id != source_id
    ]
    if not candidates:
        return None
    full = re.sub(r"\s+", "", cited_token or "").lower()
    exact = [target for target in candidates if target.full == full]
    if exact:
        return max(exact, key=lambda target: (target.text_chars, target.node_type == "rule"))
    article_level = [
        target
        for target in candidates
        if target.node_type == "chapter" and target.full == article
    ]
    if article_level:
        return max(article_level, key=lambda target: target.text_chars)
    return max(candidates, key=lambda target: target.text_chars)


def internal_part_orders(
    index: dict[tuple[str, str], list[InternalTarget]],
) -> dict[str, list[str]]:
    by_part: dict[str, set[str]] = defaultdict(set)
    for (part, article), targets in index.items():
        if targets:
            by_part[part].add(article)
    return {
        part: sorted(articles, key=article_sort_key)
        for part, articles in by_part.items()
    }


def article_sort_key(value: str) -> tuple[int, int, str]:
    match = re.match(r"(\d+)([a-z]*)", value or "", re.I)
    suffix = (match.group(2) if match else value).casefold()
    return (
        int(match.group(1)) if match else 10**9,
        len(suffix),
        suffix,
    )


def part_match_key(value: str) -> str:
    words = [
        word
        for word in norm(value).split()
        if word not in {"part", "the", "based"}
    ]
    return " ".join(words)


def named_internal_part(
    value: str,
    titles: set[str],
    *,
    require_part_label: bool = True,
    prefer_last: bool = False,
) -> str:
    matches: list[tuple[int, int, int, str]] = []
    for title in titles:
        if len(norm(title)) < 8:
            continue
        if require_part_label:
            words = re.findall(r"[A-Za-z0-9]+", title)
            pattern_parts: list[str] = []
            for word in words:
                if word.casefold() == "based":
                    pattern_parts.append(r"(?:\W+Based)?")
                elif not pattern_parts:
                    pattern_parts.append(re.escape(word))
                else:
                    pattern_parts.append(r"\W+" + re.escape(word))
            title_pattern = "".join(pattern_parts)
            suffix = r"(?:\W+Part)?\b" if CRR_PART_RE.search(title) else r"\W+Part\b"
            found = list(re.finditer(title_pattern + suffix, value or "", re.I))
            if found:
                selected = found[-1] if prefer_last else found[0]
                matches.append((selected.start(), selected.end(), len(title), title))
        else:
            normalized_value = part_match_key(value)
            normalized_title = part_match_key(title)
            position = normalized_value.rfind(normalized_title)
            if position >= 0:
                matches.append(
                    (
                        position,
                        position + len(normalized_title),
                        len(normalized_title),
                        title,
                    )
                )
    if not matches:
        return ""
    return (max(matches, key=lambda item: item[1]) if prefer_last else min(matches))[3]


def metadata_internal_part(
    source: sqlite3.Row,
    titles: set[str],
) -> str:
    """Recover a Part hint from a glossary target's substantive source URL."""

    if norm(source["title"] or "") in {
        "level 1",
        "level 1 asset",
        "level 1 assets",
        "level 2 asset",
        "level 2 assets",
    }:
        return "Liquidity Coverage Ratio (CRR)"
    metadata = parse_metadata(source["metadata_json"])
    reader = metadata.get("reader_reference_text")
    source_url = reader.get("source_url") if isinstance(reader, dict) else ""
    source_url = source_url or source["url"] or ""
    match = re.search(r"/pra-rules/([^/?#]+)", source_url, re.I)
    if not match:
        return ""
    slug = match.group(1).casefold().strip("-")
    for title in titles:
        title_slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
        if slug == title_slug:
            return title
    return ""


def llm_article_classifications(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Use the existing extraction pass only as evidence for otherwise bare cites."""

    result: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        rows = conn.execute(
            """
            SELECT source_node_id,reference_text,target_title_or_identifier,
                   target_part_or_document,metadata_json
            FROM llm_reference_resolution
            WHERE lower(target_kind)='article'
            """
        )
    except sqlite3.OperationalError:
        return result
    for row in rows:
        metadata = parse_metadata(row["metadata_json"])
        labels = " ".join(
            [
                row["target_part_or_document"] or "",
                str(metadata.get("jurisdiction_or_source") or ""),
                row["reference_text"] or "",
            ]
        )
        if CRR_LABEL_RE.search(labels):
            classification = "uk_crr"
        elif OTHER_LABEL_RE.search(labels):
            classification = "other"
        else:
            continue
        reference_value = " ".join(
            [row["reference_text"] or "", row["target_title_or_identifier"] or ""]
        )
        for citation in extract_article_citations(reference_value):
            for token in citation.tokens:
                key = (row["source_node_id"], token.base)
                previous = result.get(key)
                evidence = compact(row["reference_text"] or row["target_title_or_identifier"] or "")
                if previous and previous[0] != classification:
                    result.pop(key, None)
                elif key not in result:
                    result[key] = (classification, evidence)
    return result


def document_dominance(
    rows: list[sqlite3.Row],
) -> dict[str, tuple[str, int, int]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        metadata = parse_metadata(row["metadata_json"])
        document = compact(metadata.get("document_title") or "")
        if not document:
            continue
        for citation in extract_article_citations(row["text"] or ""):
            classification, _ = explicit_instrument(row["text"] or "", citation)
            if classification:
                counts[document][classification] += 1
    result = {}
    for document, counter in counts.items():
        total = sum(counter.values())
        if total < 2:
            continue
        classification, winning = counter.most_common(1)[0]
        if winning / total >= 0.80:
            result[document] = (classification, winning, total)
    return result


def source_dominance(
    rows: list[sqlite3.Row],
) -> dict[str, tuple[str, int, int]]:
    """Infer bare citations from explicit citations in the same atomic text."""

    result: dict[str, tuple[str, int, int]] = {}
    for row in rows:
        counter: Counter[str] = Counter()
        for citation in extract_article_citations(row["text"] or ""):
            classification, _ = explicit_instrument(row["text"] or "", citation)
            if classification in {"uk_crr", "other"}:
                counter[classification] += 1
        total = sum(counter.values())
        if not total:
            continue
        classification, winning = counter.most_common(1)[0]
        if winning / total >= 0.80:
            result[row["id"]] = (classification, winning, total)
    return result


def existing_edges(conn: sqlite3.Connection) -> dict[str, set[str]]:
    by_source: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        """
        SELECT from_node_id,to_node_id
        FROM edge
        WHERE edge_type='references'
        """
    ):
        by_source[row["from_node_id"]].add(row["to_node_id"])
    return by_source


def external_node_id(article: str) -> str:
    return f"external:uk-crr:article:{article}"


def classify_and_resolve(
    *,
    source: sqlite3.Row,
    citation: ArticleCitation,
    article: str,
    cited_token: str,
    explicit_classification: str,
    explicit_evidence: str,
    internal_part_hint: str,
    internal_index: dict[tuple[str, str], list[InternalTarget]],
    official_articles: dict[str, OfficialArticle],
    llm_classifications: dict[tuple[str, str], tuple[str, str]],
    source_context: dict[str, tuple[str, int, int]],
    dominance: dict[str, tuple[str, int, int]],
    overrides: dict[tuple[str, str], tuple[str, str]],
) -> Resolution:
    metadata = parse_metadata(source["metadata_json"])
    source_part = compact(metadata.get("part_title") or "")
    source_document = compact(metadata.get("document_title") or "")
    official = official_articles.get(article)
    if (source["id"], article) in overrides:
        explicit_classification, explicit_evidence = overrides[(source["id"], article)]

    if explicit_classification == "non_reference":
        return Resolution(
            article,
            cited_token,
            "non_reference_defined_term",
            explicit_evidence,
            issue="Article-shaped text is a lexicalised defined term, not a citation",
        )

    if explicit_classification == "other":
        return Resolution(
            article,
            cited_token,
            "other_instrument",
            explicit_evidence,
            issue="Article citation belongs to a specifically named non-CRR instrument",
        )

    if explicit_classification != "uk_crr":
        relative_internal = (
            explicit_classification == "internal"
            and bool(re.search(r"\b(?:this|that|same)\b", explicit_evidence, re.I))
        )
        target_part = (
            source_part
            if relative_internal and source_part
            else (internal_part_hint or source_part)
        )
        internal = choose_internal_target(
            internal_index,
            source_id=source["id"],
            target_part=target_part,
            article=article,
            cited_token=cited_token,
        )
        if internal:
            return Resolution(
                article,
                cited_token,
                "internal_named_part"
                if target_part and norm(target_part) != norm(source_part)
                else "internal_same_part",
                target_part,
                target_id=internal.node_id,
                target_title=internal.title,
                target_kind="internal_provision",
                source_text_chars=internal.text_chars,
            )
        if explicit_classification == "internal":
            if official:
                return Resolution(
                    article,
                    cited_token,
                    "uk_crr_internal_part_fallback",
                    internal_part_hint or explicit_evidence,
                    target_id=external_node_id(article),
                    target_title=official.title,
                    target_url=official.url,
                    target_kind="uk_crr",
                    source_text_chars=len(official.text),
                )
            return Resolution(
                article,
                cited_token,
                "internal_unresolved",
                source_part if relative_internal else (internal_part_hint or explicit_evidence),
                issue="Citation explicitly points inside the PRA Rulebook but no Article target was found",
            )

    # The PRA restated a number of CRR Articles in its own (CRR) Parts when
    # Treasury revocation removed them from the live retained-EU XML.  Preserve
    # the citation by resolving to that substantive Rulebook restatement.
    if explicit_classification == "uk_crr" and not official:
        target_part = internal_part_hint or source_part
        internal = choose_internal_target(
            internal_index,
            source_id=source["id"],
            target_part=target_part,
            article=article,
            cited_token=cited_token,
        )
        if internal:
            return Resolution(
                article,
                cited_token,
                "internal_restatement_of_uk_crr",
                explicit_evidence,
                target_id=internal.node_id,
                target_title=internal.title,
                target_kind="internal_provision",
                source_text_chars=internal.text_chars,
            )

    classification = ""
    evidence = ""
    if explicit_classification == "uk_crr":
        classification = "uk_crr_explicit"
        evidence = explicit_evidence
    elif source_part and CRR_PART_RE.search(source_part):
        classification = "uk_crr_source_part"
        evidence = source_part
    elif source["id"] in source_context:
        contextual, winning, total = source_context[source["id"]]
        if contextual == "other":
            return Resolution(
                article,
                cited_token,
                "other_instrument_same_source",
                f"{winning}/{total} explicit Article citations in the same source text",
                issue="Other citations in the same source text identify a non-CRR instrument",
            )
        classification = "uk_crr_same_source"
        evidence = f"{winning}/{total} explicit Article citations in the same source text"
    elif (source["id"], article) in llm_classifications:
        llm_classification, llm_evidence = llm_classifications[(source["id"], article)]
        if llm_classification == "other":
            return Resolution(
                article,
                cited_token,
                "other_instrument_llm",
                llm_evidence,
                issue="Existing extraction identifies a non-CRR instrument",
            )
        classification = "uk_crr_llm_context"
        evidence = llm_evidence
    elif OTHER_LABEL_RE.search(source["title"] or ""):
        return Resolution(
            article,
            cited_token,
            "other_instrument_source_title",
            compact(source["title"] or ""),
            issue="The source definition identifies a non-CRR instrument",
        )
    elif source_document in dominance:
        dominant, winning, total = dominance[source_document]
        if dominant == "other":
            return Resolution(
                article,
                cited_token,
                "other_instrument_document",
                f"{source_document}: {winning}/{total} explicit Article citations",
                issue="Document-level explicit citations predominantly name another instrument",
            )
        classification = "uk_crr_document_context"
        evidence = f"{source_document}: {winning}/{total} explicit Article citations"

    if not classification:
        return Resolution(
            article,
            cited_token,
            "ambiguous",
            source_part or source_document,
            issue="No explicit, same-Part, or reliable document-level instrument context",
        )
    if not official:
        target_part = internal_part_hint or source_part
        internal = choose_internal_target(
            internal_index,
            source_id=source["id"],
            target_part=target_part,
            article=article,
            cited_token=cited_token,
        )
        if not internal:
            parts = {
                part
                for (part, candidate_article), targets in internal_index.items()
                if candidate_article == article
                and any(target.node_id != source["id"] for target in targets)
            }
            if len(parts) == 1:
                internal = choose_internal_target(
                    internal_index,
                    source_id=source["id"],
                    target_part=next(iter(parts)),
                    article=article,
                    cited_token=cited_token,
                )
        if internal:
            return Resolution(
                article,
                cited_token,
                "internal_restatement_of_uk_crr",
                evidence,
                target_id=internal.node_id,
                target_title=internal.title,
                target_kind="internal_provision",
                source_text_chars=internal.text_chars,
            )
        return Resolution(
            article,
            cited_token,
            classification,
            evidence,
            target_id=external_node_id(article),
            target_kind="uk_crr",
            issue="Article is not present in the latest legislation.gov.uk XML",
        )
    return Resolution(
        article,
        cited_token,
        classification,
        evidence,
        target_id=external_node_id(article),
        target_title=official.title,
        target_url=official.url,
        target_kind="uk_crr",
        source_text_chars=len(official.text),
    )


def upsert_official_node(
    conn: sqlite3.Connection,
    official: OfficialArticle,
    *,
    source_xml: Path,
    retrieved_at: str,
) -> None:
    node_id = external_node_id(official.article)
    row = conn.execute(
        "SELECT metadata_json FROM node WHERE id=?",
        (node_id,),
    ).fetchone()
    metadata = parse_metadata(row["metadata_json"] if row else "{}")
    metadata.update(
        {
            "source": "UK CRR",
            "external_reference": True,
            "article": official.article,
        }
    )
    metadata["uk_crr_source_text"] = {
        "method": "legislation_gov_uk_xml",
        "source_url": official.url,
        "document_uri": official.document_uri,
        "source_xml": str(source_xml),
        "retrieved_at": retrieved_at,
        "content_hash": official.content_hash,
    }
    conn.execute(
        """
        INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
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


def insert_edge(
    conn: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    citation: ArticleCitation,
    resolution: Resolution,
    context: str,
) -> str:
    method = (
        INTERNAL_EDGE_METHOD
        if resolution.target_kind == "internal_provision"
        else GENERATED_EDGE_METHOD
    )
    edge_id = make_edge_id(
        source["id"],
        resolution.target_id,
        "references",
        method,
    )
    confidence = {
        "uk_crr_explicit": 0.99,
        "internal_same_part": 0.98,
        "uk_crr_source_part": 0.95,
        "uk_crr_llm_context": 0.92,
        "uk_crr_same_source": 0.92,
        "uk_crr_document_context": 0.90,
        "uk_crr_internal_part_fallback": 0.90,
        "internal_restatement_of_uk_crr": 0.97,
    }.get(resolution.classification, 0.88)
    first_token = citation.tokens[0] if citation.tokens else None
    if first_token and resolution.article == first_token.base:
        display_reference = f"{citation.prefix} {resolution.cited_token}"
    elif any(token.base == resolution.article for token in citation.tokens):
        display_reference = resolution.cited_token
    else:
        # Interior members of an expanded range are not lexical spans in the
        # source paragraph. Keep the full group as evidence while avoiding a
        # fabricated clickable number.
        display_reference = citation.text
    metadata = {
        "reference": compact(display_reference),
        "reference_group": citation.text,
        "article": resolution.article,
        "citation_text": citation.text,
        "classification": resolution.classification,
        "classification_evidence": resolution.classification_evidence,
        "target_title": resolution.target_title,
        "scope": "same_part_article"
        if resolution.target_kind == "internal_provision"
        else "uk_crr",
        "official_source_url": resolution.target_url,
        "source_span": {"start": citation.start, "end": citation.end},
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
            edge_id,
            source["id"],
            resolution.target_id,
            "references",
            method,
            confidence,
            context,
            source["url"] or "",
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    return edge_id


def hydrate_internal_target(conn: sqlite3.Connection, target_id: str) -> int:
    """Populate an empty structural Article from its substantive child rules."""

    row = conn.execute(
        "SELECT title,text,metadata_json FROM node WHERE id=?",
        (target_id,),
    ).fetchone()
    if not row:
        return 0
    existing = compact(row["text"] or "")
    if existing:
        return len(existing)
    children = conn.execute(
        """
        SELECT n.title,n.text
        FROM edge e
        JOIN node n ON n.id=e.to_node_id
        WHERE e.from_node_id=?
          AND e.edge_type='contains'
          AND length(trim(coalesce(n.text,''))) > 0
        ORDER BY n.title,n.id
        """,
        (target_id,),
    ).fetchall()
    if children:
        text = "\n\n".join(
            f"{compact(child['title'])}\n{compact(child['text'])}"
            for child in children
        )
        method = "contained_rule_aggregation"
        child_ids = len(children)
    elif "[deleted]" in (row["title"] or "").casefold():
        text = "[Deleted]"
        method = "deleted_provision_marker"
        child_ids = 0
    else:
        return 0
    metadata = parse_metadata(row["metadata_json"])
    metadata["article_reference_source_text"] = {
        "method": method,
        "child_rule_count": child_ids,
        "applied_at": utc_now(),
    }
    conn.execute(
        "UPDATE node SET text=?,metadata_json=? WHERE id=?",
        (
            text,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            target_id,
        ),
    )
    return len(compact(text))


def audit_and_apply(
    conn: sqlite3.Connection,
    *,
    source_xml: Path,
    overrides_path: Path,
    apply: bool,
) -> dict[str, Any]:
    official_order, official_articles = load_official_uk_crr_articles(source_xml)
    sources = source_rows(conn)
    internal_index = internal_article_index(conn)
    internal_part_titles = {
        target.part
        for targets in internal_index.values()
        for target in targets
    }
    part_orders = internal_part_orders(internal_index)
    llm_classifications = llm_article_classifications(conn)
    source_context = source_dominance(sources)
    dominance = document_dominance(sources)
    overrides = load_classification_overrides(overrides_path)
    edges_by_source = existing_edges(conn)
    retrieved_at = datetime.fromtimestamp(
        source_xml.stat().st_mtime,
        tz=timezone.utc,
    ).isoformat()

    occurrence_rows: list[dict[str, Any]] = []
    unique_resolutions: dict[tuple[str, str], Resolution] = {}
    target_context: dict[tuple[str, str], tuple[sqlite3.Row, ArticleCitation, str]] = {}
    range_errors: list[dict[str, str]] = []
    for source in sources:
        metadata = parse_metadata(source["metadata_json"])
        source_part = compact(metadata.get("part_title") or "")
        source_document = compact(metadata.get("document_title") or "")
        text = source["text"] or ""
        for citation in extract_article_citations(text):
            if is_non_reference_article_use(text, citation):
                explicit_classification = "non_reference"
                explicit_evidence = compact(text[citation.start : citation.end + 40])
            else:
                explicit_classification, explicit_evidence = explicit_instrument(text, citation)
            context = citation_context(text, citation)
            after_clause = re.split(
                r";",
                text[citation.end : min(len(text), citation.end + 400)],
                maxsplit=1,
            )[0]
            before_clause = re.split(
                r";",
                text[max(0, citation.start - 120) : citation.start],
            )[-1]
            prefixed_crr_part = ""
            if re.search(r"\(CRR\)\s*$", before_clause, re.I):
                prefixed_crr_part = named_internal_part(
                    before_clause,
                    internal_part_titles,
                    prefer_last=True,
                )
            internal_part_hint = (
                prefixed_crr_part
                or named_internal_part(after_clause, internal_part_titles)
                or named_internal_part(
                    before_clause,
                    internal_part_titles,
                    prefer_last=True,
                )
                or metadata_internal_part(source, internal_part_titles)
                or named_internal_part(
                    source_document,
                    internal_part_titles,
                    require_part_label=False,
                )
            )
            source_part_key = norm(source_part)
            hinted_part_key = norm(internal_part_hint)
            range_order = official_order
            if citation.is_range:
                endpoints = {citation.tokens[0].base, citation.tokens[-1].base}
                hinted_order = part_orders.get(hinted_part_key, [])
                if explicit_classification == "other":
                    range_order = [token.base for token in citation.tokens]
                elif internal_part_hint and endpoints.issubset(set(hinted_order)):
                    range_order = hinted_order
                elif explicit_classification == "internal":
                    relative_internal = bool(
                        re.search(r"\b(?:this|that|same)\b", explicit_evidence, re.I)
                    )
                    preferred_part_key = (
                        source_part_key
                        if relative_internal and source_part_key
                        else hinted_part_key or source_part_key
                    )
                    candidate_order = part_orders.get(preferred_part_key, [])
                    if endpoints.issubset(set(candidate_order)):
                        range_order = candidate_order
                elif (
                    citation.tokens
                    and citation.tokens[0].base in set(part_orders.get(source_part_key, []))
                    and citation.tokens[-1].base in set(part_orders.get(source_part_key, []))
                ):
                    range_order = part_orders[source_part_key]
            articles, errors = expand_citation_articles(citation, range_order)
            for error in errors:
                range_errors.append(
                    {
                        "source_node_id": source["id"],
                        "citation": citation.text,
                        "error": error,
                    }
                )
            token_by_base = {token.base: token.full for token in citation.tokens}
            if len(citation.tokens) == 1:
                decimal = re.match(
                    r"\s*\.\s*(?P<subrule>\d+[A-Za-z]?)"
                    r"(?P<paragraphs>(?:\s*\(\s*[0-9A-Za-zivxlcdm.-]+\s*\))*)",
                    text[citation.end :],
                    re.I,
                )
                if decimal:
                    token = citation.tokens[0]
                    decimal_paragraphs = re.sub(
                        r"\s+",
                        "",
                        decimal.group("paragraphs") or "",
                    ).lower()
                    token_by_base[token.base] = (
                        f"{token.base}.{decimal.group('subrule').lower()}"
                        f"{decimal_paragraphs}"
                    )
            occurrence = {
                "source_node_id": source["id"],
                "source_node_type": source["node_type"],
                "source_title": source["title"],
                "source_part": source_part,
                "source_document": source_document,
                "source_url": source["url"],
                "citation": citation.text,
                "span_start": citation.start,
                "span_end": citation.end,
                "context": context,
                "explicit_instrument": explicit_classification,
                "explicit_instrument_evidence": explicit_evidence,
                "internal_part_hint": internal_part_hint,
                "range_expansion_errors": errors,
                "resolutions": [],
            }
            for article in articles:
                cited_token = token_by_base.get(article, article)
                resolution = classify_and_resolve(
                    source=source,
                    citation=citation,
                    article=article,
                    cited_token=cited_token,
                    explicit_classification=explicit_classification,
                    explicit_evidence=explicit_evidence,
                    internal_part_hint=internal_part_hint,
                    internal_index=internal_index,
                    official_articles=official_articles,
                    llm_classifications=llm_classifications,
                    source_context=source_context,
                    dominance=dominance,
                    overrides=overrides,
                )
                resolution.already_linked = resolution.target_id in edges_by_source[source["id"]]
                key = (
                    source["id"],
                    resolution.target_id or f"unresolved:{article}",
                )
                existing = unique_resolutions.get(key)
                if not existing or (
                    existing.issue
                    and not resolution.issue
                ):
                    unique_resolutions[key] = resolution
                    target_context[key] = (source, citation, occurrence["context"])
                occurrence["resolutions"].append(asdict(resolution))
            occurrence_rows.append(occurrence)

    stale_generated_edge_ids: list[str] = []
    refreshed_generated_edges = 0
    if apply:
        desired_pairs = {
            (source_id, resolution.target_id)
            for (source_id, _), resolution in unique_resolutions.items()
            if not resolution.issue and resolution.target_id
        }
        stale_generated_edge_ids = [
            row["id"]
            for row in conn.execute(
                """
                SELECT id,from_node_id,to_node_id
                FROM edge
                WHERE source_method IN (?,?)
                """,
                (GENERATED_EDGE_METHOD, INTERNAL_EDGE_METHOD),
            )
            if (row["from_node_id"], row["to_node_id"]) not in desired_pairs
        ]
        if stale_generated_edge_ids:
            conn.executemany(
                "DELETE FROM edge WHERE id=?",
                [(edge_id_value,) for edge_id_value in stale_generated_edge_ids],
            )
            edges_by_source = existing_edges(conn)
        owned_pairs = {
            (row["from_node_id"], row["to_node_id"])
            for row in conn.execute(
                """
                SELECT from_node_id,to_node_id
                FROM edge
                WHERE source_method IN (?,?)
                """,
                (GENERATED_EDGE_METHOD, INTERNAL_EDGE_METHOD),
            )
        }
        for key, resolution in unique_resolutions.items():
            if resolution.issue or not resolution.target_id:
                continue
            source, citation, context = target_context[key]
            if resolution.target_kind == "uk_crr":
                upsert_official_node(
                    conn,
                    official_articles[resolution.article],
                    source_xml=source_xml,
                    retrieved_at=retrieved_at,
                )
            elif resolution.target_kind == "internal_provision":
                hydrated_chars = hydrate_internal_target(conn, resolution.target_id)
                if hydrated_chars:
                    resolution.source_text_chars = hydrated_chars
            pair = (source["id"], resolution.target_id)
            if resolution.already_linked and pair in owned_pairs:
                resolution.edge_id = insert_edge(
                    conn,
                    source=source,
                    citation=citation,
                    resolution=resolution,
                    context=context,
                )
                refreshed_generated_edges += 1
            elif not resolution.already_linked:
                resolution.edge_id = insert_edge(
                    conn,
                    source=source,
                    citation=citation,
                    resolution=resolution,
                    context=context,
                )
                resolution.applied = True
                edges_by_source[source["id"]].add(resolution.target_id)
                owned_pairs.add(pair)
        conn.commit()
        ensure_indexes(conn)

    for occurrence in occurrence_rows:
        for item in occurrence["resolutions"]:
            key = (
                occurrence["source_node_id"],
                item["target_id"] or f"unresolved:{item['article']}",
            )
            final = unique_resolutions[key]
            item.update(asdict(final))

    resolutions = list(unique_resolutions.values())
    actionable = [
        resolution
        for resolution in resolutions
        if resolution.target_id and not resolution.issue
    ]
    excluded_classifications = {
        "other_instrument",
        "other_instrument_llm",
        "other_instrument_document",
        "other_instrument_source_title",
        "other_instrument_same_source",
        "non_reference_defined_term",
    }
    review_required = [
        resolution
        for resolution in resolutions
        if resolution.issue and resolution.classification not in excluded_classifications
    ]
    excluded = [
        resolution
        for resolution in resolutions
        if resolution.classification in excluded_classifications
    ]
    summary = {
        "generated_at": utc_now(),
        "applied": apply,
        "source_xml": str(source_xml),
        "classification_overrides_loaded": len(overrides),
        "source_nodes_scanned": len(sources),
        "official_articles_loaded": len(official_articles),
        "citation_occurrences": len(occurrence_rows),
        "unique_source_article_resolutions": len(resolutions),
        "actionable_references": len(actionable),
        "already_linked": sum(resolution.already_linked for resolution in actionable),
        "missing_before_apply": sum(not resolution.already_linked for resolution in actionable),
        "edges_added": sum(resolution.applied for resolution in actionable),
        "stale_generated_edges_removed": (
            len(stale_generated_edge_ids)
        ),
        "generated_edges_refreshed": refreshed_generated_edges,
        "review_required": len(review_required),
        "excluded_other_instrument_or_non_reference": len(excluded),
        "range_expansion_errors": len(range_errors),
        "by_classification": dict(
            sorted(Counter(resolution.classification for resolution in resolutions).items())
        ),
        "issues": dict(
            sorted(Counter(resolution.issue for resolution in resolutions if resolution.issue).items())
        ),
    }
    return {
        "summary": summary,
        "document_dominance": {
            document: {
                "classification": classification,
                "winning_explicit_citations": winning,
                "total_explicit_citations": total,
            }
            for document, (classification, winning, total) in sorted(dominance.items())
        },
        "range_expansion_errors": range_errors,
        "occurrences": occurrence_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source-xml", type=Path, default=DEFAULT_SOURCE_XML)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db)
    result = audit_and_apply(
        conn,
        source_xml=args.source_xml,
        overrides_path=args.overrides,
        apply=args.apply,
    )
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    has_integrity_failure = bool(
        result["summary"]["range_expansion_errors"]
        or result["summary"]["review_required"]
    )
    return 2 if has_integrity_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
