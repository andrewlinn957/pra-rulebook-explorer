from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from itertools import combinations

from .models import Edge, Node
from .parse import edge_id, extract_part, node_id
from .store import upsert_edges, upsert_nodes


def derive_richer_edges(conn: sqlite3.Connection, *, max_term_degree: int = 25, max_edges_per_term: int = 300) -> dict[str, int]:
    """Derive deterministic discovery edges from the current corpus.

    These are intentionally conservative and explainable. They do not replace
    explicit references, but add useful rule-to-rule/guidance-to-rule bridges.
    """
    edges: list[Edge] = []
    edges.extend(_resolve_html_anchor_reference_edges(conn))
    edges.extend(_shared_defined_term_edges(conn, max_term_degree=max_term_degree, max_edges_per_term=max_edges_per_term))
    edges.extend(_same_rulebook_part_name_edges(conn))
    upsert_edges(conn, edges)
    conn.commit()
    counts: dict[str, int] = defaultdict(int)
    for e in edges:
        counts[e.edge_type] += 1
    return dict(counts)


def repair_internal_anchor_references(conn: sqlite3.Connection) -> dict[str, int]:
    """Repair `/pra-rules/...#anchor` links into true internal graph links.

    This is safe to run after an existing scrape. It reparses cached Part HTML
    to add unnumbered heading/container nodes, then resolves hash-link reference
    edges to parsed nodes by `metadata.html_id`.
    """
    heading_nodes = []
    heading_edges = []
    for url, raw_html in conn.execute("SELECT url, raw_html FROM document_source WHERE source_type='part'"):
        nodes, edges = extract_part(raw_html, url)
        ids = {n.id for n in nodes if n.node_type == "chapter" and (n.metadata or {}).get("heading_level")}
        heading_nodes.extend([n for n in nodes if n.id in ids])
        heading_edges.extend([e for e in edges if e.from_node_id in ids or e.to_node_id in ids])
    upsert_nodes(conn, heading_nodes)
    upsert_edges(conn, heading_edges)
    resolved_edges = _resolve_html_anchor_reference_edges(conn)
    upsert_edges(conn, resolved_edges)
    unresolved_nodes, unresolved_edges = _repair_unresolved_anchor_placeholders(conn)
    upsert_nodes(conn, unresolved_nodes)
    upsert_edges(conn, unresolved_edges)
    conn.commit()
    return {"heading_nodes": len({n.id for n in heading_nodes}), "heading_edges": len({e.id for e in heading_edges}), "html_anchor_resolved": len({e.id for e in resolved_edges}), "html_anchor_unresolved": len({e.id for e in unresolved_edges})}


