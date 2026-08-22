from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .fetch import BASE_URL
from .legal_identity import (
    canonical_part_key,
    canonical_provision_key,
    normalise_rulebook_date,
    provision_version_key,
    rulebook_date_from_url,
    snapshot_id,
    source_page_key,
)
from .models import Edge, Node
from .store import sha1

RULE_NUMBER_RE = re.compile(r"^\d+[A-Z]?(?:\.\d+[A-Z]?)*$|^\d+[A-Z]?$")
GUIDANCE_PARA_RE = re.compile(r"^\d+(?:\.\d+)*[A-Z]?$", re.IGNORECASE)
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
PRA_RULE_LINK_RE = re.compile(r"^/pra-rules/[^?#]+")
GLOSSARY_HASH_RE = re.compile(r"#glossary-term-([A-Za-z0-9]+)")
FIRM_CATEGORIES = ["CRR Firms", "Non-CRR Firms", "SII Firms", "Non-SII Firms", "Non-authorised persons"]
FIRM_CATEGORY_HREFS = {
    "/pra-rules/crr-firms": "CRR Firms",
    "/pra-rules/non-crr-firms": "Non-CRR Firms",
    "/pra-rules/sii-firms": "SII Firms",
    "/pra-rules/non-sii-firms": "Non-SII Firms",
    "/pra-rules/non-authorised-persons": "Non-authorised persons",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def absolute_url(href: str) -> str:
    return urljoin(BASE_URL, href)


def node_id(*parts: str) -> str:
    return sha1("|".join(parts))[:16]


def edge_id(*parts: str) -> str:
    return sha1("|".join(parts))[:20]


def extract_rulebook_index(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    """Parse /pra-rules listing into part nodes."""
    soup = BeautifulSoup(html, "lxml")
    nodes: list[Node] = []
    edges: list[Edge] = []
    root = Node(node_id("rulebook", "pra-rules"), "rulebook", "rulebook:pra-rules", "PRA Rules", url=url)
    nodes.append(root)

    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not PRA_RULE_LINK_RE.match(href):
            continue
        if href.rstrip("/") in {"/pra-rules", "/pra-rules/crr-firms", "/pra-rules/non-crr-firms", "/pra-rules/sii-firms", "/pra-rules/non-sii-firms", "/pra-rules/non-authorised-persons", "/pra-rules/forms"}:
            continue
        full = absolute_url(href)
        if full in seen:
            continue
        seen.add(full)
        title = clean_text(a.get_text(" "))
        if not title:
            continue
        cats = [c for c in FIRM_CATEGORIES if c.lower() in title.lower()]
        # Listing anchor text often combines categories and title. Last non-category-ish line is the title.
        parts = [clean_text(x) for x in a.get_text("\n").split("\n") if clean_text(x)]
        if len(parts) > 1:
            title = parts[-1]
            cats = [c for c in FIRM_CATEGORIES if any(c.lower() == p.lower() or c.lower() in p.lower() for p in parts[:-1])]
        stable = f"part:{urlparse(full).path.strip('/')}"
        part = Node(
            node_id(stable),
            "part",
            stable,
            title,
            url=full,
            metadata={
                "firm_categories": cats,
                "identity_type": "source_page",
                "source_page_key": source_page_key(full),
                "canonical_part_key": canonical_part_key(full),
                "rulebook_date": rulebook_date_from_url(full),
            },
        )
        nodes.append(part)
        edges.append(Edge(edge_id(root.id, part.id, "contains"), root.id, part.id, "contains", "site_structure", source_url=url))
    return nodes, edges


def extract_part(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.find("h1")
    title = clean_text(title_el.get_text(" ")) if title_el else urlparse(url).path.rstrip("/").split("/")[-2]
    part_stable = f"part:{urlparse(url).path.strip('/')}"
    parsed_date = rulebook_date_from_url(url) or normalise_rulebook_date(_rulebook_date(soup))
    source_snapshot_id = snapshot_id(url, html)
    part = Node(
        node_id(part_stable),
        "part",
        part_stable,
        title,
        url=url,
        metadata={
            "rulebook_date": parsed_date,
            "firm_categories": _part_firm_categories(soup),
            "identity_type": "source_page",
            "source_page_key": source_page_key(url),
            "canonical_part_key": canonical_part_key(url),
            "snapshot_id": source_snapshot_id,
        },
    )
    nodes: list[Node] = [part]
    edges: list[Edge] = []
    current_chapter: Node | None = None
    current_container: Node | None = None

    content = soup.select_one(".rulebook-content") or soup
    for el in content.find_all(["div"], recursive=True):
        classes = set(el.get("class", []))
        if "chapter-section" in classes:
            chapter_num_el = el.select_one(".chapter-number")
            heading_el = el.select_one(".chapter-heading")
            chapter_num = clean_text(chapter_num_el.get_text(" ")) if chapter_num_el else ""
            chapter_title = clean_text(heading_el.get_text(" ")) if heading_el else f"Chapter {chapter_num}"
            html_id = el.get("id", "")
            chapter_key = chapter_num or f"{clean_text(chapter_title).lower()}:{html_id}" or html_id
            stable = f"chapter:{part_stable}:{chapter_key}"
            article_number = _article_or_annex_number(chapter_title)
            current_chapter = Node(node_id(stable), "chapter", stable, chapter_title, url=f"{url}#{html_id}", metadata={"chapter_number": chapter_num, "article_number": article_number, "part_title": title, "html_id": html_id})
            current_container = current_chapter
            nodes.append(current_chapter)
            edges.append(Edge(edge_id(part.id, current_chapter.id, "contains"), part.id, current_chapter.id, "contains", "site_structure", source_url=url))
            continue

        if "row-block" in classes:
            number_el = el.select_one(".rule-number:not(.chapter-number)")
            if not number_el:
                heading_el = el.select_one("h2, h3, h4")
                heading_title = clean_text(heading_el.get_text(" ")) if heading_el else ""
                html_id = el.get("id", "")
                if heading_title and html_id:
                    stable = f"chapter:{part_stable}:heading:{html_id}"
                    heading = Node(node_id(stable), "chapter", stable, heading_title, url=f"{url}#{html_id}", metadata={"part_title": title, "html_id": html_id, "heading_level": heading_el.name if heading_el else ""})
                    nodes.append(heading)
                    edges.append(Edge(edge_id(part.id, heading.id, "contains"), part.id, heading.id, "contains", "site_structure", source_url=url))
                    _append_heading_body_rule(nodes, edges, heading, el, heading_el, url, part_stable, title)
                    current_container = heading
                continue
            rule_number = clean_text(number_el.get_text(" ")).rstrip(".")
            if not RULE_NUMBER_RE.match(rule_number):
                heading_el = el.select_one("h2, h3, h4")
                heading_title = clean_text(heading_el.get_text(" ")) if heading_el else ""
                html_id = el.get("id", "")
                if heading_title and html_id:
                    stable = f"chapter:{part_stable}:heading:{html_id}"
                    heading = Node(node_id(stable), "chapter", stable, heading_title, url=f"{url}#{html_id}", metadata={"part_title": title, "html_id": html_id, "heading_level": heading_el.name if heading_el else ""})
                    nodes.append(heading)
                    edges.append(Edge(edge_id(part.id, heading.id, "contains"), part.id, heading.id, "contains", "site_structure", source_url=url))
                    _append_heading_body_rule(nodes, edges, heading, el, heading_el, url, part_stable, title)
                    current_container = heading
                else:
                    body_el = el.select_one(".div-row__col-2")
                    body_text = clean_text(body_el.get_text(" ")) if body_el else clean_text(el.get_text(" "))
                    parent = current_container or current_chapter
                    if parent and html_id and len(body_text) > 20:
                        stable = f"rule:{part_stable}:unnumbered:{html_id}"
                        display_number = parent.title
                        rule = Node(
                            node_id(stable), "rule", stable, display_number, text=body_text,
                            url=f"{url}#{html_id}",
                            metadata={"rule_number": "", "display_number": display_number, "part_title": title, "effective_dates": DATE_RE.findall(clean_text(el.get_text(" "))), "html_id": html_id, "unnumbered_row": True},
                        )
                        nodes.append(rule)
                        edges.append(Edge(edge_id(parent.id, rule.id, "contains"), parent.id, rule.id, "contains", "site_structure", source_url=url))
                        _append_link_edges(edges, rule, body_el or el, url)
                        _append_inline_definition_nodes(nodes, edges, rule, body_el or el, url, part_stable, title)
                    elif body_text and html_id:
                        stable = f"chapter:{part_stable}:heading:{html_id}"
                        heading = Node(node_id(stable), "chapter", stable, body_text, url=f"{url}#{html_id}", metadata={"part_title": title, "html_id": html_id, "heading_level": ""})
                        nodes.append(heading)
                        edges.append(Edge(edge_id(part.id, heading.id, "contains"), part.id, heading.id, "contains", "site_structure", source_url=url))
                        current_container = heading
                continue
            body_el = el.select_one(".div-row__col-2")
            body_text = clean_text(body_el.get_text(" ")) if body_el else clean_text(el.get_text(" "))
            section_key = ""
            if current_chapter and not (current_chapter.metadata or {}).get("chapter_number"):
                section_key = f":{current_chapter.stable_key.rsplit(':', 1)[-1]}"
            stable = f"rule:{part_stable}{section_key}:{rule_number}"
            display_number = _display_rule_number(rule_number, current_chapter)
            rule = Node(
                node_id(stable), "rule", stable, display_number, text=body_text,
                url=f"{url}#{el.get('id','')}",
                metadata={"rule_number": rule_number, "display_number": display_number, "part_title": title, "effective_dates": DATE_RE.findall(clean_text(el.get_text(" "))), "html_id": el.get("id", "")},
            )
            nodes.append(rule)
            parent = current_container or current_chapter
            if parent:
                edges.append(Edge(edge_id(parent.id, rule.id, "contains"), parent.id, rule.id, "contains", "site_structure", source_url=url))
            else:
                edges.append(Edge(edge_id(part.id, rule.id, "contains"), part.id, rule.id, "contains", "site_structure", source_url=url))
            _append_link_edges(edges, rule, body_el or el, url)
            _append_inline_definition_nodes(nodes, edges, rule, body_el or el, url, part_stable, title)
    _add_provision_identity_layer(nodes, edges, part, url, parsed_date, source_snapshot_id)
    return _dedupe_nodes(nodes), _dedupe_edges(edges)


def _add_provision_identity_layer(
    nodes: list[Node],
    edges: list[Edge],
    part: Node,
    source_url: str,
    rulebook_date: str | None,
    source_snapshot_id: str,
) -> None:
    """Turn parsed Rule rows into dated versions with canonical identities.

    The existing ``rule`` node remains the text-bearing node so the reader's
    structural ``contains`` spine is unchanged.  Its stable key and metadata
    now describe a provision version, while a new empty ``provision`` node
    represents the date-free legal identity.
    """

    node_by_id = {node.id: node for node in nodes}
    rules = [node for node in nodes if node.node_type == "rule"]
    id_map: dict[str, str] = {}
    canonical_nodes: dict[str, Node] = {}
    version_edges: list[Edge] = []

    for rule in rules:
        parent = next(
            (
                node_by_id[edge.from_node_id]
                for edge in edges
                if edge.to_node_id == rule.id
                and edge.edge_type == "contains"
                and edge.from_node_id in node_by_id
            ),
            part,
        )
        html_id = str((rule.metadata or {}).get("html_id") or "")
        rule_number = str((rule.metadata or {}).get("rule_number") or "")
        structural_locator = _provision_structural_locator(parent, html_id)
        canonical_key = canonical_provision_key(source_url, structural_locator, rule_number or "unnumbered")
        version_key = provision_version_key(canonical_key, rulebook_date, snapshot=source_snapshot_id)
        canonical_id = node_id(canonical_key)
        version_id = node_id(version_key)
        id_map[rule.id] = version_id

        canonical_nodes.setdefault(
            canonical_key,
            Node(
                canonical_id,
                "provision",
                canonical_key,
                rule.title,
                url="",
                metadata={
                    "identity_type": "canonical_provision",
                    "canonical_part_key": canonical_part_key(source_url),
                    "structural_locator": structural_locator,
                    "rule_number": rule_number,
                    "display_number": rule.title,
                },
            ),
        )

        metadata = dict(rule.metadata or {})
        metadata.update(
            {
                "identity_type": "provision_version",
                "canonical_provision_id": canonical_id,
                "canonical_provision_key": canonical_key,
                "version_key": version_key,
                "source_page_id": part.id,
                "source_page_key": source_page_key(source_url),
                "canonical_part_key": canonical_part_key(source_url),
                "snapshot_id": source_snapshot_id,
                "rulebook_date": rulebook_date,
                "structural_locator": structural_locator,
            }
        )
        rule.id = version_id
        rule.stable_key = version_key
        rule.metadata = metadata
        version_edges.append(
            Edge(
                edge_id(canonical_id, version_id, "has_version"),
                canonical_id,
                version_id,
                "has_version",
                "legal_identity",
                source_url=source_url,
                metadata={"canonical_key": canonical_key, "version_key": version_key},
            )
        )
        version_edges.append(
            Edge(
                edge_id(version_id, part.id, "sourced_from"),
                version_id,
                part.id,
                "sourced_from",
                "legal_identity",
                source_url=source_url,
                metadata={"source_page_key": source_page_key(source_url), "snapshot_id": source_snapshot_id},
            )
        )

    for node in nodes:
        if node.id in id_map:
            continue
        node.metadata = _replace_ids(node.metadata, id_map)
    for edge in edges:
        old_from, old_to = edge.from_node_id, edge.to_node_id
        edge.from_node_id = id_map.get(old_from, old_from)
        edge.to_node_id = id_map.get(old_to, old_to)
        if old_from != edge.from_node_id or old_to != edge.to_node_id:
            suffix = (edge.metadata or {}).get("href") or edge.evidence_text or edge.source_url
            edge.id = edge_id(edge.from_node_id, edge.to_node_id, edge.edge_type, suffix)
        edge.metadata = _replace_ids(edge.metadata, id_map)

    nodes.extend(canonical_nodes.values())
    edges.extend(version_edges)


def _provision_structural_locator(parent: Node, html_id: str) -> str:
    metadata = parent.metadata or {}
    chapter_number = str(metadata.get("chapter_number") or "").strip()
    parent_html_id = str(metadata.get("html_id") or "").strip()
    if parent.node_type == "part":
        prefix = "part"
    elif chapter_number:
        prefix = f"chapter:{chapter_number}"
    else:
        prefix = f"container:{parent_html_id or parent.stable_key.rsplit(':', 1)[-1]}"
    return f"{prefix}:{html_id}" if html_id else prefix


def _replace_ids(value: object, id_map: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {key: _replace_ids(item, id_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ids(item, id_map) for item in value]
    if isinstance(value, str):
        return id_map.get(value, value)
    return value


def _append_heading_body_rule(nodes: list[Node], edges: list[Edge], heading: Node, el: Tag, heading_el: Tag | None, source_url: str, part_stable: str, part_title: str) -> None:
    body_el = el.select_one(".div-row__col-2") or el
    body_text = clean_text(body_el.get_text(" "))
    heading_text = clean_text(heading_el.get_text(" ")) if heading_el else heading.title
    if body_text.lower().startswith(heading_text.lower()):
        body_text = clean_text(body_text[len(heading_text):])
    if len(body_text) <= 20:
        return
    html_id = el.get("id", "")
    if not html_id:
        return
    stable = f"rule:{part_stable}:heading-body:{html_id}"
    rule = Node(
        node_id(stable), "rule", stable, heading.title, text=body_text,
        url=f"{source_url}#{html_id}",
        metadata={"rule_number": "", "display_number": heading.title, "part_title": part_title, "effective_dates": DATE_RE.findall(clean_text(el.get_text(" "))), "html_id": html_id, "unnumbered_row": True, "heading_body": True},
    )
    nodes.append(rule)
    edges.append(Edge(edge_id(heading.id, rule.id, "contains"), heading.id, rule.id, "contains", "site_structure", source_url=source_url))
    _append_link_edges(edges, rule, body_el, source_url)
    _append_inline_definition_nodes(nodes, edges, rule, body_el, source_url, part_stable, part_title)


def _part_firm_categories(soup: BeautifulSoup) -> list[str]:
    for item in soup.select(".side-bar__item"):
        heading = item.find(["h2", "h3"])
        if not heading or clean_text(heading.get_text(" ")).lower() != "used in":
            continue
        categories = _firm_categories_from_links(item)
        if categories:
            return categories
    main = soup.select_one(".main-content, main, .container.chapters")
    return _firm_categories_from_links(main or soup)


def _firm_categories_from_links(container: Tag | BeautifulSoup) -> list[str]:
    categories: list[str] = []
    for link in container.find_all("a", href=True):
        category = FIRM_CATEGORY_HREFS.get(link["href"].rstrip("/"))
        if category and category not in categories:
            categories.append(category)
    return categories or _firm_categories_from_text(container)


def _firm_categories_from_text(container: Tag | BeautifulSoup) -> list[str]:
    text = clean_text(container.get_text(" "))
    categories: list[str] = []
    for category in FIRM_CATEGORIES:
        if re.search(rf"\b{re.escape(category)}\b", text, re.IGNORECASE) and category not in categories:
            categories.append(category)
    if re.search(r"\bNon-authorised Persons\b", text, re.IGNORECASE) and "Non-authorised persons" not in categories:
        categories.append("Non-authorised persons")
    return categories


def extract_glossary(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    nodes: list[Node] = []
    edges: list[Edge] = []
    glossary = Node(node_id("glossary"), "glossary", "glossary", "PRA Rulebook Glossary", url=url)
    nodes.append(glossary)

    content = soup.select_one(".page-content") or soup

    # Printable/export glossary pages represent each term as a row-block, with
    # the term in .rule-number and the definition in .div-row__col-2.
    row_blocks = content.select(".row-block")
    if row_blocks:
        for row in row_blocks:
            term_el = row.select_one(".rule-number")
            definition_el = row.select_one(".div-row__col-2")
            term = clean_text(term_el.get_text(" ")) if term_el else ""
            definition = clean_text(definition_el.get_text(" ")) if definition_el else ""
            if not term or not definition or term.lower() == "definition":
                continue
            stable = f"defined_term:glossary:{term.lower()}"
            n = Node(
                node_id(stable), "defined_term", stable, term, text=definition, url=url,
                metadata={"source": "glossary", "dates": DATE_RE.findall(clean_text(row.get_text(" ")))},
            )
            nodes.append(n)
            edges.append(Edge(edge_id(glossary.id, n.id, "defines"), glossary.id, n.id, "defines", "glossary_source", source_url=url))
            _append_link_edges(edges, n, definition_el or row, url)
        return _dedupe_nodes(nodes), _dedupe_edges(edges)

    # Normal paginated glossary pages use h3 headings for visible terms.
    for h in content.find_all("h3"):
        term = clean_text(h.get_text(" "))
        if not term or term.lower() in {"export page as", "follow bank of england", "browse website"}:
            continue
        definition_parts: list[str] = []
        cursor = h.next_sibling
        while cursor is not None:
            if isinstance(cursor, Tag) and cursor.name == "h3":
                break
            if isinstance(cursor, Tag):
                text = clean_text(cursor.get_text(" "))
                if "Legal Instruments that change this definition" in text:
                    break
                if text and not text.startswith("PDF ") and not text.startswith("Print "):
                    definition_parts.append(text)
            cursor = cursor.next_sibling
        definition = clean_text(" ".join(definition_parts))
        if not definition or len(definition) < 8:
            continue
        anchor = h.find_parent(id=True) or h
        stable = f"defined_term:glossary:{term.lower()}"
        n = Node(node_id(stable), "defined_term", stable, term, text=definition, url=f"{url}#{anchor.get('id','')}", metadata={"source": "glossary", "dates": DATE_RE.findall(definition)})
        nodes.append(n)
        edges.append(Edge(edge_id(glossary.id, n.id, "defines"), glossary.id, n.id, "defines", "glossary_source", source_url=url))
        _append_link_edges(edges, n, h.find_parent() or h, url)
    return _dedupe_nodes(nodes), _dedupe_edges(edges)


def _append_link_edges(edges: list[Edge], from_node: Node, container: Tag, source_url: str) -> None:
    for a in container.find_all("a", href=True):
        href = a["href"]
        text = clean_text(a.get_text(" "))
        if not href or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        if m := GLOSSARY_HASH_RE.search(href):
            term = clean_text(a.get("title") or text)
            target = f"defined_term:glossary:{term.lower()}" if term else f"glossary-term:{m.group(1)}"
            to_id = node_id(target)
            edges.append(Edge(edge_id(from_node.id, to_id, "uses_defined_term", href), from_node.id, to_id, "uses_defined_term", "html_glossary_link", 1.0, term or text, source_url, {"href": href, "target_key": target, "glossary_hash": m.group(1)}))
        elif href.startswith("/pra-rules/"):
            parsed_href = urlparse(absolute_url(href))
            fragment = f"#{parsed_href.fragment}" if parsed_href.fragment else ""
            target_key = f"url:{parsed_href.path.strip('/')}{fragment}"
            to_id = node_id(target_key)
            edges.append(Edge(edge_id(from_node.id, to_id, "references", href), from_node.id, to_id, "references", "html_link", 1.0, text, source_url, {"href": absolute_url(href), "target_key": target_key}))
        elif href.startswith("/") or href.startswith("http"):
            target_key = f"external:{absolute_url(href)}"
            to_id = node_id(target_key)
            edges.append(Edge(edge_id(from_node.id, to_id, "references", href), from_node.id, to_id, "references", "html_link", 0.8, text, source_url, {"href": absolute_url(href), "target_key": target_key}))


def _append_inline_definition_nodes(nodes: list[Node], edges: list[Edge], from_node: Node, container: Tag, source_url: str, part_stable: str, part_title: str) -> None:
    """Extract Part-local definitions embedded in rule text.

    The PRA site does not expose all definitions solely via the central
    Glossary/CRR pages. Some Part-specific terms are rendered inline as a term
    paragraph followed by an indented definition paragraph, while the clickable
    term opens a glossary modal. Preserve those as first-class definition nodes
    so the graph has the definition text even when the central glossary export
    does not.
    """
    blocks = [b for b in container.find_all(["p", "li"], recursive=True) if clean_text(b.get_text(" "))]
    for i, block in enumerate(blocks[:-1]):
        term_link = block.select_one("a.glossary-link[href]") or block.find("a", href=GLOSSARY_HASH_RE)
        if not term_link:
            continue
        term_text = clean_text(term_link.get("title") or term_link.get_text(" "))
        block_text = clean_text(block.get_text(" "))
        # A term heading is usually just the linked term. Avoid treating normal
        # prose references as definitions.
        if not term_text or len(block_text) > len(term_text) + 8:
            continue
        definition = _inline_definition_text(block)
        if not definition:
            # Retain support for older markup where the term and definition
            # are not siblings beneath the same container.
            definition = clean_text(blocks[i + 1].get_text(" "))
        if not re.match(r"^(means|includes|has the meaning|is|are)\b", definition, re.IGNORECASE):
            continue
        href = term_link.get("href", "")
        glossary_hash = (GLOSSARY_HASH_RE.search(href) or [None, ""])[1]
        glossary_id = term_link.get("data-glossary-id", "")
        stable = f"defined_term:part:{part_stable}:{term_text.lower()}"
        term_node = Node(
            node_id(stable), "defined_term", stable, term_text, text=definition, url=source_url,
            metadata={
                "source": "inline_part_definition",
                "part_title": part_title,
                "rule_id": from_node.id,
                "rule_title": from_node.title,
                "glossary_hash": glossary_hash,
                "glossary_id": glossary_id,
            },
        )
        nodes.append(term_node)
        edges.append(Edge(edge_id(from_node.id, term_node.id, "defines", stable), from_node.id, term_node.id, "defines", "inline_part_definition", 1.0, term_text, source_url, {"part_title": part_title, "glossary_hash": glossary_hash, "glossary_id": glossary_id}))
        edges.append(Edge(edge_id(from_node.id, term_node.id, "uses_defined_term", stable), from_node.id, term_node.id, "uses_defined_term", "inline_part_definition", 1.0, term_text, source_url, {"part_title": part_title, "glossary_hash": glossary_hash, "glossary_id": glossary_id}))


def _inline_definition_text(term_block: Tag) -> str:
    """Return every sibling block belonging to an inline definition.

    Definitions in the Rulebook are commonly represented as a term ``<p>``,
    a lead-in ``<p>`` and then one or more ``<ol>``/``<ul>`` blocks. Taking
    only the next paragraph silently loses the enumerated limbs. The next term
    heading is the reliable boundary because it uses the same short linked-term
    markup as the current heading.
    """
    parts: list[str] = []
    for sibling in term_block.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if _inline_term_heading(sibling):
            break
        text = clean_text(sibling.get_text(" "))
        if text:
            parts.append(text)
    return clean_text(" ".join(parts))


def _inline_term_heading(block: Tag) -> str:
    term_link = block.select_one("a.glossary-link[href]") or block.find("a", href=GLOSSARY_HASH_RE)
    if not term_link:
        return ""
    term_text = clean_text(term_link.get("title") or term_link.get_text(" "))
    block_text = clean_text(block.get_text(" "))
    return term_text if term_text and len(block_text) <= len(term_text) + 8 else ""


def _article_or_annex_number(title: str) -> str:
    text = clean_text(title)
    match = re.match(r"^(Article\s+\d+[A-Za-z]*|Annex\s+[IVXLCDM]+)\b", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _display_rule_number(rule_number: str, current_chapter: Node | None) -> str:
    """Return a compact legal citation for a row-level provision.

    CRR-style pages repeat paragraph numbers inside each Article/Annex. A bare
    "1" or "2" is ambiguous, so display these as Article 2(1), Annex I(3),
    etc. Conventional PRA chapter rules already carry meaningful numbering
    such as 2.1, so leave those as-is.
    """
    if current_chapter:
        article_number = clean_text((current_chapter.metadata or {}).get("article_number", ""))
        if article_number:
            suffix = "".join(f"({part})" for part in rule_number.split(".") if part)
            return f"{article_number}{suffix}"
    return rule_number


def _rulebook_date(soup: BeautifulSoup) -> str | None:
    content = soup.select_one(".rulebook-content")
    if content and content.get("data-rulebook-date"):
        return content.get("data-rulebook-date")
    text = clean_text(soup.get_text(" "))
    match = DATE_RE.search(text)
    return match.group(0) if match else None


def _dedupe_nodes(nodes: list[Node]) -> list[Node]:
    seen = set(); out=[]
    for n in nodes:
        if n.id not in seen:
            out.append(n); seen.add(n.id)
    return out


def _dedupe_edges(edges: list[Edge]) -> list[Edge]:
    seen = set(); out=[]
    for e in edges:
        if e.id not in seen:
            out.append(e); seen.add(e.id)
    return out

GUIDANCE_LINK_RE = re.compile(r"^/guidance/[^?#]+")


def extract_guidance_index(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    root = Node(node_id("guidance"), "guidance_index", "guidance", "PRA Guidance", url=url)
    nodes: list[Node] = [root]
    edges: list[Edge] = []
    seen: set[str] = set()
    for a in soup.select(".search-results a[href]"):
        href = a.get("href", "")
        if not GUIDANCE_LINK_RE.match(href):
            continue
        full = absolute_url(href)
        if full in seen:
            continue
        seen.add(full)
        h3 = a.find("h3")
        title = clean_text(h3.get_text(" ") if h3 else a.get_text(" "))
        tags = [clean_text(t.get_text(" ")) for t in a.select(".release-tag")]
        doc_type = "supervisory_statement" if "/supervisory-statements/" in href else "statement_of_policy" if "/statements-of-policy/" in href else "guidance_document"
        stable = f"guidance_document:{urlparse(full).path.strip('/')}"
        n = Node(node_id(stable), "guidance_document", stable, title, url=full, metadata={"document_type": doc_type, "firm_categories": tags})
        nodes.append(n)
        edges.append(Edge(edge_id(root.id, n.id, "contains"), root.id, n.id, "contains", "site_structure", source_url=url))
    return nodes, edges


def extract_guidance_detail(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.find("h1")
    title = clean_text(title_el.get_text(" ")) if title_el else urlparse(url).path.rstrip("/").split("/")[-2]
    doc_type = "supervisory_statement" if "/supervisory-statements/" in url else "statement_of_policy" if "/statements-of-policy/" in url else "guidance_document"
    doc_stable = f"guidance_document:{urlparse(url).path.strip('/')}"
    doc = Node(node_id(doc_stable), "guidance_document", doc_stable, title, url=url, metadata={"document_type": doc_type, "rulebook_date": _rulebook_date(soup)})
    nodes: list[Node] = [doc]
    edges: list[Edge] = []
    current_section: Node | None = None
    content = soup.select_one(".rulebook-content") or soup.select_one(".page-content") or soup
    for el in content.find_all("div", recursive=True):
        classes = set(el.get("class", []))
        if "chapter-section" in classes:
            num_el = el.select_one(".chapter-number")
            heading_el = el.select_one(".chapter-heading")
            num = clean_text(num_el.get_text(" ")) if num_el else ""
            heading = clean_text(heading_el.get_text(" ")) if heading_el else f"Section {num}"
            html_id = el.get("id", "")
            section_key = num or clean_text(heading).lower() or html_id
            stable = f"guidance_section:{doc_stable}:{section_key}"
            current_section = Node(node_id(stable), "guidance_section", stable, heading, url=f"{url}#{html_id}", metadata={"section_number": num, "document_title": title, "html_id": html_id})
            nodes.append(current_section)
            edges.append(Edge(edge_id(doc.id, current_section.id, "contains"), doc.id, current_section.id, "contains", "site_structure", source_url=url))
            continue
        if "row-block" in classes:
            number_el = el.select_one(".rule-number:not(.chapter-number)")
            body_el = el.select_one(".div-row__col-2")
            if not body_el:
                continue
            para = clean_text(number_el.get_text(" ")).rstrip(".") if number_el else ""
            text = clean_text(body_el.get_text(" "))
            if not text:
                continue
            html_id = el.get("id", "")
            if para and GUIDANCE_PARA_RE.match(para):
                # Numbered guidance paragraphs have a stable legal identity in their
                # paragraph number, but some guidance documents restart numbering in
                # appendices/sections. Use the current section as context when present.
                # The HTML id is stored as an alias/metadata, not as the canonical key.
                paragraph_parent_key = current_section.stable_key if current_section else doc_stable
                stable = f"guidance_paragraph:{paragraph_parent_key}:{para}"
                para_title = f"{title} {para}"
                metadata = {"paragraph_number": para, "document_title": title, "html_id": html_id}
            elif html_id and len(text) > 20:
                stable = f"guidance_paragraph:{doc_stable}:unnumbered:{html_id}"
                para_title = f"{title} – unnumbered paragraph"
                metadata = {"paragraph_number": "", "document_title": title, "html_id": html_id, "unnumbered_row": True}
            else:
                continue
            n = Node(node_id(stable), "guidance_paragraph", stable, para_title, text=text, url=f"{url}#{html_id}", metadata=metadata)
            nodes.append(n)
            parent = current_section or doc
            edges.append(Edge(edge_id(parent.id, n.id, "contains"), parent.id, n.id, "contains", "site_structure", source_url=url))
            _append_link_edges(edges, n, body_el, url)
    return _dedupe_nodes(nodes), _dedupe_edges(edges)


def extract_crr_terms(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    """Parse CRR Terms List, remapping every edge endpoint consistently.

    Glossary parsing emits glossary-style IDs. After renaming nodes to the
    CRR namespace, every edge must have both from_node_id and to_node_id
    remapped through the same id_map, then be re-keyed.
    """
    nodes, edges = extract_glossary(html, url)
    old_root = node_id("glossary")
    new_root = node_id("crr_terms_list")
    id_map: dict[str, str] = {}
    for n in nodes:
        if n.node_type == "glossary":
            id_map[n.id] = new_root
            n.node_type = "crr_terms_list"
            n.stable_key = "crr_terms_list"
            n.title = "CRR Terms List"
            n.id = new_root
        elif n.node_type == "defined_term":
            new_stable = n.stable_key.replace("defined_term:glossary:", "defined_term:crr:")
            new_id = node_id(new_stable)
            id_map[n.id] = new_id
            n.stable_key = new_stable
            n.id = new_id
            n.metadata["source"] = "crr_terms_list"

    # Drop defines edges; rebuild them from the renamed root below.
    edges = [e for e in edges if e.edge_type != "defines"]

    # Remap BOTH endpoints of every remaining edge, then re-key.
    for e in edges:
        e.from_node_id = id_map.get(e.from_node_id, e.from_node_id)
        e.to_node_id = id_map.get(e.to_node_id, e.to_node_id)
        if e.source_method == "glossary_source":
            e.source_method = "crr_terms_source"
        e.id = edge_id(e.from_node_id, e.to_node_id, e.edge_type)

    for n in nodes:
        if n.node_type == "defined_term":
            edges.append(Edge(edge_id(new_root, n.id, "defines"), new_root, n.id, "defines", "crr_terms_source", source_url=url))
    return _dedupe_nodes(nodes), _dedupe_edges(edges)


def extract_legal_instruments_index(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    root = Node(node_id("legal_instruments"), "legal_instruments_index", "legal_instruments", "PRA Legal Instruments", url=url)
    nodes: list[Node] = [root]
    edges: list[Edge] = []
    for card in soup.select(".card-block"):
        link = card.select_one("a.card-block__link[href]")
        title_el = card.select_one(".card-block__title")
        if not link or not title_el:
            continue
        title = clean_text(title_el.get_text(" "))
        href = absolute_url(link["href"])
        date_text = clean_text(card.select_one(".card-block__date").get_text(" ")) if card.select_one(".card-block__date") else ""
        effective = [clean_text(h.get_text(" ")) for h in card.select(".card-block__sub-title") if "Effective" in clean_text(h.get_text(" "))]
        stable = f"legal_instrument:{href}"
        inst = Node(node_id(stable), "legal_instrument", stable, title, url=href, metadata={"published": date_text, "effective": effective})
        nodes.append(inst)
        edges.append(Edge(edge_id(root.id, inst.id, "contains"), root.id, inst.id, "contains", "site_structure", source_url=url))
        for a in card.select(".card-block__bottom a[href]"):
            ahref = a.get("href", "")
            text = clean_text(a.get_text(" "))
            if ahref.startswith("/pra-rules/"):
                target_key = f"url:{urlparse(absolute_url(ahref)).path.strip('/')}"
                to_id = node_id(target_key)
                edges.append(Edge(edge_id(inst.id, to_id, "amends", ahref), inst.id, to_id, "amends", "legal_instrument_listing", 1.0, text, url, {"href": absolute_url(ahref), "target_key": target_key}))
            elif ahref.startswith("/glossary") or ahref.startswith("/crr-terms-list"):
                target_key = "glossary" if ahref.startswith("/glossary") else "crr_terms_list"
                edges.append(Edge(edge_id(inst.id, node_id(target_key), "amends", ahref), inst.id, node_id(target_key), "amends", "legal_instrument_listing", 1.0, text, url, {"href": absolute_url(ahref), "target_key": target_key}))
            else:
                target_key = f"external:{absolute_url(ahref)}"
                edges.append(Edge(edge_id(inst.id, node_id(target_key), "references", ahref), inst.id, node_id(target_key), "references", "legal_instrument_listing", 0.9, text, url, {"href": absolute_url(ahref), "target_key": target_key}))
    return _dedupe_nodes(nodes), _dedupe_edges(edges)
