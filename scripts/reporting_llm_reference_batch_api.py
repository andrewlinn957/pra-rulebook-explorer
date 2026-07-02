#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
PACKAGES = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages/packages"
RUNS = ROOT / "logs/reporting-llm-reference-api-batches"
API_BASE = "https://api.openai.com/v1"
PROMPT_VERSION = "reporting-reference-extract-v1"
DEFAULT_MODEL = os.environ.get("PRA_REPORTING_LLM_REFERENCE_MODEL") or "gpt-4.1-nano"

REF_RX = re.compile(
    r"\b(article|rule|chapter|annex|template|table|paragraph|regulation|directive|CRR|UK CRR|FSMA|SS\s*\d|SoP\s*\d|statement of policy|supervisory statement|PRA\d{3}|COR\d{3}|LVR\d{3}|FSA\d{3}|REP\d{3}|MLAR)\b",
    re.I,
)

SYSTEM_PROMPT = """You extract explicit regulatory and reporting cross-references from PRA/Bank of England reporting source text.
Return JSON only. Do not infer targets from outside knowledge. Do not invent references.
Extract references a human reader would understand as pointing to another reporting return, data item, template, table, annex, instruction set, validation rule, PRA Rulebook provision, CRR/UK CRR article, PRA supervisory statement, statement of policy, consultation/policy statement, statute, regulation, directive, or external regulatory source.
Include references even if oddly formatted, partial, non-linked, or embedded in prose.
Do not include purely generic mentions with no target, such as "this template", "the firm", "the PRA", "these instructions", unless a distinct target is named.
Do not include the source document itself merely because its own title appears.
For each reference, quote the exact supporting words from the source text.
If there are no references, return {"references": []}.
"""

