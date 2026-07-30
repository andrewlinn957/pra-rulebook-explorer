"""Deterministic extraction helpers for numbered legal Article citations.

The PRA corpus contains both singular and coordinated citations, for example:

* ``Article 380``
* ``Article 379 and 380``
* ``Articles 378, 379 and 380``
* ``Articles 399 to 403``
* ``CRR Article 213(1)(b)``

The ordinary Rulebook reference parser historically recognised only a subset of
these forms and required ``CRR`` to appear in a small evidence window.  This
module keeps syntax extraction independent from instrument classification so a
corpus audit can account for every Article-shaped citation before deciding
whether it points to the UK CRR, an internal PRA provision, or another legal
instrument.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree


ARTICLE_PREFIX_RE = re.compile(r"\b(?P<prefix>Articles?|Arts?\.?)\s*", re.I)
ARTICLE_TOKEN_RE = re.compile(
    r"(?P<base>\d{1,3}[A-Za-z]{0,3})"
    r"(?P<paragraphs>"
    r"(?:\s*\.\s*[0-9][0-9A-Za-z-]*)?"
    r"(?:\s*\(\s*[0-9A-Za-zivxlcdm.-]+\s*\))*)",
    re.I,
)
ARTICLE_SEPARATOR_RE = re.compile(
    r"\s*(?P<separator>,(?:\s*(?:and|or))?|\band\b|\bor\b|\bto\b|[-–—])\s*",
    re.I,
)
CRR_EXPLICIT_RE = re.compile(
    r"\b(?:UK\s+)?CRR\b"
    r"|\bCapital\s+Requirements?\s+Regulation\b"
    r"|\bRegulation\s*\(EU\)\s*(?:No\.?\s*)?575\s*/\s*2013\b"
    r"|\bRegulation\s*\(EU\)\s*(?:No\.?\s*)?2013\s*/\s*575\b",
    re.I,
)
NON_CRR_INSTRUMENT_PATTERN = (
    r"(?:"
    r"(?:Commission\s+)?(?:Delegated|Implementing)\s+Regulation"
    r"|(?:Solvency\s+II\s+)?Commission\s+Delegated\s+Regulation"
    r"|Regulation\s*\((?:EU|EC)\)"
    r"|(?:AIFM|AIFMD|UCITS|DGSD|Financial\s+Groups?|Money\s+Laundering|"
    r"Banking\s+Consolidation|Capital\s+Requirements?|Solvency\s+II|"
    r"EU\s+Prospectus)\s+Directive"
    r"|Directive(?:\s+\d{4}/\d+/(?:EU|EC))?"
    r"|Solvency\s+II\s+Directive"
    r"|(?:Regulated\s+Activities|Core\s+Activities|Transitional|Compensation\s+"
    r"Transitionals?|Credit\s+Unions?\s+\(Northern\s+Ireland\)|"
    r"Bank\s+Recovery\s+and\s+Resolution(?:\s+\(No\.?\s*2\))?)\s+Order"
    r"|PRA-regulated\s+Activities\s+Order"
    r"|Excluded\s+Activities(?:\s+and\s+Prohibitions)?\s+Order"
    r"|Insolvency\s+\(Northern\s+Ireland\)\s+Order"
    r"|Financial\s+Services\s+and\s+Markets\s+Act"
    r"|Banking\s+Act"
    r"|Companies\s+Act"
    r"|FSMA"
    r"|CRD(?:\s+[IVX]+)?"
    r"|BRRD"
    r"|MiFID(?:\s+II)?"
    r"|MiFIR"
    r"|EMIR"
    r"|CSDR"
    r"|LCR\s+Delegated\s+Act"
    r"|Delegated\s+Act"
    r"|LCR"
    r"|Auction\s+Regulation"
    r"|Benchmarks?\s+Regulation"
    r"|Statutory\s+Audit\s+Regulation"
    r"|IAS\s+Regulations?"
    r"|Commission\s+Recommendation"
    r"|Business\s+Transfers\s+Regulations?"
    r"|Employers['’]?\s+Liability\s+Order"
    r"|Road\s+Traffic\s+\(Northern\s+Ireland\)\s+Order"
    r"|Welfare\s+Reform\s+and\s+Pensions\s+\(Northern\s+Ireland\)\s+Order"
    r"|AIFMD|DGSD|RAO|CDR|CIR|CRR2|DIS\s+rules"
    r")"
)
NON_CRR_INSTRUMENT_RE = re.compile(
    rf"\b{NON_CRR_INSTRUMENT_PATTERN}",
    re.I,
)
INTERNAL_ARTICLE_CONTEXT_RE = re.compile(
    r"^\s*(?:of|in|under)\s+(?:"
    r"(?:this|that|the\s+same)\s+(?:Article|Chapter|Part|Title|Section)"
    r"|Chapter\s+[0-9A-Za-z]+"
    r"|(?:the\s+)?[A-Z][A-Za-z0-9 &/-]{1,100}\(CRR\)\s+Part"
    r"|(?:the\s+)?[A-Z][A-Za-z0-9 &()/-]{1,100}\s+Part"
    r"\s+of\s+the\s+PRA\s+Rulebook"
    r"|(?:the\s+)?PRA\s+Rulebook"
    r")\b",
    re.I,
)
NON_REFERENCE_ARTICLE_SUFFIX_RE = re.compile(
    r"^\s+(?:"
    r"undertakings?|entit(?:y|ies)|defaults?|relationships?"
    r")\b",
    re.I,
)
COORDINATED_INSTRUMENT_PREFIX = (
    r"(?:\s*(?:(?:,|and|or)\s*)?(?:Articles?|Arts?\.?)\s*"
    r"\d{1,3}[A-Za-z]{0,3}(?:\s*\([^)]*\))*\s*)*"
    r"(?:\s*(?:,|and|or|[-–—])\s*\([^)]*\))*"
    r"(?:\s*(?:,|and|or|[-–—])\s*(?:[-–—]\s*)?\d+(?:\s*\([^)]*\))*)*"
    r"(?:\s+and\s+(?:Schedule\s+\d+|Annex\s+[IVXLCDM]+(?:\s+point\s+\([^)]*\))?))?"
    r"(?:\s*\.\s*\d+(?:\s*\([^)]*\))*)?"
    r"(?:\s+(?:first|second|third)\s+paragraph)?"
    r"\s*(?:(?:of|under)\s+(?:the\s+)?(?:Annex\s+to\s+)?"
    r"|in\s+(?:the\s+)?(?:version\s+of\s+)?)"
)
LEGISLATION_NS = "http://www.legislation.gov.uk/namespaces/legislation"


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_article(value: str) -> str:
    match = re.match(r"\s*(\d{1,3}[A-Za-z]{0,3})", value or "", re.I)
    return match.group(1).lower() if match else ""


def normalized_full_token(base: str, paragraphs: str = "") -> str:
    cleaned_paragraphs = re.sub(r"\s+", "", paragraphs or "").lower()
    return f"{normalized_article(base)}{cleaned_paragraphs}"


@dataclass(frozen=True)
class ArticleToken:
    base: str
    paragraphs: str
    full: str
    start: int
    end: int


@dataclass(frozen=True)
class ArticleCitation:
    start: int
    end: int
    text: str
    prefix: str
    tokens: tuple[ArticleToken, ...]
    separators: tuple[str, ...]

    @property
    def is_range(self) -> bool:
        return any(separator.casefold() in {"to", "-", "–", "—"} for separator in self.separators)

    @property
    def bases(self) -> tuple[str, ...]:
        return tuple(token.base for token in self.tokens)


@dataclass(frozen=True)
class OfficialArticle:
    article: str
    title: str
    text: str
    url: str
    document_uri: str
    content_hash: str


def extract_article_citations(value: str) -> list[ArticleCitation]:
    """Extract singular, list, and range Article citations without guessing source.

    A separator is accepted only when followed by another complete numeric
    Article token.  Consequently ``Article 48(1)(a) and (b)`` stays one target,
    while ``Article 379 and 380`` produces two.
    """

    text = value or ""
    citations: list[ArticleCitation] = []
    for prefix_match in ARTICLE_PREFIX_RE.finditer(text):
        token_match = ARTICLE_TOKEN_RE.match(text, prefix_match.end())
        if not token_match:
            continue
        tokens = [_token_from_match(token_match)]
        separators: list[str] = []
        cursor = token_match.end()
        while True:
            separator_match = ARTICLE_SEPARATOR_RE.match(text, cursor)
            if not separator_match:
                break
            next_token = ARTICLE_TOKEN_RE.match(text, separator_match.end())
            if not next_token:
                break
            separator = compact(separator_match.group("separator")).casefold()
            tokens.append(_token_from_match(next_token))
            separators.append(separator)
            cursor = next_token.end()
        citations.append(
            ArticleCitation(
                start=prefix_match.start(),
                end=cursor,
                text=compact(text[prefix_match.start() : cursor]),
                prefix=prefix_match.group("prefix"),
                tokens=tuple(tokens),
                separators=tuple(separators),
            )
        )
    return citations


def _token_from_match(match: re.Match[str]) -> ArticleToken:
    base = normalized_article(match.group("base"))
    paragraphs = re.sub(r"\s+", "", match.group("paragraphs") or "").lower()
    return ArticleToken(
        base=base,
        paragraphs=paragraphs,
        full=f"{base}{paragraphs}",
        start=match.start(),
        end=match.end(),
    )


def citation_context(value: str, citation: ArticleCitation, radius: int = 220) -> str:
    """Return enough adjacent prose to identify the cited legal instrument."""

    text = value or ""
    start = max(0, citation.start - radius)
    end = min(len(text), citation.end + radius)
    return compact(text[start:end])


def explicit_instrument(
    value: str,
    citation: ArticleCitation,
) -> tuple[str, str]:
    """Classify instrument wording close to one citation.

    Returns ``("uk_crr", evidence)``, ``("other", evidence)``, or
    ``("", "")``.  A specific non-CRR instrument next to the citation wins
    over a more distant CRR mention in the same window.
    """

    text = value or ""
    before = text[max(0, citation.start - 500) : citation.start]
    after = text[citation.end : min(len(text), citation.end + 200)]

    generic_order = re.match(r"^\s*of\s+the\s+Order\b", after, re.I)
    if generic_order:
        return "other", compact(generic_order.group(0))

    annotated_other = re.match(
        rf"^\s+and\s+Annex\s+[IVXLCDM]+(?:\s+point\s+\([^)]*\))?\s+"
        rf"(?P<instrument>{NON_CRR_INSTRUMENT_PATTERN})",
        after,
        re.I,
    )
    if annotated_other:
        return "other", compact(annotated_other.group("instrument"))

    # Coordinated shorthand often puts the instrument after a later Article,
    # e.g. "Art. 93 and Art. 94 of the Solvency II Directive".  Permit only
    # same-clause punctuation/list wording between this citation and "of".
    other_after = re.match(
        rf"^(?:{COORDINATED_INSTRUMENT_PREFIX}|[\s,()\[\]]*)"
        rf"(?P<instrument>{NON_CRR_INSTRUMENT_PATTERN})",
        after,
        re.I,
    )
    if other_after:
        evidence = other_after.group("instrument")
        if not CRR_EXPLICIT_RE.search(evidence):
            return "other", compact(evidence)
    other_before = re.search(
        rf"(?P<instrument>{NON_CRR_INSTRUMENT_PATTERN})[\s,()\[\]]*$",
        before,
        re.I,
    )
    if other_before:
        evidence = other_before.group("instrument")
        if not CRR_EXPLICIT_RE.search(evidence):
            return "other", compact(evidence)

    crr_after = re.match(
        rf"^(?:{COORDINATED_INSTRUMENT_PREFIX}|[\s,()\[\]]*)"
        r"(?P<instrument>(?:UK\s+)?CRR\b|Capital\s+Requirements?\s+Regulation\b|"
        r"Regulation\s*\(EU\)\s*(?:No\.?\s*)?(?:575\s*/\s*2013|2013\s*/\s*575)\b)",
        after,
        re.I,
    )
    if crr_after:
        return "uk_crr", compact(crr_after.group("instrument"))
    internal_part_before = re.search(
        r"(?P<part>[A-Z][A-Za-z0-9 &/-]{1,100}\s+\(CRR\))[\s,()\[\]]*$",
        before,
        re.I,
    )
    if internal_part_before:
        return "internal", compact(internal_part_before.group("part"))

    crr_before = re.search(
        r"(?P<instrument>(?:UK\s+)?CRR|Capital\s+Requirements?\s+Regulation|"
        r"Regulation\s*\(EU\)\s*(?:No\.?\s*)?(?:575\s*/\s*2013|2013\s*/\s*575))"
        r"[\s,()\[\]]*$",
        before,
        re.I,
    )
    if crr_before:
        return "uk_crr", compact(crr_before.group("instrument"))

    internal = INTERNAL_ARTICLE_CONTEXT_RE.match(after)
    if internal:
        return "internal", compact(internal.group(0))

    if re.search(r"\bextension\s+of\s*$", before, re.I):
        return "other", "Article 50 TEU withdrawal-extension context"

    return "", ""


def is_non_reference_article_use(value: str, citation: ArticleCitation) -> bool:
    """Identify lexicalised defined terms that merely start with ``Article``.

    Expressions such as ``Article 109 undertaking`` and ``article 9 default``
    are nouns defined by the Rulebook, not citations to Article 109 or 9.
    """

    text = value or ""
    if NON_REFERENCE_ARTICLE_SUFFIX_RE.match(text[citation.end :]):
        return True
    # PDF OCR can split ordinary words into fragments, producing false
    # "art 30" matches inside text such as "sm allp art 30".
    return bool(re.search(r"p\s+art\s+\d+\s*$", text[max(0, citation.start - 4) : citation.end], re.I))


def _distance_to_span(start: int, end: int, target_start: int, target_end: int) -> int:
    if end < target_start:
        return target_start - end
    if start > target_end:
        return start - target_end
    return 0


def load_official_uk_crr_articles(path: Path) -> tuple[list[str], dict[str, OfficialArticle]]:
    """Load the latest revised UK CRR Article order and text from legislation XML."""

    root = ElementTree.parse(path).getroot()
    parent_by_child = {child: parent for parent in root.iter() for child in parent}
    order: list[str] = []
    articles: dict[str, OfficialArticle] = {}
    p1_tag = f"{{{LEGISLATION_NS}}}P1"
    title_tag = f"{{{LEGISLATION_NS}}}Title"
    for provision in root.iter(p1_tag):
        document_uri = provision.attrib.get("DocumentURI") or ""
        parsed = urlsplit(document_uri)
        match = re.search(r"/article/([^/]+)$", parsed.path, re.I)
        if not match:
            continue
        article = normalized_article(match.group(1))
        if not article or article in articles:
            continue
        parent = parent_by_child.get(provision)
        title_node = parent.find(title_tag) if parent is not None else None
        provision_title = compact(" ".join(title_node.itertext())) if title_node is not None else ""
        rendered = _render_legislation_element(provision)
        heading = f"UK CRR Article {article.upper() if article[-1:].isalpha() else article}"
        title = f"{heading} — {provision_title}" if provision_title else heading
        text = f"{heading}\n{provision_title}\n\n{rendered}" if provision_title else f"{heading}\n\n{rendered}"
        text = _dedupe_article_heading(text, article)
        url = f"https://www.legislation.gov.uk/eur/2013/575/article/{article}"
        articles[article] = OfficialArticle(
            article=article,
            title=title,
            text=text,
            url=url,
            document_uri=document_uri.replace("http://", "https://", 1),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        order.append(article)
    return order, articles


def _render_legislation_element(element: ElementTree.Element) -> str:
    block_tags = {
        "P1para",
        "P2",
        "P3",
        "P4",
        "P5",
        "ListItem",
        "Para",
        "Tabular",
        "tr",
    }
    chunks: list[str] = []

    def visit(node: ElementTree.Element) -> None:
        local_name = node.tag.rsplit("}", 1)[-1]
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


def _dedupe_article_heading(value: str, article: str) -> str:
    lines = value.splitlines()
    if len(lines) < 3:
        return value.strip()
    body = "\n".join(lines[2:]).strip()
    body = re.sub(
        rf"^Article\s+{re.escape(article)}\b\s*",
        "",
        body,
        count=1,
        flags=re.I,
    )
    return "\n".join([lines[0], lines[1], "", body]).strip()


def expand_citation_articles(
    citation: ArticleCitation,
    official_order: list[str],
) -> tuple[list[str], list[str]]:
    """Expand coordinated citations and inclusive Article ranges.

    Returns ``(article_numbers, errors)``.  Article order comes from the current
    legislation contents rather than numeric guessing, so lettered provisions
    such as 47a are handled correctly.
    """

    if not citation.tokens:
        return [], []
    index = {article: position for position, article in enumerate(official_order)}
    expanded: list[str] = [citation.tokens[0].base]
    errors: list[str] = []
    for token_index, (separator, token) in enumerate(
        zip(citation.separators, citation.tokens[1:]),
        start=1,
    ):
        # The preceding lexical token, not the last expanded range member, is
        # the left endpoint.
        left = citation.tokens[token_index - 1].base
        right = token.base
        if separator in {"to", "-", "–", "—"}:
            if left not in index or right not in index or index[left] > index[right]:
                errors.append(f"cannot expand Article range {left} {separator} {right}")
                expanded.append(right)
            else:
                expanded.extend(official_order[index[left] + 1 : index[right] + 1])
        else:
            expanded.append(right)
    seen: set[str] = set()
    unique = []
    for article in expanded:
        if article not in seen:
            seen.add(article)
            unique.append(article)
    return unique, errors
