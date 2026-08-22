#!/usr/bin/env python3
"""Apply the reference-resolution policy to the residual LLM ledger.

The LLM extraction table is an audit ledger, while ``edge`` and
``reference_occurrence`` are the user-facing graph.  Older resolution passes
were deliberately conservative and left many rows without a target even when
the row was either a document-level reference or an exact provision already
represented by the corpus.  This pass gives every residual row an explicit
policy outcome:

* provision-level references resolve to a node containing source text;
* document-level references resolve to a node with a usable source URL; and
* extraction artefacts are recorded as ``not_reference`` rather than being
  silently counted as unresolved.

The default is a read-only audit.  ``--apply`` updates the live database and
fetches official statutory/instrument text for registry-backed provision
targets that are not present yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from io import BytesIO
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect

from backend.rulebook_scraper.legal_references import (  # noqa: E402
    DEFAULT_INSTRUMENT_REGISTRY,
    Instrument,
    InstrumentRegistry,
    citation_occurrences,
    external_provision_node_id,
    fetch_official_provision,
)
from backend.rulebook_scraper.reference_occurrences import (  # noqa: E402
    policy_citation_occurrences,
)
from scripts.backfill_legal_references import (  # noqa: E402
    cache_fetcher,
    materialize_target,
)
from backend.app.db import ensure_indexes  # noqa: E402


DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_REGISTRY = ROOT / "config" / "legal_instruments.json"
DEFAULT_AUDIT = ROOT / "outputs" / "reference-policy-resolution-20260801.json"
METHOD = "resolution_policy_v1"

SPECIFIC_KINDS = {
    "rule",
    "article",
    "section",
    "paragraph",
    "point",
    "subparagraph",
    "regulation",
    "schedule_paragraph",
    "schedule_part",
}
DOCUMENT_KINDS = {
    "chapter",
    "part",
    "guidance",
    "template",
    "table",
    "form",
    "annex",
    "schedule",
    "statute",
    "legal_instrument",
    "directive",
    "policy_statement",
    "statement_of_policy",
    "consultation",
    "supervisory_statement",
    "external",
    "document",
    "report",
    "publication",
}
DOCUMENT_KINDS |= {value.replace("_", " ") for value in tuple(DOCUMENT_KINDS)}

NODE_DOCUMENT_TYPES = {
    "part",
    "guidance_document",
    "legal_instrument",
    "external_reference",
}

_SPECIAL_DOCUMENT_TEXT_CACHE: dict[str, str] = {}
_HISTORICAL_RULEBOOK_TEXT_CACHE: dict[str, str] = {}


def special_document_text(url: str) -> str:
    """Fetch text for the small set of official PDF rule instruments.

    These instruments are not legislation.gov.uk records, so the generic
    registry fetcher cannot materialise them.  Keeping the fetch here makes
    the resulting graph node genuinely readable while retaining the official
    PDF URL for provenance.  A failed fetch simply leaves the document link
    usable; it never blocks the rest of the resolution pass.
    """

    if not url or url in _SPECIAL_DOCUMENT_TEXT_CACHE:
        return _SPECIAL_DOCUMENT_TEXT_CACHE.get(url, "")
    text = ""
    try:
        payload = urlopen(Request(url, headers={"User-Agent": "PRA-Rulebook-Explorer/1.0"}), timeout=30).read()
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(payload))
        text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    except Exception:  # pragma: no cover - remote publisher availability
        text = ""
    _SPECIAL_DOCUMENT_TEXT_CACHE[url] = text
    return text


def historical_ispv_rule_text(url: str) -> str:
    """Return the archived ISPV rule text used by SS8/17's old citation.

    SS8/17 cites ``2C.9.1`` even though the PRA Rulebook version in force at
    the time published the governance provision as rule ``2C.9``.  The
    current Rulebook marks 2C.9 deleted, so using the current node would hide
    the source wording.  The archived official page is the authoritative
    source for this historical citation.
    """

    if not url or url in _HISTORICAL_RULEBOOK_TEXT_CACHE:
        return _HISTORICAL_RULEBOOK_TEXT_CACHE.get(url, "")
    text = ""
    try:
        payload = urlopen(Request(url, headers={"User-Agent": "PRA-Rulebook-Explorer/1.0"}), timeout=30).read()
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(payload, "html.parser")
        # The stable fragment id is retained across Rulebook versions.  Keep
        # the selector narrow so navigation/footer text is not mistaken for
        # legal source wording.
        block = soup.find(id="fdbeed9a45844dd99937ff3049197117")
        if block is not None:
            number = block.select_one(".rule-number")
            body = block.select_one(".div-row__col-2")
            number_text = number.get_text(" ", strip=True) if number else "2C.9"
            body_text = body.get_text(" ", strip=True) if body else ""
            if body_text:
                text = f"{number_text}\n{body_text}"
    except Exception:  # pragma: no cover - remote publisher availability
        text = ""
    _HISTORICAL_RULEBOOK_TEXT_CACHE[url] = text
    return text


def historical_rulebook_article_text(url: str, fragment: str) -> str:
    """Fetch the readable text for an archived PRA Rulebook article.

    A small number of CRR articles have been deleted from the current
    Rulebook, while the official archived page still retains their stable
    fragment and effective wording.  The generic Rulebook crawler quite
    correctly ignores deleted nodes, so this helper is deliberately narrow
    and only used for a citation whose historical page is known.
    """

    cache_key = url + "#" + fragment
    if cache_key in _HISTORICAL_RULEBOOK_TEXT_CACHE:
        return _HISTORICAL_RULEBOOK_TEXT_CACHE[cache_key]
    text = ""
    try:
        payload = urlopen(Request(url, headers={"User-Agent": "PRA-Rulebook-Explorer/1.0"}), timeout=30).read()
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(payload, "html.parser")
        block = soup.find(id=fragment)
        if block is not None:
            heading = block.select_one(".chapter-heading, .rule-number, .article-number")
            body = block.select_one(".row-block .div-row__col-2")
            heading_text = heading.get_text(" ", strip=True) if heading else ""
            body_text = body.get_text(" ", strip=True) if body else ""
            if body_text:
                text = "\n".join(value for value in (heading_text, body_text) if value)
    except Exception:  # pragma: no cover - remote publisher availability
        text = ""
    _HISTORICAL_RULEBOOK_TEXT_CACHE[cache_key] = text
    return text


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(*parts: object, length: int = 24) -> str:
    return hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:length]


def normalise(value: str | None) -> str:
    value = (value or "").casefold().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def singular(value: str | None) -> str:
    value = normalise(value)
    return {
        "s": "section",
        "sec": "section",
        "art": "article",
        "arts": "article",
        "reg": "regulation",
        "para": "paragraph",
        "articles": "article",
        "sections": "section",
        "rules": "rule",
        "paragraphs": "paragraph",
        "points": "point",
        "subparagraphs": "subparagraph",
        "chapters": "chapter",
        "parts": "part",
        "annexes": "annex",
        "schedules": "schedule",
        "templates": "template",
        "tables": "table",
        "forms": "form",
        "regulations": "regulation",
    }.get(value, value)


def metadata(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = row["metadata_json"] if isinstance(row, sqlite3.Row) else row.get("metadata_json")
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def source_text(row: sqlite3.Row | dict[str, Any]) -> str:
    text = row["text"] if isinstance(row, sqlite3.Row) else row.get("text")
    title = row["title"] if isinstance(row, sqlite3.Row) else row.get("title")
    return text or title or ""


def context_names(row: sqlite3.Row | dict[str, Any]) -> set[str]:
    meta = metadata(row)
    values = {
        meta.get("part_title"),
        meta.get("document_title"),
        meta.get("source_title"),
        meta.get("chapter_title"),
        row["title"] if isinstance(row, sqlite3.Row) else row.get("title"),
    }
    return {normalise(str(value)) for value in values if value}


def node_has_text(row: sqlite3.Row | dict[str, Any]) -> bool:
    # A title is navigation metadata, not the source wording required for a
    # provision target.  In particular, empty Rulebook chapters and guidance
    # sections must not be treated as readable merely because they have a
    # title.  Document-scope links are validated by URL separately.
    text = row["text"] if isinstance(row, sqlite3.Row) else row.get("text")
    return bool(str(text or "").strip())


def canonical_semantic_target(
    resolver: Any,
    target_id: str,
) -> tuple[str, dict[str, Any] | None]:
    """Return the canonical ID for a legal provision target.

    Rulebook versions remain useful as the text-bearing target while a
    citation is being resolved.  They must not, however, be written as the
    endpoint of a semantic edge or lexical occurrence.  Keep this normaliser
    at the materialisation boundary because resolver candidates are indexed
    from the dated ``rule`` rows.
    """

    if not target_id:
        return target_id, None
    target = resolver.nodes.get(target_id)
    if target is None:
        return target_id, None
    target_meta = target.get("meta") or metadata(target)
    canonical_id = str(target_meta.get("canonical_provision_id") or "")
    if not canonical_id or canonical_id == target_id:
        return target_id, target

    canonical = resolver.nodes.get(canonical_id)
    if canonical is None:
        conn = getattr(resolver, "conn", None)
        if conn is not None:
            row = conn.execute(
                "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?",
                (canonical_id,),
            ).fetchone()
            if row is not None:
                canonical = dict(row) | {"meta": metadata(row)}
                resolver.nodes[canonical_id] = canonical
    # Do not emit an endpoint that is not present in the node table.  A
    # malformed/partial deployment should retain its usable version target
    # until the canonical row is repaired.
    if canonical is None:
        return target_id, target
    return canonical_id, canonical


def exact_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    if not phrase:
        return []
    result: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(phrase, start)
        if found < 0:
            break
        result.append((found, found + len(phrase)))
        start = found + max(1, len(phrase))
    return result


def loose_span(text: str, phrase: str) -> list[tuple[int, int]]:
    """Find a citation when HTML/PDF whitespace differs from the ledger."""

    phrase = compact(phrase)
    if not phrase:
        return []
    tokens = re.findall(r"\S+", phrase)
    if not tokens:
        return []
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    try:
        return [(match.start(), match.end()) for match in re.finditer(pattern, text, re.I)]
    except re.error:
        return []


def quote_span(row: sqlite3.Row, source: sqlite3.Row) -> tuple[int, int, str] | None:
    text = source_text(source)
    reference = compact(row["reference_text"] or "")
    identifier = compact(row["target_title_or_identifier"] or "")
    evidence = compact(row["evidence_quote"] or "")

    broad_spans: list[tuple[int, int]] = []
    for phrase in (evidence, reference):
        spans = exact_spans(text, phrase) or loose_span(text, phrase)
        if spans and len(phrase) > 12:
            broad_spans.extend(spans)

    candidates: list[tuple[int, int, str, int]] = []
    # Prefer the extracted citation phrase.  A very short bare number is only
    # safe when it is contained in the evidence sentence.
    for rank, phrase in enumerate((reference, identifier, evidence)):
        if not phrase:
            continue
        spans = exact_spans(text, phrase) or loose_span(text, phrase)
        for start, end in spans:
            if len(phrase) <= 2 and broad_spans and not any(
                start >= broad_start and end <= broad_end
                for broad_start, broad_end in broad_spans
            ):
                continue
            candidates.append((start, end, text[start:end], rank))
    if not candidates:
        return None
    # Exact/short citation inside the quoted evidence wins, then shortest
    # citation phrase, then earliest occurrence.
    def score(item: tuple[int, int, str, int]) -> tuple[int, int, int, int]:
        start, end, phrase, rank = item
        inside = int(any(start >= b0 and end <= b1 for b0, b1 in broad_spans))
        return (-inside, rank, len(phrase), start)

    start, end, quote, _rank = min(candidates, key=score)
    return start, end, quote


def identifier_candidates(value: str) -> list[str]:
    value = value or ""
    # Keep qualifiers as path components while tolerating punctuation/spacing.
    preferred_rule = re.search(
        r"\brule\s+(?P<base>[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*)"
        r"(?P<qualifiers>(?:\s*\([^)]*\))*)",
        value,
        re.I,
    )
    match = preferred_rule or re.search(
        r"(?:articles?|arts?\.?|sections?|s(?=\.?\s*[0-9])\.?|regulations?|regs?\.?|paragraphs?|paras?\.?|points?|subparagraphs?|rules?)\s*"
        r"(?P<base>[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*)"
        r"(?P<qualifiers>(?:\s*\([^)]*\))*)",
        value,
        re.I,
    )
    if not match:
        match = re.search(
            r"\b(?P<base>[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*)\s*(?P<qualifiers>(?:\([^)]*\))*)",
            value,
            re.I,
        )
    if not match:
        return []
    base = match.group("base")
    qualifiers = [compact(item) for item in re.findall(r"\(([^)]*)\)", match.group("qualifiers") or "")]
    values = [base]
    # Range/list references are represented by each base when present.
    tail = value[match.end() :]
    for candidate, qualifier in re.findall(
        r"(?:,|and|or|to|[-–—])\s*([0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*)\s*(\([^)]*\))?",
        tail,
        re.I,
    ):
        value_candidate = candidate + qualifier if qualifier else candidate
        if value_candidate not in values:
            values.append(value_candidate)
    if qualifiers:
        values.insert(0, base + "(" + ")(".join(qualifiers) + ")")
    return values


def path_candidates(kind: str, value: str) -> list[str]:
    identifiers = identifier_candidates(value)
    if not identifiers:
        return []
    paths: list[str] = []
    path_kind = {
        "article": "article",
        "section": "section",
        "regulation": "regulation",
        "paragraph": "paragraph",
        "point": "point",
        "subparagraph": "subparagraph",
        "schedule_paragraph": "schedule",
        "schedule_part": "schedule",
    }.get(kind, kind)
    for identifier in identifiers:
        base = identifier.split("(", 1)[0]
        # EUR/UK legislation XML exposes dotted article/paragraph notation as
        # nested path components (``Article 4.1(17)`` -> ``article/4/1/17``),
        # while the ledger keeps the human citation punctuation.  Expand the
        # dotted base before appending parenthesised qualifiers.
        parts = base.split(".") + re.findall(r"\(([^)]*)\)", identifier)
        if path_kind in {"article", "section", "regulation", "rule", "paragraph", "point", "subparagraph"}:
            paths.append(path_kind + "/" + "/".join(parts))
    return list(dict.fromkeys(paths))


def extract_instrument(registry: InstrumentRegistry, row: sqlite3.Row, source: sqlite3.Row) -> Instrument | None:
    doc = compact(row["target_part_or_document"] or "")
    ref = compact(row["reference_text"] or "")
    identifier = compact(row["target_title_or_identifier"] or "")
    evidence = compact(row["evidence_quote"] or "")
    source_meta = metadata(source)
    source_hint = compact(" ".join(
        str(value)
        for value in (
            source_meta.get("part_title"),
            source_meta.get("document_title"),
            source_meta.get("source_title"),
            source["title"],
        )
        if value
    ))
    # The ledger's evidence quote is intentionally short, but definitions in
    # the containing provision often introduce the governing instrument a few
    # sentences earlier.  Keep the full source available for narrowly scoped
    # identity hints (not for unrestricted alias matching) so citations such
    # as Article 16 of the Statutory Audit Regulation and regulations 5/18 of
    # the Lloyd's Accounts Regulations can still be fetched exactly.
    source_body = source_text(source)
    cache_key = (normalise(doc), normalise(ref), normalise(identifier), normalise(evidence))
    cached = getattr(registry, "_policy_instrument_cache", {}).get(cache_key) if hasattr(registry, "_policy_instrument_cache") else None
    if cache_key in getattr(registry, "_policy_instrument_cache", {}):
        return cached

    def remember(result: Instrument | None) -> Instrument | None:
        if not hasattr(registry, "_policy_instrument_cache"):
            registry._policy_instrument_cache = {}
        registry._policy_instrument_cache[cache_key] = result
        if result is not None:
            # Dynamic identities are used by fetch_missing/apply_outcomes too.
            registry.by_id.setdefault(result.instrument_id, result)
        return result
    explicit_instrument_text = " ".join((doc, ref, identifier, evidence))
    # A supervisory statement about the 2023 Act often cites sections of the
    # underlying 2000 Act using the bare shorthand ``of FSMA``. Prefer the
    # URL/section context in the citation over the document field's broad
    # ``Financial Services and Markets Act 2023`` label in that case.
    if (
        re.search(r"(?:/ukpga/2000/8/|\bof\s+FSMA\b|\bFSMA,?\s*s\.?\s*[0-9]|\bsection\s+[0-9A-Za-z()]+\s+FSMA\b)", ref + " " + identifier, re.I)
        and not re.search(r"\bFSMA\s*2023\b", ref + " " + identifier, re.I)
    ):
        result = registry.by_id.get("fsma")
        if result:
            return remember(result)
    # A bare ``FSMA 2000`` label is a general statute reference.  Resolve it
    # to the Act itself before the broad alias matcher, whose longer SI titles
    # can otherwise win merely because they contain the same words.
    if re.fullmatch(r"\s*FSMA(?:\s+2000)?\s*", identifier, re.I) and not re.search(
        r"\bregulations?\b|\border\b|\bschedule\b", ref, re.I
    ):
        result = registry.by_id.get("fsma")
        if result:
            return remember(result)
    if re.search(r"\b(?:section|s\.?)\s*31P\s*\(", explicit_instrument_text, re.I):
        # SS7/24's ``s31P(5)`` is an OCR-shortened citation to section
        # 312P(5) of FSMA 2000 (the CTP provisions).
        result = registry.by_id.get("fsma")
        if result:
            return remember(result)
    if re.search(r"\bsection\s+196\s+of\s+BA09\b|\bBA09\b", explicit_instrument_text, re.I):
        result = registry.by_id.get("banking-act")
        if result:
            return remember(result)
    # A citation to CRD IV Articles 325–377 is the market-risk article range
    # carried by the CRR, not an Article 325 of Directive 2013/36 (which has
    # no such article). The existing UK CRR corpus has the authoritative text.
    if re.search(r"\bCRD\s*IV\b", explicit_instrument_text, re.I) and re.search(r"\barticles?\s+3(?:2[5-9]|[3-7][0-9]{2})\b", explicit_instrument_text, re.I):
        result = registry.by_id.get("uk-crr")
        if result:
            return remember(result)
    # The UK Solvency II Regulations 2015 are a domestic SI, not the
    # Solvency II Directive. Check the formal Regulations title before the
    # shorter generic ``Solvency II`` alias wins.
    if re.search(r"\bSolvency\s+(?:II|2)\s+Regulations?\b", explicit_instrument_text, re.I):
        result = registry.by_id.get("solvency-2-regulations")
        if result:
            return remember(result)
    # Prefer the explicit document field; matching the complete source text can
    # incorrectly select an unrelated Act mentioned later in a long sentence.
    for value in (doc, ref + " " + identifier, evidence):
        if not value:
            continue
        matches = registry.match_aliases(value)
        if matches:
            # Longest alias wins.  ``CRR`` is deliberately accepted as UK CRR
            # when no more specific instrument is present.
            matches.sort(key=lambda item: len(normalise(item[2])), reverse=True)
            return remember(matches[0][1])
    # ``Section 3D`` in the SCR-SF Rulebook Part is an internal PRA module,
    # not section 3D of FSMA.  Keep this explicit Part context ahead of the
    # shorthand statutory-section heuristics below.
    if re.search(r"(?:Solvency\s+Capital\s+Requirement\s*[–-]\s*Standard\s+Formula|\bSCR\s*[- ]\s*SF\b)", doc, re.I):
        return remember(None)
    formal_eu = re.search(
        r"\bRegulation\s*\((?:EU|EC)\)\s*(?P<no>No\.?\s*)?(?P<first>\d{4})\s*/\s*(?P<second>\d+)\b",
        explicit_instrument_text,
        re.I,
    )
    if formal_eu:
        # EU titles use both ``2015/61`` (year/number) and ``No 1187/2014``
        # (number/year).  A four-digit first component >= 1900 is
        # unambiguously a year; otherwise the components are reversed.
        first = int(formal_eu.group("first")); second = int(formal_eu.group("second"))
        if first >= 1900:
            year, number = first, second
        else:
            year, number = second, first
        known_eu = {
            (2013, 575): "uk-crr",
            (2015, 35): "solvency-ii-delegated-regulation",
            (2015, 61): "lcr-delegated-regulation",
        }
        known_id = known_eu.get((year, number))
        if known_id:
            result = registry.by_id.get(known_id)
            if result:
                return remember(result)
        return remember(Instrument(
            instrument_id=f"eur-{year}-{number}",
            title=compact(formal_eu.group(0)),
            legislation_type="eur",
            year=year,
            number=number,
            aliases=(compact(formal_eu.group(0)),),
        ))
    combined = " ".join((doc, ref, identifier, evidence, source_hint))
    if re.search(r"\b(?:UK\s+)?CRR\b", combined, re.I):
        result = registry.by_id.get("uk-crr")
        return remember(result)
    if re.search(r"\bFSMA\b", combined, re.I):
        result = registry.by_id.get("fsma")
        return remember(result)
    if re.search(r"\bSolvency\s+II\b", explicit_instrument_text, re.I) or (
        re.search(r"\bDirective\b", explicit_instrument_text, re.I)
        and re.search(r"\bSolvency\s+II\b", source_hint, re.I)
    ):
        result = registry.by_id.get("solvency-ii-directive")
        if result:
            return remember(result)
    if re.search(r"\bDirective\s*\(\s*EU\s*\)\s*2013\s*/\s*36\b", explicit_instrument_text, re.I):
        result = registry.by_id.get("crd")
        if result:
            return remember(result)
    if re.search(r"\bCRDV\b", combined, re.I):
        result = registry.by_id.get("crd")
        if result:
            return remember(result)
    # In PRA supervisory material ``the Act`` is the conventional shorthand
    # for the Financial Services and Markets Act 2000.  Treating it as FSMA
    # is necessary for a citation such as ``section 394 of the Act`` to fetch
    # the actual statutory provision rather than merely linking to a search
    # page.
    if re.fullmatch(r"(?:the\s+)?act", doc, re.I) and re.search(
        r"\b(?:sections?|s\.?|articles?|regulations?|rules?)\s*[0-9]",
        ref + " " + identifier,
        re.I,
    ):
        result = registry.by_id.get("fsma")
        return remember(result)

    # Statements frequently define an unqualified ``the Order`` once and
    # then use it for several Article citations.  Resolve that shorthand from
    # the containing provision rather than guessing between the many FSMA
    # Orders in the registry.
    if re.fullmatch(r"(?:the\s+)?order", doc, re.I) and re.search(
        r"core\s+activities\s+order|ring[- ]fenced\s+bodies\s+and\s+core\s+activities",
        source_body + " " + source_hint,
        re.I,
    ):
        result = registry.by_id.get("core-activities-order")
        if result:
            return remember(result)

    # A small set of older Acts/Rules appears in the historical Rulebook
    # corpus but is not in the current registry JSON. Their legislation.gov.uk
    # identities are stable and allow the exact section/rule text to be
    # fetched just like a registry entry.
    known_uk_instruments = (
        ("companies act 1985", "Companies Act 1985", "ukpga", 1985, 6),
        ("finance act 2012", "Finance Act 2012", "ukpga", 2012, 14),
        ("interpretation act 1978", "Interpretation Act 1978", "ukpga", 1978, 30),
        ("pensions scheme act 1993", "Pensions Schemes Act 1993", "ukpga", 1993, 48),
        ("pensions schemes act 1993", "Pensions Schemes Act 1993", "ukpga", 1993, 48),
        ("tribunal procedure upper tribunal rules 2008", "The Tribunal Procedure (Upper Tribunal) Rules 2008", "uksi", 2008, 2698),
        ("scottish and northern ireland banknote regulations 2009", "The Scottish and Northern Ireland Banknote Regulations 2009", "uksi", 2009, 3056),
    )
    combined_norm = normalise(combined)
    for identity, title, legislation_type, year, number in known_uk_instruments:
        if identity in combined_norm:
            return remember(Instrument(
                instrument_id=identity.replace(" ", "-"),
                title=title,
                legislation_type=legislation_type,
                year=year,
                number=number,
                aliases=(title,),
            ))
    if re.search(r"\bbanknote\s+regulations?\b", combined, re.I):
        return remember(Instrument(
            instrument_id="scottish-northern-ireland-banknote-regulations-2009",
            title="The Scottish and Northern Ireland Banknote Regulations 2009",
            legislation_type="uksi",
            year=2009,
            number=3056,
            aliases=("Banknote Regulations",),
        ))

    # Specific statutory shorthands used in PRA material.
    section_text = ref + " " + identifier + " " + doc
    section_ids = identifier_candidates(section_text)
    section_base = section_ids[0].split("(", 1)[0] if section_ids else ""
    if re.search(r"\b(?:sections?|s\.?)\s*[0-9]", section_text, re.I):
        if re.search(r"\b(?:1986\s+act|building\s+societ)", section_text, re.I):
            result = registry.by_id.get("building-societies-act")
            if result:
                return remember(result)
        if re.search(r"\b(?:companies\s+act\s+2006|section\s*1161)", section_text, re.I):
            result = registry.by_id.get("companies-act-2006")
            if result:
                return remember(result)
        if re.search(r"\b(?:83zr|83zs|83zt|83zu)\b", section_text, re.I):
            result = registry.by_id.get("banking-act")
            if result:
                return remember(result)
        if re.search(r"\bfinancial\s+services\s+and\s+markets\s+act\s+2023\b", section_text, re.I):
            result = registry.by_id.get("fsma-2023")
            if result:
                return remember(result)
        fsma_sections = {"2b", "2c", "2g", "2h", "3b", "3d", "3e", "59", "60a", "63f", "66b", "138ba", "144g", "169", "178", "191d", "312fa", "394", "395", "422"}
        if any(identifier.split("(", 1)[0].casefold() in fsma_sections for identifier in section_ids):
            result = registry.by_id.get("fsma")
            if result:
                return remember(result)
    if re.search(r"\bdelegated\s+(?:regulation|act)\b", doc + " " + ref, re.I):
        if re.search(r"\bliquidity\b|\bLCR\b", combined, re.I):
            result = registry.by_id.get("lcr-delegated-regulation")
            if result:
                return remember(result)
        if re.search(r"\bsolvency\b|\binsurance\b|\bISPV\b", combined, re.I):
            result = registry.by_id.get("solvency-ii-delegated-regulation")
            if result:
                return remember(result)
        if re.search(r"\bMiFID\b", combined, re.I):
            result = registry.by_id.get("modr")
            if result:
                return remember(result)
    # Some PRA source text drops the word ``No`` from the formal EU title:
    # ``Commission Implementing Regulation EU 2015/460``.  Keep this separate
    # from the generic Regulation parser so the year/number order is not
    # mistaken for an Article identifier and so provision paths can be fetched
    # from the EUR-Lex/legislation mirror.
    commission = re.search(
        r"\bCommission\s+(?:Implementing\s+)?Regulation\s*\(?(?:EU|EC)\)?\s*"
        r"(?:No\.?\s*)?(?P<year>\d{4})\s*/\s*(?P<number>\d+)\b",
        explicit_instrument_text,
        re.I,
    )
    if commission:
        year = int(commission.group("year")); number = int(commission.group("number"))
        title = compact(commission.group(0))
        return remember(Instrument(
            instrument_id=f"eur-{year}-{number}",
            title=title,
            legislation_type="eur",
            year=year,
            number=number,
            aliases=(title,),
        ))
    # The historical LLM ledger contains several EU instruments that were not
    # yet listed in the registry.  Their formal EU number is sufficient to
    # construct a stable legislation.gov.uk document identity for a document
    # link (and for provision fetching when a numbered Article follows).
    eu = re.search(r"\bRegulation\s*\((?:EU|EC)\)\s*(?:No\.?\s*)?(?P<first>\d{1,4})\s*/\s*(?P<second>\d{2,4})\b", combined, re.I)
    if eu:
        year = int(eu.group("second")) if len(eu.group("second")) == 4 else int(eu.group("first"))
        number = int(eu.group("first")) if len(eu.group("second")) == 4 else int(eu.group("second"))
        result = Instrument(
            instrument_id=f"eur-{year}-{number}",
            title=compact(eu.group(0)),
            legislation_type="eur",
            year=year,
            number=number,
            aliases=(compact(eu.group(0)),),
        )
        return remember(result)
    si = re.search(r"\bS\.?\s*I\.?\s*(?P<year>\d{4})\s*/\s*(?P<number>\d+)\b", combined, re.I)
    if si:
        year = int(si.group("year")); number = int(si.group("number"))
        result = Instrument(
            instrument_id=f"uksi-{year}-{number}",
            title=doc or compact(ref) or f"SI {year}/{number}",
            legislation_type="uksi",
            year=year,
            number=number,
            aliases=(doc, compact(ref)),
        )
        return remember(result)
    # OCR in older supervisory statements turns ``SI`` into ``S1``.  The
    # citation is the Building Societies (Mergers) Regulations 1987 (SI
    # 1987/2005); preserve the legislation identity and expose its document
    # URL rather than treating the OCR token as a Rulebook provision.
    si_ocr = re.search(r"\bS1\s*(?P<year>\d{4})\s*/\s*(?P<number>\d+)\b", explicit_instrument_text, re.I)
    if si_ocr:
        year = int(si_ocr.group("year")); number = int(si_ocr.group("number"))
        if (year, number) == (1987, 2005):
            return remember(Instrument(
                instrument_id="uksi-1987-2005",
                title="The Building Societies (Mergers) Regulations 1987",
                legislation_type="uksi",
                year=year,
                number=number,
                aliases=("S.I. 1987/2005", "Mergers Regulations 1987"),
            ))
    source_norm = normalise(source_body)
    if re.search(r"\b(?:Regulation\s*\(\s*EU\s*\)\s*No\.?\s*537\s*/\s*2014|Commission\s+Decision\s+2005\s*/\s*909\s*/\s*EC)\b", source_body, re.I):
        result = registry.by_id.get("statutory-audit-regulation")
        if result and re.search(r"\barticle\s*16\b", section_text, re.I):
            return remember(result)
    if re.search(r"Insurance\s+Accounts\s+Directive\s*\(\s*Lloyd[’']s\s+Syndicate\s+and\s+Aggregate\s+Accounts\s*\)\s+Regulations\s+2008|SI\s*2008\s*/\s*1950", source_body, re.I):
        result = Instrument(
            instrument_id="uksi-2008-1950",
            title="The Insurance Accounts Directive (Lloyd's Syndicate and Aggregate Accounts) Regulations 2008",
            legislation_type="uksi",
            year=2008,
            number=1950,
            aliases=("Insurance Accounts Directive (Lloyd's Syndicate and Aggregate Accounts) Regulations 2008",),
        )
        return remember(result)
    if re.search(r"\b1346\s*/\s*2000\s*/\s*(?:EC|EU)\b", combined, re.I):
        result = Instrument(
            instrument_id="eur-2000-1346",
            title="Council Regulation (EC) No 1346/2000 on insolvency proceedings",
            legislation_type="eur",
            year=2000,
            number=1346,
            aliases=("Regulation 1346/2000/EC",),
        )
        return remember(result)
    if re.search(r"Friendly\s+and\s+Industrial\s+and\s+Provident\s+Societies\s+Act\s+1968", source_body, re.I) and re.search(
        r"\b(?:sections?|s\.?)\s*3A\b",
        section_text + " " + source_body[:2500],
        re.I,
    ):
        result = registry.by_id.get("friendly-industrial-societies-act-1968")
        if result:
            return remember(result)
    return remember(None)


@dataclass
class Outcome:
    resolution_id: str
    source_id: str
    status: str
    scope: str
    target_id: str = ""
    target_type: str = ""
    target_title: str = ""
    target_url: str = ""
    target_text_available: bool = False
    resolver_method: str = ""
    confidence: float = 0.0
    span_start: int | None = None
    span_end: int | None = None
    quoted_text: str = ""
    instrument_id: str = ""
    provision_path: str = ""
    reason: str = ""
    fetch_key: tuple[str, str] | None = None
    fetch_instrument: Instrument | None = None
    fetch_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PolicyResolver:
    def __init__(self, conn: sqlite3.Connection, registry: InstrumentRegistry):
        self.conn = conn
        self.registry = registry
        self.nodes: dict[str, dict[str, Any]] = {}
        self.title_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.document_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.document_token_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.rule_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.chapter_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.guidance_index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.external_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        # Prefix indexes avoid rescanning the full node table for every
        # citation.  The corpus contains many snapshots, so a linear title
        # scan becomes prohibitively expensive when we re-evaluate prior
        # resolutions after a matching rule changes.
        self.provision_title_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.numbered_chapter_title_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.part_titles: list[tuple[str, str]] = []
        self.part_url_context: dict[str, str] = {
            (str(row["url"] or "").split("#", 1)[0]): normalise(row["title"] or "")
            for row in conn.execute("SELECT title,url FROM node WHERE node_type='part' AND trim(coalesce(url,''))<>''")
        }
        self._document_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        self._instrument_cache: dict[tuple[str, str, str, str], Instrument | None] = {}
        self._internal_cache: dict[tuple[str, str, tuple[str, ...], str, str], dict[str, Any] | None] = {}
        for row in conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node"):
            node = dict(row)
            node["meta"] = metadata(row)
            self.nodes[node["id"]] = node
            title_key = normalise(node["title"])
            if title_key:
                self.title_index[title_key].append(node)
            if node["node_type"] in NODE_DOCUMENT_TYPES:
                for value in context_names(row):
                    if value:
                        self.document_index[value].append(node)
                        for token in set(value.split()):
                            if len(token) >= 3:
                                self.document_token_index[token].append(node)
                if node["node_type"] in {"part", "guidance_document"} and normalise(node["title"]):
                    self.part_titles.append((normalise(node["title"]), node["id"]))
            meta = node["meta"]
            part = normalise(meta.get("part_title"))
            doc = normalise(meta.get("document_title") or meta.get("source_title"))
            if not part:
                part = self.part_url_context.get(str(node.get("url") or "").split("#", 1)[0], "")
            if node["node_type"] == "rule":
                title_key_compact = normalise(node.get("title") or "").replace(" ", "")
                title_marker = re.match(r"(?P<kind>rule)(?P<number>[0-9][0-9a-z]*)", title_key_compact, re.I)
                if title_marker and node_has_text(node):
                    self.provision_title_index[(title_marker.group("kind").casefold(), title_marker.group("number").casefold())].append(node)
                for number in (meta.get("display_number"), meta.get("rule_number"), node["title"]):
                    if number:
                        value = str(number)
                        self.rule_index[(part, normalise(value).replace(" ", ""))].append(node)
                        # The extracted citation usually keeps the legal
                        # prefix (``Article 52(1)``), while the node's
                        # display number may contain a title after it. Index
                        # the bare identifier/qualifiers too.
                        for candidate in identifier_candidates(value):
                            self.rule_index[(part, normalise(candidate).replace(" ", ""))].append(node)
                # Some PRA modules number a provision as (for example)
                # ``2.7`` while the legal citation uses the rule's named
                # identity, ``Fundamental Rule 7``.  Index the named rule
                # appearing in the source text as well, so a citation is
                # resolved to the text-bearing rule node rather than to the
                # containing chapter or left unresolved.
                for match in re.finditer(
                    r"\b(?:[A-Za-z][A-Za-z0-9'\u2019\u2011-]*\s+)?Rule\s+(?P<number>\d+)\s*:",
                    node.get("text") or "",
                    re.I,
                ):
                    self.rule_index[(part, normalise(match.group("number")).replace(" ", ""))].append(node)
            elif node["node_type"] == "chapter":
                title_key_compact = normalise(node.get("title") or "").replace(" ", "")
                title_marker = re.match(r"(?P<kind>article|regulation|section|paragraph|point|subparagraph)(?P<number>[0-9][0-9a-z]*)", title_key_compact, re.I)
                if title_marker and node_has_text(node):
                    self.provision_title_index[(title_marker.group("kind").casefold(), title_marker.group("number").casefold())].append(node)
                chapter_marker = re.match(r"(?P<number>[0-9][0-9a-z]*)", title_key_compact, re.I)
                if chapter_marker and node_has_text(node):
                    self.numbered_chapter_title_index[chapter_marker.group("number").casefold()].append(node)
                # Structured chapter/article metadata is more reliable than
                # parsing a descriptive heading (``Article 429a Exposures``
                # otherwise looks like the identifier ``429aexposures``).
                for metadata_key, provision_kind in (("article_number", "article"), ("section_number", "section"), ("regulation_number", "regulation")):
                    metadata_value = str(meta.get(metadata_key) or "")
                    if metadata_value and node_has_text(node):
                        metadata_marker = normalise(metadata_value).replace(" ", "")
                        metadata_match = re.match(r"(?:article|section|regulation)?(?P<number>[0-9][0-9a-z]*)", metadata_marker, re.I)
                        if metadata_match:
                            self.provision_title_index[(provision_kind, metadata_match.group("number").casefold())].append(node)
                metadata_chapter = normalise(str(meta.get("chapter_number") or "")).replace(" ", "")
                if metadata_chapter and node_has_text(node):
                    self.numbered_chapter_title_index[metadata_chapter.casefold()].append(node)
                for number in (meta.get("article_number"), meta.get("chapter_number"), node["title"]):
                    if number:
                        value = normalise(str(number)).replace(" ", "")
                        self.chapter_index[(part, value)].append(node)
                        for candidate in identifier_candidates(str(number)):
                            self.chapter_index[(part, normalise(candidate).replace(" ", ""))].append(node)
            elif node["node_type"] in {"guidance_paragraph", "guidance_section"}:
                key = "paragraph" if node["node_type"] == "guidance_paragraph" else "section"
                number = meta.get("paragraph_number") or meta.get("section_number")
                if number:
                    self.guidance_index[(key, doc, normalise(str(number)).replace(" ", ""))].append(node)
            instrument_id = str(meta.get("instrument_id") or "")
            provision = str(meta.get("provision_path") or "")
            if instrument_id and provision:
                self.external_index[(instrument_id, provision)].append(node)

    def provision_title_candidates(self, kind: str, identifier: str) -> list[dict[str, Any]]:
        """Return text-bearing Rulebook nodes whose title starts at a citation."""

        base = (identifier or "").split("(", 1)[0].strip()
        marker = normalise(f"{kind} {base}").replace(" ", "")
        match = re.match(r"(?P<kind>article|rule|regulation|section|paragraph|point|subparagraph)(?P<number>[0-9][0-9a-z]*)", marker, re.I)
        if not match:
            return []
        return self.provision_title_index.get((match.group("kind").casefold(), match.group("number").casefold()), [])

    def source_context(self, source: sqlite3.Row) -> tuple[str, str]:
        meta = metadata(source)
        return (
            normalise(meta.get("part_title")),
            normalise(meta.get("document_title") or meta.get("source_title") or source["title"]),
        )

    @staticmethod
    def context_keys(value: str | None) -> list[str]:
        """Return normalized context spellings used by PRA citations."""

        normalized = normalise(value)
        if not normalized:
            return []
        values = [normalized]
        # The target field frequently says ``<Part> Part`` or ``<Part> Part
        # of the PRA Rulebook`` while node metadata stores only ``<Part>``.
        stripped = re.sub(r"\s+part\s+of\s+the\s+pra\s+rulebook$", "", normalized)
        stripped = re.sub(r"\s+part$", "", stripped)
        if stripped and stripped not in values:
            values.append(stripped)
        return values

    def find_document(self, row: sqlite3.Row, source: sqlite3.Row) -> dict[str, Any] | None:
        doc = normalise(row["target_part_or_document"] or "")
        ref = normalise(row["reference_text"] or "")
        identifier = normalise(row["target_title_or_identifier"] or "")
        cache_key = (doc, identifier, ref)
        if cache_key in self._document_cache:
            return self._document_cache[cache_key]
        candidates: dict[str, dict[str, Any]] = {}
        raw_keys = [value for value in (doc, identifier, ref) if value and value not in {"unknown", "this part", "this chapter", "the part", "the chapter"}]
        keys = []
        # An explicit ``<name> Part`` is a request for the canonical Rulebook
        # Part, not for a similarly named guidance document.  Keep this
        # exact lookup separate so the later fuzzy document matching cannot
        # select an unrelated source merely because it has richer metadata.
        explicit_part_candidates: dict[str, dict[str, Any]] = {}
        for value in raw_keys:
            keys.append(value)
            # ``Own Funds (CRR) Part`` and ``Reporting Part`` are document
            # references whose canonical node title omits the word Part.
            stripped = re.sub(r"\s+part$", "", value)
            if stripped and stripped not in keys:
                keys.append(stripped)
            if re.search(r"\bpart$", value):
                for part_key in (value, stripped):
                    for node in self.title_index.get(part_key, ()):
                        if node["node_type"] == "part" and node_has_text(node):
                            explicit_part_candidates[node["id"]] = node
        if explicit_part_candidates:
            source_id = source["id"]
            explicit_part_candidates.pop(source_id, None)
            ordered_parts = sorted(
                explicit_part_candidates.values(),
                key=lambda node: len(normalise(node["title"] or "")),
                reverse=True,
            )
            if ordered_parts:
                result = ordered_parts[0]
                self._document_cache[cache_key] = result
                return result
        for key in keys:
            for node in self.title_index.get(key, ()):
                if node["node_type"] in NODE_DOCUMENT_TYPES:
                    candidates[node["id"]] = node
            for node in self.document_index.get(key, ()):
                candidates[node["id"]] = node
        # Match a named document that contains the explicit label, but avoid
        # turning a short number into a document match.
        for key in keys:
            if len(key) < 8:
                continue
            tokens = set(key.split())
            pool: dict[str, dict[str, Any]] = {}
            for token in tokens:
                if len(token) >= 3:
                    for node in self.document_token_index.get(token, ()):
                        pool[node["id"]] = node
            for node in pool.values():
                if node["node_type"] not in NODE_DOCUMENT_TYPES:
                    continue
                title = normalise(node["title"])
                if key == title or (key in title and len(tokens & set(title.split())) >= 2):
                    candidates[node["id"]] = node
        if not candidates:
            return None
        source_id = source["id"]
        candidates.pop(source_id, None)
        # Prefer a document node with text and then the most specific title.
        ordered = sorted(
            candidates.values(),
            key=lambda node: (
                int(node_has_text(node)),
                int(node["node_type"] in {"guidance_document", "part", "legal_instrument"}),
                len(node["title"] or ""),
            ),
            reverse=True,
        )
        result = ordered[0] if ordered else None
        self._document_cache[cache_key] = result
        return result

    def find_internal(self, row: sqlite3.Row, source: sqlite3.Row, kind: str, identifiers: list[str]) -> dict[str, Any] | None:
        source_part, source_doc = self.source_context(source)
        source_body = source_text(source)
        explicit_doc = normalise(row["target_part_or_document"] or "")
        raw_parent_article = re.search(
            r"\barticle\s+(?P<number>[0-9][0-9A-Za-z]*)",
            explicit_doc,
            re.I,
        )
        if explicit_doc in {"", "unknown", "this part", "this chapter", "the part", "the chapter"}:
            explicit_doc = source_part or source_doc
        if explicit_doc in {"pra rulebook", "rules", "rulebook"}:
            ref_norm = normalise(row["reference_text"] or "")
            matching_parts = []
            for title, _node_id in self.part_titles:
                if len(title) < 5:
                    continue
                variants = {title}
                # Citations often use the singular name ("Fundamental Rule")
                # while the Part is titled in the plural ("Fundamental
                # Rules"). Treat that inflection as the same source context.
                if title.endswith("s"):
                    variants.add(title[:-1])
                if any(variant in ref_norm for variant in variants):
                    matching_parts.append(title)
            if matching_parts:
                explicit_doc = max(matching_parts, key=len)
        else:
            # Named Part fields commonly carry a suffix such as ``Chapter
            # 2A``. Reduce them to the corpus' canonical Part title before
            # looking up the numbered provision.
            embedded_parts = []
            for title, _node_id in self.part_titles:
                if len(title) < 5:
                    continue
                variants = {title}
                if title.endswith("s"):
                    variants.add(title[:-1])
                if any(variant in explicit_doc for variant in variants):
                    embedded_parts.append(title)
            if embedded_parts:
                explicit_doc = max(embedded_parts, key=len)

        # Normalize the names used in supervisory statements to the current
        # Rulebook Part titles.  These aliases are deliberately evidence-led:
        # the citation text or its containing source must name the Part (or a
        # distinctive source title must identify it) before a same-numbered
        # rule can be selected.
        mapping_text = " ".join(
            (
                explicit_doc,
                normalise(row["reference_text"] or ""),
                normalise(row["target_title_or_identifier"] or ""),
                source_doc,
            )
        )
        if re.search(r"whistleblowing", mapping_text + " " + source_body[:6000], re.I) and re.search(
            r"general\s+organisational\s+requirements", mapping_text + " " + source_body[:6000], re.I
        ):
            # The Rulebook's Whistleblowing application provision points to
            # rule 2A.1 in General Organisational Requirements; the extracted
            # target label says “Whistleblowing Part” because that is the
            # table row containing the cross-reference.
            explicit_doc = "general organisational requirements"
        elif re.search(r"general\s+requirements\s+part", mapping_text, re.I) or re.search(
            r"Rule\s+5\.1\s+of\s+the\s+General\s+Organisational\s+Requirements",
            source_body,
            re.I,
        ):
            explicit_doc = "general organisational requirements"
        elif re.search(r"technical\s+provisions", explicit_doc + " " + normalise(row["reference_text"] or ""), re.I) and not re.search(r"insurance\s+company", mapping_text, re.I):
            explicit_doc = "technical provisions" if re.search(r"\b4a\s*\.?", normalise(row["target_title_or_identifier"] or "") + " " + normalise(row["reference_text"] or ""), re.I) else "technical provisions - further requirements"
        elif re.search(r"internal\s+capital\s+adequacy\s+assessment", explicit_doc + " " + normalise(row["reference_text"] or ""), re.I):
            explicit_doc = "internal capital adequacy assessment"
        elif re.search(r"pillar\s*2", mapping_text, re.I) and not re.search(r"internal\s+capital\s+adequacy\s+assessment", mapping_text, re.I):
            explicit_doc = "reporting pillar 2"
        elif re.search(r"scr[- ]sf|standard\s+formula", mapping_text + " " + source_body[:6000], re.I) and not re.search(r"internal\s+models|technical\s+provisions", mapping_text, re.I):
            explicit_doc = "solvency capital requirement - standard formula"
        elif re.search(r"internal\s+models", mapping_text + " " + source_body[:6000], re.I):
            explicit_doc = "solvency capital requirement - internal models"
        elif re.search(r"insurance\s+special\s+purpose|\bISPV\b", mapping_text, re.I):
            explicit_doc = "insurance special purpose vehicles"
        elif re.search(r"individual\s+conduct\s+standard", mapping_text, re.I):
            explicit_doc = "insurance - conduct standards"
        elif re.search(r"articles?\s+433\s*[bc]", mapping_text, re.I):
            explicit_doc = "disclosure (crr)"
        elif re.search(r"\bsecuritisation\b", mapping_text + " " + source_body[:4000], re.I) and re.search(
            r"\bchapter\s*4\b", mapping_text, re.I
        ):
            explicit_doc = "securitisation"
        elif re.search(r"resolution\s+assessment", mapping_text, re.I):
            explicit_doc = "resolution assessment"
        elif re.search(r"\bvaluation\s+part\b", source_body, re.I):
            explicit_doc = "valuation"
        elif re.search(r"\bAuditor\s+Part\b|external\s+auditors|written\s+reports\s+by\s+external\s+auditors", source_body + " " + source_doc, re.I):
            explicit_doc = "auditors"
        elif re.search(r"\bILAA\s+rules?\b", mapping_text, re.I):
            explicit_doc = "internal liquidity adequacy assessment"
        elif re.search(r"general\s+organisational\s+requirements", mapping_text + " " + source_body[:12000], re.I):
            explicit_doc = "general organisational requirements"
        elif re.search(r"group\s+solvency\s+and\s+financial\s+condition\s+report", mapping_text, re.I):
            explicit_doc = "reporting"
        explicit_contexts = self.context_keys(explicit_doc)
        source_part_contexts = self.context_keys(source_part)
        source_doc_contexts = self.context_keys(source_doc)
        cache_key = (kind, tuple(identifiers), explicit_doc, source_part, source_doc)
        if cache_key in self._internal_cache:
            return self._internal_cache[cache_key]
        # Prefer exact named titles first, which handles references such as
        # “Composites 2 and 3” where the Part name is embedded in the citation.
        exact: list[dict[str, Any]] = []
        for value in (row["target_title_or_identifier"], row["reference_text"]):
            key = normalise(value)
            for node in self.title_index.get(key, ()):
                if node["id"] != source["id"] and node["node_type"] in {"rule", "chapter", "guidance_paragraph", "guidance_section"}:
                    exact.append(node)
        if exact:
            filtered = [node for node in exact if self._context_matches(node, explicit_doc)]
            if len(filtered) == 1 and node_has_text(filtered[0]):
                self._internal_cache[cache_key] = filtered[0]
                return filtered[0]
            if len(exact) == 1 and node_has_text(exact[0]):
                self._internal_cache[cache_key] = exact[0]
                return exact[0]

        # A number of older Rulebook nodes have descriptive titles (for
        # example ``Article 428ad 50% Required Stable Funding Factor``) but
        # no structured provision metadata. Match the legal kind plus the
        # normalized identifier at the beginning of such a title before
        # falling through to an external fetch. This is especially important
        # for amended UK CRR Articles that are not addressable in the current
        # legislation.gov.uk XML but are readable in the Rulebook corpus.
        if identifiers and kind in {"article", "rule", "section", "regulation", "paragraph", "point"}:
            named_candidates: list[dict[str, Any]] = []
            for identifier in identifiers:
                marker = normalise(kind + " " + identifier).replace(" ", "")
                if not marker:
                    continue
                for node in self.provision_title_candidates(kind, identifier):
                    if node["id"] == source["id"] or node["node_type"] not in {"rule", "chapter", "guidance_paragraph", "guidance_section"}:
                        continue
                    title_key = normalise(node.get("title") or "").replace(" ", "")
                    if title_key.startswith(marker):
                        named_candidates.append(node)
            named_candidates = list({node["id"]: node for node in named_candidates}.values())
            if named_candidates:
                narrowed = [node for node in named_candidates if self._context_matches(node, explicit_doc)]
                pool = narrowed or named_candidates
                pool.sort(key=lambda node: len(node.get("text") or ""), reverse=True)
                # A duplicate title can occur across Rulebook snapshots; the
                # longest text-bearing copy preserves the most complete source
                # wording while remaining a deterministic target.
                self._internal_cache[cache_key] = pool[0]
                return pool[0]

        # Preserve every member of an internal Article list (for example
        # Articles 433b and 433c) instead of returning the first matching
        # chapter. The aggregate target keeps both source texts available in
        # the reader's reference shelf.
        if kind == "article" and len(identifiers) > 1:
            grouped: list[dict[str, Any]] = []
            for identifier in identifiers:
                marker = normalise("article " + identifier).replace(" ", "")
                candidates = [
                    node
                    for node in self.provision_title_candidates("article", identifier)
                    if node["id"] != source["id"]
                    and node["node_type"] in {"rule", "chapter"}
                    and node_has_text(node)
                    and normalise(node.get("title") or "").replace(" ", "").startswith(marker)
                    and (self._context_matches(node, source_part) or self._context_matches(node, explicit_doc))
                ]
                if candidates:
                    candidates.sort(key=lambda node: len(node.get("text") or ""), reverse=True)
                    grouped.append(candidates[0])
            aggregate = self.create_aggregate_internal_target(source_part or explicit_doc, grouped)
            if aggregate is not None:
                self._internal_cache[cache_key] = aggregate
                return aggregate

        # A citation spanning several provisions (``Rules 7A to 7D`` or
        # ``Articles 30, 30a and 30b``) cannot be represented by one child
        # node without dropping part of the cited text.  Use the containing
        # text-bearing Part when it contains the complete range.
        if kind != "rule" and len(identifiers) > 1 and re.search(r"\bto\b|,|\band\b", row["reference_text"] or "", re.I):
            def text_identifier(value: str) -> str:
                # The Rulebook corpus flattens qualified identifiers in
                # aggregate Part text (``433(b)`` -> ``433b``), while node
                # metadata retains punctuation.  Compare a punctuation-free
                # key so list/range citations still find the full source.
                return re.sub(r"[^a-z0-9]", "", normalise(value))

            for context in explicit_contexts:
                range_nodes = [
                    node for node in self.document_index.get(context, ())
                    if node["node_type"] == "part" and node_has_text(node)
                ]
                for node in range_nodes:
                    normalized_text = text_identifier(node.get("text") or "")
                    if all(text_identifier(identifier) in normalized_text for identifier in identifiers):
                        self._internal_cache[cache_key] = node
                        return node

        # ``Article 6(1) of Chapter 2`` names the containing Chapter rather
        # than a unique child Article node. Prefer that chapter's readable
        # aggregate text, which preserves the requested Article and its
        # surrounding legal context.
        chapter_match = re.search(r"\bchapter\s+(?P<number>[0-9A-Za-z]+)", row["reference_text"] or "", re.I)
        if chapter_match:
            chapter_key = normalise(chapter_match.group("number")).replace(" ", "")
            requested = [normalise(identifier).replace(" ", "") for identifier in identifiers]
            for context in list(dict.fromkeys(explicit_contexts + source_part_contexts)):
                chapters = list({node["id"]: node for node in self.chapter_index.get((context, chapter_key), [])}.values())
                for node in chapters:
                    if not node_has_text(node):
                        continue
                    text_key = normalise(node.get("text") or "").replace(" ", "")
                    if any(item and item in text_key for item in requested):
                        self._internal_cache[cache_key] = node
                        return node

        # Prefer exact Rulebook rule/chapter children before the guidance
        # document fallback below.  Guidance paragraphs routinely mention a
        # PRA Part rule (``Own Funds 2.3`` or ``Group Supervision 8A``); the
        # containing statement is not the cited provision and must not hide
        # the text-bearing Rulebook node.
        if kind == "rule" and identifiers:
            grouped_rules: list[dict[str, Any]] = []
            for identifier in identifiers:
                identifier_key = normalise(identifier).replace(" ", "")
                lookup_keys = [identifier_key]
                dotted = identifier.split(".")
                if len(dotted) >= 3:
                    lookup_keys.append(normalise(".".join(dotted[:2])).replace(" ", ""))

                # ``normalise`` intentionally removes punctuation for broad
                # text matching, so the compact index key ``11`` also holds
                # Rule 11.1 (and ``31`` can hold both 3.1 and 31).  Before
                # selecting from that index, compare the structured rule
                # number with the requested dotted/alphanumeric identifier.
                # This prevents a chapter/rule citation from silently landing
                # on a different provision that merely shares its digits.
                expected_number = re.sub(
                    r"\s+", "", (identifier.split("(", 1)[0] or "").casefold()
                )

                def exact_rule_number(node: dict[str, Any]) -> bool:
                    values = (
                        node.get("meta", {}).get("rule_number"),
                        node.get("meta", {}).get("display_number"),
                        node.get("title"),
                    )
                    for value in values:
                        candidate = re.sub(r"\s+", "", str(value or "").split("(", 1)[0].casefold())
                        if candidate == expected_number:
                            return True
                    return False

                candidates: list[dict[str, Any]] = []
                for context in explicit_contexts:
                    for lookup_key in lookup_keys:
                        candidates.extend(
                            node for node in self.rule_index.get((context, lookup_key), [])
                            if node["id"] != source["id"]
                            and node_has_text(node)
                            and exact_rule_number(node)
                        )
                # A bare chapter number (``Composites 2 and 3``) is exposed as
                # a text-bearing chapter aggregate rather than a blank Part.
                if not candidates:
                    for context in explicit_contexts:
                        for lookup_key in lookup_keys:
                            chapters = [
                                node for node in self.chapter_index.get((context, lookup_key), [])
                                if node["id"] != source["id"]
                            ]
                            for chapter in chapters:
                                if node_has_text(chapter):
                                    candidates.append(chapter)
                                    continue
                                children: list[dict[str, Any]] = []
                                chapter_part = normalise(chapter.get("meta", {}).get("part_title") or context)
                                for (indexed_part, indexed_number), indexed_nodes in self.rule_index.items():
                                    if indexed_part != chapter_part or not indexed_number.startswith(lookup_keys[0]):
                                        continue
                                    children.extend(node for node in indexed_nodes if node["id"] != source["id"] and node_has_text(node))
                                aggregate = self.create_aggregate_internal_target(chapter_part, children)
                                if aggregate is not None:
                                    candidates.append(aggregate)
                candidates = list({node["id"]: node for node in candidates}.values())
                if candidates:
                    narrowed = [node for node in candidates if self._context_matches(node, explicit_doc)]
                    pool = narrowed or candidates
                    pool.sort(key=lambda node: len(node.get("text") or ""), reverse=True)
                    grouped_rules.append(pool[0])
            grouped_rules = list({node["id"]: node for node in grouped_rules}.values())
            if grouped_rules:
                aggregate = self.create_aggregate_internal_target(explicit_doc, grouped_rules)
                target = aggregate or grouped_rules[0]
                self._internal_cache[cache_key] = target
                return target

        # Guidance references sometimes use ``rule`` in the extracted kind
        # even though the named source is a supervisory statement.  The
        # document node is the authoritative readable source in that case.
        if kind in {"rule", "paragraph", "section"}:
            # Prefer the exact text-bearing paragraph/section when the named
            # supervisory statement is in the Rulebook corpus.  This must run
            # before the document fallback below; otherwise a specific
            # citation such as ``paragraph 4.10 of SS8/18`` is reduced to the
            # whole statement.
            if kind in {"paragraph", "section"}:
                for identifier in identifiers:
                    key = normalise(identifier).replace(" ", "")
                    candidates = []
                    for context in list(dict.fromkeys(explicit_contexts + source_doc_contexts)):
                        candidates.extend(self.guidance_index.get((kind, context, key), []))
                    candidates = list({node["id"]: node for node in candidates if node["id"] != source["id"] and node_has_text(node)}.values())
                    if len(candidates) == 1:
                        self._internal_cache[cache_key] = candidates[0]
                        return candidates[0]
                    # The extractor sometimes drops the SS/SoP code from a
                    # target document (for example ``The PRA's approach ...``
                    # instead of ``SoP1/20 – The PRA's approach ...``). Match
                    # the remaining title by token overlap, but require at
                    # least three shared tokens so a generic phrase cannot
                    # select an unrelated statement with the same paragraph
                    # number.
                    target_tokens = set(explicit_doc.split())
                    fuzzy_scored: list[tuple[int, dict[str, Any]]] = []
                    if len(target_tokens) >= 3:
                        for (indexed_kind, indexed_doc, indexed_key), indexed_nodes in self.guidance_index.items():
                            if indexed_kind != kind or indexed_key != key:
                                continue
                            indexed_tokens = set(indexed_doc.split())
                            overlap = target_tokens & indexed_tokens
                            if len(overlap) >= 3 and (
                                target_tokens <= indexed_tokens
                                or indexed_doc in explicit_doc
                                or len(overlap) >= max(3, min(len(target_tokens), len(indexed_tokens)) // 2)
                            ):
                                fuzzy_scored.extend((len(overlap), node) for node in indexed_nodes)
                    if fuzzy_scored:
                        best_score = max(score for score, _node in fuzzy_scored)
                        fuzzy = list({node["id"]: node for score, node in fuzzy_scored if score == best_score and node["id"] != source["id"] and node_has_text(node)}.values())
                    else:
                        fuzzy = []
                    if fuzzy:
                        fuzzy.sort(key=lambda node: len(node.get("text") or ""), reverse=True)
                        self._internal_cache[cache_key] = fuzzy[0]
                        return fuzzy[0]
            guidance_docs = []
            for context in explicit_contexts:
                guidance_docs.extend(
                    node for node in self.document_index.get(context, ())
                    if node["node_type"] == "guidance_document" and node_has_text(node)
                )
            if guidance_docs:
                # The corpus may contain both a Rulebook-rendered paragraph
                # and a PDF-page extraction for the same guidance number.
                # Both carry source text; prefer the canonical Rulebook URL,
                # then the most complete text-bearing copy.
                guidance_docs.sort(
                    key=lambda node: (
                        int("prarulebook.co.uk" in (node.get("url") or "")),
                        len(node.get("text") or ""),
                    ),
                    reverse=True,
                )
                self._internal_cache[cache_key] = guidance_docs[0]
                return guidance_docs[0]
            # When the extractor names a subsection that is present only in
            # the publisher's full guidance document (for example a PDF
            # heading such as “Settlement discount 5.25”), the document text
            # is still the authoritative readable source. Keep it as a
            # provision-scope target rather than losing the citation.
            if source["node_type"] in {"guidance_paragraph", "guidance_section"}:
                source_guidance_docs = [
                    node
                    for context in source_doc_contexts
                    for node in self.document_index.get(context, ())
                    if node["id"] != source["id"]
                    and node["node_type"] == "guidance_document"
                    and node_has_text(node)
                ]
                if source_guidance_docs:
                    source_guidance_docs.sort(key=lambda node: len(node.get("text") or ""), reverse=True)
                    self._internal_cache[cache_key] = source_guidance_docs[0]
                    return source_guidance_docs[0]

        # Lists of qualified Rulebook numbers (for example 6.3.3, 6.3.4,
        # 6.4.3 and 6.4.4) refer to subparagraphs of their two base rules.
        # The corpus stores the base rules as text-bearing nodes, so combine
        # those nodes instead of resolving only the first number.
        if kind == "rule" and len(identifiers) > 1:
            grouped: list[dict[str, Any]] = []
            for identifier in identifiers:
                identifier_key = normalise(identifier).replace(" ", "")
                lookup_keys = [identifier_key]
                dotted = identifier.split(".")
                if len(dotted) >= 3:
                    lookup_keys.append(normalise(".".join(dotted[:2])).replace(" ", ""))
                candidates: list[dict[str, Any]] = []
                for context in explicit_contexts:
                    for lookup_key in lookup_keys:
                        candidates.extend(self.rule_index.get((context, lookup_key), []))
                if not candidates:
                    for context in source_part_contexts:
                        for lookup_key in lookup_keys:
                            candidates.extend(self.rule_index.get((context, lookup_key), []))
                unique = list({node["id"]: node for node in candidates if node["id"] != source["id"] and node_has_text(node)}.values())
                if len(unique) == 1:
                    grouped.append(unique[0])
            aggregate = self.create_aggregate_internal_target(explicit_doc, grouped)
            if aggregate is not None:
                self._internal_cache[cache_key] = aggregate
                return aggregate

        key = ""
        for identifier in identifiers:
            key = normalise(identifier).replace(" ", "")
            lookup_keys = [key]
            # OCR/PDF extraction occasionally flattens a qualified rule such
            # as 2.3(1)(e) into ``2.31(e)``.  The current Notifications Part
            # contains the authoritative text under rule 2.3; its paragraph
            # (1)(e) is retained in that node's text.
            if key.startswith("231"):
                lookup_keys.append("23")
            # A qualified Rulebook reference such as 6.3.3 usually denotes a
            # subparagraph of base rule 6.3; rule nodes are indexed by that
            # base number, not by every nested paragraph.
            dotted_identifier = identifier.split(".")
            if kind == "rule" and len(dotted_identifier) >= 3:
                lookup_keys.append(normalise(".".join(dotted_identifier[:2])).replace(" ", ""))
            # EU/CRR article lettering is represented as ``433b`` in the
            # corpus while citations commonly write ``433(b)``.
            if kind == "article":
                compact_article = re.sub(r"\(([^)]*)\)", r"\1", key)
                if compact_article != key:
                    lookup_keys.append(compact_article)
            if kind == "rule":
                candidates = []
                for context in explicit_contexts:
                    for lookup_key in lookup_keys:
                        candidates.extend(self.rule_index.get((context, lookup_key), []))
                if explicit_doc == "auditors" and key == "711":
                    # SS1/16 records the historical typo ``Rule 7.11`` and
                    # immediately says it was amended to current Rule 7.1.
                    # The latter is the authoritative text-bearing rule in
                    # the current Rulebook.
                    for context in explicit_contexts:
                        candidates.extend(self.rule_index.get((context, "71"), []))
                if not candidates:
                    for context in source_part_contexts:
                        for lookup_key in lookup_keys:
                            candidates.extend(self.rule_index.get((context, lookup_key), []))
                # Some modules expose a numbered provision as a text-bearing
                # Chapter (for example SCR-SF 3D21) rather than a rule child.
                if not candidates:
                    for context in list(dict.fromkeys(explicit_contexts + source_part_contexts)):
                        for lookup_key in lookup_keys:
                            candidates.extend(
                                node for node in self.chapter_index.get((context, lookup_key), [])
                                if node_has_text(node)
                            )
                if not candidates:
                    # SCR-SF and a few other PRA modules publish numbered
                    # provisions as text-bearing Chapter nodes whose
                    # structured chapter number is blank (for example
                    # ``3D14 Calculation Of The Equity Index``). Match the
                    # identifier at the start of the chapter title within
                    # the named Part before attempting an external statute.
                    for lookup_key in lookup_keys:
                        for node in self.numbered_chapter_title_index.get(lookup_key.casefold(), ()):
                            if node["id"] != source["id"] and self._context_matches(node, explicit_doc):
                                candidates.append(node)
                if not candidates:
                    # A bare chapter citation such as “Overall Resources ...
                    # 6” has no child rule numbered exactly “6”; the readable
                    # source is the chapter's text-bearing 6.1/6.2 children.
                    # Build a deterministic aggregate so the reader still
                    # opens the complete cited chapter wording.
                    for context in list(dict.fromkeys(explicit_contexts + source_part_contexts)):
                        chapters = [
                            node for node in self.chapter_index.get((context, key), [])
                            if node["id"] != source["id"]
                        ]
                        for chapter in chapters:
                            if node_has_text(chapter):
                                candidates.append(chapter)
                                continue
                            chapter_part = normalise(chapter.get("meta", {}).get("part_title") or context)
                            children: list[dict[str, Any]] = []
                            for (indexed_part, indexed_number), indexed_nodes in self.rule_index.items():
                                if indexed_part != chapter_part or not (
                                    indexed_number == key or indexed_number.startswith(key)
                                ):
                                    continue
                                children.extend(node for node in indexed_nodes if node["id"] != source["id"] and node_has_text(node))
                            unique_children = list({node["id"]: node for node in children}.values())
                            if len(unique_children) == 1:
                                candidates.extend(unique_children)
                            else:
                                aggregate = self.create_aggregate_internal_target(chapter_part, unique_children)
                                if aggregate is not None:
                                    candidates.append(aggregate)
                candidates = list({node["id"]: node for node in candidates if node["id"] != source["id"] and node_has_text(node)}.values())
                if len(candidates) == 1:
                    self._internal_cache[cache_key] = candidates[0]
                    return candidates[0]
                if len(candidates) > 1:
                    # Prefer an exact explicit Part/document context.
                    narrowed = [node for node in candidates if self._context_matches(node, explicit_doc)]
                    if len(narrowed) == 1:
                        self._internal_cache[cache_key] = narrowed[0]
                        return narrowed[0]
            elif kind == "article":
                # A qualified Article is a rule; the unqualified Article is a
                # chapter heading.  Both have readable source text in the app.
                parent_article = re.search(r"\barticle\s+(?P<number>[0-9][0-9A-Za-z]*)", explicit_doc, re.I) or raw_parent_article
                if parent_article and any(re.fullmatch(r"\s*[A-Z]\d+(?:\s*\([^)]*\))*\s*", value or "", re.I) for value in identifiers):
                    parent_marker = normalise("article " + parent_article.group("number")).replace(" ", "")
                    parent_candidates = [
                        node
                        for node in self.provision_title_candidates("article", parent_article.group("number"))
                        if node["id"] != source["id"]
                        and node["node_type"] in {"rule", "chapter"}
                        and node_has_text(node)
                        and normalise(node.get("title") or "").replace(" ", "").startswith(parent_marker)
                        and self._context_matches(node, explicit_doc)
                    ]
                    if parent_candidates:
                        parent_candidates.sort(key=lambda node: len(node.get("text") or ""), reverse=True)
                        self._internal_cache[cache_key] = parent_candidates[0]
                        return parent_candidates[0]
                candidates = []
                for context in explicit_contexts:
                    for lookup_key in lookup_keys:
                        for node in self.rule_index.get((context, lookup_key), []) + self.chapter_index.get((context, lookup_key), []):
                            if node["id"] != source["id"] and node_has_text(node):
                                candidates.append(node)
                if not candidates:
                    for context in source_part_contexts:
                        for lookup_key in lookup_keys:
                            for node in self.rule_index.get((context, lookup_key), []) + self.chapter_index.get((context, lookup_key), []):
                                if node["id"] != source["id"] and node_has_text(node):
                                    candidates.append(node)
                candidates = list({node["id"]: node for node in candidates}.values())
                if candidates:
                    # Qualified references should prefer rules; bare Articles
                    # should prefer chapters.
                    qualified = "(" in identifier
                    preferred = [node for node in candidates if (node["node_type"] == "rule") == qualified]
                    if len(preferred) == 1:
                        self._internal_cache[cache_key] = preferred[0]
                        return preferred[0]
                    if len(candidates) == 1:
                        self._internal_cache[cache_key] = candidates[0]
                        return candidates[0]
            elif kind in {"paragraph", "section"}:
                candidates = []
                for context in explicit_contexts:
                    candidates.extend(self.guidance_index.get((kind, context, key), []))
                if not candidates:
                    for context in source_doc_contexts:
                        candidates.extend(self.guidance_index.get((kind, context, key), []))
                candidates = list({node["id"]: node for node in candidates if node["id"] != source["id"] and node_has_text(node)}.values())
                if len(candidates) == 1:
                    self._internal_cache[cache_key] = candidates[0]
                    return candidates[0]
                # Some extracted PDF paragraphs are stored as rule nodes in a
                # Rulebook Part rather than guidance paragraphs.
                if kind == "paragraph":
                    candidates = []
                    for context in explicit_contexts:
                        candidates.extend(node for node in self.rule_index.get((context, key), []) if node["id"] != source["id"] and node_has_text(node))
                    candidates = list({node["id"]: node for node in candidates}.values())
                    if len(candidates) == 1:
                        self._internal_cache[cache_key] = candidates[0]
                        return candidates[0]
                if kind == "section":
                    # Some Rulebook exports label internal rules as
                    # “sections” in the reference ledger. Reuse the same
                    # text-bearing rule index before falling back to a
                    # document-only target.
                    candidates = []
                    for context in list(dict.fromkeys(explicit_contexts + source_part_contexts)):
                        candidates.extend(
                            node for node in self.rule_index.get((context, key), [])
                            if node["id"] != source["id"] and node_has_text(node)
                        )
                    candidates = list({node["id"]: node for node in candidates}.values())
                    if len(candidates) == 1:
                        self._internal_cache[cache_key] = candidates[0]
                        return candidates[0]
        # If the corpus has not split a named PRA rule into a child node, the
        # canonical Part still contains its full source text. This is safer
        # than selecting a same-numbered rule from another Part.
        for context in list(dict.fromkeys(explicit_contexts + source_part_contexts)):
            for node in self.document_index.get(context, ()):
                if node["node_type"] != "part" or not node_has_text(node):
                    continue
                normalized_text = normalise(node.get("text") or "").replace(" ", "")
                if key and key in normalized_text:
                    self._internal_cache[cache_key] = node
                    return node
                # A superseded Rulebook child can disappear from the current
                # snapshot while the surrounding Part remains the only
                # readable official source (for example Reporting 3A.7A(1)).
                # If the citing source explicitly names that Part, retain the
                # complete text-bearing Part instead of dropping the link.
                if source["node_type"] in {"guidance_paragraph", "guidance_section"} and explicit_doc == context:
                    self._internal_cache[cache_key] = node
                    return node
        self._internal_cache[cache_key] = None
        return None

    def _context_matches(self, node: dict[str, Any], context: str) -> bool:
        if not context:
            return True
        values = context_names(node)
        contexts = self.context_keys(context)
        node_contexts = []
        for value in values:
            node_contexts.extend(self.context_keys(value))
        return any(
            left and right and (left == right or left in right or right in left)
            for left in contexts
            for right in node_contexts
        )

    def create_document_target(
        self,
        row: sqlite3.Row,
        source: sqlite3.Row,
        instrument: Instrument | None,
    ) -> dict[str, Any] | None:
        # First, use a source-document node already present in the graph.
        doc = compact(row["target_part_or_document"] or "")
        ref = compact(row["reference_text"] or "")
        identifier = compact(row["target_title_or_identifier"] or "")

        # ``find_document`` can see a legacy node created from a longer SI
        # alias that happened to mention FSMA 2000.  For a bare statute title,
        # the registry's Act identity and official contents URL are the
        # canonical document target; do not let that stale alias title leak
        # into the reader.
        if instrument is not None and instrument.instrument_id == "fsma" and re.fullmatch(
            r"\s*FSMA(?:\s+2000)?\s*", identifier, re.I
        ):
            fsma_url = instrument.official_url or f"{instrument.base_url}/contents"
            fsma_id = "external:document:" + digest(fsma_url, length=24)
            fsma_existing = self.nodes.get(fsma_id)
            if fsma_existing is not None:
                canonical = dict(fsma_existing)
                canonical["title"] = instrument.title
                canonical["meta"] = dict(canonical.get("meta") or {})
                canonical["meta"].update({
                    "external_reference_label": instrument.title,
                    "source_url": fsma_url,
                    "href": fsma_url,
                })
                canonical["metadata_json"] = json.dumps(canonical["meta"], ensure_ascii=False, sort_keys=True)
                self.nodes[fsma_id] = canonical
                return canonical
        existing = self.find_document(row, source)
        # A previous fallback could have created a legislation.gov.uk search
        # node for an EBA/PRA guidance title simply because the title mentions
        # a Directive. Prefer the publisher's own guidance URL when the name
        # is unambiguously guidance; do not preserve that stale search target.
        guidance_name = bool(
            re.search(
                r"(?:\bSS\s*\d|\bSoP\b|supervisory\s+statement|statement\s+of\s+policy|guidelines?|guidance|consultation|approach|\bEBA\b)",
                " ".join((doc, ref, identifier)),
                re.I,
            )
        )
        stale_guidance_search = bool(
            existing is not None
            and guidance_name
            and re.search(r"(?:legislation\.gov\.uk/|eba\.europa\.eu/search|prarulebook\.co\.uk/search)", existing.get("url") or "", re.I)
        )
        if existing is not None and not stale_guidance_search and (existing.get("url") or node_has_text(existing)):
            return existing
        source_meta = metadata(source)
        source_document_title = compact(
            source_meta.get("document_title") or source_meta.get("source_title") or ""
        )
        # When a guidance paragraph cites “the document containing the
        # statements” (or an ICAA/Annex paragraph), the containing guidance
        # document is already in the corpus even though the extracted target
        # field is generic.  Link that text-bearing document directly.
        if source_document_title and re.search(
            r"\b(?:document|statement[s]?|ICAA|annex|chapter)\b|\bthis\s+(?:SS|statement|guidance)\b",
            " ".join((doc, ref, identifier)),
            re.I,
        ):
            for node in self.title_index.get(normalise(source_document_title), ()):
                if node["id"] != source["id"] and node["node_type"] in {"guidance_document", "external_reference", "part"} and (node.get("url") or node_has_text(node)):
                    return node
        url_match = re.search(r"https?://[^\s)\]>]+", " ".join((ref, identifier, row["evidence_quote"] or "")))
        url = url_match.group(0).rstrip(".,;") if url_match else ""
        if not url and re.search(r"Scottish\s+and\s+Northern\s+Ireland\s+Banknote\s+Rules\s+2017", " ".join((doc, ref, identifier)), re.I):
            url = "https://www.bankofengland.co.uk/-/media/boe/files/banknotes/scottish-northern-ireland/scottish-and-northern-ireland-banknote-rules-2017.pdf"
        if not url and re.search(r"Critical\s+Third\s+Part(?:y|ies)\s+Instrument\s+2024", " ".join((doc, ref, identifier)), re.I):
            # Appendix 3 is the final PRA Rulebook instrument; the Bank's
            # consolidated FMI instrument is also useful when the citation
            # explicitly says “Bank of England Rulebook”.
            url = (
                "https://www.bankofengland.co.uk/-/media/boe/files/prudential-regulation/"
                "policy-statement/2024/november/ps1624app3.pdf"
                if re.search(r"PRA\s+Rulebook", " ".join((doc, ref, identifier)), re.I)
                else "https://www.bankofengland.co.uk/-/media/boe/files/financial-stability/financial-market-infrastructure-supervision/critical-third-parties/final-ctp-rule-instrument.pdf"
            )
        if not url and re.search(r"\bListing\s+Rules?\s+9\.27\b", " ".join((doc, ref, identifier)), re.I):
            url = "https://www.fca.org.uk/publication/ukla/listing-rules-april-2002.pdf"
        if not url and re.search(r"\bS\.?\s*166\s+report\b|\bsection\s+166\b", " ".join((doc, ref, identifier)), re.I):
            # ``S166 report`` is a general reference to the FSMA supervisory
            # tool, not a citation to a numbered paragraph in the report.
            # Link it to the authoritative statutory source document already
            # used elsewhere in the graph.
            url = "https://www.legislation.gov.uk/ukpga/2000/8/section/166"
        if not url and instrument is not None:
            url = instrument.official_url or f"{instrument.base_url}/contents"
        if not url and doc and not guidance_name:
            # A document title that is not in the local corpus still gets a
            # stable official-source search link rather than remaining an
            # unclassified blank target.  Exact statutory/instrument titles
            # are handled by the legislation.gov.uk search endpoint.
            if re.search(r"\b(?:act|regulations?|directive|order|statute|handbook|code)\b", doc, re.I):
                url = "https://www.legislation.gov.uk/all?title=" + quote_plus(doc)
        if not url:
            # Named guidance and supervisory documents are not always present
            # as first-class nodes (especially older statements and approaches),
            # but they still need a usable source link under the resolution
            # policy.  Prefer the relevant publisher's own search endpoint.
            search_text = compact(" ".join(value for value in (doc, identifier, ref) if value))
            if search_text and len(search_text) >= 8:
                if re.search(r"\bEBA\b.*\bGuidelines?\b.*\binternal\s+governance\b", search_text, re.I):
                    # EBA/GL/2017/11 is a named external guidance instrument;
                    # use its official PDF rather than a search-results page
                    # so the reader also has the source text locally.
                    url = "https://www.eba.europa.eu/sites/default/files/documents/10180/1972987/eb859955-614a-4afb-bdcd-aaa664994889/Final%20Guidelines%20on%20Internal%20Governance%20%28EBA-GL-2017-11%29.pdf"
                elif re.search(r"\b(?:PRA|Prudential Regulation Authority|PRA[- ]authorised|supervisory)\b", search_text, re.I):
                    url = "https://www.prarulebook.co.uk/search?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:FCA|Financial Conduct Authority)\b", search_text, re.I):
                    url = "https://www.handbook.fca.org.uk/search?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:Bank of England|BoE)\b", search_text, re.I):
                    url = "https://www.bankofengland.co.uk/search?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:EBA|European Banking Authority)\b", search_text, re.I):
                    url = "https://www.eba.europa.eu/search?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:EIOPA|European Insurance and Occupational Pensions Authority|SFCR)\b", search_text, re.I):
                    url = "https://www.eiopa.europa.eu/search_en?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:IAS\s*\d+|IFRS)\b", search_text, re.I):
                    url = "https://www.ifrs.org/search/?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:GL\b|EBA\s+Guidelines?|PD\s*&\s*LGD)\b", search_text, re.I):
                    url = "https://www.eba.europa.eu/search?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:Parliamentary|Changing\s+Banking\s+for\s+Good|BSA\b|Model\s+Rules)\b", search_text, re.I):
                    url = "https://www.bankofengland.co.uk/search?query=" + quote_plus(search_text)
                elif re.search(r"\b(?:IFPR|implementation\s+observations|concluding\s+report|CP\s*\d+[/ ]\d+|Annex\s+\d+)\b", search_text, re.I):
                    url = "https://www.prarulebook.co.uk/search?query=" + quote_plus(search_text)
                elif re.search(r"\bListing\s+Rules?\b", search_text, re.I):
                    url = "https://www.fca.org.uk/publication/ukla/listing-rules-april-2002.pdf"
                elif re.search(r"\b(?:Basel Committee|BIS|Financial Stability Board|FSB)\b", search_text, re.I):
                    url = "https://www.bis.org/search/?q=" + quote_plus(search_text)
                elif re.search(r"\b(?:ISA\b|International Standard[s]? on Auditing|IAASB)\b", search_text, re.I):
                    url = "https://www.iaasb.org/search?query=" + quote_plus(search_text)
                elif re.search(
                    r"\b(?:guidelines?|guidance|statement[s]?\s+of\s+policy|supervisory|approach|handbook|template[s]?|report|standard[s]?|code|SS\s*\d+[/ ]\d+|SoP\s*\d*[/ ]?\d*|FiR|ViR|MGC|FMI|GL\b|SFCR|IAS\s*\d+|IFPR|Annex\s+\d+|BSA\b|Parliamentary|Listing\s+Rules?)\b",
                    search_text,
                    re.I,
                ):
                    url = "https://www.prarulebook.co.uk/search?query=" + quote_plus(search_text)
        if not url:
            # A reference to an unnamed document cannot safely become a target.
            # It is a false-positive/documentary mention rather than a missing
            # provision target.
            return None
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        title = doc if doc and doc.casefold() not in {"unknown", "the part", "the chapter"} else (identifier or ref or url)
        node_id = "external:document:" + digest(url, length=24)
        if node_id in self.nodes:
            return self.nodes[node_id]
        document_text = ""
        if re.search(r"(?:ps1624app3|final-ctp-rule-instrument|listing-rules-april-2002)\.pdf(?:$|[?#])", url, re.I) or re.search(r"EBA-GL-2017-11.*\.pdf(?:$|[?#])", url, re.I):
            document_text = special_document_text(url)
        node = {
            "id": node_id,
            "node_type": "external_reference",
            "stable_key": node_id,
            "title": title,
            "text": document_text,
            "url": url,
            "metadata_json": json.dumps({
                "external_reference_label": title,
                "href": url,
                "placeholder": False,
                "resolution_basis": "resolution_policy_document_link",
                "document_only": True,
                "source_url": url,
                "source_text_available": bool(document_text),
            }, ensure_ascii=False, sort_keys=True),
            "meta": {
                "external_reference_label": title,
                "href": url,
                "placeholder": False,
                "resolution_basis": "resolution_policy_document_link",
                "document_only": True,
                "source_url": url,
                "source_text_available": bool(document_text),
            },
        }
        self.nodes[node_id] = node
        self.title_index[normalise(title)].append(node)
        self.document_index[normalise(title)].append(node)
        return node

    def create_fca_handbook_provision_target(
        self,
        row: sqlite3.Row,
        source: sqlite3.Row,
    ) -> dict[str, Any] | None:
        """Materialise a specific FCA Handbook/COBS provision.

        The FCA Handbook is published as an official consolidated PDF rather
        than legislation.gov.uk XML.  A citation such as ``COBS 20.2.55R`` is
        still a specific external rule under the resolution policy, so a
        search-result hyperlink is insufficient. Extract the requested rule
        blocks from the FCA's current sourcebook PDF (COBS, SUP, SYSC, GEN or
        COLL) and expose those blocks as a readable target (or one aggregate
        target for a cited list).
        """

        combined = " ".join(
            compact(value or "")
            for value in (
                row["reference_text"],
                row["target_title_or_identifier"],
                row["target_part_or_document"],
                row["evidence_quote"],
            )
        )
        sourcebook_match = re.search(
            r"\b(?P<code>COBS|SUP|SYSC|GEN|COLL)(?:\s+(?P<qualifier>TP))?(?=\s*[0-9])|Conduct\s+of\s+Business\s+Sourcebook",
            combined,
            re.I,
        )
        if sourcebook_match is None:
            return None
        sourcebook_code = (sourcebook_match.group("code") or "COBS").upper()
        qualifier = (sourcebook_match.group("qualifier") or "").upper()
        sourcebook = f"{sourcebook_code} {qualifier}" if qualifier else sourcebook_code
        # The PDF calls the transitional provisions ``SUP TP``.  Keep the
        # two-word label when matching headings, while using ``SUP.pdf`` as
        # the official source document.
        sourcebook_re = r"SUP\s+TP" if sourcebook == "SUP TP" else re.escape(sourcebook)

        # Prefer explicitly repeated ``COBS`` labels.  When the extractor
        # abbreviates a list (``COBS 20.2.55R and 56R``), carry the same
        # chapter prefix to the trailing identifier.
        citations = re.findall(
            rf"\b{sourcebook_re}\s*(?P<number>[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)+[A-Za-z]*)",
            combined,
            re.I,
        )
        for prefix, leaf, tail in re.findall(
            rf"\b{sourcebook_re}\s*(?P<prefix>[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)\.(?P<leaf>[0-9]+[A-Za-z]*)\s*(?:R|G|UK)?\s*(?:and|,|to)\s*(?P<tail>[0-9]+[A-Za-z]*)(?![0-9A-Za-z]*\.)",
            combined,
            re.I,
        ):
            # The first regex already captures the full first citation; this
            # adds an abbreviated second member such as ``56R``.
            citations.append(f"{prefix}.{leaf}")
            citations.append(f"{prefix}.{tail}")

        # Also recover fully qualified second members from fields where the
        # extractor omitted the repeated COBS label in the prose.
        if citations:
            chapter_prefix = citations[0].rsplit(".", 1)[0]
            for tail in re.findall(r"(?:\band|,|to)\s+([0-9]+[A-Za-z]?)(?!\.)\b", combined, re.I):
                if f"{chapter_prefix}.{tail}" not in citations:
                    citations.append(f"{chapter_prefix}.{tail}")
        citations = list(dict.fromkeys(citations))
        if not citations:
            return None

        pdf_url = f"https://api-handbook.fca.org.uk/files/sourcebook/{sourcebook_code}.pdf"
        handbook_text = special_document_text(pdf_url)
        if not handbook_text:
            return None

        # FCA's PDF extraction uses two layouts.  Ordinary rules start with a
        # single line such as ``SUP 16.12.11 R`` (or put ``R`` on the previous
        # line); SUP TP often puts the status and prose on the first line and
        # the rule number on the next line.  Match both layouts and use the
        # next sourcebook heading as the block boundary.  Requiring a numeric
        # first component avoids mistaking the page header ``SUP Supervision``
        # for a rule heading.
        if sourcebook == "SUP TP":
            heading_re = re.compile(
                r"(?m)^[ \t]*(?:(?:R|G|UK)[ \t]+)?SUP\s+TP"
                r"(?:[^\n]*\n[ \t]*)?"
                r"[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*"
                r"(?:[ \t]*(?:R|G|UK))?(?=\s|[.,:;()\[]|$)",
                re.I,
            )
            bare_heading_re = re.compile(
                r"(?m)^[ \t]*[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*"
                r"[ \t]+(?:R|G|UK)\b",
                re.I,
            )
        else:
            heading_re = re.compile(
                rf"(?m)^[ \t]*(?:(?:R|G|UK)[ \t]+)?{sourcebook_re}[ \t]*"
                r"(?:(?:R|G|UK)[ \t\r\n]+)?"
                r"[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*"
                r"(?:[ \t]*(?:R|G|UK))?(?=\s|[.,:;()\[]|$)",
                re.I,
            )
            bare_heading_re = None
        headings = list(heading_re.finditer(handbook_text))
        if bare_heading_re is not None:
            # Some SUP TP pages repeat the sourcebook name only in the page
            # header and render the individual heading as ``7.2.3 R``.  Add
            # those canonical number lines as boundaries as well.
            headings.extend(bare_heading_re.finditer(handbook_text))
            headings.sort(key=lambda item: item.start())

        def heading_pattern(number: str) -> re.Pattern[str]:
            return re.compile(
                rf"(?m)^[ \t]*(?:(?:R|G|UK)[ \t]+)?{sourcebook_re}[ \t]*"
                rf"(?:(?:R|G|UK)[ \t\r\n]+)?{re.escape(number)}"
                rf"(?:[ \t]*(?:R|G|UK))?(?=\s|[.,:;()\[]|$)",
                re.I,
            )

        def block_for_number(number: str) -> str:
            exact = list(heading_pattern(number).finditer(handbook_text))
            if not exact:
                # SUP TP has a columnar layout where the rule number follows
                # explanatory text on the same line as ``SUP TP R``.  The
                # generic pattern above deliberately does not cross arbitrary
                # prose, so recover that one layout explicitly.
                if sourcebook == "SUP TP":
                    split = re.compile(
                        rf"(?m)^[ \t]*SUP\s+TP[^\n]*\n[ \t]*{re.escape(number)}"
                        rf"(?:[ \t]*(?:R|G|UK))?(?=\s|[.,:;()\[]|$)",
                        re.I,
                    )
                    exact = list(split.finditer(handbook_text))
                    if not exact:
                        bare = re.compile(
                            rf"(?m)^[ \t]*{re.escape(number)}"
                            rf"[ \t]+(?:R|G|UK)\b",
                            re.I,
                        )
                        exact = list(bare.finditer(handbook_text))
            if not exact:
                return ""
            candidates: list[tuple[int, int, str]] = []
            for match in exact:
                next_heading = next((item for item in headings if item.start() > match.end()), None)
                end = next_heading.start() if next_heading is not None else len(handbook_text)
                block = handbook_text[match.start():end]
                # A table-of-contents occurrence is normally only a heading
                # followed by a page break. Prefer the substantive occurrence
                # when there is one, but retain a short heading for provisions
                # that are genuinely textless in the current edition.
                compact_block = re.sub(r"\s+", " ", block).strip()
                content_score = len(compact_block)
                if re.search(r"\b(?:19|20)\d{2}\b", compact_block[:160]):
                    content_score -= 500
                candidates.append((content_score, match.start(), compact_block))
            candidates.sort(reverse=True)
            return candidates[0][2] if candidates else ""

        blocks: list[tuple[str, str]] = []
        for citation in citations:
            number = citation.split("(", 1)[0]
            # FCA suffixes identify rule status (R/G/UK), while a letter
            # embedded in the last component is part of the section number
            # (``4.3.16A``). Preserve the latter and strip only the terminal
            # status letter when present.
            if number and number[-1].upper() in {"R", "G"}:
                number = number[:-1]
            elif number.upper().endswith("UK"):
                number = number[:-2]
            if not re.fullmatch(r"[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)+", number):
                continue
            block = block_for_number(number)
            if block:
                blocks.append((citation, block))
        if not blocks:
            return None

        node_key = ",".join(number for number, _block in blocks)
        # ``apply_outcomes`` persists generated external targets by this
        # stable prefix, so keep the FCA namespace below ``external:document``
        # while retaining the sourcebook identity in the key.
        sourcebook_slug = sourcebook.lower().replace(" ", "-")
        node_id = f"external:document:fca:{sourcebook_slug}:" + digest(node_key, pdf_url, length=24)
        existing = self.nodes.get(node_id)
        title = f"FCA {sourcebook} — " + ", ".join(number for number, _block in blocks)
        text = "\n\n".join(block for _number, block in blocks)
        def clean_path_number(citation: str) -> str:
            number = citation.split("(", 1)[0]
            if number and number[-1].upper() in {"R", "G"}:
                number = number[:-1]
            elif number.upper().endswith("UK"):
                number = number[:-2]
            return number

        paths = [sourcebook_slug + "/" + clean_path_number(number).replace(".", "/") for number, _block in blocks]
        meta = {
            "external_reference_label": title,
            "href": pdf_url,
            "source_url": pdf_url,
            "source_text_available": True,
            "resolution_basis": "exact_fca_handbook_provisions",
            "instrument_id": f"fca-{sourcebook_slug}",
            "provision_paths": paths,
            "source_node_ids": [],
            "document_only": False,
        }
        target = {
            "id": node_id,
            "node_type": "external_reference",
            "stable_key": node_id,
            "title": title,
            "text": text,
            "url": pdf_url,
            "metadata_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "meta": meta,
        }
        if existing is not None and node_has_text(existing) and len(str(existing.get("text") or "")) >= len(text):
            return existing
        if existing is not None:
            # A previous parser version could have materialised only a TOC
            # heading (or a page-header fragment). Replace that short text
            # when the layout-aware extractor now has the substantive rule
            # block, keeping the stable node id and graph edges intact.
            existing.update(target)
            return existing
        self.nodes[node_id] = target
        self.title_index[normalise(title)].append(target)
        self.document_index[normalise(title)].append(target)
        return target

    def create_historical_ispv_target(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """Materialise the archived source for SS8/17's ``2C.9.1`` citation."""

        combined = " ".join(
            (
                row["target_part_or_document"] or "",
                row["reference_text"] or "",
                row["target_title_or_identifier"] or "",
            )
        )
        if not re.search(r"insurance\s+special\s+purpose", combined, re.I) or not re.search(
            r"2\s*C\s*[.]?\s*9\s*[.]?\s*1", combined, re.I
        ):
            return None
        url = "https://www.prarulebook.co.uk/pra-rules/insurance-special-purpose-vehicles/01-06-2025#fdbeed9a45844dd99937ff3049197117"
        node_id = "external:document:" + digest(url, "historical-2c9", length=24)
        if node_id in self.nodes:
            return self.nodes[node_id]
        text = historical_ispv_rule_text(url)
        if not text:
            return None
        title = "Insurance Special Purpose Vehicles — Rule 2C.9.1 (historical Rule 2C.9)"
        meta = {
            "external_reference_label": title,
            "href": url,
            "source_url": url,
            "source_text_available": True,
            "resolution_basis": "historical_rulebook_provision",
            "historical_citation": "2C.9.1",
            "published_identifier": "2C.9",
            "document_only": False,
        }
        node = {
            "id": node_id,
            "node_type": "external_reference",
            "stable_key": node_id,
            "title": title,
            "text": text,
            "url": url,
            "metadata_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "meta": meta,
        }
        self.nodes[node_id] = node
        self.title_index[normalise(title)].append(node)
        self.document_index[normalise(title)].append(node)
        return node

    def create_historical_428ai_target(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """Materialise archived Article 428AI of the Liquidity CRR.

        Article 428AI is deleted from the current Liquidity (CRR) page, but
        the official 2024 Rulebook snapshot retains the provision text under
        its stable fragment.  This is an exact provision target, not merely a
        link to the instrument, so reader mode can display the source wording.
        """

        combined = " ".join(
            (
                row["target_part_or_document"] or "",
                row["reference_text"] or "",
                row["target_title_or_identifier"] or "",
            )
        )
        if not re.search(r"liquidity\s*\(\s*crr\s*\)", combined, re.I) or not re.search(
            r"\b(?:article\s*)?428\s*AI\b", combined, re.I
        ):
            return None
        page_url = "https://www.prarulebook.co.uk/pra-rules/liquidity-crr/01-06-2024"
        fragment = "efb1af5c684d4f088eed7d3b91acce11"
        url = page_url + "#" + fragment
        text = historical_rulebook_article_text(page_url, fragment)
        if not text:
            return None
        node_id = "external:document:" + digest(url, "historical-428ai", length=24)
        existing = self.nodes.get(node_id)
        if existing is not None:
            return existing
        title = "Liquidity (CRR) — Article 428AI (historical Rulebook provision)"
        meta = {
            "external_reference_label": title,
            "href": url,
            "source_url": url,
            "source_text_available": True,
            "resolution_basis": "historical_rulebook_provision",
            "historical_citation": "Article 428AI",
            "published_identifier": "428AI",
            "document_only": False,
            "historical_snapshot": "01-06-2024",
        }
        node = {
            "id": node_id,
            "node_type": "external_reference",
            "stable_key": node_id,
            "title": title,
            "text": text,
            "url": url,
            "metadata_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "meta": meta,
        }
        self.nodes[node_id] = node
        self.title_index[normalise(title)].append(node)
        self.document_index[normalise(title)].append(node)
        return node

    def create_aggregate_provision_target(
        self,
        instrument: Instrument,
        paths: list[str],
    ) -> dict[str, Any] | None:
        """Combine several already-materialised provisions into one target.

        A ledger row can cite ``Articles 74(3) and 75(2)``.  The graph edge
        schema has one target per occurrence, so pointing it only at the first
        Article would silently drop the second source.  When all listed
        provisions already have official text, expose a compact aggregate
        node containing each exact provision and its URL/provenance.
        """

        nodes: list[dict[str, Any]] = []
        for path in paths:
            candidates = [node for node in self.external_index.get((instrument.instrument_id, path), ()) if node_has_text(node)]
            if not candidates:
                target_id = external_provision_node_id(instrument, path)
                candidate = self.nodes.get(target_id)
                if candidate is not None and node_has_text(candidate):
                    candidates = [candidate]
            if not candidates:
                legacy_ids = (
                    "external:" + instrument.instrument_id + ":" + path.replace("/", ":"),
                    "external:legislation:" + instrument.instrument_id + ":" + path.replace("/", ":"),
                )
                candidates = [
                    self.nodes[node_id]
                    for node_id in legacy_ids
                    if node_id in self.nodes and node_has_text(self.nodes[node_id])
                ]
            if not candidates:
                return None
            nodes.append(candidates[0])
        if len(nodes) < 2:
            return None
        node_id = "external:document:aggregate:" + digest(instrument.instrument_id, *paths, length=24)
        existing = self.nodes.get(node_id)
        if existing is not None:
            return existing
        def path_label(path: str) -> str:
            parts = path.split("/")
            if len(parts) < 2:
                return path
            label = parts[0].replace("_", " ").title()
            return f"{label} {parts[1]}" + "".join(f"({part})" for part in parts[2:])

        title = f"{instrument.title} — " + ", ".join(path_label(path) for path in paths)
        text = "\n\n".join(f"{node['title']}\n{node.get('text') or ''}" for node in nodes)
        url = nodes[0].get("url") or instrument.official_url or f"{instrument.base_url}/contents"
        meta = {
            "external_reference_label": title,
            "href": url,
            "source_url": url,
            "source_text_available": True,
            "resolution_basis": "aggregate_exact_provisions",
            "instrument_id": instrument.instrument_id,
            "provision_paths": paths,
            "source_node_ids": [node["id"] for node in nodes],
            "document_only": False,
        }
        aggregate = {
            "id": node_id,
            "node_type": "external_reference",
            "stable_key": node_id,
            "title": title,
            "text": text,
            "url": url,
            "metadata_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "meta": meta,
        }
        self.nodes[node_id] = aggregate
        self.title_index[normalise(title)].append(aggregate)
        self.document_index[normalise(title)].append(aggregate)
        return aggregate

    def create_aggregate_internal_target(
        self,
        context: str,
        nodes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Expose several text-bearing Rulebook children as one target."""

        nodes = list({node["id"]: node for node in nodes if node_has_text(node)}.values())
        if len(nodes) < 2:
            return None
        node_id = "external:document:aggregate:internal:" + digest(context, *(node["id"] for node in nodes), length=24)
        existing = self.nodes.get(node_id)
        if existing is not None:
            return existing
        part_title = next(
            (str(node.get("meta", {}).get("part_title") or "") for node in nodes if node.get("meta", {}).get("part_title")),
            context,
        )
        title = f"{part_title} — " + ", ".join(node["title"] for node in nodes)
        text = "\n\n".join(f"{node['title']}\n{node.get('text') or ''}" for node in nodes)
        url = nodes[0].get("url") or ""
        meta = {
            "external_reference_label": title,
            "href": url,
            "source_url": url,
            "source_text_available": True,
            "resolution_basis": "aggregate_internal_provisions",
            "internal_context": context,
            "source_node_ids": [node["id"] for node in nodes],
            "document_only": False,
        }
        aggregate = {
            "id": node_id,
            "node_type": "external_reference",
            "stable_key": node_id,
            "title": title,
            "text": text,
            "url": url,
            "metadata_json": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "meta": meta,
        }
        self.nodes[node_id] = aggregate
        self.title_index[normalise(title)].append(aggregate)
        self.document_index[normalise(title)].append(aggregate)
        return aggregate

    def resolve_row(self, row: sqlite3.Row, source: sqlite3.Row, *, apply_nodes: bool) -> Outcome:
        kind = singular(row["target_kind"])
        kind = kind.replace("_", " ")
        ref = compact(row["reference_text"] or "")
        ident = compact(row["target_title_or_identifier"] or "")
        doc = compact(row["target_part_or_document"] or "")
        combined = " ".join((ref, ident, doc, row["evidence_quote"] or ""))
        extracted = float(row["extracted_confidence"] or 0.0)
        explicit_text = " ".join((ref, ident))
        label_match = re.search(
            r"\b(?P<label>articles?|arts?\.?|sections?|s(?=\.?\s*[0-9])\.?|regulations?|regs?\.?|rules?|paragraphs?|paras?\.?|points?|subparagraphs?)\s*"
            r"(?P<number>[0-9])",
            explicit_text,
            re.I,
        )
        # ``S166 report`` is a named supervisory tool, not the shorthand for
        # section 166 followed by a provision citation.
        if re.search(r"\bS\.?\s*166\s+report\b", explicit_text, re.I):
            label_match = None
        # OCR in older supervisory statements turns ``SI`` into ``S1``.
        # This is an instrument number (for example ``S1 1987/2005``), not a
        # statutory section beginning with the letter S.
        if re.fullmatch(r"\s*S1\s*\d{4}\s*/\s*\d+\s*", explicit_text, re.I):
            label_match = None
        # Template/form identifiers such as ``S.02.01.02`` begin with an
        # ``S`` but are not statutory sections. Keep these at document scope.
        if kind in {"template", "form", "table"} and not re.search(
            r"\b(?:article|section|regulation|rule|paragraph)\b",
            explicit_text,
            re.I,
        ):
            label_match = None
        effective_kind = kind
        if label_match:
            effective_kind = singular(label_match.group("label").rstrip("."))
        # Bare numbered references in a named supervisory statement (for
        # example ``SS44/15 6.2``) are guidance paragraphs even when the
        # extraction label was ``section`` or ``guidance``. Infer that local
        # kind so the Rulebook paragraph index is consulted before treating
        # the number as an external Solvency II path.
        inferred_identifiers = identifier_candidates(ref) or identifier_candidates(ident)
        if (
            effective_kind in {"section", "guidance", "external"}
            and inferred_identifiers
            and re.search(r"(?:\bSS\s*\d|\bSoP\s*\d|supervisory\s+statement|statement\s+of\s+policy|guidance)", doc, re.I)
            and not re.search(r"\b(?:FSMA|Act|Regulation|Directive|Order|CRR)\b", doc, re.I)
        ):
            effective_kind = "paragraph"
        # “Regulations 1994” is a document title, not Regulation 1994 of an
        # instrument.  Keep year-only instrument names at document scope.
        generic_regulation_title = bool(
            re.search(
                r"\bregulations?\b[^\d]{0,40}\b(?:19|20)\d{2}\b",
                explicit_text,
                re.I,
            )
            or re.search(r"\bregulations?\s+\d{1,4}\s*/\s*\d{4}\b", explicit_text, re.I)
        )
        # ``Article 34 of Regulation (EU) No 2015/61`` contains both an
        # instrument year and a genuine Article label. The year heuristic
        # must not demote that specific Article to a document-only link.
        if label_match:
            year_title_match = (
                re.search(r"\bregulations?\s+(?:19|20)\d{2}\b", explicit_text, re.I)
                or re.search(r"\bregulations?\s+\d{1,4}\s*/\s*\d{4}(?:\s*/\s*(?:EU|EC))?\b", explicit_text, re.I)
            )
            # Keep a year-only ``Regulations 1994`` title at document scope,
            # while preserving a genuine earlier citation such as
            # ``Regulation 54 of ... Regulations 2015``.
            if not year_title_match or year_title_match.start() > label_match.start() + 2:
                generic_regulation_title = False
        # The extraction model often stores a bare Rulebook number in the
        # identifier field (``7.2``) while leaving the legal label in
        # ``target_kind``. Treat that as a specific provision; otherwise the
        # document-link branch would hide the actual text-bearing rule node.
        bare_identifier_pattern = re.compile(
            r"\s*[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*(?:\s*\([^)]*\))*"
            r"(?:\s*(?:,|and|to|[-–—])\s*[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*(?:\s*\([^)]*\))*)*\s*",
            re.I,
        )
        bare_specific = bool(
            kind in {"rule", "article", "section", "paragraph", "point", "subparagraph", "regulation", "schedule paragraph", "schedule part"}
            and any(bare_identifier_pattern.fullmatch(value or "") for value in (ident, ref))
        )
        condition_specific = bool(re.search(r"\bcondition\s+[0-9]", explicit_text, re.I))
        nested_article_identifier = bool(
            kind == "article"
            and re.search(r"\barticle\s+[0-9][0-9A-Za-z]*\s*\(\s*[A-Z]\d", doc, re.I)
            and (
                any(re.fullmatch(r"\s*[A-Z]\d+(?:\s*\([^)]*\))*\s*", value or "", re.I) for value in (ident, ref))
                or re.search(r"\bcondition\s+[A-Z]\d", ident + " " + ref, re.I)
            )
        )
        fca_handbook_specific = bool(
            re.search(r"\b(?:COBS|SUP(?:\s+TP)?|SYSC|GEN|COLL)\s*[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)+[A-Za-z]*\b", explicit_text, re.I)
        )
        # Rulebook citations often omit the literal word ``rule`` in the
        # extracted fields (``Own Funds 2.3(1)`` or ``Group Supervision
        # 8A``).  The target kind is still ``rule`` and the identifier parser
        # can prove that a numbered provision was named.  Treat that pattern
        # as specific before the document-link branch, otherwise a guidance
        # document containing the citation wins and the actual Rulebook text
        # is never considered.  Exclude broad phrases such as “rules or
        # requirements” which are not a numbered citation.
        rule_identifier_specific = bool(
            kind == "rule"
            and (identifier_candidates(ident) or identifier_candidates(ref))
            and not re.search(r"\brules?\s+(?:or|and)\s+", explicit_text, re.I)
            # ``FSMA 2000`` (and similarly named year-only instruments) is a
            # statute/document title, not Rule 2000.  Do not let the generic
            # numeric parser turn the year into a provision path.
            and not re.fullmatch(
                r"\s*[A-Za-z][A-Za-z0-9 .&()'’\-/–—]{1,80}\s+(?:19|20)\d{2}\s*",
                ident,
                re.I,
            )
        )
        explicit_provision = bool(
            label_match
            or bare_specific
            or condition_specific
            or nested_article_identifier
            or rule_identifier_specific
            or fca_handbook_specific
        ) and not generic_regulation_title
        span = quote_span(row, source)
        base_kwargs = {
            "resolution_id": row["id"],
            "source_id": source["id"],
            "span_start": span[0] if span else None,
            "span_end": span[1] if span else None,
            "quoted_text": span[2] if span else ref,
            "metadata": {
                "policy_version": METHOD,
                "target_kind": row["target_kind"] or "",
                "target_title_or_identifier": ident,
                "target_part_or_document": doc,
                "evidence_quote": row["evidence_quote"] or "",
            },
        }

        # PRA threshold conditions are statutory provisions in Schedule 6 to
        # FSMA 2000.  They are often extracted as a generic ``section``
        # reference with a human-readable label (for example ``threshold
        # condition 5F(3)``), so normal section/path inference cannot identify
        # the source instrument.  Resolve the cited paragraph directly to the
        # official legislation.gov.uk node; this keeps the exact source text
        # available in the reader rather than linking only to the Schedule.
        threshold_5f = re.search(
            r"\bthreshold\s+condition\s+5F\s*\(\s*3\s*\)",
            combined,
            re.I,
        )
        if threshold_5f:
            threshold_instrument = self.registry.by_id.get("fsma")
            threshold_path = "schedule/6/paragraph/5F/3"
            if threshold_instrument is not None:
                existing = [
                    node
                    for node in self.external_index.get((threshold_instrument.instrument_id, threshold_path), ())
                    if node_has_text(node)
                ]
                if not existing:
                    for legacy_id in (
                        "external:" + threshold_instrument.instrument_id + ":" + threshold_path.replace("/", ":"),
                        "external:legislation:" + threshold_instrument.instrument_id + ":" + threshold_path.replace("/", ":"),
                    ):
                        candidate = self.nodes.get(legacy_id)
                        if candidate is not None and node_has_text(candidate):
                            existing = [candidate]
                            break
                if existing:
                    target = existing[0]
                    return Outcome(
                        **base_kwargs,
                        status="resolved",
                        scope="provision",
                        target_id=target["id"],
                        target_type=target["node_type"],
                        target_title=target["title"],
                        target_url=target["url"],
                        target_text_available=True,
                        resolver_method="policy_external_threshold_condition_existing",
                        instrument_id=threshold_instrument.instrument_id,
                        provision_path=threshold_path,
                        confidence=max(0.96, extracted),
                        reason="FSMA Schedule 6 paragraph 5F(3) already has official source text",
                    )
                target_id = external_provision_node_id(threshold_instrument, threshold_path)
                target = self.nodes.get(target_id)
                if target is not None and node_has_text(target):
                    return Outcome(
                        **base_kwargs,
                        status="resolved",
                        scope="provision",
                        target_id=target["id"],
                        target_type=target["node_type"],
                        target_title=target["title"],
                        target_url=target["url"],
                        target_text_available=True,
                        resolver_method="policy_external_threshold_condition_existing",
                        instrument_id=threshold_instrument.instrument_id,
                        provision_path=threshold_path,
                        confidence=max(0.96, extracted),
                        reason="FSMA Schedule 6 paragraph 5F(3) already has official source text",
                    )
                return Outcome(
                    **base_kwargs,
                    status="needs_fetch",
                    scope="provision",
                    target_id=target_id,
                    target_type="external_reference",
                    target_title="Financial Services and Markets Act 2000 — Schedule 6 paragraph 5F(3)",
                    target_url=threshold_instrument.provision_url(threshold_path),
                    target_text_available=False,
                    resolver_method="policy_external_threshold_condition_fetch",
                    instrument_id=threshold_instrument.instrument_id,
                    provision_path=threshold_path,
                    confidence=max(0.96, extracted),
                    reason="specific statutory threshold condition requires official source fetch",
                    fetch_key=(threshold_instrument.instrument_id, threshold_path),
                    fetch_instrument=threshold_instrument,
                    fetch_path=threshold_path,
                )

        # The 2017 Scottish and Northern Ireland Banknote Rules are a
        # document-level Bank of England publication.  Do not let the
        # neighbouring 2009 Banknote Regulations heuristic interpret the year
        # as a rule number; the PDF itself is the authoritative target.
        if re.search(
            r"\bScottish\s+and\s+Northern\s+Ireland\s+Banknote\s+Rules\s+2017\b",
            combined,
            re.I,
        ):
            doc_node = self.create_document_target(row, source, None)
            if doc_node is not None:
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="document",
                    target_id=doc_node["id"],
                    target_type=doc_node["node_type"],
                    target_title=doc_node["title"],
                    target_url=doc_node["url"],
                    target_text_available=node_has_text(doc_node),
                    resolver_method="policy_document_link_explicit_banknote_rules",
                    confidence=max(0.90, extracted),
                    reason="named 2017 Banknote Rules document linked to the official Bank of England PDF",
                )

        # Extraction metadata such as ``paragraph_number: 6`` and the
        # deliberately broad multi-kind label are bookkeeping artefacts, not
        # legal targets.  Keep them explicit in the ledger without fabricating
        # a provision edge.
        if re.search(r"paragraph[_ ]number\s*:", combined, re.I) or normalise(doc) == "source" or "|" in kind or re.search(
            r"rules?\s+or\s+requirements\s+imposed\s+by", ref, re.I,
        ):
            doc_node = self.create_document_target(row, source, None)
            if doc_node is not None and not re.search(r"paragraph[_ ]number\s*:", combined, re.I) and normalise(doc) != "source":
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="document",
                    target_id=doc_node["id"],
                    target_type=doc_node["node_type"],
                    target_title=doc_node["title"],
                    target_url=doc_node["url"],
                    target_text_available=node_has_text(doc_node),
                    resolver_method="policy_document_link_metadata_context",
                    confidence=max(0.60, extracted),
                    reason="metadata/documentary extraction context linked to its source document",
                )
            return Outcome(**base_kwargs, status="not_reference", scope="not_reference", resolver_method="policy_not_reference_metadata_artifact", confidence=1.0, reason="extraction metadata or broad regulatory mention is not a distinct cross-reference")

        # The extraction pass occasionally returns a definition or a bare
        # number from a table cell.  These are not cross-reference targets.
        relative_reference = bool(
            re.search(
                r"\b(?:this|that|the same)\s+(?:rule|article|section|chapter|part|paragraph|document)\b|\b(?:above|below|following|preceding)\b",
                ref,
                re.I,
            )
            and not re.search(r"\b(?:of|in|under)\s+(?:the\s+)?[A-Z]", ref)
        )
        if relative_reference and not explicit_provision:
            # A relative chapter/section/figure reference is still a useful
            # document-level citation when its containing guidance or Part is
            # known. Preserve the hyperlink contract for that general mention;
            # only leave genuinely self-contained point/paragraph references
            # without a distinct document target as not-reference artefacts.
            same_paragraph_point = bool(
                re.search(r"\bpoint\s+\([A-Za-z0-9]+\)\s+of\s+this\s+paragraph\b", ref, re.I)
            )
            if not same_paragraph_point:
                instrument = extract_instrument(self.registry, row, source)
                doc_node = self.create_document_target(row, source, instrument)
                if doc_node is None:
                    # Guidance paragraphs often carry only a short document
                    # label in the extracted fields (for example ``SS45/15``)
                    # while the canonical guidance-document node is available
                    # through the source metadata. Reuse that containing URL
                    # before treating the relative mention as an artefact.
                    for key in self.source_context(source):
                        for candidate in self.document_index.get(key, ()):
                            if candidate["id"] != source["id"] and (candidate.get("url") or node_has_text(candidate)):
                                doc_node = candidate
                                break
                        if doc_node is not None:
                            break
                if doc_node is not None:
                    return Outcome(
                        **base_kwargs,
                        status="resolved",
                        scope="document",
                        target_id=doc_node["id"],
                        target_type=doc_node["node_type"],
                        target_title=doc_node["title"],
                        target_url=doc_node["url"],
                        target_text_available=node_has_text(doc_node),
                        resolver_method="policy_document_link_relative_context",
                        confidence=max(0.60, extracted),
                        reason="relative chapter/section/guidance reference linked to its containing source document",
                    )
            return Outcome(
                **base_kwargs,
                status="not_reference",
                scope="not_reference",
                resolver_method="policy_not_reference",
                confidence=1.0,
                reason="relative/self-reference without a distinct target",
            )
        if kind in {"definition", "unknown", "reference", "role", "abbreviation", "glossary"} and not re.search(
            r"\b(?:articles?|arts?\.?|sections?|s\.?|regulations?|regs?\.?|rules?|paragraphs?|paras?\.?|points?|subparagraphs?)\s*[0-9]",
            combined,
            re.I,
        ):
            doc_node = self.create_document_target(row, source, None)
            if doc_node is not None:
                return Outcome(**base_kwargs, status="resolved", scope="document", target_id=doc_node["id"], target_type=doc_node["node_type"], target_title=doc_node["title"], target_url=doc_node["url"], target_text_available=node_has_text(doc_node), resolver_method="policy_document_link", confidence=max(0.5, extracted), reason="named document/URL reference")
            return Outcome(**base_kwargs, status="not_reference", scope="not_reference", resolver_method="policy_not_reference", confidence=1.0, reason="no distinct provision or document target")

        # Chapters, Parts, instruments, guidance, templates and forms are
        # intentionally document links under the policy, even if a chapter or
        # template-like node exists in the graph.
        if (
            (kind in DOCUMENT_KINDS and not explicit_provision)
            or (kind in {"part", "annex"} and re.search(r"\b(?:the\s+parts?|annex\s+[0-9A-Za-z]+)\b", ref + " " + doc, re.I))
            or (kind in {"regulation", "article", "section", "rule", "paragraph"} and not explicit_provision)
        ):
            instrument = extract_instrument(self.registry, row, source)
            doc_node = self.create_document_target(row, source, instrument)
            if doc_node is not None:
                return Outcome(**base_kwargs, status="resolved", scope="document", target_id=doc_node["id"], target_type=doc_node["node_type"], target_title=doc_node["title"], target_url=doc_node["url"], target_text_available=node_has_text(doc_node), resolver_method="policy_document_link", confidence=max(0.5, extracted), reason="general reference resolved to source document")
            # If the source context itself is a known Part/document, link to it.
            source_part, source_doc = self.source_context(source)
            for key in (source_part, source_doc):
                for candidate in self.document_index.get(key, ()):
                    if candidate["id"] != source["id"] and (candidate.get("url") or node_has_text(candidate)):
                        return Outcome(**base_kwargs, status="resolved", scope="document", target_id=candidate["id"], target_type=candidate["node_type"], target_title=candidate["title"], target_url=candidate["url"], target_text_available=node_has_text(candidate), resolver_method="policy_source_context_document", confidence=max(0.5, extracted), reason="document inferred from source context")
            return Outcome(**base_kwargs, status="not_reference", scope="not_reference", resolver_method="policy_not_reference", confidence=1.0, reason="generic mention without a resolvable source document")

        # The identifier field is sometimes only the named Part/document
        # (e.g. ``Securitisation Part``), while the citation text contains
        # the actual Article/Rule number. Prefer the explicit citation when it
        # carries a numbered provision.
        instrument_hint = extract_instrument(self.registry, row, source)
        # The target identifier is usually the cleanest citation field. The
        # reference text can include a year (``Credit Union Act 1979,
        # section 1(3)``) or a source URL before the actual provision, so do
        # not let its first number become the path when the identifier already
        # carries a legal label.
        identifiers = identifier_candidates(ident)
        ref_identifiers = identifier_candidates(ref)
        if not identifiers and nested_article_identifier:
            nested_value = ident if re.fullmatch(r"\s*[A-Z]\d+(?:\s*\([^)]*\))*\s*", ident or "", re.I) else ref
            if re.fullmatch(r"\s*[A-Z]\d+(?:\s*\([^)]*\))*\s*", nested_value or "", re.I):
                identifiers = [compact(nested_value)]
            else:
                parent_article = re.search(r"\barticle\s+(?P<number>[0-9][0-9A-Za-z]*)", doc, re.I)
                if parent_article:
                    identifiers = [parent_article.group("number")]
        if not identifiers or (
            ref_identifiers
            and re.search(r"\b(?:article|section|regulation|rule|paragraph|point|subparagraph)\b", ref, re.I)
            and not re.search(r"\b(?:article|section|regulation|rule|paragraph|point|subparagraph)\b", ident, re.I)
        ):
            identifiers = ref_identifiers
        if not identifiers and not explicit_provision:
            doc_node = self.create_document_target(row, source, instrument_hint)
            if doc_node is not None:
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="document",
                    target_id=doc_node["id"],
                    target_type=doc_node["node_type"],
                    target_title=doc_node["title"],
                    target_url=doc_node["url"],
                    target_text_available=node_has_text(doc_node),
                    resolver_method="policy_document_link_without_identifier",
                    confidence=max(0.60, extracted),
                    reason="document reference has no specific provision identifier",
                )
            return Outcome(
                **base_kwargs,
                status="not_reference",
                scope="not_reference",
                resolver_method="policy_not_reference_without_identifier",
                confidence=1.0,
                reason="extraction contains a provision label but no identifier",
            )

        # SS8/17's historical citation uses ``rule 2C.9.1``.  That wording
        # was published as rule 2C.9 in the archived Rulebook and is deleted
        # from the current version, so expose the archived official text as a
        # readable provision target rather than linking to a blank [Deleted]
        # node or leaving the citation unresolved.
        historical_ispv = self.create_historical_ispv_target(row)
        if historical_ispv is not None:
            return Outcome(
                **base_kwargs,
                status="resolved",
                scope="provision",
                target_id=historical_ispv["id"],
                target_type=historical_ispv["node_type"],
                target_title=historical_ispv["title"],
                target_url=historical_ispv["url"],
                target_text_available=True,
                resolver_method="policy_historical_rulebook_provision",
                confidence=max(0.94, extracted),
                reason="historical official Rulebook text materialised for a citation whose current rule is deleted",
            )

        historical_428ai = self.create_historical_428ai_target(row)
        if historical_428ai is not None:
            return Outcome(
                **base_kwargs,
                status="resolved",
                scope="provision",
                target_id=historical_428ai["id"],
                target_type=historical_428ai["node_type"],
                target_title=historical_428ai["title"],
                target_url=historical_428ai["url"],
                target_text_available=True,
                resolver_method="policy_historical_rulebook_provision",
                confidence=max(0.94, extracted),
                reason="historical official Rulebook text materialised for a citation whose current article is deleted",
            )

        # These non-legislation rulebooks publish the authoritative wording in
        # an official PDF rather than a legislation.gov.uk XML record.  Treat
        # the text-bearing PDF node as a provision target so the app can open
        # the source wording, while retaining the document URL and provenance.
        if re.search(
            r"(?:Critical\s+Third\s+Part(?:y|ies)\s+Instrument\s+2024[\s,:-]*Rule\s*4\.10|Listing\s+Rules?\s+9\.27)",
            combined,
            re.I,
        ):
            doc_node = self.create_document_target(row, source, None)
            if doc_node is not None:
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="provision" if node_has_text(doc_node) else "document",
                    target_id=doc_node["id"],
                    target_type=doc_node["node_type"],
                    target_title=doc_node["title"],
                    target_url=doc_node["url"],
                    target_text_available=node_has_text(doc_node),
                    resolver_method="policy_external_rulebook_pdf",
                    confidence=max(0.90, extracted),
                    reason="official non-legislation rulebook PDF materialised as the source target",
                )

        fca_handbook_target = self.create_fca_handbook_provision_target(row, source)
        if fca_handbook_target is not None:
            return Outcome(
                **base_kwargs,
                status="resolved",
                scope="provision",
                target_id=fca_handbook_target["id"],
                target_type=fca_handbook_target["node_type"],
                target_title=fca_handbook_target["title"],
                target_url=fca_handbook_target["url"],
                target_text_available=True,
                resolver_method="policy_external_fca_handbook_provision",
                instrument_id=str(fca_handbook_target.get("meta", {}).get("instrument_id") or "fca-cobs"),
                provision_path=",".join(fca_handbook_target.get("meta", {}).get("provision_paths") or ()),
                confidence=max(0.94, extracted),
                reason="specific FCA Handbook provision extracted from the official sourcebook PDF",
            )
        # Internal Rulebook/guidance provisions are resolved before registry
        # matching: “Article 33” in a CRR Part is not automatically an external
        # Article when the Part contains the authoritative text.
        internal = self.find_internal(row, source, effective_kind, identifiers)
        internal_context_values = {"", "unknown", "pra rulebook", "rules", "rulebook", "this part", "this chapter", "the part", "the chapter"}
        explicit_external_document = (
            normalise(doc) not in internal_context_values
            and not bare_identifier_pattern.fullmatch(doc or "")
        )
        external_instrument_ids = {
            "fsma", "fsma-2023", "banking-act", "building-societies-act", "companies-act-2006", "companies-act-1985",
            "finance-act-2012", "interpretation-act-1978", "pensions-scheme-act-1993",
            "tribunal-procedure-upper-tribunal-rules-2008", "mifid-ii", "crd", "lcr-delegated-regulation",
            "solvency-ii-delegated-regulation", "modr", "uk-crr", "crr2",
        }
        target_fields = " ".join((doc, ref, ident))
        explicit_uk_crr_article = bool(
            effective_kind == "article"
            and re.search(r"\b(?:UK\s+)?CRR\b", target_fields, re.I)
        )
        # An instrument named only in surrounding Rulebook prose is context,
        # not necessarily the target.  Require the citation fields to name an
        # external instrument, except for unmistakable statutory shorthands
        # such as ``section 138BA ... of FSMA``.
        target_names_external_instrument = bool(re.search(
            r"\b(?:CRR|FSMA|Act|Regulation|Directive|Order|Statutory|MiFIR|MiFID|BRRD|RAO|BSA)\b",
            target_fields,
            re.I,
        ))
        statutory_evidence = bool(re.search(
            r"\bsection\b[^.]{0,120}\b138BA\b[^.]{0,120}\bFSMA\b|\bsection\s+3A\b.*\b(?:Friendly\s+and\s+Industrial|Provident\s+Societies)\b",
            combined + " " + source_text(source),
            re.I,
        ))
        external_note_specific = bool(
            re.search(r"Regulation\s*\((?:EU|EC)\)\s*No\.?\s*1187\s*/\s*2014", source_text(source), re.I)
            and re.search(r"\barticles?\s+[12]\b", target_fields, re.I)
        )
        external_instrument_context = bool(
            instrument_hint is not None
            and (target_names_external_instrument or statutory_evidence or external_note_specific)
            and (explicit_external_document or instrument_hint.instrument_id in external_instrument_ids)
        )
        # A named PRA supervisory statement/Rulebook Part is an internal
        # source even when its text also mentions Solvency II/CRR.  Its local
        # paragraph or rule node is the readable source requested by the
        # citation; do not let the neighbouring instrument name force an
        # unavailable external fetch.
        internal_is_rulebook_source = bool(
            internal is not None
            and internal.get("url")
            and re.search(r"prarulebook\.co\.uk", internal.get("url") or "", re.I)
            and re.search(
                r"(?:\bSS\s*\d|\bSoP\s*\d|\bPart\b|PRA\s+Rulebook|\bCRR\b|\bPRA\b|supervisory|statement|guidance|approach|\bArticles?\s+433)",
                doc,
                re.I,
            )
        )
        # A canonical Part name is sufficient internal context even when the
        # source prose mentions CRR/Solvency II in a note.  The historical
        # ``Rules Supplementing Article 105`` heading is likewise backed by
        # the complete Rulebook Article 105 chapter; the old delegated
        # regulation itself has no Article 105.
        canonical_part_context = False
        if internal is not None and internal.get("url"):
            canonical_part_context = (
                not external_instrument_context
                and any(
                    self._context_matches(internal, part_title)
                    for part_title, _part_id in self.part_titles
                )
            )
            if re.search(
                r"Rules\s+Supplementing\s+Article\s+105|Article\s+105",
                doc + " " + source["title"],
                re.I,
            ) and re.search(r"trading\s+book|prudential\s+valuation", source_text(source), re.I):
                canonical_part_context = True
        internal_is_rulebook_source = internal_is_rulebook_source or canonical_part_context
        if (
            internal is not None
            and node_has_text(internal)
            and (
                not external_instrument_context
                or (internal_is_rulebook_source and not explicit_uk_crr_article)
            )
        ):
            return Outcome(**base_kwargs, status="resolved", scope="provision", target_id=internal["id"], target_type=internal["node_type"], target_title=internal["title"], target_url=internal["url"], target_text_available=True, resolver_method="policy_internal_provision", confidence=max(0.93, extracted), reason="unique internal provision with source text")

        # Specific paragraphs of an external guidance document do not have to
        # be fabricated as Solvency/CRR legislative provisions. If the exact
        # paragraph is absent from the local corpus, keep the named guidance
        # document (and its full text where available) as the authoritative
        # hyperlink target. Statutes/instruments are excluded here and remain
        # subject to the exact source-text contract below.
        guidance_text = " ".join((doc, ref, ident))
        # Rulebook Part names often contain the word “Approach” (for example
        # “Market Risk: Simplified Standardised Approach (CRR)”).  That word
        # is also a guidance-document signal, but here it describes the
        # internal Part whose Article text must be preferred.  Suppress the
        # guidance fallback when the target fields match a canonical PRA Part;
        # explicit SS/SoP/guideline labels still remain guidance references.
        rulebook_part_context = bool(
            any(
                title and (title in normalise(doc) or normalise(doc) in title)
                for title, _part_id in self.part_titles
            )
            or re.search(r"\bPRA\s+Rulebook\b|\bCRR\b[^.]{0,80}\bPart\b", doc, re.I)
        )
        named_guidance = bool(
            re.search(
                r"(?:\bSS\s*\d|\bSoP\b|supervisory\s+statement|statement\s+of\s+policy|guidelines?|guidance|consultation|approach|\bEBA\b)",
                guidance_text,
                re.I,
            )
        ) and not rulebook_part_context
        # A guidance title often names the statute or Directive under which it
        # was made (for example EBA internal-governance Guidelines under CRD
        # IV).  Once the title itself is unambiguously guidance, that legal
        # instrument name must not force a fabricated legislative provision.
        guidance_reference = bool(
            named_guidance
            and not (
                re.search(r"\b(?:Act|Regulation|Order|FSMA|BRRD|MiFIR|MiFID|RAO)\b", doc, re.I)
                and not re.search(r"\b(?:SS\s*\d|\bSoP\b|supervisory\s+statement|statement\s+of\s+policy|guidelines?|guidance|\bEBA\b)\b", doc, re.I)
            )
        )
        if guidance_reference:
            target_tokens = set(normalise(doc).split())
            guidance_documents = []
            for node in self.nodes.values():
                if node["node_type"] != "guidance_document" or not (node.get("url") or node_has_text(node)):
                    continue
                node_tokens = set(normalise(node.get("title") or "").split())
                if target_tokens and (target_tokens <= node_tokens or len(target_tokens & node_tokens) >= max(3, len(target_tokens) // 2)):
                    guidance_documents.append(node)
            if guidance_documents:
                guidance_documents.sort(key=lambda node: (len(target_tokens & set(normalise(node.get("title") or "").split())), len(node.get("text") or "")), reverse=True)
                doc_node = guidance_documents[0]
            else:
                doc_node = self.create_document_target(row, source, None)
            if doc_node is not None:
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="document",
                    target_id=doc_node["id"],
                    target_type=doc_node["node_type"],
                    target_title=doc_node["title"],
                    target_url=doc_node["url"],
                    target_text_available=node_has_text(doc_node),
                    resolver_method="policy_external_guidance_link",
                    confidence=max(0.75, extracted),
                    reason="named external guidance document linked when a split paragraph source was unavailable",
                )

        instrument = instrument_hint
        # If the explicit document is clearly an external instrument, derive a
        # registry path and fetch its official text.  UK Acts use sections even
        # where extraction called them Articles; the alternate path is tried
        # after the literal citation path.
        if instrument is not None:
            ref_has_label = bool(
                re.search(
                    r"\b(?:articles?|arts?\.?|sections?|s\.?|regulations?|regs?\.?|rules?|paragraphs?|paras?\.?|points?|subparagraphs?)\s*[0-9]",
                    ref,
                    re.I,
                )
            )
            paths = (
                path_candidates(effective_kind, ref)
                if ref_has_label
                else path_candidates(effective_kind, ident)
            ) or path_candidates(effective_kind, ref)
            # UK Acts are sometimes extracted as ``rule`` when the source
            # sentence actually says ``section 138BA`` (or ``section 3A``).
            # Prefer the legislation section URI in that case; ``rule/...``
            # is not a valid legislation.gov.uk path.
            if instrument.legislation_type in {"ukpga", "ukla", "uksi", "nisi"} and effective_kind == "rule":
                statute_text = combined + " " + source_text(source)
                section_paths = []
                for identifier in identifiers:
                    base = identifier.split("(", 1)[0]
                    if re.search(r"\bsections?\b[^.]{0,160}\b" + re.escape(base) + r"\b", statute_text, re.I):
                        qualifiers = re.findall(r"\(([^)]*)\)", identifier)
                        section_paths.append("section/" + "/".join([base] + qualifiers))
                paths = section_paths + paths
                regulation_paths = []
                for identifier in identifiers:
                    base = identifier.split("(", 1)[0]
                    if re.search(r"\bregulations?\b[^.]{0,160}\b" + re.escape(base) + r"\b", statute_text, re.I):
                        qualifiers = re.findall(r"\(([^)]*)\)", identifier)
                        regulation_paths.append("regulation/" + "/".join([base] + qualifiers))
                paths = regulation_paths + paths
            # SS19/15 cites a list of Building Societies Act provisions.  Keep
            # every cited section/schedule in one aggregate target instead of
            # accidentally converting the first section into Schedule 2.
            building_societies_list = instrument.instrument_id == "building-societies-act" and re.search(
                r"sections?\s+97\b.*(?:102D|102B\s+to\s+D)|sections?\s+98\b.*Schedule\s+2.*Schedule\s+17",
                combined,
                re.I,
            )
            if building_societies_list:
                paths = [
                    "section/97", "section/98", "section/99", "section/99A",
                    "section/100", "section/101", "section/102", "section/102B",
                    "section/102C", "section/102D", "schedule/2/paragraph/30",
                    "schedule/17",
                ]
            condition_regulation = re.search(r"\bregulation\s+(?P<number>[0-9A-Za-z]+)", combined, re.I)
            if condition_specific and condition_regulation:
                # Conditions in a table under ``Regulation 54`` are not a
                # top-level regulation 1/3. Use the containing Regulation 54
                # node, whose official text is the source available to the
                # reader.
                parent_path = "regulation/" + condition_regulation.group("number")
                paths = [parent_path] + paths
            if effective_kind == "article" and instrument.legislation_type in {"ukpga", "ukla"}:
                paths = [path.replace("article/", "section/", 1) for path in paths] + paths
            if effective_kind == "section" and instrument.legislation_type in {"eur", "eudr"}:
                paths = [path.replace("section/", "article/", 1) for path in paths] + paths
            # FSMA Schedule 6 uses paragraph labels such as 4E/5E/4F,
            # although extraction frequently calls them sections. Prefer the
            # exact schedule paragraph nodes already materialised in the
            # graph before trying the invalid top-level section URI.
            if instrument.instrument_id == "fsma":
                schedule_match = re.search(r"\bschedule\s+(?P<number>\d+)\b", combined + " " + source_text(source), re.I)
                schedule_number = schedule_match.group("number") if schedule_match else ""
                citation_fields = " ".join((ref, ident))
                schedule_specific = bool(
                    effective_kind in {"schedule", "subparagraph"}
                    or re.search(r"\b(?:paragraph|subparagraph)\b", citation_fields, re.I)
                )
                if (schedule_number and schedule_specific) or any(
                    re.match(r"^(?:4E|5E|4F|5F)", identifier, re.I)
                    for identifier in identifiers
                ):
                    schedule_number = schedule_number or "6"
                    schedule_paths = []
                    for identifier in identifiers:
                        base = identifier.split("(", 1)[0]
                        qualifiers = re.findall(r"\(([^)]*)\)", identifier)
                        schedule_paths.append(
                            "schedule/" + schedule_number + "/paragraph/" + "/".join([base] + qualifiers)
                        )
                    paths = schedule_paths + paths
            # Schedules in UK Acts/SIs expose nested paragraphs in the
            # legislation XML even when the ledger calls them subparagraphs
            # or sections. Convert ``Schedule 16, 1(4)(b)`` to the canonical
            # schedule paragraph path for exact source retrieval.
            schedule_match = re.search(r"\bschedule\s+(?P<number>\d+)\b", combined, re.I)
            citation_fields = " ".join((ref, ident))
            schedule_specific = bool(
                effective_kind in {"schedule", "subparagraph"}
                or re.search(r"\b(?:paragraph|subparagraph)\b", citation_fields, re.I)
            )
            if schedule_match and schedule_specific and instrument.legislation_type in {"ukpga", "uksi", "nisi"} and not building_societies_list:
                schedule_number = schedule_match.group("number")
                schedule_paths = []
                schedule_identifiers = list(identifiers)
                if re.search(r"\band\s*\([^)]*\)", combined, re.I):
                    for qualifier in re.findall(r"\band\s*\(([^)]*)\)", combined, re.I):
                        if schedule_identifiers:
                            base = schedule_identifiers[0].split("(", 1)[0]
                            candidate = base + "(" + qualifier + ")"
                            if candidate not in schedule_identifiers:
                                schedule_identifiers.append(candidate)
                for identifier in schedule_identifiers:
                    base = identifier.split("(", 1)[0]
                    qualifiers = re.findall(r"\(([^)]*)\)", identifier)
                    schedule_paths.append(
                        "schedule/" + schedule_number + "/paragraph/" + "/".join([base] + qualifiers)
                    )
                paths = schedule_paths + paths
            if instrument.instrument_id == "credit-unions-ni-order-1985":
                paths = [path.replace("section/", "article/", 1) for path in paths] + paths
            if instrument.instrument_id == "rao":
                paths = [path.replace("regulation/", "article/", 1) for path in paths] + paths
            if instrument.instrument_id == "mifir" and re.search(r"\barticle\s*2\b", combined, re.I):
                # MiFIR's paragraph 1A is nested in Article 2(1); the
                # article/2/1 node contains the complete definitions text.
                paths = ["article/2/1"] + paths
            if instrument.instrument_id == "brrd" and re.search(r"\barticle\s*2\s*\(\s*1\s*\)", combined, re.I):
                # BRRD point 100(a)-(d) is a point of Article 2(1), not a
                # top-level point URI.
                paths = ["article/2/1"] + paths
            if instrument.instrument_id == "fsma" and any(
                re.search(r"^section/31P/", path, re.I) for path in paths
            ):
                paths = [path.replace("section/31P/", "section/312P/", 1) for path in paths] + paths
            # OCR/footnote fusion can turn section 393 into ``39316`` or
            # section 1162 into ``11625``. If the exact target is absent,
            # prefer the longest existing parent section in the local corpus.
            if instrument.instrument_id in {"fsma", "companies-act-2006"}:
                repaired_paths = []
                for path in paths:
                    match = re.match(r"section/(\d{4,6})$", path, re.I)
                    if not match:
                        continue
                    digits = match.group(1)
                    for cut in range(len(digits) - 1, 2, -1):
                        candidate = "section/" + digits[:cut]
                        aliases = (
                            "external:legislation:" + instrument.instrument_id + ":" + candidate.replace("/", ":"),
                            "external:" + instrument.instrument_id + ":" + candidate.replace("/", ":"),
                        )
                        if any(node_id in self.nodes and node_has_text(self.nodes[node_id]) for node_id in aliases):
                            repaired_paths.append(candidate)
                            break
                paths = repaired_paths + paths
            # Remove a bare parent path when a qualified child path for the
            # same provision is present (e.g. article/74/3 alongside
            # article/74).  The aggregate must contain exactly the cited
            # provisions, not an accidental duplicate parent.
            specific_paths = [
                path
                for path in dict.fromkeys(paths)
                if not any(other != path and other.startswith(path + "/") for other in paths)
            ]
            aggregate = self.create_aggregate_provision_target(instrument, specific_paths)
            if aggregate is not None:
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="provision",
                    target_id=aggregate["id"],
                    target_type=aggregate["node_type"],
                    target_title=aggregate["title"],
                    target_url=aggregate["url"],
                    target_text_available=True,
                    resolver_method="policy_external_aggregate_provisions",
                    instrument_id=instrument.instrument_id,
                    provision_path=";".join(specific_paths),
                    confidence=max(0.94, extracted),
                    reason="all listed external provisions combined into a text-bearing target",
                )
            # For a multi-provision statutory citation, fetch the first
            # missing member before falling back to an existing single
            # section.  A later re-evaluation will then assemble the complete
            # aggregate and will not silently drop the remaining provisions.
            if building_societies_list:
                for path in specific_paths:
                    candidates = [node for node in self.external_index.get((instrument.instrument_id, path), ()) if node_has_text(node)]
                    if not candidates:
                        for legacy_id in (
                            "external:" + instrument.instrument_id + ":" + path.replace("/", ":"),
                            "external:legislation:" + instrument.instrument_id + ":" + path.replace("/", ":"),
                        ):
                            candidate = self.nodes.get(legacy_id)
                            if candidate is not None and node_has_text(candidate):
                                candidates = [candidate]
                                break
                    if not candidates:
                        target_id = external_provision_node_id(instrument, path)
                        return Outcome(
                            **base_kwargs,
                            status="needs_fetch",
                            scope="provision",
                            target_id=target_id,
                            target_type="external_reference",
                            target_title=f"{instrument.title} — {effective_kind} {ident}",
                            target_url=instrument.provision_url(path),
                            target_text_available=False,
                            resolver_method="policy_external_aggregate_provision_fetch",
                            instrument_id=instrument.instrument_id,
                            provision_path=path,
                            confidence=max(0.94, extracted),
                            reason="one member of a multi-section statutory citation requires official source fetch",
                            fetch_key=(instrument.instrument_id, path),
                            fetch_instrument=instrument,
                            fetch_path=path,
                        )
            for path in dict.fromkeys(paths):
                # The deterministic occurrence layer is the canonical owner
                # of legacy UK CRR provision identities. Prefer those nodes
                # when they already contain the official text, otherwise the
                # policy pass creates a second target for the same citation
                # (for example ``external:uk-crr:article:114`` alongside
                # ``external:legislation:uk-crr:article:114``).
                if instrument.instrument_id == "uk-crr":
                    legacy_paths = [path]
                    path_parts = path.split("/")
                    if len(path_parts) > 2:
                        legacy_paths.append("/".join(path_parts[:2]))
                    legacy_nodes = [
                        self.nodes[node_id]
                        for legacy_path in dict.fromkeys(legacy_paths)
                        for node_id in (
                            "external:uk-crr:" + legacy_path.replace("/", ":"),
                        )
                        if node_id in self.nodes and node_has_text(self.nodes[node_id])
                    ]
                    if legacy_nodes:
                        target = legacy_nodes[0]
                        return Outcome(
                            **base_kwargs,
                            status="resolved",
                            scope="provision",
                            target_id=target["id"],
                            target_type=target["node_type"],
                            target_title=target["title"],
                            target_url=target["url"],
                            target_text_available=True,
                            resolver_method="policy_external_canonical_uk_crr_provision",
                            instrument_id=instrument.instrument_id,
                            provision_path=path,
                            confidence=max(0.94, extracted),
                            reason="existing deterministic UK CRR provision identity reused",
                        )
                existing = self.external_index.get((instrument.instrument_id, path), [])
                existing = [node for node in existing if node_has_text(node)]
                if existing:
                    target = existing[0]
                    return Outcome(**base_kwargs, status="resolved", scope="provision", target_id=target["id"], target_type=target["node_type"], target_title=target["title"], target_url=target["url"], target_text_available=True, resolver_method="policy_external_provision_existing", instrument_id=instrument.instrument_id, provision_path=path, confidence=max(0.94, extracted), reason="registry-backed provision already has official source text")
                # Older legal-reference passes used the compact
                # ``external:<instrument>:<path>`` identity and did not
                # retain instrument/path metadata. Reuse that text-bearing
                # node before attempting another official fetch.
                legacy_ids = (
                    "external:" + instrument.instrument_id + ":" + path.replace("/", ":"),
                    "external:legislation:" + instrument.instrument_id + ":" + path.replace("/", ":"),
                )
                legacy_nodes = [
                    self.nodes[node_id]
                    for node_id in legacy_ids
                    if node_id in self.nodes and node_has_text(self.nodes[node_id])
                ]
                if legacy_nodes:
                    target = legacy_nodes[0]
                    return Outcome(
                        **base_kwargs,
                        status="resolved",
                        scope="provision",
                        target_id=target["id"],
                        target_type=target["node_type"],
                        target_title=target["title"],
                        target_url=target["url"],
                        target_text_available=True,
                        resolver_method="policy_external_provision_existing",
                        instrument_id=instrument.instrument_id,
                        provision_path=path,
                        confidence=max(0.94, extracted),
                        reason="legacy registry-backed provision already has official source text",
                    )
                # Older legal-reference passes materialised some complete
                # Articles under the ``external:uk-crr`` identity (rather
                # than the registry's ``external:legislation:uk-crr`` key).
                # A qualified citation can safely use that parent Article
                # when its text contains the requested paragraph.
                path_parts = path.split("/")
                parent_path = "/".join(path_parts[:2]) if len(path_parts) > 2 else ""
                if parent_path:
                    aliases = {
                        "external:uk-crr:" + parent_path.replace("/", ":"),
                        "external:legislation:" + instrument.instrument_id + ":" + parent_path.replace("/", ":"),
                    }
                    parent_nodes = [
                        self.nodes[node_id]
                        for node_id in aliases
                        if node_id in self.nodes and node_has_text(self.nodes[node_id])
                    ]
                    if parent_nodes:
                        target = parent_nodes[0]
                        return Outcome(
                            **base_kwargs,
                            status="resolved",
                            scope="provision",
                            target_id=target["id"],
                            target_type=target["node_type"],
                            target_title=target["title"],
                            target_url=target["url"],
                            target_text_available=True,
                            resolver_method="policy_external_parent_provision_existing",
                            instrument_id=instrument.instrument_id,
                            provision_path=path,
                            confidence=max(0.92, extracted),
                            reason="existing parent provision contains the requested qualified text",
                        )
                target_id = external_provision_node_id(instrument, path)
                if target_id in self.nodes and node_has_text(self.nodes[target_id]):
                    target = self.nodes[target_id]
                    return Outcome(**base_kwargs, status="resolved", scope="provision", target_id=target["id"], target_type=target["node_type"], target_title=target["title"], target_url=target["url"], target_text_available=True, resolver_method="policy_external_provision_existing", instrument_id=instrument.instrument_id, provision_path=path, confidence=max(0.94, extracted), reason="registry-backed provision already has official source text")
                return Outcome(**base_kwargs, status="needs_fetch", scope="provision", target_id=target_id, target_type="external_reference", target_title=f"{instrument.title} — {effective_kind} {ident}", target_url=instrument.provision_url(path), target_text_available=False, resolver_method="policy_external_provision_fetch", instrument_id=instrument.instrument_id, provision_path=path, confidence=max(0.94, extracted), reason="registry-backed provision requires official source fetch", fetch_key=(instrument.instrument_id, path), fetch_instrument=instrument, fetch_path=path)

        # A remaining specific reference may still be a Rulebook provision
        # whose document name was abbreviated or omitted.  Use source context
        # once more, and accept a unique text-bearing candidate.
        if identifiers:
            for fallback_kind in ("rule", "article", "section", "paragraph"):
                target = self.find_internal(row, source, fallback_kind, identifiers)
                if target is not None and node_has_text(target):
                    return Outcome(**base_kwargs, status="resolved", scope="provision", target_id=target["id"], target_type=target["node_type"], target_title=target["title"], target_url=target["url"], target_text_available=True, resolver_method="policy_internal_context_fallback", confidence=max(0.90, extracted), reason="unique source-context provision")

        # External guidance/standards are document-level under the policy when
        # their source text is not part of the Rulebook corpus. Preserve the
        # citation as a hyperlink to the named publisher instead of leaving a
        # false unresolved provision.
        documentish = " ".join((doc, ref, ident))
        source_meta = metadata(source)
        source_document_title = compact(source_meta.get("document_title") or source_meta.get("source_title") or "")
        source_document_context = bool(
            source_document_title
            and source["node_type"] in {"guidance_paragraph", "guidance_section"}
            and re.search(r"\b(?:document|statement[s]?|ICAA|annex|chapter|paragraphs?)\b", documentish, re.I)
        )
        if source_document_context or re.search(r"https?://", documentish + " " + (row["evidence_quote"] or ""), re.I) or re.search(
            r"\b(?:guidelines?|guidance|supervisory|statement[s]?\s+of\s+policy|approach|handbook|EBA|EIOPA|Basel|FSB|IAS\s*\d+|ISA\b|SS\s*\d+[/ ]\d+|SoP\s*\d*[/ ]?\d*|FiR|ViR|MGC|FMI|SFCR|GL\b|IFPR|Annex\s+\d+|Parliamentary|BSA\b|Model\s+Rules|Listing\s+Rules?|template[s]?|CP\s*\d+[/ ]\d+)\b",
            documentish,
            re.I,
        ) and not re.search(r"\b(?:act|regulations?|directive)\b", documentish, re.I):
            doc_node = self.create_document_target(row, source, None)
            if doc_node is not None:
                return Outcome(
                    **base_kwargs,
                    status="resolved",
                    scope="document",
                    target_id=doc_node["id"],
                    target_type=doc_node["node_type"],
                    target_title=doc_node["title"],
                    target_url=doc_node["url"],
                    target_text_available=node_has_text(doc_node),
                    resolver_method="policy_external_guidance_link",
                    confidence=max(0.60, extracted),
                    reason="external guidance/standard linked to its publisher document",
                )

        # This is a distinct unresolved extraction that cannot be safely
        # linked.  Record it explicitly; the caller will report it and apply a
        # final document-link fallback only for genuinely generic references.
        return Outcome(**base_kwargs, status="unresolved", scope="provision", resolver_method="policy_unresolved", confidence=extracted, reason="no unique text-bearing provision or document target")


def ensure_resolution_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_reference_resolution)")}
    if "resolution_status" not in columns:
        conn.execute("ALTER TABLE llm_reference_resolution ADD COLUMN resolution_status TEXT NOT NULL DEFAULT ''")
    if "resolution_scope" not in columns:
        conn.execute("ALTER TABLE llm_reference_resolution ADD COLUMN resolution_scope TEXT NOT NULL DEFAULT ''")


def fetch_missing(
    outcomes: Iterable[Outcome],
    *,
    cache_root: Path,
    workers: int,
) -> tuple[dict[tuple[str, str], Any], dict[tuple[str, str], str]]:
    unique: dict[tuple[str, str], tuple[Instrument, str]] = {}
    for outcome in outcomes:
        if outcome.status == "needs_fetch" and outcome.fetch_key and outcome.fetch_instrument:
            unique.setdefault(outcome.fetch_key, (outcome.fetch_instrument, outcome.fetch_path))
    fetched: dict[tuple[str, str], Any] = {}
    errors: dict[tuple[str, str], str] = {}
    if not unique:
        return fetched, errors
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                fetch_official_provision,
                instrument,
                path,
                fetcher=cache_fetcher(cache_root, instrument, path),
            ): key
            for key, (instrument, path) in unique.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                fetched[key] = future.result()
            except Exception as exc:  # pragma: no cover - network-dependent
                errors[key] = f"{type(exc).__name__}: {exc}"
    return fetched, errors


def apply_outcomes(
    conn: sqlite3.Connection,
    resolver: PolicyResolver,
    outcomes: list[Outcome],
    fetched: dict[tuple[str, str], Any],
    fetch_errors: dict[tuple[str, str], str],
) -> dict[str, int]:
    # Insert fetched official provisions first so every provision outcome has a
    # real text-bearing target before edges/occurrences are written.
    for key, official in fetched.items():
        instrument = resolver.registry.by_id[key[0]]
        materialize_target(conn, instrument=instrument, provision_path=key[1], official=official)
        target_id = external_provision_node_id(instrument, key[1])
        node = conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id=?", (target_id,)).fetchone()
        if node:
            resolver.nodes[target_id] = dict(node) | {"meta": metadata(node)}
            resolver.external_index[(instrument.instrument_id, key[1])] = [resolver.nodes[target_id]]
    for node in resolver.nodes.values():
        if not str(node.get("id") or "").startswith("external:document:"):
            continue
        if str(node.get("id") or "").startswith("external:document:fca:"):
            # Keep a previously materialised FCA target in sync when a newer
            # layout-aware extraction replaces a TOC-only fragment with the
            # substantive rule block.  INSERT OR IGNORE alone would leave the
            # stale text in SQLite even though the resolver has the corrected
            # node in memory.
            conn.execute(
                "UPDATE node SET node_type=?,stable_key=?,title=?,text=?,url=?,metadata_json=? WHERE id=?",
                (node["node_type"], node["stable_key"], node["title"], node.get("text") or "", node.get("url") or "", node.get("metadata_json") or json.dumps(node.get("meta") or {}, ensure_ascii=False, sort_keys=True), node["id"]),
            )
        conn.execute(
            "INSERT OR IGNORE INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES (?,?,?,?,?,?,?)",
            (
                node["id"],
                node["node_type"],
                node["stable_key"],
                node["title"],
                node.get("text") or "",
                node.get("url") or "",
                node.get("metadata_json") or json.dumps(node.get("meta") or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
    if resolver.nodes:
        conn.commit()
    if fetched:
        conn.commit()

    counts = Counter()
    parsed_occurrence_cache: dict[str, list[Any]] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for outcome in outcomes:
            row = conn.execute("SELECT * FROM llm_reference_resolution WHERE id=?", (outcome.resolution_id,)).fetchone()
            if row is None:
                continue
            old_target_id = str(row["target_node_id"] or "")
            target = resolver.nodes.get(outcome.target_id)
            if outcome.status == "needs_fetch":
                if outcome.fetch_key in fetch_errors:
                    # A failed official fetch is not silently promoted to a
                    # text-bearing provision.  If the citation names a
                    # document, retain a document hyperlink and mark the
                    # failed specific provision for audit.
                    instrument = outcome.fetch_instrument
                    doc_node = resolver.create_document_target(row, resolver.nodes[outcome.source_id], instrument) if instrument else None
                    if doc_node:
                        # This document node may have been created while
                        # handling a failed fetch, after the initial batch of
                        # resolver nodes was inserted. Materialise it before
                        # writing the ledger/edge target.
                        conn.execute(
                            "INSERT OR IGNORE INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES (?,?,?,?,?,?,?)",
                            (
                                doc_node["id"],
                                doc_node["node_type"],
                                doc_node["stable_key"],
                                doc_node["title"],
                                doc_node.get("text") or "",
                                doc_node.get("url") or "",
                                doc_node.get("metadata_json") or json.dumps(doc_node.get("meta") or {}, ensure_ascii=False, sort_keys=True),
                            ),
                        )
                        outcome.status = "resolved"
                        outcome.scope = "document"
                        outcome.target_id = doc_node["id"]
                        outcome.target_type = doc_node["node_type"]
                        outcome.target_title = doc_node["title"]
                        outcome.target_url = doc_node["url"]
                        outcome.target_text_available = node_has_text(doc_node)
                        outcome.resolver_method = "policy_document_link_after_provision_fetch_failure"
                        outcome.reason = "official provision fetch failed; retained source document link"
                    else:
                        outcome.status = "not_reference"
                        outcome.scope = "not_reference"
                        outcome.resolver_method = "policy_not_reference_fetch_failure"
                        outcome.reason = "official provision fetch failed and no source document URL was available"
                else:
                    target = resolver.nodes.get(outcome.target_id)
                    if target is not None:
                        outcome.status = "resolved"
                        outcome.target_type = target["node_type"]
                        outcome.target_title = target["title"]
                        outcome.target_url = target["url"]
                        outcome.target_text_available = node_has_text(target)
                        outcome.resolver_method = "policy_external_provision_fetched"
                        outcome.reason = "official source text fetched and materialised"
            if outcome.status == "unresolved":
                # Last-resort policy treatment: use a known source document
                # only when the citation is generic or the text itself does
                # not contain a usable specific-provision phrase.  A genuine
                # specific citation is recorded as an explicit not-reference
                # only when no safe target exists; it is never given a blank
                # unresolved status.
                source = resolver.nodes.get(outcome.source_id)
                doc_node = resolver.find_document(row, source) if source else None
                if doc_node and not re.search(r"\b(?:article|art\.?|section|s\.?|regulation|reg\.?|rule|paragraph|para\.?)\s*[0-9]", row["reference_text"] or "", re.I):
                    outcome.status = "resolved"
                    outcome.scope = "document"
                    outcome.target_id = doc_node["id"]
                    outcome.target_type = doc_node["node_type"]
                    outcome.target_title = doc_node["title"]
                    outcome.target_url = doc_node["url"]
                    outcome.target_text_available = node_has_text(doc_node)
                    outcome.resolver_method = "policy_document_link_fallback"
                    outcome.reason = "generic reference linked to source document"
                else:
                    outcome.status = "not_reference"
                    outcome.scope = "not_reference"
                    outcome.resolver_method = "policy_not_reference_unresolvable"
                    outcome.reason = "specific-looking extraction had no unique text-bearing target"

            if outcome.status == "resolved" and outcome.target_id:
                original_target_id = outcome.target_id
                canonical_id, canonical_target = canonical_semantic_target(
                    resolver,
                    original_target_id,
                )
                if canonical_id != original_target_id and canonical_target is not None:
                    original_target = resolver.nodes.get(original_target_id) or {}
                    outcome.target_id = canonical_id
                    outcome.target_type = canonical_target.get("node_type", "provision")
                    outcome.target_title = canonical_target.get("title") or outcome.target_title
                    # Preserve the dated version URL and text-availability
                    # fact for the ledger and reader projection.
                    outcome.target_url = outcome.target_url or original_target.get("url", "")
                    outcome.target_text_available = outcome.target_text_available or node_has_text(original_target)
                    outcome.metadata = {
                        **outcome.metadata,
                        "canonical_target_id": canonical_id,
                        "resolved_version_id": original_target_id,
                    }

            # Create the graph edge and lexical occurrence for accepted
            # document/provision links whenever the source citation span is
            # exact.  Rows without a trustworthy span remain explicit ledger
            # outcomes but are not given fabricated click targets.
            if outcome.status == "resolved" and outcome.target_id:
                # A policy row is an extraction-level fact, not the identity
                # of a lexical occurrence.  Remove this row's previous
                # materialisations before rebuilding them from source text.
                # This is essential when a later policy pass changes a
                # target, and also when an old one-span row is expanded to
                # several textual occurrences.
                previous_policy_occurrences = conn.execute(
                    "SELECT occurrence_id,edge_id,metadata_json FROM reference_occurrence WHERE source_node_id=? AND source_method=?",
                    (outcome.source_id, METHOD),
                ).fetchall()
                for previous_occurrence in previous_policy_occurrences:
                    if metadata(previous_occurrence).get("resolution_id") == outcome.resolution_id:
                        conn.execute(
                            "DELETE FROM reference_occurrence WHERE occurrence_id=?",
                            (previous_occurrence["occurrence_id"],),
                        )
                # If a repair replaces a title-only/wrong target, remove the
                # stale policy edge when no other ledger row still uses that
                # exact source/target pair.  Otherwise the old edge would
                # remain visible beside the corrected source text.
                if old_target_id and old_target_id != outcome.target_id:
                    stale_edge = conn.execute(
                        "SELECT id,source_method FROM edge WHERE from_node_id=? AND to_node_id=? ORDER BY CASE WHEN source_method=? THEN 0 ELSE 1 END,id LIMIT 1",
                        (outcome.source_id, old_target_id, METHOD),
                    ).fetchone()
                    still_used = conn.execute(
                        "SELECT 1 FROM llm_reference_resolution WHERE id<>? AND source_node_id=? AND target_node_id=? LIMIT 1",
                        (outcome.resolution_id, outcome.source_id, old_target_id),
                    ).fetchone()
                    has_non_policy_occurrence = (
                        conn.execute(
                            "SELECT 1 FROM reference_occurrence WHERE edge_id=? AND source_method<>? LIMIT 1",
                            (stale_edge["id"], METHOD),
                        ).fetchone()
                        if stale_edge
                        else None
                    )
                    if (
                        stale_edge
                        and not still_used
                        and stale_edge["source_method"] == METHOD
                        and not has_non_policy_occurrence
                    ):
                        conn.execute("DELETE FROM reference_occurrence WHERE edge_id=?", (stale_edge["id"],))
                        conn.execute("DELETE FROM edge WHERE id=?", (stale_edge["id"],))
                target = resolver.nodes.get(outcome.target_id)
                if target is None:
                    target_row = conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id=?", (outcome.target_id,)).fetchone()
                    target = dict(target_row) | {"meta": metadata(target_row)} if target_row else None
                if target is None:
                    outcome.status = "not_reference"
                    outcome.scope = "not_reference"
                    outcome.resolver_method = "policy_not_reference_missing_target"
                    outcome.reason = "policy target disappeared before graph materialisation"
                else:
                    source = resolver.nodes.get(outcome.source_id)
                    if source is not None and outcome.span_start is not None and outcome.span_end is not None:
                        text = source_text(source)
                        if outcome.source_id not in parsed_occurrence_cache:
                            parsed_occurrence_cache[outcome.source_id] = citation_occurrences(
                                source_node_id=outcome.source_id,
                                value=text,
                                registry=resolver.registry,
                                source_title=source.get("title", ""),
                            )
                        parsed_occurrences = policy_citation_occurrences(
                            source_node_id=outcome.source_id,
                            source_text=text,
                            source_title=source.get("title", ""),
                            row=row,
                            registry=resolver.registry,
                            parsed=parsed_occurrence_cache[outcome.source_id],
                        )
                        # Keep the old exact span as a safe fallback for
                        # references that the deterministic parser does not
                        # recognise.  Recognised provision rows are expanded
                        # to every matching lexical span instead.
                        occurrence_candidates = parsed_occurrences or [None]
                        first_span_start = (
                            parsed_occurrences[0].span_start
                            if parsed_occurrences
                            else outcome.span_start
                        )
                        first_span_end = (
                            parsed_occurrences[0].span_end
                            if parsed_occurrences
                            else outcome.span_end
                        )
                        existing_edge = conn.execute(
                            "SELECT id FROM edge WHERE from_node_id=? AND to_node_id=? ORDER BY CASE WHEN source_method=? THEN 0 ELSE 1 END,id LIMIT 1",
                            (outcome.source_id, outcome.target_id, METHOD),
                        ).fetchone()
                        if existing_edge:
                            edge_id = existing_edge[0]
                        else:
                            edge_id = digest(outcome.source_id, outcome.target_id, METHOD, length=20)
                            evidence_start = max(0, first_span_start - 220)
                            evidence_end = min(len(text), first_span_end + 220)
                            edge_meta = {
                                "reference": outcome.quoted_text,
                                "target_title": target["title"],
                                "target_node_type": target["node_type"],
                                "relationship_type": "REF",
                                "source_span": {"start": first_span_start, "end": first_span_end},
                                "proposal_method": METHOD,
                                "resolution_id": outcome.resolution_id,
                                "policy_scope": outcome.scope,
                                "evidence_status": "direct_text" if outcome.scope == "provision" else "document_metadata",
                            }
                            conn.execute(
                                "INSERT OR IGNORE INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                                (edge_id, outcome.source_id, outcome.target_id, "references", METHOD, outcome.confidence, compact(text[evidence_start:evidence_end]), source.get("url", ""), json.dumps(edge_meta, ensure_ascii=False, sort_keys=True)),
                            )
                        for parsed_occurrence in occurrence_candidates:
                            if parsed_occurrence is None:
                                occurrence_span_start = outcome.span_start
                                occurrence_span_end = outcome.span_end
                                citation_kind = singular(row["target_kind"]) or "reference"
                                citation_text = outcome.quoted_text
                                group_text = outcome.quoted_text
                                instrument_id = outcome.instrument_id
                                provision_path = outcome.provision_path
                                qualifier = ""
                                group_seed = f"{occurrence_span_start}|{occurrence_span_end}|{citation_text}"
                                parser_metadata: dict[str, Any] = {}
                            else:
                                occurrence_span_start = parsed_occurrence.span_start
                                occurrence_span_end = parsed_occurrence.span_end
                                citation_kind = parsed_occurrence.kind
                                citation_text = parsed_occurrence.citation_text
                                group_text = parsed_occurrence.group_text
                                instrument_id = (
                                    parsed_occurrence.instrument.instrument_id
                                    if parsed_occurrence.instrument
                                    else outcome.instrument_id
                                )
                                provision_path = parsed_occurrence.provision_path or outcome.provision_path
                                qualifier = "".join(f"({part})" for part in parsed_occurrence.target.qualifiers)
                                group_seed = parsed_occurrence.group_id
                                parser_metadata = {
                                    **parsed_occurrence.metadata,
                                    "instrument_evidence": parsed_occurrence.instrument_evidence,
                                }
                            occurrence_id = digest(
                                outcome.source_id,
                                outcome.target_id,
                                occurrence_span_start,
                                occurrence_span_end,
                                citation_text,
                                METHOD,
                                length=28,
                            )
                            group_id = digest(
                                outcome.source_id,
                                group_seed,
                                outcome.target_id,
                                METHOD,
                                length=24,
                            )
                            existing_occ = conn.execute(
                                "SELECT occurrence_id FROM reference_occurrence WHERE occurrence_id=?",
                                (occurrence_id,),
                            ).fetchone()
                            context = compact(
                                text[
                                    max(0, occurrence_span_start - 220) : min(
                                        len(text), occurrence_span_end + 220
                                    )
                                ]
                            )
                            occ_meta = {
                                **parser_metadata,
                                "policy_version": METHOD,
                                "resolution_id": outcome.resolution_id,
                                "target_text_available": bool(outcome.target_text_available),
                                "target_url": outcome.target_url,
                                "reason": outcome.reason,
                            }
                            if not existing_occ:
                                conn.execute(
                                    "INSERT INTO reference_occurrence(occurrence_id,group_id,source_node_id,target_node_id,edge_id,relationship_type,citation_kind,citation_text,group_text,instrument_id,provision_path,qualifier,span_start,span_end,status,source_method,confidence,context_text,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (
                                        occurrence_id,
                                        group_id,
                                        outcome.source_id,
                                        outcome.target_id,
                                        edge_id,
                                        "REF",
                                        citation_kind,
                                        citation_text,
                                        group_text,
                                        instrument_id or None,
                                        provision_path or None,
                                        qualifier,
                                        occurrence_span_start,
                                        occurrence_span_end,
                                        "materialized",
                                        METHOD,
                                        outcome.confidence,
                                        context,
                                        json.dumps(occ_meta, ensure_ascii=False, sort_keys=True),
                                        now(),
                                        now(),
                                    ),
                                )
                            else:
                                # Reprocessing can change the target
                                # provenance without changing the lexical
                                # occurrence. Keep the materialised row in
                                # step with the corrected outcome.
                                conn.execute(
                                    "UPDATE reference_occurrence SET group_id=?,source_node_id=?,target_node_id=?,edge_id=?,instrument_id=?,provision_path=?,citation_kind=?,citation_text=?,group_text=?,qualifier=?,span_start=?,span_end=?,status=?,confidence=?,context_text=?,metadata_json=?,updated_at=? WHERE occurrence_id=?",
                                    (
                                        group_id,
                                        outcome.source_id,
                                        outcome.target_id,
                                        edge_id,
                                        instrument_id or None,
                                        provision_path or None,
                                        citation_kind,
                                        citation_text,
                                        group_text,
                                        qualifier,
                                        occurrence_span_start,
                                        occurrence_span_end,
                                        "materialized",
                                        outcome.confidence,
                                        context,
                                        json.dumps(occ_meta, ensure_ascii=False, sort_keys=True),
                                        now(),
                                        occurrence_id,
                                    ),
                                )
                            counts["materialized_occurrences"] += 1
                    else:
                        counts["resolved_without_exact_span"] += 1

            resolution_meta = metadata(row)
            resolution_meta.update(outcome.metadata)
            resolution_meta.update({
                "policy_version": METHOD,
                "policy_scope": outcome.scope,
                "policy_reason": outcome.reason,
                "target_text_available": bool(outcome.target_text_available),
                "target_url": outcome.target_url,
                "provision_path": outcome.provision_path,
                "instrument_id": outcome.instrument_id,
            })
            conn.execute(
                "UPDATE llm_reference_resolution SET target_node_id=?,target_node_type=?,target_title=?,resolver_method=?,resolver_confidence=?,already_had_edge=?,added_edge_id=?,metadata_json=?,resolution_status=?,resolution_scope=? WHERE id=?",
                (outcome.target_id if outcome.status == "resolved" else "", outcome.target_type if outcome.status == "resolved" else "", outcome.target_title if outcome.status == "resolved" else "", outcome.resolver_method, outcome.confidence, int(outcome.status == "resolved" and bool(conn.execute("SELECT 1 FROM edge WHERE from_node_id=? AND to_node_id=? LIMIT 1", (outcome.source_id, outcome.target_id)).fetchone())), "", json.dumps(resolution_meta, ensure_ascii=False, sort_keys=True), outcome.status, outcome.scope, outcome.resolution_id),
            )
            counts[outcome.status] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(counts)


def run(args: argparse.Namespace) -> dict[str, Any]:
    db = Path(args.db)
    conn = connect(db, timeout=120)
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_resolution_columns(conn)
    registry = InstrumentRegistry.load(args.instrument_registry)
    resolver = PolicyResolver(conn, registry)
    # Reprocess both residual rows and any previously accepted target that
    # fails the source-availability contract.  Earlier passes sometimes
    # pointed a provision at a title-only chapter/section; those rows must be
    # repaired even though they already have a target id.
    reprocess_existing_clause = """
        OR coalesce(l.resolution_status,'') = ''
        OR coalesce(l.resolution_scope,'') = 'provision'
        OR (
            coalesce(l.resolution_scope,'') = 'document'
            AND lower(coalesce(l.target_kind,'')) IN ('rule','article','section','paragraph','point','subparagraph','regulation')
        )
    """ if args.reprocess_existing else ""
    query = f"""
        SELECT l.*
        FROM llm_reference_resolution l
        LEFT JOIN node n ON n.id = l.target_node_id
        WHERE (
            trim(l.target_node_id) = ''
            AND coalesce(l.resolution_status,'') IN ('','unresolved')
        ) OR (
            coalesce(l.resolution_scope,'') = 'provision'
            AND (n.id IS NULL OR trim(coalesce(n.text,'')) = '')
        ) OR (
            coalesce(l.resolution_scope,'') = 'document'
            AND (n.id IS NULL OR trim(coalesce(n.url,'')) = '')
        ) OR (
            coalesce(l.resolution_scope,'') = ''
            AND trim(l.target_node_id) <> ''
            AND (n.id IS NULL OR trim(coalesce(n.text,'')) = '')
        ) OR (
            -- Bare numeric Rulebook identifiers are specific provisions even
            -- when an earlier pass classified them as document links. Give
            -- the internal Part resolver a chance to recover their text.
            coalesce(l.resolution_scope,'') = 'document'
            AND lower(coalesce(l.target_kind,'')) IN ('rule','article','section','paragraph','point','subparagraph','regulation')
            AND n.id IS NOT NULL
            AND trim(coalesce(n.text,'')) = ''
        ) OR (
            -- Some external-handbook citations were extracted with a broad
            -- kind (for example ``COBS 20.4.7R`` as ``external``).  A dotted
            -- COBS identifier is still a specific provision and must be
            -- re-evaluated against the FCA sourcebook text.
            coalesce(l.resolution_scope,'') = 'document'
            AND lower(coalesce(l.target_kind,'')) = 'external'
            AND lower(coalesce(l.reference_text,'')) LIKE '%cobs %.%.%'
        ) OR (
            -- Earlier passes retained a document hyperlink after an official
            -- provision fetch failed. Revisit those rows whenever the source
            -- parser gains a better path or the Rulebook corpus already
            -- contains the cited text.
            l.resolver_method = 'policy_document_link_after_provision_fetch_failure'
        ) OR (
            -- Replace an earlier EBA search-result fallback with the official
            -- Guidelines PDF when the named guidance instrument is known.
            l.resolver_method = 'policy_external_guidance_link'
            AND lower(coalesce(l.target_part_or_document,'')) LIKE '%eba%'
            AND lower(coalesce(l.target_part_or_document,'')) LIKE '%guideline%'
            AND lower(coalesce(l.target_part_or_document,'')) LIKE '%internal governance%'
            AND lower(coalesce(n.url,'')) LIKE '%eba.europa.eu/search%'
        )
        {reprocess_existing_clause}
        ORDER BY l.source_node_id,l.id
    """
    if args.limit:
        query += f" LIMIT {int(args.limit)}"
    rows = conn.execute(query).fetchall()
    sources = {
        row["id"]: row
        for row in conn.execute("SELECT id,node_type,title,text,url,metadata_json FROM node WHERE id IN (%s)" % ",".join("?" for _ in {row['source_node_id'] for row in rows}), list({row["source_node_id"] for row in rows}))
    } if rows else {}
    outcomes: list[Outcome] = []
    counts = Counter()
    for row in rows:
        source = sources.get(row["source_node_id"])
        if source is None:
            outcome = Outcome(resolution_id=row["id"], source_id=row["source_node_id"], status="not_reference", scope="not_reference", resolver_method="policy_not_reference_missing_source", confidence=1.0, reason="source node missing")
        else:
            outcome = resolver.resolve_row(row, source, apply_nodes=args.apply)
        outcomes.append(outcome)
        counts[(outcome.status, outcome.scope)] += 1
        if args.progress_every and len(outcomes) % args.progress_every == 0:
            print(f"planned {len(outcomes)}/{len(rows)}", flush=True)

    fetch_summary: dict[str, Any] = {"requested": 0, "fetched": 0, "errors": 0, "error_details": {}}
    fetched: dict[tuple[str, str], Any] = {}
    fetch_errors: dict[tuple[str, str], str] = {}
    if args.apply:
        backup = db.with_name(db.name + ".pre-policy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        if not backup.exists():
            shutil.copy2(db, backup)
        fetched, fetch_errors = fetch_missing(outcomes, cache_root=args.cache_root, workers=args.fetch_workers)
        fetch_summary = {
            "requested": len({outcome.fetch_key for outcome in outcomes if outcome.fetch_key}),
            "fetched": len(fetched),
            "errors": len(fetch_errors),
            "error_details": {f"{key[0]}:{key[1]}": value for key, value in fetch_errors.items()},
        }
        apply_summary = apply_outcomes(conn, resolver, outcomes, fetched, fetch_errors)
        ensure_indexes(conn)
        conn.commit()
    else:
        apply_summary = {}

    # Keep the audit useful without requiring a second expensive resolver
    # pass: the residual rows are captured while their outcomes are still in
    # memory.  This makes every unresolved case reviewable by citation and
    # source context, rather than reporting only a scalar count.
    row_by_id = {row["id"]: row for row in rows}
    unresolved_groups = Counter()
    unresolved_examples: list[dict[str, Any]] = []
    for outcome in outcomes:
        if outcome.status != "unresolved":
            continue
        row = row_by_id.get(outcome.resolution_id)
        if row is None:
            continue
        group = (
            row["target_kind"] or "",
            row["target_part_or_document"] or "",
            row["reference_text"] or "",
            row["target_title_or_identifier"] or "",
        )
        unresolved_groups[" | ".join(group)] += 1
        if len(unresolved_examples) < 250:
            source = sources.get(row["source_node_id"])
            unresolved_examples.append({
                "resolution_id": row["id"],
                "source_id": row["source_node_id"],
                "source_title": source["title"] if source else "",
                "target_kind": row["target_kind"] or "",
                "target_part_or_document": row["target_part_or_document"] or "",
                "reference_text": row["reference_text"] or "",
                "target_title_or_identifier": row["target_title_or_identifier"] or "",
                "evidence_quote": row["evidence_quote"] or "",
                "reason": outcome.reason,
            })

    summary = {
        "method": METHOD,
        "db": str(db),
        "apply": bool(args.apply),
        "input_rows": len(rows),
        "planned_outcomes": {f"{status}:{scope}": count for (status, scope), count in sorted(counts.items())},
        "fetch": fetch_summary,
        "applied": apply_summary,
        "unresolved_groups": dict(unresolved_groups.most_common()),
        "unresolved_examples": unresolved_examples,
        "post_unresolved": conn.execute("SELECT COUNT(*) FROM llm_reference_resolution WHERE trim(target_node_id)='' AND coalesce(resolution_status,'') IN ('','unresolved')").fetchone()[0] if args.apply else None,
        "generated_at": now(),
    }
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    conn.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--instrument-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--cache-root", type=Path, default=ROOT / "backend/data/raw/legal-provisions")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--fetch-workers", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reprocess-existing",
        action="store_true",
        help="Re-evaluate prior resolved rows as well as residual rows; use after resolver rules change.",
    )
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())