USER_TEMPLATE = """Extract explicit cross-references from this reporting source span.

Return exactly this JSON shape:
{{
  "references": [
    {{
      "reference_text": "exact referenced phrase as written",
      "target_kind": "data_item|reporting_return|template|table|annex|instruction|validation_rule|rule|article|chapter|section|guidance|definition|legal_instrument|statute|regulation|directive|policy_statement|consultation|external|unknown",
      "target_title_or_identifier": "best target identifier/title from the text, e.g. COR011, PRA101, C 72.00, Article 430, Annex XI",
      "target_part_or_document": "explicit Part/document/source named in the text, or empty string",
      "jurisdiction_or_source": "PRA Rulebook|PRA|Bank of England|CRR|UK CRR|FSMA|EBA|EU|other|unknown",
      "evidence_quote": "short exact quote from the source text",
      "reason": "brief reason this is a cross-reference",
      "confidence": 0.0
    }}
  ]
}}

Source span:
- span_id: {span_id}
- source_title: {source_title}
- source_url: {source_url}
- file_type: {file_type}
- span_type: {span_type}
- page: {page}
- sheet: {sheet}
- row: {row}
- heading: {heading}

Text:
{text}
"""

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS reporting_llm_reference_extraction (
  span_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  response_json TEXT DEFAULT '{{}}',
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (span_id) REFERENCES source_span(span_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reporting_llm_reference_resolution (
  id TEXT PRIMARY KEY,
  span_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  ref_index INTEGER NOT NULL,
  reference_text TEXT NOT NULL,
  target_kind TEXT DEFAULT '',
  target_title_or_identifier TEXT DEFAULT '',
  target_part_or_document TEXT DEFAULT '',
  evidence_quote TEXT DEFAULT '',
  extracted_confidence REAL DEFAULT 0,
  target_node_id TEXT DEFAULT '',
  target_node_type TEXT DEFAULT '',
  target_label TEXT DEFAULT '',
  resolver_method TEXT DEFAULT '',
  resolver_confidence REAL DEFAULT 0,
  added_edge_id TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{{}}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reporting_llm_ref_extraction_source ON reporting_llm_reference_extraction(source_id);
CREATE INDEX IF NOT EXISTS idx_reporting_llm_ref_resolution_span ON reporting_llm_reference_resolution(span_id);
CREATE INDEX IF NOT EXISTS idx_reporting_llm_ref_resolution_target ON reporting_llm_reference_resolution(target_node_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def edge_id(*parts: str) -> str:
    return "edge:" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def norm(value: str) -> str:
    value = (value or "").lower().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


ARTICLE_TOKEN_RE = r"[0-9]+[A-Za-z]?(?:\([^)]+\))*"
ARTICLE_RANGE_END_RE = rf"(?:{ARTICLE_TOKEN_RE}|\(\s*[A-Za-z0-9-]+\s*\))"
ARTICLE_SEGMENT_RE = rf"{ARTICLE_TOKEN_RE}(?:\s*(?:to|-)\s*{ARTICLE_RANGE_END_RE})?"
ARTICLE_RANGE_SPEC_RE = rf"{ARTICLE_SEGMENT_RE}(?:\s*(?:,|and|or)\s*{ARTICLE_SEGMENT_RE})*"


def _article_range_context(value: str, match_end: int | None = None) -> str:
    """Best-effort legal instrument/context for article-range target nodes."""
    window = value[match_end or 0 : (match_end or 0) + 140]
    before = value[: match_end or len(value)]
    combined = f"{window} {value}"
    part = _rulebook_part_context(value)
    if part not in {"Regulatory reference", "PRA Rulebook", "CRR"}:
        return part
    reg = re.search(r"\b(?:Delegated\s+)?Regulation\s*\(EU\)\s*(?:No\s*)?\d{4}\s*/\s*\d+\b", combined, re.I)
    if reg:
        return re.sub(r"\s+", " ", reg.group(0)).strip()
    directive = re.search(r"\b[A-Z][A-Za-z ]{2,40}\s+Directive\b", combined, re.I)
    if directive:
        return re.sub(r"\s+", " ", directive.group(0)).strip()
    # Prefer a nearby CRD marker over a later CRR marker in mixed references such
    # as "Articles 12 and 28 to 31 CRD and Article 93 CRR".
    near = f"{before[-80:]} {window}"
    crd_pos = re.search(r"\bCRD\b", near, re.I)
    crr_pos = re.search(r"\b(?:UK\s+)?CRR\b|\(CRR\)", near, re.I)
    if crd_pos and (not crr_pos or crd_pos.start() < crr_pos.start()):
        return "CRD"
    if crr_pos or re.search(r"\b(?:UK\s+)?CRR\b|\(CRR\)", combined, re.I):
        return "CRR"
    if re.search(r"\bCRD\b", combined, re.I):
        return "CRD"
    return ""


def _clean_article_range_spec(spec: str) -> str:
    spec = re.sub(r"\s+", " ", spec or "").strip(" ,.;")
    spec = re.sub(r"\bto(?=[0-9])", "to ", spec, flags=re.I)
    spec = re.sub(r"\s*-\s*", "-", spec)
    return spec


def canonical_article_range_refs(value: str) -> list[tuple[str, str]]:
    """Materialisable Article range/list references.

    These are reference-target nodes unless exact article text is separately loaded.
    We materialise ranges/lists rather than resolving to the first singular Article,
    which would create false precision for references like "Articles 223 to 228 CRR".
    """
    if not value:
        return []
    refs: list[tuple[str, str]] = []
    for m in re.finditer(rf"\bArticles\s+({ARTICLE_RANGE_SPEC_RE})", value, re.I):
        spec = _clean_article_range_spec(m.group(1))
        if not re.search(r"\b(?:to|and|or)\b|-|,", spec, re.I):
            continue
        context = _article_range_context(value, m.end())
        if not context:
            continue
        refs.append((f"structure:article_range:{norm(context)}:{norm(spec)}", f"{context}, Articles {spec}"))

    seen = set()
    out = []
    for key, label in refs:
        if key not in seen:
            seen.add(key)
            out.append((key, label))
    return out


def canonical_article_refs(value: str) -> list[tuple[str, str]]:
    """Return canonical article reference keys and display labels from extracted text.

    These are deliberately source-level legal reference targets, not claims that the
    full legal/provision text has been loaded into the graph.
    """
    if not value:
        return []
    source = "CRR" if re.search(r"\b(?:UK\s+)?CRR\b|\(CRR\)", value, re.I) else "regulatory"
    refs: list[tuple[str, str]] = []
    # Singular references, e.g. Article 112(1)(i), Article 325h, CRR Article 49(2), CRR art 4(100).
    for m in re.finditer(r"\b(?:Article|art\.?)\s+([0-9]+[A-Za-z]?(?:\([^)]+\))*)", value, re.I):
        article = m.group(1)
        key = f"article:{source}:{article.lower()}"
        label = f"{source.upper() if source == 'crr' else 'Regulatory'} Article {article}"
        refs.append((key, label))
    for m in re.finditer(r"\b([A-Z][A-Za-z ]{2,40}?)\s+art\.?:?\s*([0-9]+[A-Za-z]?(?:\([^)]+\))*)", value, re.I):
        instrument = norm(m.group(1))
        if instrument and instrument not in {"crr", "uk crr"} and not any(x in instrument for x in ("directive", "regulation", "accounting")):
            article = m.group(2)
            key = f"article:{instrument}:{article.lower()}"
            label = f"{m.group(1).strip()} Article {article}"
            refs.append((key, label))
    # Plural references, e.g. Articles 122A and 122B.
    for m in re.finditer(r"\bArticles\s+([0-9]+[A-Za-z]?)(?:\s*,\s*|\s+and\s+)([0-9]+[A-Za-z]?)", value, re.I):
        for article in (m.group(1), m.group(2)):
            key = f"article:{source}:{article.lower()}"
            label = f"{source.upper() if source == 'crr' else 'Regulatory'} Article {article}"
            refs.append((key, label))
    # Preserve order while deduplicating.
    seen = set()
    out = []
    for key, label in refs:
        if key not in seen:
            seen.add(key)
            out.append((key, label))
    return out


def canonical_rule_refs(value: str) -> list[tuple[str, str]]:
    if not value or not re.search(r"\bPRA\s+Rulebook\b|\bPart of the PRA rulebook\b|\(CRR\) Part", value, re.I):
        return []
    part = "PRA Rulebook"
    mpart = re.search(r"([A-Z][A-Za-z :&()/-]+?(?:\(CRR\))?\s+Part)(?:\s+of\s+the\s+PRA\s+Rulebook)?", value, re.I)
    if mpart:
        part = re.sub(r"\s+", " ", mpart.group(1)).strip()
    refs: list[tuple[str, str]] = []
    for m in re.finditer(r"\brule\s+([0-9]+(?:\.[0-9]+)*(?:\([^)]+\))?)", value, re.I):
        rule = m.group(1)
        key = f"rule:pra:{norm(part)}:{rule.lower()}"
        refs.append((key, f"{part} rule {rule}"))
    seen = set()
    out = []
    for key, label in refs:
        if key not in seen:
            seen.add(key)
            out.append((key, label))
    return out


def _rulebook_part_context(value: str) -> str:
    """Best-effort named Part/context for materialised structural references."""
    patterns = [
        r"\b(?:of|in)\s+(?:Chapter\s+(?:[0-9]+[A-Za-z]?|[IVXL]+)\s+of\s+)?(?:the\s+)?([A-Z][A-Za-z0-9 :&()/-]+?\s*\(CRR\)\s+Part)(?:\s+of\s+the\s+PRA\s+Rulebook)?",
        r"([A-Z][A-Za-z0-9 :&()/-]+?\s*\(CRR\)\s+Part)(?:\s+of\s+the\s+PRA\s+Rulebook)?",
        r"(Title\s+[IVXLC]+\s+of\s+Part\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)\s+(?:of\s+the\s+)?CRR)",
        r"(Part\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)\s+(?:of\s+the\s+)?CRR)",
        r"(PRA\s+Rulebook,\s*[A-Z][A-Za-z0-9 :&()/-]+?\s*\(CRR\)\s+Part)",
    ]
    for pat in patterns:
        m = re.search(pat, value, re.I)
        if m:
            part = re.sub(r"^PRA\s+Rulebook,\s*", "", m.group(1), flags=re.I)
            part = re.sub(r"^PRA\s+Rulebook\s+", "", part, flags=re.I)
            part = re.sub(r"^of\s+the\s+", "", part, flags=re.I)
            part = re.sub(r"^(?:Articles?|Rules?)\b.*?\bof\s+(?:the\s+)?", "", part, flags=re.I)
            part = re.sub(r"^Articles?\s+[0-9]+[A-Za-z]?(?:\([^)]+\))?\s+", "", part, flags=re.I)
            part = re.sub(r"^Chapter\s+(?:[0-9]+[A-Za-z]?|[IVXL]+)\s+of\s+(?:the\s+)?", "", part, flags=re.I)
            part = re.sub(rf"\bArticles?\s+{ARTICLE_RANGE_SPEC_RE}\s+", "", part, flags=re.I)
            part = re.sub(r"\b(Credit Risk)\s+\1:", r"\1:", part, flags=re.I)
            specific = re.search(r"((?:Credit Risk|Liquidity Coverage Ratio|Large Exposures|Leverage Ratio|Own Funds|Operational Risk|Reporting|Trading Book|Counterparty Credit Risk|Market Risk)[A-Za-z0-9 :&()/-]*\(CRR\)\s+Part)$", part, re.I)
            if specific:
                part = specific.group(1)
            return re.sub(r"\s+", " ", part).strip(" ,")
    if re.search(r"\bCRR\b", value, re.I):
        return "CRR"
    if re.search(r"\bPRA\s+Rulebook\b", value, re.I):
        return "PRA Rulebook"
    return "Regulatory reference"


def canonical_rulebook_structure_refs(value: str) -> list[tuple[str, str]]:
    """Materialisable PRA/CRR structural references: Parts, Chapters, Sections and ranges.

    These are intentionally target-reference nodes. They do not claim that the
    complete rule text for the Part/Chapter/Section has been loaded.
    """
    if not value:
        return []
    # Skip generic mentions with no named target.
    if norm(value) in {"part of pra rulebook", "part of the pra rulebook", "pra rulebook", "part of the pra rulebook pra rulebook", "part of the pra rulebook part of the pra rulebook"}:
        return []
    part = _rulebook_part_context(value)
    if part == "Regulatory reference":
        return []
    refs: list[tuple[str, str]] = []

    # Chapters / chapter ranges/lists.
    for m in re.finditer(r"\bChapters?\s+((?:[0-9]+[A-Za-z]?|[IVXL]+)(?:\s*(?:,|and|or|to|-)\s*(?:Chapters?\s+)?(?:[0-9]+[A-Za-z]?|[IVXL]+))*)", value, re.I):
        spec = re.sub(r"\bChapters?\s+", "", m.group(1), flags=re.I)
        spec = re.sub(r"\s+", " ", spec).strip()
        if spec:
            refs.append((f"structure:chapter:{norm(part)}:{norm(spec)}", f"{part}, Chapter {spec}"))

    # Sections / section ranges/lists.
    for m in re.finditer(r"\bSections?\s+((?:[0-9]+[A-Za-z]?|[IVXL]+)(?:\s*(?:,|and|or|to|-)\s*(?:Sections?\s+)?(?:[0-9]+[A-Za-z]?|[IVXL]+))*)", value, re.I):
        spec = re.sub(r"\bSections?\s+", "", m.group(1), flags=re.I)
        spec = re.sub(r"\s+", " ", spec).strip()
        if spec:
            refs.append((f"structure:section:{norm(part)}:{norm(spec)}", f"{part}, Section {spec}"))

    # Rule ranges/lists, e.g. Rules 4.1 to 4.10; individual rules are handled above.
    for m in re.finditer(r"\bRules\s+([0-9]+(?:\.[0-9]+)*(?:\([^)]+\))?\s*(?:to|-|and|or|,)\s*[0-9]+(?:\.[0-9]+)*(?:\([^)]+\))?(?:\s*(?:,|and|or)\s*[0-9]+(?:\.[0-9]+)*(?:\([^)]+\))?)*)", value, re.I):
        spec = re.sub(r"\s+", " ", m.group(1)).strip()
        refs.append((f"structure:rule_range:{norm(part)}:{norm(spec)}", f"{part}, Rules {spec}"))

    # Article ranges/lists where a single article node would be misleading.
    # Named Part-level references. Emitted last so more specific chapter/section/range
    # targets win during resolution.
    if not refs and part not in {"CRR", "PRA Rulebook", "Regulatory reference"} and re.search(r"\bPart\b", part, re.I):
        refs.append((f"structure:part:{norm(part)}", part))

    seen = set()
    out = []
    for key, label in refs:
        if key not in seen:
            seen.add(key)
            out.append((key, label))
    return out


def canonical_external_ref(ref: dict[str, Any]) -> tuple[str, str, str] | None:
    kind = norm(str(ref.get("target_kind") or ""))
    text = str(ref.get("reference_text") or "").strip()
    ident = str(ref.get("target_title_or_identifier") or "").strip()
    doc = str(ref.get("target_part_or_document") or "").strip()
    hay = " ".join([text, ident, doc])

    # IFRS Foundation standards and interpretations. Keep paragraph/section
    # suffixes in the canonical key, but only as external reference targets.
    ifrs = re.search(r"\b(IFRS|IAS|IFRIC|SIC)\s+(\d+)(?:[\.\s]+((?!(?:IFRS|IAS|IFRIC|SIC)\b)[A-Z]{0,3}\s*\d+(?:\.\d+)*(?:[A-Za-z])?(?:\([^)]+\))*(?:\s*(?:-|to|,)\s*[A-Z]{0,3}\s*\d+(?:\.\d+)*(?:[A-Za-z])?(?:\([^)]+\))*)*))?", hay, re.I)
    if ifrs:
        family = ifrs.group(1).upper()
        num = ifrs.group(2)
        para = re.sub(r"\s+", "", ifrs.group(3) or "")
        label = f"{family} {num}{('.' + para) if para else ''}"
        return f"external:ifrs:{family.lower()}-{num.lower()}{(':' + norm(para)) if para else ''}", label, "ExternalReference"

    # ECB legal acts, e.g. ECB/2013/33 Annex 2.Part 2.4-5.
    ecb = re.search(r"\bECB\s*/\s*(\d{4})\s*/\s*(\d+)\b(?:\s+Annex\s*([0-9IVXLC]+)(?:\.\s*Part\s*([0-9.\-]+))?)?", hay, re.I)
    if ecb:
        code = f"ECB/{ecb.group(1)}/{ecb.group(2)}"
        suffix = ""
        if ecb.group(3):
            suffix = f" Annex {ecb.group(3)}"
            if ecb.group(4):
                suffix += f" Part {ecb.group(4)}"
        label = f"{code}{suffix}"
        return f"external:ecb:{code.lower()}{(':' + norm(suffix)) if suffix else ''}", label, "LegalInstrument"

    # Named CRR/CRD shorthand and EU legal instrument identifiers.
    if re.search(r"\bCapital\s+Requirements\s+Regulation\b|\bCRR\b", hay, re.I) and re.search(r"\b575\s*/\s*2013\b", hay):
        return "external:eu-regulation:575-2013", "Regulation (EU) No 575/2013 (CRR)", "LegalInstrument"
    if re.search(r"\bCapital\s+Requirements\s+Directive\b|\bCRD\b", hay, re.I) and re.search(r"\b2013\s*/\s*36\s*/\s*(?:EU|UE)\b|\b36\s*/\s*2013\b", hay, re.I):
        return "external:eu-directive:2013-36", "Directive 2013/36/EU (CRD IV)", "LegalInstrument"
    eu = re.search(r"\b((?:Commission\s+)?(?:Delegated\s+|Implementing\s+)?Regulation|Directive)\s*(?:\(EU\)|\(EEC\)|EU)?\s*(?:No\s*)?(\d{1,5})\s*/\s*(\d{4})(?:\d{1,2})?\b", hay, re.I)
    if not eu:
        eu = re.search(r"\b((?:Commission\s+)?(?:Delegated\s+|Implementing\s+)?Regulation|Directive)\s*(?:\(EU\)|\(EEC\)|EU)?\s*(\d{4})\s*/\s*(\d{1,5})\b", hay, re.I)
        if eu:
            kind_label = re.sub(r"\s+", " ", eu.group(1)).strip().title()
            year, number = eu.group(2), eu.group(3)
            return f"external:eu-{norm(kind_label)}:{year}-{number}", f"{kind_label} (EU) {year}/{number}", "LegalInstrument"
    else:
        kind_label = re.sub(r"\s+", " ", eu.group(1)).strip().title()
        number, year = eu.group(2), eu.group(3)
        return f"external:eu-{norm(kind_label)}:{number}-{year}", f"{kind_label} (EU) No {number}/{year}", "LegalInstrument"

    acct = re.search(r"\bAccounting\s+Directive\b(?:\s*(?:art\.?|Article)?\s*([0-9]+(?:\([^)]+\))*))?", hay, re.I)
    if acct:
        suffix = acct.group(1) or ""
        label = f"Accounting Directive{(' Article ' + suffix) if suffix else ''}"
        return f"external:accounting-directive{(':' + norm(suffix)) if suffix else ''}", label, "LegalInstrument"

    eba = re.search(r"\bEBA\s+(Guidelines|ITS|RTS|Q&A|Opinion|Report)\b(?:\s+(?:on|for)\s+([^.;\n]{5,120}))?", hay, re.I)
    if eba:
        typ = eba.group(1).upper()
        topic = re.sub(r"\s+", " ", (eba.group(2) or "")).strip(" .;:,()")
        label = f"EBA {typ}{(' on ' + topic) if topic else ''}"
        return f"external:eba:{typ.lower()}:{sha1(norm(topic))[:12] if topic else 'generic'}", label[:240], "ExternalReference"

    ps = re.search(r"\b(?:PS|CP|SS|SoP)\s*\d+\s*/\s*\d+\b", hay, re.I)
    if ps:
        code = re.sub(r"\s+", "", ps.group(0).upper())
        return f"external:policy:{code.lower()}", code, "PolicyStatement"
    url = re.search(r"https?://\S+|www\.\S+", hay, re.I)
    if url:
        raw = url.group(0).rstrip("),.;]")
        slug = raw.rstrip("/").split("/")[-1] or raw
        label = ident if ident and not ident.lower().startswith("http") else slug.replace("-", " ")
        return f"external:url:{sha1(raw.lower())}", label[:240], "ExternalReference"
    if kind in {"regulation", "directive", "standard", "external", "policy statement", "consultation", "guidance", "legal instrument"} or "regulation" in kind or "directive" in kind or "standard" in kind:
        label = ident or text
        if len(label) >= 4 and not norm(label) in {"crr", "pra", "eba", "eu", "external", "standard"}:
            node_type = "LegalInstrument" if kind in {"regulation", "directive", "standard"} else "ExternalReference"
            return f"external:{kind.replace(' ', '_')}:{sha1(norm(label))}", label[:240], node_type
    return None


def classify_deliberately_unresolved(ref: dict[str, Any]) -> str | None:
    kind = norm(str(ref.get("target_kind") or ""))
    text = str(ref.get("reference_text") or "")
    ident = str(ref.get("target_title_or_identifier") or "")
    doc = str(ref.get("target_part_or_document") or "")
    hay = " ".join([text, ident, doc])
    hn = norm(hay)
    if not hn or len(hn) <= 2:
        return "deliberately_unresolved_noisy_fragment"
    if "|" in str(ref.get("target_kind") or "") and len(str(ref.get("target_kind") or "").split("|")) >= 6:
        return "deliberately_unresolved_ambiguous_kind"
    if hn in {"pra rulebook", "part of pra rulebook", "part of the pra rulebook", "the pra rulebook", "crr", "regulation", "that regulation", "this implementing regulation", "implementing regulation"}:
        return "deliberately_unresolved_generic_reference"
    if re.search(r"\b(that|this|these|thereof|relevant|applicable)\b", hay, re.I) and re.search(r"\b(Regulation|Directive|Part|Article|Annex|standard|rule)\b", hay, re.I):
        return "deliberately_unresolved_relative_reference"
    if kind in {"unknown", "reference", "definition", "paragraph", "namespace"}:
        return "deliberately_unresolved_low_value_reference"
    if re.fullmatch(r"[A-Za-z]?\(?[a-z0-9]{1,3}\)?", hn):
        return "deliberately_unresolved_noisy_fragment"
    return None


def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return key


def request(method: str, path: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {api_key()}"
    resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=kwargs.pop("timeout", 120), **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:2000]}")
    return resp


def connect(path: Path = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    return conn


def package_source_ids() -> set[str]:
    ids: set[str] = set()
    for p in PACKAGES.glob("*_package.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        for src in data.get("source_provenance") or []:
            sid = src.get("source_id")
            if sid:
                ids.add(sid)
    return ids


def selected_spans(conn: sqlite3.Connection, *, limit: int | None, rerun: bool, max_chars: int) -> list[sqlite3.Row]:
    ids = sorted(package_source_ids())
    placeholders = ",".join("?" for _ in ids)
    span_types = ("pdf_paragraph", "paragraph", "provision", "heading", "xlsx_row", "xlsx_workbook", "xlsx_sheet", "archive_text_extract")
    type_ph = ",".join("?" for _ in span_types)
    sql = f"""
    SELECT s.span_id,s.source_id,s.span_type,s.page_number,s.sheet_name,s.row_number,s.heading_path,s.anchor,
           s.raw_text,d.title source_title,d.url source_url,d.file_type
    FROM source_span s
    JOIN source_document d ON d.source_id=s.source_id
    WHERE s.source_id IN ({placeholders})
      AND s.span_type IN ({type_ph})
      AND length(trim(coalesce(s.raw_text,''))) >= 40
    ORDER BY d.source_id,s.page_number,s.sheet_name,s.row_number,s.span_id
    """
    rows: list[sqlite3.Row] = []
    for r in conn.execute(sql, ids + list(span_types)):
        text = (r["raw_text"] or "").strip()
        if not REF_RX.search(text):
            continue
        h = sha1("\n".join([r["span_id"], r["source_id"], text[:max_chars]]))
        if not rerun:
            old = conn.execute(
                "SELECT 1 FROM reporting_llm_reference_extraction WHERE span_id=? AND prompt_version=? AND text_hash=? AND status='ok'",
                (r["span_id"], PROMPT_VERSION, h),
            ).fetchone()
            if old:
                continue
        rows.append(r)
        if limit and len(rows) >= limit:
            break
    return rows


def run_dir(name: str | None = None) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    if name:
        path = RUNS / name
        path.mkdir(parents=True, exist_ok=True)
        return path
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def prompt_for(r: sqlite3.Row, max_chars: int) -> str:
    return USER_TEMPLATE.format(
        span_id=r["span_id"],
        source_title=r["source_title"] or "",
        source_url=r["source_url"] or "",
        file_type=r["file_type"] or "",
        span_type=r["span_type"] or "",
        page=r["page_number"] or "",
        sheet=r["sheet_name"] or "",
        row=r["row_number"] or "",
        heading=r["heading_path"] or "",
        text=(r["raw_text"] or "").strip()[:max_chars],
    )


def command_prepare(args: argparse.Namespace) -> None:
    rd = run_dir(args.name)
    conn = connect(args.db)
    rows = selected_spans(conn, limit=args.limit, rerun=args.rerun, max_chars=args.max_chars)
    input_path = rd / "input.jsonl"
    with input_path.open("w", encoding="utf-8") as f:
        for r in rows:
            body = {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt_for(r, args.max_chars)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            f.write(json.dumps({"custom_id": r["span_id"], "method": "POST", "url": "/v1/chat/completions", "body": body}, ensure_ascii=False) + "\n")
    manifest = {
        "created_at": now(),
        "model": args.model,
        "prompt_version": PROMPT_VERSION,
        "max_chars": args.max_chars,
        "span_count": len(rows),
        "input_file": str(input_path),
        "status": "prepared",
    }
    (rd / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(rd), "span_count": len(rows), "bytes": input_path.stat().st_size}, indent=2))


def command_submit(args: argparse.Namespace) -> None:
    rd = Path(args.run_dir)
    input_path = rd / "input.jsonl"
    with input_path.open("rb") as f:
        file_resp = request("POST", "/files", files={"file": ("input.jsonl", f, "application/jsonl")}, data={"purpose": "batch"}, timeout=300).json()
    batch_resp = request(
        "POST",
        "/batches",
        json={"input_file_id": file_resp["id"], "endpoint": "/v1/chat/completions", "completion_window": "24h", "metadata": {"project": "pra-rulebook-explorer", "run_dir": rd.name, "pass": "reporting-references"}},
    ).json()
    mp = rd / "manifest.json"
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    manifest.update({"status": "submitted", "file": file_resp, "batch": batch_resp, "submitted_at": now()})
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(rd), "file_id": file_resp["id"], "batch_id": batch_resp["id"], "status": batch_resp.get("status")}, indent=2))


def command_status(args: argparse.Namespace) -> None:
    rd = Path(args.run_dir)
    mp = rd / "manifest.json"
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    bid = args.batch_id or manifest.get("batch", {}).get("id")
    if not bid:
        raise RuntimeError("No batch id found")
    batch = request("GET", f"/batches/{bid}").json()
    manifest["batch"] = batch
    manifest["status_checked_at"] = now()
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(batch, indent=2))