def _resolve_html_anchor_reference_edges(conn: sqlite3.Connection) -> list[Edge]:
    """Resolve PRA Rulebook hash links to parsed provision nodes.

    The source site often links to internal provisions as
    `/pra-rules/some-part#<html-id>` rather than the dated URL we scrape. The
    original parser deliberately kept only the path as a placeholder target,
    which means multiple distinct anchors in the same Part collapsed to one
    `rule_reference` node. Resolve those links after the corpus has been parsed
    by matching the hash against node.metadata.html_id.

    Some legacy `html_link` rows lost the fragment in edge metadata, while the
    cached source page still contains the exact anchor href. For those, resolve
    only when the source HTML contains one unambiguous same-text link to the
    same Rulebook path with a fragment.
    """
    import re
    from urllib.parse import urljoin, urlparse

    from bs4 import BeautifulSoup

    def norm_text(value: str) -> str:
        return " ".join((value or "").split()).casefold()

    def pra_path(value: str) -> str:
        parsed = urlparse(urljoin("https://www.prarulebook.co.uk", value or ""))
        path = parsed.path.strip("/")
        return path[:-11] if re.search(r"/\d{2}-\d{2}-\d{4}$", path) else path

    def is_internal_anchor_path(path: str) -> bool:
        return path.startswith("/pra-rules/") or path.startswith("/guidance/")

    by_html_id: dict[str, tuple[str, str, str]] = {}
    for node_id, title, url, metadata_json in conn.execute("SELECT id,title,url,metadata_json FROM node WHERE node_type IN ('chapter','rule','guidance_section','guidance_paragraph')"):
        meta = json.loads(metadata_json or "{}")
        html_id = meta.get("html_id")
        if html_id and html_id not in by_html_id:
            by_html_id[html_id] = (node_id, title, url)

    source_anchor_cache: dict[str, list[tuple[str, str, str]]] = {}

    def resolve_href_from_source_html(source_url: str, edge_href: str, evidence_text: str, target_title: str = "") -> str | None:
        if not source_url or not edge_href or not evidence_text:
            return None
        if source_url not in source_anchor_cache:
            raw = conn.execute("SELECT raw_html FROM document_source WHERE url=?", (source_url,)).fetchone()
            anchors: list[tuple[str, str, str]] = []
            if raw:
                soup = BeautifulSoup(raw[0] or "", "lxml")
                for a in soup.find_all("a", href=True):
                    href = urljoin("https://www.prarulebook.co.uk", a.get("href", ""))
                    parsed = urlparse(href)
                    if parsed.fragment and is_internal_anchor_path(parsed.path):
                        anchors.append((pra_path(href), norm_text(a.get_text(" ", strip=True)), href))
            source_anchor_cache[source_url] = anchors
        target_path = pra_path(edge_href)
        edge_text = norm_text(evidence_text)
        target_text = norm_text(target_title)
        allowed_texts = {edge_text}
        # Legacy placeholder edges sometimes kept only the Part name as edge
        # evidence while the source anchor included the specific provision, e.g.
        # "Senior Management Functions" vs "Senior Management Functions 6.2".
        # Use the placeholder title to recover that exact source href only when
        # it is a clear extension of the edge text; otherwise numeric/title
        # collisions can create false provision links.
        if edge_text and target_text.startswith(edge_text) and len(edge_text) >= 4:
            allowed_texts.add(target_text)
        matches = [href for path, text, href in source_anchor_cache[source_url] if path == target_path and text in allowed_texts]
        return matches[0] if len(set(matches)) == 1 else None

    out: list[Edge] = []
    stale_edge_ids: list[str] = []
    rows = conn.execute(
        """
        SELECT e.id,e.from_node_id,e.to_node_id,e.evidence_text,e.source_url,e.metadata_json,
               target.node_type AS target_type,target.title AS target_title
        FROM edge e
        LEFT JOIN node target ON target.id=e.to_node_id
        WHERE e.edge_type='references'
          AND e.source_method='html_link'
          AND (e.metadata_json LIKE '%/pra-rules/%' OR e.metadata_json LIKE '%/guidance/%')
        """
    ).fetchall()
    for row in rows:
        meta = json.loads(row[5] or "{}")
        href = meta.get("href", "")
        resolution_basis = "edge_metadata_href"
        match = re.search(r"#([A-Za-z0-9]+)", href)
        if not match:
            source_href = resolve_href_from_source_html(row[4] or "", href, row[3] or "", row[7] or "")
            if not source_href:
                continue
            href = source_href
            resolution_basis = "source_html_href"
            match = re.search(r"#([A-Za-z0-9]+)", href)
        if not match:
            continue
        html_id = match.group(1)
        target = by_html_id.get(html_id)
        if not target:
            continue
        target_id, target_title, target_url = target
        if target_id == row[1]:
            continue
        out.append(Edge(
            edge_id(row[1], target_id, "references", f"html_anchor:{html_id}"),
            row[1], target_id, "references", "html_anchor_resolved", 0.98,
            row[3] or target_title, row[4] or target_url,
            {"href": href, "html_id": html_id, "target_title": target_title, "replaces_edge_id": row[0], "resolution_basis": resolution_basis, "evidence_status": "direct_text", "extraction_run_id": "internal_anchor_resolution"},
        ))
        stale_edge_ids.append(row[0])

    if stale_edge_ids:
        conn.executemany("DELETE FROM edge WHERE id=?", [(edge_id_,) for edge_id_ in stale_edge_ids])
    return out


