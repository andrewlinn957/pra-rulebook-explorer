from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from itertools import combinations

from .models import Edge
from .parse import edge_id
from .store import upsert_edges


def derive_richer_edges(conn: sqlite3.Connection, *, max_term_degree: int = 25, max_edges_per_term: int = 300) -> dict[str, int]:
    """Derive deterministic discovery edges from the current corpus.

    These are intentionally conservative and explainable. They do not replace
    explicit references, but add useful rule-to-rule/guidance-to-rule bridges.
    """
    edges: list[Edge] = []
    edges.extend(_shared_defined_term_edges(conn, max_term_degree=max_term_degree, max_edges_per_term=max_edges_per_term))
    edges.extend(_same_rulebook_part_name_edges(conn))
    upsert_edges(conn, edges)
    conn.commit()
    counts: dict[str, int] = defaultdict(int)
    for e in edges:
        counts[e.edge_type] += 1
    return dict(counts)


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
MODAL_RE = re.compile(r"\b(?P<subject>[A-Z][A-Za-z0-9 ,()\-/]{0,80}?)\s+(?P<modal>must|shall|should|may|is required to|are required to)\s+(?P<action>[a-z][a-z\-]+)(?P<object>[^.;:]{0,140})", re.I)

TOPICS = {
    "capital": ["own funds", "capital", "capital requirement", "scr", "mrel", "leverage ratio", "buffer"],
    "liquidity": ["liquidity", "liquidity coverage ratio", "liquidity coverage requirement", "lcr", "nsfr", "funding", "liquid assets", "liquidity buffer", "cash flow"],
    "governance": ["governance", "senior management", "management body", "committee", "accountability"],
    "operational resilience": ["operational resilience", "important business service", "impact tolerance", "disruption"],
    "outsourcing": ["outsourcing", "third party", "service provider", "material outsourcing"],
    "remuneration": ["remuneration", "bonus", "variable remuneration", "malus", "clawback"],
    "credit risk": ["credit risk", "exposure", "irb", "standardised approach", "default"],
    "market risk": ["market risk", "trading book", "position risk", "foreign exchange"],
    "insurance solvency": ["solvency ii", "technical provisions", "matching adjustment", "risk margin", "insurance"],
    "reporting disclosure": ["report", "reporting", "disclosure", "submit", "notification", "notify"],
    "resolution": ["resolution", "recovery plan", "resolvability", "bail-in", "stabilisation"],
    "depositor protection": ["depositor protection", "fscs", "eligible deposit", "single customer view"],
}


def derive_phase4_edges_and_nodes(conn: sqlite3.Connection) -> dict[str, int]:
    """Add richer explainable NLP-ish enrichment: regex references, topics, obligations."""
    nodes: list[Node] = []
    edges: list[Edge] = []
    edges.extend(_regex_rule_reference_edges(conn))
    topic_nodes, topic_edges = _topic_edges(conn)
    obligation_nodes, obligation_edges = _obligation_pattern_edges(conn)
    nodes.extend(topic_nodes); nodes.extend(obligation_nodes)
    edges.extend(topic_edges); edges.extend(obligation_edges)
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
    part_titles: set[str] = set()
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


def _topic_edges(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    topic_nodes = [Node(id=edge_id("topic", name, "node")[:16], node_type="topic", stable_key=f"topic:{name}", title=name.title(), text=", ".join(keywords), metadata={"keywords": keywords}) for name, keywords in TOPICS.items()]
    topic_id = {n.title.lower(): n.id for n in topic_nodes}
    rows = conn.execute("SELECT id,node_type,title,text,url FROM node WHERE node_type IN ('rule','chapter','guidance_paragraph','guidance_document','part')").fetchall()
    edges: list[Edge] = []
    for node_id, node_type, title, text, url in rows:
        hay = f"{title} {text}".lower()
        for topic, keywords in TOPICS.items():
            hits = [kw for kw in keywords if kw in hay]
            if not hits:
                continue
            confidence = min(0.95, 0.45 + 0.12 * len(hits))
            title_lower = (title or '').lower()
            title_hits = [kw for kw in hits if kw in title_lower]
            if title_hits:
                confidence = max(confidence, min(0.95, 0.72 + 0.08 * len(title_hits)))
            edges.append(Edge(edge_id(node_id, topic_id[topic], "has_topic"), node_id, topic_id[topic], "has_topic", "keyword_topic", confidence, "; ".join(hits[:6]), url, {"topic": topic, "matched_keywords": hits, "node_type": node_type}))
    return topic_nodes, edges


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
    'references', 'uses_defined_term', 'defines', 'has_topic', 'has_obligation_pattern',
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