def parse_model_json(content: str) -> dict[str, Any]:
    content = (content or "").strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("references"), list):
            return parsed
    except Exception:
        pass
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no JSON object")
    parsed = json.loads(m.group(0))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("references"), list):
        raise ValueError("bad JSON shape")
    return parsed


def download_file(file_id: str, dest: Path) -> None:
    resp = request("GET", f"/files/{file_id}/content", stream=True, timeout=300)
    with dest.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def current_span_hashes(conn: sqlite3.Connection, max_chars: int) -> dict[str, tuple[str, str]]:
    rows = selected_spans(conn, limit=None, rerun=True, max_chars=max_chars)
    return {r["span_id"]: (r["source_id"], sha1("\n".join([r["span_id"], r["source_id"], (r["raw_text"] or "").strip()[:max_chars]]))) for r in rows}


def command_import(args: argparse.Namespace) -> None:
    rd = Path(args.run_dir)
    mp = rd / "manifest.json"
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    batch = manifest.get("batch", {})
    if batch.get("status") != "completed":
        bid = args.batch_id or batch.get("id")
        if not bid:
            raise RuntimeError("No batch id found")
        batch = request("GET", f"/batches/{bid}").json()
        manifest["batch"] = batch
    if batch.get("status") != "completed":
        raise RuntimeError(f"Batch is not completed: {batch.get('status')}")
    output_file_id = batch.get("output_file_id")
    if not output_file_id:
        raise RuntimeError("Completed batch has no output_file_id")
    output_path = rd / "output.jsonl"
    if not output_path.exists() or args.redownload:
        download_file(output_file_id, output_path)
    conn = connect(args.db)
    hashes = current_span_hashes(conn, int(manifest.get("max_chars") or 3000))
    ok = errors = 0
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            sid = rec["custom_id"]
            response = rec.get("response") or {}
            err_obj = rec.get("error") or response.get("error")
            source_id, h = hashes.get(sid, ("", "batch-api"))
            if err_obj:
                status, parsed, error = "error", {}, json.dumps(err_obj, ensure_ascii=False)
                errors += 1
            else:
                try:
                    body = response.get("body") or {}
                    parsed = parse_model_json(body["choices"][0]["message"]["content"])
                    status, error = "ok", ""
                    ok += 1
                except Exception as exc:
                    status, parsed, error = "error", {}, str(exc)
                    errors += 1
            conn.execute(
                """
                INSERT INTO reporting_llm_reference_extraction (span_id,source_id,model,prompt_version,text_hash,status,response_json,error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(span_id) DO UPDATE SET source_id=excluded.source_id,model=excluded.model,prompt_version=excluded.prompt_version,
                  text_hash=excluded.text_hash,status=excluded.status,response_json=excluded.response_json,error=excluded.error,updated_at=excluded.updated_at
                """,
                (sid, source_id, manifest.get("model") or DEFAULT_MODEL, PROMPT_VERSION, h, status, json.dumps(parsed, ensure_ascii=False), error, now(), now()),
            )
    conn.commit()
    manifest.update({"status": "imported", "imported_at": now(), "import_ok": ok, "import_errors": errors, "output_file": str(output_path)})
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"ok": ok, "errors": errors, "output": str(output_path)}, indent=2))


