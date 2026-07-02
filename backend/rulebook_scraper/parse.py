from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .fetch import BASE_URL
from .models import Edge, Node
from .store import sha1

RULE_NUMBER_RE = re.compile(r"^\d+[A-Z]?(?:\.\d+[A-Z]?)*$|^\d+[A-Z]?$")
GUIDANCE_PARA_RE = re.compile(r"^\d+(?:\.\d+)*[A-Z]?$", re.IGNORECASE)
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
PRA_RULE_LINK_RE = re.compile(r"^/pra-rules/[^?#]+")
GLOSSARY_HASH_RE = re.compile(r"#glossary-term-([A-Za-z0-9]+)")
FIRM_CATEGORIES = ["CRR Firms", "Non-CRR Firms", "SII Firms", "Non-SII Firms", "Non-authorised persons"]


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
        part = Node(node_id(stable), "part", stable, title, url=full, metadata={"firm_categories": cats})
        nodes.append(part)
        edges.append(Edge(edge_id(root.id, part.id, "contains"), root.id, part.id, "contains", "site_structure", source_url=url))
    return nodes, edges


def extract_part(html: str, url: str) -> tuple[list[Node], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.find("h1")
    title = clean_text(title_el.get_text(" ")) if title_el else urlparse(url).path.rstrip("/").split("/")[-2]
    part_stable = f"part:{urlparse(url).path.strip('/')}"
    part = Node(node_id(part_stable), "part", part_stable, title, url=url, metadata={"rulebook_date": _rulebook_date(soup)})
    nodes: list[Node] = [part]
    edges: list[Edge] = []
    current_chapter: Node | None = None

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
                else:
                    body_el = el.select_one(".div-row__col-2")
                    body_text = clean_text(body_el.get_text(" ")) if body_el else clean_text(el.get_text(" "))
                    if current_chapter and html_id and len(body_text) > 20:
                        stable = f"rule:{part_stable}:unnumbered:{html_id}"
                        display_number = current_chapter.title
                        rule = Node(
                            node_id(stable), "rule", stable, display_number, text=body_text,
                            url=f"{url}#{html_id}",
                            metadata={"rule_number": "", "display_number": display_number, "part_title": title, "effective_dates": DATE_RE.findall(clean_text(el.get_text(" "))), "html_id": html_id, "unnumbered_row": True},
                        )
                        nodes.append(rule)
                        edges.append(Edge(edge_id(current_chapter.id, rule.id, "contains"), current_chapter.id, rule.id, "contains", "site_structure", source_url=url))
                        _append_link_edges(edges, rule, body_el or el, url)
                        _append_inline_definition_nodes(nodes, edges, rule, body_el or el, url, part_stable, title)
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
            if current_chapter:
                edges.append(Edge(edge_id(current_chapter.id, rule.id, "contains"), current_chapter.id, rule.id, "contains", "site_structure", source_url=url))
            else:
                edges.append(Edge(edge_id(part.id, rule.id, "contains"), part.id, rule.id, "contains", "site_structure", source_url=url))
            _append_link_edges(edges, rule, body_el or el, url)
            _append_inline_definition_nodes(nodes, edges, rule, body_el or el, url, part_stable, title)
    return _dedupe_nodes(nodes), _dedupe_edges(edges)


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
        definition_block = blocks[i + 1]
        definition = clean_text(definition_block.get_text(" "))
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
    nodes, edges = extract_glossary(html, url)
    old_root = node_id("glossary")
    new_root = node_id("crr_terms_list")
    for n in nodes:
        if n.node_type == "glossary":
            n.node_type = "crr_terms_list"
            n.stable_key = "crr_terms_list"
            n.title = "CRR Terms List"
            n.id = new_root
        elif n.node_type == "defined_term":
            n.stable_key = n.stable_key.replace("defined_term:glossary:", "defined_term:crr:")
            n.id = node_id(n.stable_key)
            n.metadata["source"] = "crr_terms_list"
    for e in edges:
        if e.from_node_id == old_root:
            e.from_node_id = new_root
        if e.edge_type == "defines":
            # Recompute to_node_id from target_key is not needed for current rows;
            # define edges point at nodes in order, so align by evidence later.
            e.source_method = "crr_terms_source"
            e.id = edge_id(e.from_node_id, e.to_node_id, "defines")
    # Safer rebuild define edges after node ids change.
    edges = [e for e in edges if e.edge_type != "defines"]
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