def _repair_unresolved_anchor_placeholders(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    """Give still-unresolved hash links unique placeholders.

    If an anchor cannot be resolved to a parsed node, keep it as unresolved, but
    do not let it collapse onto a Part-level placeholder with an unrelated title.
    """
    import re

    nodes: list[Node] = []
    edges: list[Edge] = []
    stale_edge_ids: list[str] = []
    rows = conn.execute(
        """
        SELECT e.id,e.from_node_id,e.evidence_text,e.source_url,e.metadata_json,
               target.node_type AS target_type
        FROM edge e
        LEFT JOIN node target ON target.id=e.to_node_id
        WHERE e.edge_type='references'
          AND e.source_method='html_link'
          AND e.metadata_json LIKE '%#%'
          AND e.metadata_json LIKE '%/pra-rules/%'
        """
    ).fetchall()
    resolved = {
        (source_id, href)
        for source_id, href in conn.execute(
            """
            SELECT from_node_id, json_extract(metadata_json, '$.href')
            FROM edge
            WHERE edge_type='references' AND source_method='html_anchor_resolved'
            """
        )
    }
    for row in rows:
        meta = json.loads(row[4] or "{}")
        href = meta.get("href", "")
        if (row[1], href) in resolved:
            stale_edge_ids.append(row[0])
            continue
        parsed = re.match(r"https?://[^/]+/(pra-rules/[^#?]+)#([A-Za-z0-9]+)", href)
        if not parsed:
            continue
        target_key = f"url:{parsed.group(1)}#{parsed.group(2)}"
        target_id = node_id(target_key)
        title = row[2] or target_key.rsplit("#", 1)[-1]
        nodes.append(Node(target_id, "rule_reference", target_key, title, url=href, metadata={"placeholder": True, "unresolved_anchor": True, "href": href, "target_key": target_key, "html_id": parsed.group(2)}))
        edges.append(Edge(edge_id(row[1], target_id, "references", f"html_anchor_unresolved:{parsed.group(2)}"), row[1], target_id, "references", "html_anchor_unresolved", 0.55, title, row[3] or href, {"href": href, "html_id": parsed.group(2), "target_key": target_key, "replaces_edge_id": row[0]}))
        if row[5] in (None, "rule_reference"):
            stale_edge_ids.append(row[0])
    for row in conn.execute(
        """
        SELECT e.to_node_id,e.evidence_text,e.metadata_json
        FROM edge e
        LEFT JOIN node target ON target.id=e.to_node_id
        WHERE e.edge_type='references'
          AND e.source_method='html_anchor_unresolved'
          AND target.id IS NULL
        """
    ):
        meta = json.loads(row[2] or "{}")
        href = meta.get("href", "")
        target_key = meta.get("target_key", "")
        html_id = meta.get("html_id", "")
        if not target_key:
            continue
        nodes.append(Node(row[0], "rule_reference", target_key, row[1] or target_key.rsplit("#", 1)[-1], url=href, metadata={"placeholder": True, "unresolved_anchor": True, "href": href, "target_key": target_key, "html_id": html_id}))
    if stale_edge_ids:
        conn.executemany("DELETE FROM edge WHERE id=?", [(edge_id_,) for edge_id_ in stale_edge_ids])
    conn.execute(
        """
        DELETE FROM edge
        WHERE source_method='html_anchor_unresolved'
          AND EXISTS (
            SELECT 1 FROM edge resolved
            WHERE resolved.source_method='html_anchor_resolved'
              AND resolved.from_node_id=edge.from_node_id
              AND json_extract(resolved.metadata_json, '$.href')=json_extract(edge.metadata_json, '$.href')
          )
        """
    )
    return nodes, edges


def _shared_defined_term_edges(conn: sqlite3.Connection, *, max_term_degree: int, max_edges_per_term: int) -> list[Edge]:
    term_to_sources: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for source_id, source_type, source_title, term_id, term_title in conn.execute(
        """
        SELECT s.id, s.node_type, s.title, t.id, t.title
        FROM edge e
        JOIN node s ON s.id = e.from_node_id
        JOIN node t ON t.id = e.to_node_id
        WHERE e.edge_type = 'uses_defined_term'
          AND s.node_type IN ('rule', 'guidance_paragraph')
          AND t.node_type = 'defined_term'
        """
    ):
        term_to_sources[term_id].append((source_id, source_type, source_title, term_title))

    out: list[Edge] = []
    for term_id, sources in term_to_sources.items():
        # Low-degree terms are often the most interesting. Very high-degree terms
        # such as firm/PRA create hairballs, so skip them for pairwise edges.
        unique = {s[0]: s for s in sources}
        sources = list(unique.values())
        if not (2 <= len(sources) <= max_term_degree):
            continue
        term_title = sources[0][3]
        made = 0
        for a, b in combinations(sorted(sources), 2):
            if made >= max_edges_per_term:
                break
            from_id, from_type, from_title, _ = a
            to_id, to_type, to_title, _ = b
            if from_id == to_id:
                continue
            if from_type == to_type == 'rule':
                edge_type = 'shares_defined_term'
            else:
                edge_type = 'shares_defined_term_with_guidance'
            out.append(Edge(
                edge_id(from_id, to_id, edge_type, term_id), from_id, to_id, edge_type,
                'derived_term_overlap', 0.75, term_title, '',
                {'term_node_id': term_id, 'term_title': term_title, 'from_title': from_title, 'to_title': to_title},
            ))
            made += 1
    return out


def _same_rulebook_part_name_edges(conn: sqlite3.Connection) -> list[Edge]:
    """Link guidance/legal-instrument target placeholders to parsed Parts by title.

    Legal-instrument listings often point to a dated Part URL. The parsed current
    Part has a different stable key, so this creates an explicit title-based
    bridge while preserving provenance as derived.
    """
    parts = [(row[0], row[1].strip().lower(), row[2]) for row in conn.execute("SELECT id, title, url FROM node WHERE node_type='part'")]
    by_title = {title: (pid, url) for pid, title, url in parts if title}
    out: list[Edge] = []
    for ref_id, ref_title, ref_url in conn.execute("SELECT id, title, url FROM node WHERE node_type IN ('rule_reference','external_reference')"):
        key = (ref_title or '').strip().lower()
        if key in by_title:
            part_id, part_url = by_title[key]
            out.append(Edge(
                edge_id(ref_id, part_id, 'resolves_to_part'), ref_id, part_id, 'resolves_to_part',
                'title_match', 0.85, ref_title, ref_url or part_url, {'matched_title': ref_title},
            ))
    return out

import re
from .models import Node
from .store import upsert_nodes

RULE_REF_RE = re.compile(r"(?<![\w/])(?P<num>\d{1,3}\.\d{1,3}[A-Z]?(?:\([a-z0-9ivx]+\))?)(?![\w/])")
ARTICLE_REF_RE = re.compile(r"\bArticles?\s+(?P<refs>\d+[a-z]?(?:\s*\([a-z0-9]+\))?(?:\s*,\s*(?:and\s+)?\d+[a-z]?(?:\s*\([a-z0-9]+\))?)*(?:\s*,?\s*and\s+\d+[a-z]?(?:\s*\([a-z0-9]+\))?)?)", re.I)
MODAL_RE = re.compile(r"\b(?P<subject>[A-Z][A-Za-z0-9 ,()\-/]{0,80}?)\s+(?P<modal>must|shall|should|may|is required to|are required to)\s+(?P<action>[a-z][a-z\-]+)(?P<object>[^.;:]{0,140})", re.I)


def derive_phase4_edges_and_nodes(conn: sqlite3.Connection) -> dict[str, int]:
    """Add richer explainable NLP-ish enrichment: regex references and obligations."""
    nodes: list[Node] = []
    edges: list[Edge] = []
    edges.extend(_regex_rule_reference_edges(conn))
    obligation_nodes, obligation_edges = _obligation_pattern_edges(conn)
    nodes.extend(obligation_nodes)
    edges.extend(obligation_edges)
    upsert_nodes(conn, nodes)
    upsert_edges(conn, edges)
    conn.commit()
    counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        counts[f"node:{n.node_type}"] += 1
    for e in edges:
        counts[e.edge_type] += 1
    return dict(counts)


def _regex_rule_reference_edges(conn: sqlite3.Connection) -> list[Edge]:
    rules = conn.execute("SELECT id,title,text,url,metadata_json FROM node WHERE node_type IN ('rule','guidance_paragraph')").fetchall()
    by_part_num: dict[tuple[str, str], tuple[str, str, str]] = {}
    by_part_article: dict[tuple[str, str], tuple[str, str, str]] = {}
    part_titles: set[str] = set()
    for r in conn.execute("SELECT id,node_type,title,url,metadata_json FROM node WHERE node_type IN ('chapter','rule') ORDER BY CASE node_type WHEN 'chapter' THEN 0 ELSE 1 END"):
        meta = json.loads(r[4] or "{}")
        part = _norm_part(meta.get("part_title") or "")
        article = _norm_article(meta.get("article_number") or r[2] or "")
        if part and article and (part, article) not in by_part_article:
            by_part_article[(part, article)] = (r[0], r[2], r[3])
    for r in conn.execute("SELECT id,title,url,metadata_json FROM node WHERE node_type='rule'"):
        meta = json.loads(r[3] or "{}")
        part = _norm_part(meta.get("part_title") or "")
        num = meta.get("rule_number")
        if part and num:
            by_part_num[(part, num)] = (r[0], r[1], r[2])
            part_titles.add(part)
    named_patterns = _named_part_reference_patterns(part_titles)
    out: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for source_id, source_title, text, source_url, metadata_json in rules:
        text = text or ""
        meta = json.loads(metadata_json or "{}")
        source_part = _norm_part(meta.get("part_title") or "")
        for m in RULE_REF_RE.finditer(text):
            num = m.group("num").split("(", 1)[0]
            target = by_part_num.get((source_part, num))
            if not target or target[0] == source_id:
                continue
            key = (source_id, target[0], f"same:{num}")
            if key in seen:
                continue
            seen.add(key)
            evidence = _window(text, m.start(), m.end())
            out.append(Edge(edge_id(source_id, target[0], "references", f"regex:{num}"), source_id, target[0], "references", "regex_reference", 0.82, evidence, source_url, {"reference": num, "target_title": target[1], "scope": "same_part"}))

        for m in ARTICLE_REF_RE.finditer(text):
            evidence = _window(text, m.start(), m.end())
            for article in _article_refs(m.group("refs")):
                if _is_uk_crr_article_context(evidence):
                    target_id, target_title, target_url = _ensure_uk_crr_article_node(conn, article)
                    key = (source_id, target_id, f"uk_crr_article:{article}")
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(Edge(
                        edge_id(source_id, target_id, "references", f"regex_uk_crr_article:{article}"),
                        source_id, target_id, "references", "regex_uk_crr_article_reference", 0.90, evidence, source_url,
                        {"reference": f"Article {article}", "target_title": target_title, "scope": "uk_crr", "target_url": target_url},
                    ))
                    continue
                target = by_part_article.get((source_part, article))
                if not target or target[0] == source_id:
                    continue
                key = (source_id, target[0], f"article:{article}")
                if key in seen:
                    continue
                seen.add(key)
                out.append(Edge(edge_id(source_id, target[0], "references", f"regex_article:{article}"), source_id, target[0], "references", "regex_article_reference", 0.86, evidence, source_url, {"reference": f"Article {article}", "target_title": target[1], "scope": "same_part_article"}))

        for part_title, pattern in named_patterns:
            for m in pattern.finditer(text):
                num = m.group("num").split("(", 1)[0]
                target = by_part_num.get((part_title, num))
                if not target or target[0] == source_id:
                    continue
                key = (source_id, target[0], f"named:{part_title}:{num}")
                if key in seen:
                    continue
                seen.add(key)
                evidence = _window(text, m.start(), m.end())
                out.append(Edge(
                    edge_id(source_id, target[0], "references", f"regex_named:{part_title}:{num}"),
                    source_id, target[0], "references", "regex_named_reference", 0.88, evidence, source_url,
                    {"reference": num, "part_title": part_title, "target_title": target[1], "scope": "cross_part_named"},
                ))
    return out


def _norm_part(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().replace("–", "-").replace("—", "-")).strip()


def _norm_article(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    match = re.match(r"Article\s+(\d+[a-z]?)\b", text, re.I)
    return match.group(1).lower() if match else ""


def _is_liquidity_crr_part(value: str) -> bool:
    return _norm_part(value or "") in {"liquidity coverage ratio (crr)", "liquidity (crr)"}


def _is_uk_crr_article_context(value: str) -> bool:
    return bool(re.search(r"\b(?:of\s+the\s+|of\s+)?(?:UK\s+)?CRR\b|\b(?:UK\s+)?CRR\s+Article\b", value or "", re.I))


def _ensure_uk_crr_article_node(conn: sqlite3.Connection, article: str) -> tuple[str, str, str]:
    article = (article or "").lower()
    node_id_value = f"external:uk-crr:article:{article}"
    title = f"UK CRR Article {article.upper() if article.isalpha() else article}"
    url = f"https://www.legislation.gov.uk/eur/2013/575/article/{article}"
    metadata = {"source": "UK CRR", "external_reference": True, "article": article}
    conn.execute(
        """
        INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET title=excluded.title,url=excluded.url,metadata_json=excluded.metadata_json
        """,
        (node_id_value, "external_reference", node_id_value, title, "", url, json.dumps(metadata, ensure_ascii=False)),
    )
    return node_id_value, title, url


def _article_refs(value: str) -> list[str]:
    return [m.group(1).lower() for m in re.finditer(r"\b(\d+[a-z]?)(?:\s*\([a-z0-9]+\))?", value or "", re.I)]


def _named_part_reference_patterns(part_titles: set[str]) -> list[tuple[str, re.Pattern]]:
    """Compile conservative cross-Part references like `Notifications 10.3`.

    This deliberately requires the full Part title immediately followed by a rule
    number, so it does not try to infer loose phrases such as "the Notifications
    Part". That keeps false positives manageable while catching the common PRA
    Rulebook drafting pattern.
    """
    patterns: list[tuple[str, re.Pattern]] = []
    for part in sorted(part_titles, key=len, reverse=True):
        if len(part) < 4:
            continue
        # Allow ordinary whitespace/hyphen variation but require a word boundary
        # before and after the named Part.
        escaped = re.escape(part).replace(r"\ ", r"\s+").replace(r"\-", r"[-–—]")
        patterns.append((part, re.compile(rf"(?<![A-Za-z0-9]){escaped}\s+(?P<num>\d{{1,3}}\.\d{{1,3}}[A-Z]?(?:\([a-z0-9ivx]+\))?)(?![\w/])", re.I)))
    return patterns


def _obligation_pattern_edges(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    nodes_by_key: dict[str, Node] = {}
    edges: list[Edge] = []
    rows = conn.execute("SELECT id,node_type,title,text,url FROM node WHERE node_type IN ('rule','guidance_paragraph') AND LENGTH(COALESCE(text,'')) > 40").fetchall()
    for node_id, node_type, title, text, url in rows:
        for m in list(MODAL_RE.finditer(text or ""))[:6]:
            modal = re.sub(r"\s+", " ", m.group("modal").lower())
            action = m.group("action").lower()
            obj = _normalise_object(m.group("object"))
            if len(obj) < 3:
                continue
            key = f"obligation_pattern:{modal}:{action}:{obj}"
            pattern_id = edge_id("obligation", key, "node")[:16]
            if key not in nodes_by_key:
                nodes_by_key[key] = Node(pattern_id, "obligation_pattern", key, f"{modal} {action} {obj}", metadata={"modal": modal, "action": action, "object": obj})
            edges.append(Edge(edge_id(node_id, pattern_id, "has_obligation_pattern"), node_id, pattern_id, "has_obligation_pattern", "regex_obligation", 0.72, _window(text, m.start(), m.end()), url, {"modal": modal, "action": action, "object": obj, "source_node_type": node_type}))
    return list(nodes_by_key.values()), edges


def _normalise_object(value: str) -> str:
    value = re.sub(r"\b(the|a|an|that|to|of|for|in|on|and|or)\b", " ", value.lower())
    value = re.sub(r"[^a-z0-9 /-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return " ".join(value.split()[:8])


def _window(text: str, start: int, end: int, pad: int = 90) -> str:
    return re.sub(r"\s+", " ", (text or "")[max(0, start-pad):min(len(text or ""), end+pad)]).strip()

ROLLUP_EDGE_TYPES = {
    'references', 'uses_defined_term', 'defines', 'has_obligation_pattern',
    'similar_to', 'shares_defined_term', 'shares_defined_term_with_guidance', 'resolves_to_part'
}
CONTAINER_NODE_TYPES = {'part', 'chapter', 'guidance_document', 'guidance_section'}


def derive_rollup_and_shared_analysis_edges(conn: sqlite3.Connection, *, max_descendants: int = 900, max_edges_per_container: int = 450, max_obligation_degree: int = 18) -> dict[str, int]:
    """Roll leaf analysis edges up to useful containers and connect shared obligations."""
    edges: list[Edge] = []
    edges.extend(_rollup_child_analysis_edges(conn, max_descendants=max_descendants, max_edges_per_container=max_edges_per_container))
    edges.extend(_shared_obligation_pattern_edges(conn, max_obligation_degree=max_obligation_degree))
    upsert_edges(conn, edges)
    conn.commit()
    counts: dict[str, int] = defaultdict(int)
    for e in edges:
        counts[e.edge_type] += 1
        counts[f"method:{e.source_method}"] += 1
    return dict(counts)


def _rollup_child_analysis_edges(conn: sqlite3.Connection, *, max_descendants: int, max_edges_per_container: int) -> list[Edge]:
    """Roll immediate child analysis edges to their parent containers.

    This gives headings/chapters/parts useful non-hierarchy links without creating
    an enormous transitive closure. Running this repeatedly is idempotent because
    edge ids are deterministic.
    """
    rows = conn.execute(
        f"""
        SELECT parent.id AS parent_id, parent.node_type AS parent_type, parent.title AS parent_title, parent.url AS parent_url,
               child.id AS child_id, child.title AS child_title,
               target.id AS target_id, target.title AS target_title,
               e.id AS edge_id, e.edge_type, e.source_method, e.confidence, e.evidence_text, e.source_url
        FROM edge c
        JOIN node parent ON parent.id=c.from_node_id
        JOIN node child ON child.id=c.to_node_id
        JOIN edge e ON e.from_node_id=child.id
        JOIN node target ON target.id=e.to_node_id
        WHERE c.edge_type='contains'
          AND parent.node_type IN ({','.join('?' for _ in CONTAINER_NODE_TYPES)})
          AND e.edge_type IN ({','.join('?' for _ in ROLLUP_EDGE_TYPES)})
          AND e.source_method != 'rollup_child_edge'
          AND target.id != parent.id
        ORDER BY parent.id, e.confidence DESC
        """,
        [*CONTAINER_NODE_TYPES, *ROLLUP_EDGE_TYPES],
    ).fetchall()
    out: list[Edge] = []
    per_parent: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str, str, str]] = set()
    for r in rows:
        if per_parent[r['parent_id']] >= max_edges_per_container:
            continue
        key = (r['parent_id'], r['target_id'], r['edge_type'], r['child_id'])
        if key in seen:
            continue
        seen.add(key)
        per_parent[r['parent_id']] += 1
        confidence = min(0.78, max(0.35, float(r['confidence'] or 0.5) * 0.72))
        out.append(Edge(
            edge_id(r['parent_id'], r['target_id'], r['edge_type'], 'rollup', r['child_id']),
            r['parent_id'], r['target_id'], r['edge_type'], 'rollup_child_edge', confidence,
            r['evidence_text'] or f"Via child node: {r['child_title']}",
            r['parent_url'] or r['source_url'] or '',
            {
                'rolled_up_from_node_id': r['child_id'],
                'rolled_up_from_title': r['child_title'],
                'target_title': r['target_title'],
                'original_edge_id': r['edge_id'],
                'original_source_method': r['source_method'],
                'container_type': r['parent_type'],
                'container_title': r['parent_title'],
            },
        ))
    return out


def _shared_obligation_pattern_edges(conn: sqlite3.Connection, *, max_obligation_degree: int) -> list[Edge]:
    pattern_to_sources: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for source_id, source_type, source_title, pattern_id, pattern_title in conn.execute(
        """
        SELECT s.id, s.node_type, s.title, p.id, p.title
        FROM edge e
        JOIN node s ON s.id=e.from_node_id
        JOIN node p ON p.id=e.to_node_id
        WHERE e.edge_type='has_obligation_pattern'
          AND e.source_method='regex_obligation'
          AND s.node_type IN ('rule','guidance_paragraph')
          AND p.node_type='obligation_pattern'
        """
    ):
        pattern_to_sources[pattern_id].append((source_id, source_type, source_title, pattern_title))
    out: list[Edge] = []
    for pattern_id, sources in pattern_to_sources.items():
        unique = {s[0]: s for s in sources}
        sources = list(unique.values())
        if not (2 <= len(sources) <= max_obligation_degree):
            continue
        pattern_title = sources[0][3]
        for a, b in combinations(sorted(sources), 2):
            from_id, from_type, from_title, _ = a
            to_id, to_type, to_title, _ = b
            if from_id == to_id:
                continue
            out.append(Edge(
                edge_id(from_id, to_id, 'shares_obligation_pattern', pattern_id),
                from_id, to_id, 'shares_obligation_pattern', 'derived_obligation_overlap', 0.68,
                pattern_title, '',
                {'pattern_node_id': pattern_id, 'pattern_title': pattern_title, 'from_title': from_title, 'to_title': to_title, 'from_type': from_type, 'to_type': to_type},
            ))
    return out