class Resolver:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.nodes = [dict(r) for r in conn.execute("SELECT node_id,node_type,label,properties_json,review_status FROM graph_node")]
        for n in self.nodes:
            n["norm_label"] = norm(n.get("label") or "")
            try:
                n["props"] = json.loads(n.get("properties_json") or "{}")
            except Exception:
                n["props"] = {}
        self.by_node_id = {n["node_id"]: n for n in self.nodes}
        self.data_items: dict[str, dict[str, Any]] = {}
        self.return_aliases: dict[str, tuple[str, dict[str, Any]]] = {}
        for n in self.nodes:
            if n["node_type"] not in {"DataItem", "ReportingObligation"}:
                continue
            code = (n["props"].get("data_item_code") or n.get("label") or "").upper()
            if not code:
                continue
            # Prefer ReportingObligation targets for return-level references.
            if code not in self.data_items or n["node_type"] == "ReportingObligation":
                self.data_items[code] = n
        for code, n in self.data_items.items():
            self._add_return_alias(code, n)
            self._add_return_alias(code.replace("-", " "), n)
            self._add_return_alias((n.get("label") or "").replace(" reporting obligation", ""), n)

        # Common reporting package/template aliases seen in PRA instructions.
        manual_return_aliases = {
            "FINREP": "FINREP",
            "FSA001 or FINREP": "FINREP",
            "FSA001": "FINREP",  # no FSA001 node is currently loaded; source text treats it as FINREP replacement/alternative.
            "CA1": "COREP-OWN-FUNDS",
            "CA2": "COREP-OWN-FUNDS",
            "CA3": "COREP-OWN-FUNDS",
            "CA4": "COREP-OWN-FUNDS",
            "CA5": "COREP-OWN-FUNDS",
            "CA1 SDDT": "COREP-OWN-FUNDS",
            "CA2 SDDT": "COREP-OWN-FUNDS",
            "CR SA": "COREP-CREDIT-RISK",
            "CR SA SDDT": "COREP-CREDIT-RISK",
            "CR IRB": "COREP-CREDIT-RISK",
            "CR IRB SDDT": "COREP-CREDIT-RISK",
            "CR SEC": "COREP-CREDIT-RISK",
            "CR SEC SDDT": "COREP-CREDIT-RISK",
            "LE1": "COREP-LARGE-EXPOSURES",
            "LE2": "COREP-LARGE-EXPOSURES",
            "LE3": "COREP-LARGE-EXPOSURES",
            "LARGE EXPOSURES": "COREP-LARGE-EXPOSURES",
            "CCR": "COREP-CCR",
            "COUNTERPARTY CREDIT RISK": "COREP-CCR",
        }
        for alias, code in manual_return_aliases.items():
            n = self.data_items.get(code)
            if n:
                self._add_return_alias(alias, n)
        self.templates = defaultdict(list)
        for n in self.nodes:
            if n["node_type"] == "Template":
                code = (n["props"].get("template_code") or n["label"] or "").upper().replace(" ", "")
                if code:
                    self.templates[code].append(n)
                m = re.match(r"\s*([A-Z]{1,2})\s*(\d{1,3})\.(\d{1,2})\b", n.get("label") or "", re.I)
                if m:
                    label_code = f"{m.group(1).upper()}{int(m.group(2)):02d}.{int(m.group(3)):02d}"
                    self.templates[label_code].append(n)
        self.provisions = [n for n in self.nodes if n["node_type"] == "Provision"]
        self.provisions_by_external_key = {n["props"].get("canonical_key"): n for n in self.provisions if n["props"].get("canonical_key")}
        self.sources = [n for n in self.nodes if n["node_type"] in {"SourceDocument", "InstructionSet", "ValidationRule"}]
        self.external_refs = [n for n in self.nodes if n["node_type"] in {"ExternalReference", "LegalInstrument", "PolicyStatement"}]
        self.external_refs_by_key = {n["props"].get("canonical_key"): n for n in self.external_refs if n["props"].get("canonical_key")}
        self.sources_by_annex = defaultdict(list)
        for n in self.sources:
            m = re.match(r"annex\s+([ivxlcdm]+)\b", n["norm_label"])
            if m:
                self.sources_by_annex[m.group(1).upper()].append(n)

    def _best_template(self, code: str):
        code = code.upper().replace(" ", "")
        if code in self.templates:
            return self.templates[code][0], "template_code_exact", 0.94
        return None, "", 0.0

    def _add_return_alias(self, alias: str, node: dict[str, Any]) -> None:
        key = compact_code(alias)
        if key:
            self.return_aliases[key] = (alias.upper(), node)

    def _best_return_alias(self, hay: str):
        text = (hay or "").upper()
        matches: list[tuple[int, dict[str, Any], str]] = []
        for alias_key, (alias_text, node) in self.return_aliases.items():
            if len(alias_key) < 3:
                continue
            parts = re.findall(r"[A-Z0-9]+", alias_text)
            if not parts:
                continue
            pattern = r"(?<![A-Z0-9])" + r"[\s\-/]*".join(re.escape(p) for p in parts) + r"(?![A-Z0-9])"
            if re.search(pattern, text):
                matches.append((len(alias_key), node, alias_key))
        if not matches:
            return None, "", 0.0
        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1], "reporting_return_alias", 0.89

    def resolve(self, ref: dict[str, Any]):
        text = str(ref.get("reference_text") or "")
        ident = str(ref.get("target_title_or_identifier") or "")
        doc = str(ref.get("target_part_or_document") or "")
        kind = norm(str(ref.get("target_kind") or ""))
        hay = " ".join([text, ident, doc])

        # Data item / return codes.
        for m in re.finditer(r"\b(?:PRA|COR|LVR|FSA|REP|MLAR)\s*-?\s*(\d{3})\b", hay, re.I):
            prefix = re.match(r"\b([A-Z]+)", m.group(0).replace(" ", ""), re.I).group(1).upper()
            code = f"{prefix}{m.group(1)}"
            n = self.data_items.get(code)
            if n:
                return n, "data_item_code_exact", 0.96
        # Template codes, e.g. C 72.00 / F 01.01.
        for m in re.finditer(r"\b([A-Z]{1,2})\s*(\d{1,3})\.(\d{1,2})\b", hay, re.I):
            code = f"{m.group(1).upper()}{int(m.group(2)):02d}.{int(m.group(3)):02d}"
            n, method, score = self._best_template(code)
            if n:
                return n, method, score
        for m in re.finditer(r"\b([CF])\s*-?\s*(\d{2})(?![0-9.])\b", hay, re.I):
            code = f"{m.group(1).upper()}{int(m.group(2)):02d}.00"
            n, method, score = self._best_template(code)
            if n:
                return n, "template_code_alias", 0.9

        # Canonical external standards/instruments should win over broad Annex
        # source-document matches and over generic "Regulatory Article" nodes.
        ext = canonical_external_ref(ref)
        if ext and (ext[0].startswith("external:ifrs:") or ext[0].startswith("external:ecb:") or ext[0].startswith("external:eba:") or ext[0].startswith("external:eu-") or ext[0].startswith("external:accounting-directive")):
            n = self.external_refs_by_key.get(ext[0])
            if n:
                return n, "materialized_external_reference", 0.9

        # Article/provision labels already projected into reporting graph.
        for key, _label in canonical_article_range_refs(hay) + canonical_article_refs(hay) + canonical_rule_refs(hay) + canonical_rulebook_structure_refs(hay):
            n = self.provisions_by_external_key.get(key)
            if n:
                return n, "materialized_legal_reference", 0.9
        art = re.search(r"\bArticle\s+(\d+[A-Za-z]?)\b", hay, re.I)
        if art:
            wanted = f"article {art.group(1).lower()}"
            docn = norm(doc or text)
            best = []
            for n in self.provisions:
                label = n["norm_label"]
                nid = norm(n["node_id"])
                if wanted in label or wanted.replace(" ", "") in nid:
                    score = 0.88
                    if "crr" in docn and "crr" in nid:
                        score += 0.04
                    if "reporting" in docn and "reporting" in nid:
                        score += 0.04
                    best.append((score, n))
            if best:
                best.sort(key=lambda x: x[0], reverse=True)
                return best[0][1], "article_provision_projected", min(best[0][0], 0.95)

        # Return aliases, deliberately after legal article matching to avoid
        # treating references to e.g. the Large Exposures (CRR) Part as a return.
        return_alias_kinds = {"reporting return", "data item", "template", "instruction", "validation rule", "unknown", "reference", "data item reporting return", "reporting return template"}
        if kind in return_alias_kinds or "reporting return" in kind or "data item" in kind or "template" in kind:
            n, method, score = self._best_return_alias(hay)
            if n:
                return n, method, score

        # Annex/table/source-document level references.
        key = norm(ident or text)
        annex = re.search(r"\bAnnex\s+([IVXLCDM]+)\b", hay, re.I)
        if annex:
            candidates = self.sources_by_annex.get(annex.group(1).upper()) or []
            if candidates:
                # Prefer the PDF instructions/source document where both PDF and XLSX package files exist.
                candidates = sorted(candidates, key=lambda n: (" pdf" not in n["norm_label"], n["norm_label"]))
                return candidates[0], "annex_source_document", 0.86

        if kind in {"annex", "table", "instruction", "validation rule", "guidance", "policy statement", "consultation", "external", "reporting return", "regulation", "directive", "reference", "unknown"} and key:
            candidates = []
            for n in self.sources:
                label = n["norm_label"]
                if key and (key == label or key in label or label in key):
                    candidates.append((0.82 if key != label else 0.92, n))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1], "source_title_match", candidates[0][0]

        ext = canonical_external_ref(ref)
        if ext:
            n = self.external_refs_by_key.get(ext[0])
            if n:
                return n, "materialized_external_reference", 0.88
        deliberate = classify_deliberately_unresolved(ref)
        if deliberate:
            return None, deliberate, 0.0
        return None, "unresolved", 0.0


def command_resolve(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    resolver = Resolver(conn)
    conn.execute("DELETE FROM reporting_llm_reference_resolution")
    rows = conn.execute("SELECT span_id,source_id,response_json FROM reporting_llm_reference_extraction WHERE status='ok' AND prompt_version=? ORDER BY span_id", (PROMPT_VERSION,)).fetchall()
    edge_rows = {}
    total = resolved = added = unresolved = classified_unresolved = 0
    for row in rows:
        refs = json.loads(row["response_json"] or "{}").get("references") or []
        if not isinstance(refs, list):
            continue
        source_node_id = f"source_span:{row['span_id']}"
        # Some spans were not projected as graph nodes. Anchor the edge from the source document in that case.
        if not conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (source_node_id,)).fetchone():
            source_node_id = f"source_document:{row['source_id']}"
        if not conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (source_node_id,)).fetchone():
            continue
        for idx, ref in enumerate(refs):
            if not isinstance(ref, dict):
                continue
            total += 1
            target, method, rconf = resolver.resolve(ref)
            extracted = float(ref.get("confidence") or 0)
            target_id = target["node_id"] if target else ""
            if target_id:
                resolved += 1
            elif method != "unresolved":
                classified_unresolved += 1
            else:
                unresolved += 1
            eid = ""
            if target_id and rconf >= args.min_resolver_confidence and extracted >= args.min_extracted_confidence:
                etype = "REFERENCES_RULE"
                if target["node_type"] in {"DataItem", "ReportingObligation"}:
                    etype = "REFERENCES_RETURN"
                elif target["node_type"] == "Template":
                    etype = "REFERENCES_TEMPLATE"
                elif target["node_type"] in {"SourceDocument", "InstructionSet", "ValidationRule"}:
                    etype = "REFERENCES_SOURCE"
                elif target["node_type"] in {"ExternalReference", "LegalInstrument", "PolicyStatement"}:
                    etype = "REFERENCES_EXTERNAL"
                eid = edge_id(source_node_id, etype, target_id, row["span_id"], str(idx), PROMPT_VERSION)
                props = {"llm_reference": ref, "resolver_method": method, "prompt_version": PROMPT_VERSION, "decision": "accepted_by_reporting_llm_reference_pass"}
                edge_rows[eid] = (eid, source_node_id, target_id, etype, json.dumps(props, ensure_ascii=False), row["span_id"], min(0.93, extracted * rconf), "reporting_llm_reference", "accepted_candidate")
                added += 1
            rid = sha1("|".join([row["span_id"], str(idx), str(ref.get("reference_text") or "")]))
            conn.execute(
                """
                INSERT INTO reporting_llm_reference_resolution
                (id,span_id,source_id,ref_index,reference_text,target_kind,target_title_or_identifier,target_part_or_document,evidence_quote,extracted_confidence,target_node_id,target_node_type,target_label,resolver_method,resolver_confidence,added_edge_id,metadata_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (rid, row["span_id"], row["source_id"], idx, ref.get("reference_text", ""), ref.get("target_kind", ""), ref.get("target_title_or_identifier", ""), ref.get("target_part_or_document", ""), ref.get("evidence_quote", ""), extracted, target_id, target["node_type"] if target else "", target["label"] if target else "", method, rconf, eid, json.dumps(ref, ensure_ascii=False), now()),
            )
    if args.add_edges:
        conn.execute("DELETE FROM graph_edge WHERE extraction_method='reporting_llm_reference'")
        if edge_rows:
            conn.executemany(
                """
                INSERT INTO graph_edge (edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(edge_id) DO UPDATE SET confidence=excluded.confidence,properties_json=excluded.properties_json,review_status=excluded.review_status
                """,
                list(edge_rows.values()),
            )
    conn.commit()
    print(json.dumps({"extracted_spans": len(rows), "total_refs": total, "target_resolved_refs": resolved, "classified_unresolved_refs": classified_unresolved, "unresolved_refs": unresolved, "new_edges": len(edge_rows) if args.add_edges else 0, "new_edges_available": len(edge_rows)}, indent=2))


def command_materialize_references(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    rows = conn.execute(
        """
        SELECT metadata_json,COUNT(*) c
        FROM reporting_llm_reference_resolution
        WHERE extracted_confidence >= ?
        GROUP BY metadata_json
        """,
        (args.min_extracted_confidence,),
    ).fetchall()
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            ref = json.loads(row["metadata_json"] or "{}")
        except Exception:
            continue
        count = int(row["c"] or 0)
        hay = " ".join(str(ref.get(k) or "") for k in ("reference_text", "target_title_or_identifier", "target_part_or_document"))
        for key, label in canonical_article_range_refs(hay) + canonical_article_refs(hay) + canonical_rule_refs(hay) + canonical_rulebook_structure_refs(hay):
            rec = candidates.setdefault(key, {"canonical_key": key, "label": label, "node_type": "Provision", "count": 0, "examples": [], "min_count": 1 if key.startswith("structure:article_range:") else args.min_count})
            rec["count"] += count
            if len(rec["examples"]) < 5:
                rec["examples"].append(ref)
        ext = canonical_external_ref(ref)
        if ext:
            key, label, node_type = ext
            rec = candidates.setdefault(key, {"canonical_key": key, "label": label, "node_type": node_type, "count": 0, "examples": [], "min_count": 1 if (key.startswith("external:ifrs:") or key.startswith("external:ecb:") or key.startswith("external:eba:") or key.startswith("external:eu-") or key.startswith("external:accounting-directive")) else args.min_count})
            rec["count"] += count
            if len(rec["examples"]) < 5:
                rec["examples"].append(ref)

    node_rows = []
    for rec in candidates.values():
        if rec["count"] < rec.get("min_count", args.min_count):
            continue
        prefix = "provision" if rec["node_type"] == "Provision" else "external_reference"
        node_id = f"{prefix}:materialized:{sha1(rec['canonical_key'])[:16]}"
        props = {
            "canonical_key": rec["canonical_key"],
            "materialized_from": "reporting_llm_reference_resolution",
            "occurrence_count": rec["count"],
            "prompt_version": PROMPT_VERSION,
            "reference_target_only": True,
            "examples": rec["examples"],
        }
        node_rows.append((node_id, rec["node_type"], rec["label"], "reporting_materialized_reference", rec["canonical_key"], json.dumps(props, ensure_ascii=False), "accepted_candidate"))

    conn.execute("DELETE FROM graph_node WHERE source_table='reporting_materialized_reference'")
    if node_rows:
        conn.executemany(
            """
            INSERT INTO graph_node (node_id,node_type,label,source_table,source_pk,properties_json,review_status)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET label=excluded.label,properties_json=excluded.properties_json,review_status=excluded.review_status
            """,
            node_rows,
        )
    conn.commit()
    by_type = defaultdict(int)
    for _node_id, node_type, *_rest in node_rows:
        by_type[node_type] += 1
    print(json.dumps({"candidate_targets": len(candidates), "nodes_materialized": len(node_rows), "nodes_by_type": dict(sorted(by_type.items()))}, indent=2))


def command_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    data = {
        "extractions": {r["status"]: r["c"] for r in conn.execute("SELECT status,COUNT(*) c FROM reporting_llm_reference_extraction GROUP BY status")},
        "refs_by_method": {r["resolver_method"]: r["c"] for r in conn.execute("SELECT resolver_method,COUNT(*) c FROM reporting_llm_reference_resolution GROUP BY resolver_method")},
        "edges_by_type": {r["edge_type"]: r["c"] for r in conn.execute("SELECT edge_type,COUNT(*) c FROM graph_edge WHERE extraction_method='reporting_llm_reference' GROUP BY edge_type")},
    }
    print(json.dumps(data, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Reporting source LLM reference extraction via OpenAI Batch API")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-chars", type=int, default=3000)
    p.add_argument("--limit", type=int)
    p.add_argument("--rerun", action="store_true")
    p.add_argument("--name")
    p.set_defaults(func=command_prepare)
    p = sub.add_parser("submit")
    p.add_argument("run_dir")
    p.set_defaults(func=command_submit)
    p = sub.add_parser("status")
    p.add_argument("run_dir")
    p.add_argument("--batch-id")
    p.set_defaults(func=command_status)
    p = sub.add_parser("import")
    p.add_argument("run_dir")
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--batch-id")
    p.add_argument("--redownload", action="store_true")
    p.set_defaults(func=command_import)
    p = sub.add_parser("resolve")
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--add-edges", action="store_true")
    p.add_argument("--min-resolver-confidence", type=float, default=0.8)
    p.add_argument("--min-extracted-confidence", type=float, default=0.72)
    p.set_defaults(func=command_resolve)
    p = sub.add_parser("materialize-references")
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--min-count", type=int, default=2)
    p.add_argument("--min-extracted-confidence", type=float, default=0.72)
    p.set_defaults(func=command_materialize_references)
    p = sub.add_parser("stats")
    p.add_argument("--db", type=Path, default=DB)
    p.set_defaults(func=command_stats)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
