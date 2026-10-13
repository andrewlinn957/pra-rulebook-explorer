from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict, deque
from typing import Any

import networkx as nx

from .db import get_node, row_to_edge, row_to_node
from .taxonomy import RETIRED_EDGE_TYPES, RETIRED_NODE_TYPES

EXPLICIT_METHODS = {"site_structure", "html_link", "html_anchor_resolved", "html_glossary_link", "glossary_source", "crr_terms_source", "legal_instrument_listing", "legal_reference_occurrence_v1", "reference_recall_stage_v1", "regex_reference", "regex_named_reference", "llm_extracted_reference", "resolved_part_reference", "fca_waivers_list"}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "nodes": conn.execute("SELECT COUNT(*) FROM node").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0],
        "nodes_by_type": dict(conn.execute("SELECT node_type, COUNT(*) FROM node GROUP BY node_type ORDER BY node_type").fetchall()),
        "edges_by_type": dict(conn.execute("SELECT edge_type, COUNT(*) FROM edge GROUP BY edge_type ORDER BY edge_type").fetchall()),
        "edge_methods": dict(conn.execute("SELECT source_method, COUNT(*) FROM edge GROUP BY source_method ORDER BY source_method").fetchall()),
        "missing_edge_targets": conn.execute("SELECT COUNT(*) FROM edge e LEFT JOIN node n ON e.to_node_id=n.id WHERE n.id IS NULL").fetchone()[0],
    }


def _fts_safe_query(q: str) -> str:
    """Escape user input for FTS5 MATCH.

    Wraps the query in double quotes so FTS5 treats it as a phrase rather than
    interpreting NEAR, column filters, * and other syntax. Internal double
    quotes are doubled per the SQLite string-literal convention.
    """
    escaped = q.replace('"', '""')
    return f'"{escaped}"'


def search(conn: sqlite3.Connection, q: str, *, node_types: list[str] | None = None, limit: int = 25) -> list[dict[str, Any]]:
    params: list[Any] = []
    retired_node_clause = " AND n.node_type NOT IN (%s)" % ",".join("?" for _ in RETIRED_NODE_TYPES)
    retired_node_params = sorted(RETIRED_NODE_TYPES)
    type_clause = ""
    if node_types:
        type_clause = " AND n.node_type IN (%s)" % ",".join("?" for _ in node_types)
        params.extend(node_types)
    try:
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json,
                   CASE WHEN n.title LIKE ? THEN -1000000.0 ELSE bm25(node_fts) END AS score
            FROM node_fts f JOIN node n ON n.id=f.id
            JOIN canonical_node cn ON cn.id=n.id AND cn.is_canonical=1
            WHERE node_fts MATCH ? {retired_node_clause} {type_clause}
            ORDER BY score LIMIT ?
            """,
            [f"%{q}%", _fts_safe_query(q), *retired_node_params, *params, limit],
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json,
                   CASE WHEN n.title LIKE ? THEN -1000000.0 ELSE 0 END AS score
            FROM node n
            JOIN canonical_node cn ON cn.id=n.id AND cn.is_canonical=1
            WHERE (n.title LIKE ? OR n.text LIKE ?) {retired_node_clause} {type_clause}
            ORDER BY score, n.title
            LIMIT ?
            """,
            [f"%{q}%", f"%{q}%", f"%{q}%", *retired_node_params, *params, limit],
        ).fetchall()
    out = []
    for row in rows:
        node = row_to_node(row)
        node["score"] = row["score"]
        node["snippet"] = _snippet(node.get("text") or node.get("title") or "", q)
        out.append(node)
    return out


def neighbourhood(conn: sqlite3.Connection, node_id: str, *, depth: int = 1, limit: int = 250, edge_types: list[str] | None = None, explicit_only: bool = False) -> dict[str, Any]:
    method_clause = " AND source_method IN (%s)" % ",".join("?" for _ in EXPLICIT_METHODS) if explicit_only else ""
    method_params = list(EXPLICIT_METHODS) if explicit_only else []
    retired_clause = " AND edge_type NOT IN (%s)" % ",".join("?" for _ in RETIRED_EDGE_TYPES)
    retired_params = sorted(RETIRED_EDGE_TYPES)
    retired_node_clause = (
        " AND NOT EXISTS(SELECT 1 FROM node n WHERE n.id=edge.from_node_id AND n.node_type IN (%s))"
        " AND NOT EXISTS(SELECT 1 FROM node n WHERE n.id=edge.to_node_id AND n.node_type IN (%s))"
    ) % (",".join("?" for _ in RETIRED_NODE_TYPES), ",".join("?" for _ in RETIRED_NODE_TYPES))
    retired_node_params = sorted(RETIRED_NODE_TYPES)
    edge_type_clause = ""
    params_extra: list[Any] = []
    if edge_types:
        edge_type_clause = " AND edge_type IN (%s)" % ",".join("?" for _ in edge_types)
        params_extra.extend(edge_types)

    available = dict(conn.execute(
        f"""
        SELECT edge_type, COUNT(*) FROM edge
        WHERE (from_node_id=? OR to_node_id=?) {retired_clause} {method_clause} {retired_node_clause}
        GROUP BY edge_type ORDER BY edge_type
        """,
        [node_id, node_id, *retired_params, *method_params,
         *retired_node_params, *retired_node_params],
    ).fetchall())

    seen_nodes = {node_id}
    seen_edges: dict[str, dict[str, Any]] = {}
    queue = deque([(node_id, 0)])
    while queue and len(seen_nodes) < limit:
        current, d = queue.popleft()
        if d >= depth:
            continue
        rows = conn.execute(
            f"""
            SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json
            FROM edge
            WHERE (from_node_id=? OR to_node_id=?) {edge_type_clause} {retired_clause} {method_clause} {retired_node_clause}
            ORDER BY confidence DESC
            LIMIT ?
            """,
            [current, current, *params_extra, *retired_params, *method_params,
             *retired_node_params, *retired_node_params, limit],
        ).fetchall()
        for row in rows:
            edge = row_to_edge(row)
            seen_edges[edge["id"]] = edge
            other = edge["to_node_id"] if edge["from_node_id"] == current else edge["from_node_id"]
            if other not in seen_nodes and len(seen_nodes) < limit:
                seen_nodes.add(other)
                queue.append((other, d + 1))
    nodes = [row_to_node(r) for r in conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id IN (%s)" % ",".join("?" for _ in seen_nodes),
        list(seen_nodes),
    ).fetchall()]
    edges = list(seen_edges.values())
    _attach_reference_occurrences(conn, edges)
    nodes, edges = _roll_up_guidance_reference_nodes(conn, nodes, edges)
    return {"nodes": nodes, "edges": edges, "available_edge_types": available}


def _attach_reference_occurrences(
    conn: sqlite3.Connection,
    edges: list[dict[str, Any]],
) -> None:
    """Attach every lexical citation occurrence without changing graph topology."""

    if not edges or not conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='reference_occurrence'
        """
    ).fetchone():
        return
    edge_ids = [str(edge["id"]) for edge in edges if edge.get("id")]
    if not edge_ids:
        return
    placeholders = ",".join("?" for _ in edge_ids)
    occurrences_by_edge: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        f"""
        SELECT o.occurrence_id,o.group_id,o.source_node_id,o.target_node_id,o.edge_id,
               o.relationship_type,o.citation_kind,o.citation_text,o.group_text,
               o.instrument_id,o.provision_path,o.qualifier,o.span_start,o.span_end,
               o.status,o.source_method,o.confidence,o.context_text,o.metadata_json,
               n.text AS source_text,n.title AS source_title
        FROM reference_occurrence o JOIN edge e ON e.id=o.edge_id
        LEFT JOIN node n ON n.id=o.source_node_id
        WHERE o.edge_id IN ({placeholders}) AND o.status='materialized'
          AND e.from_node_id=o.source_node_id AND e.to_node_id=o.target_node_id
          AND e.edge_type=CASE UPPER(o.relationship_type)
            WHEN 'REF' THEN 'references'
            WHEN 'DEF' THEN 'uses_defined_term'
            WHEN 'AMEND' THEN 'amends'
            WHEN 'AMENDS' THEN 'amends'
            ELSE '' END
        ORDER BY o.source_node_id,o.span_start,o.span_end,o.occurrence_id
        """,
        edge_ids,
    ):
        occurrence = dict(row)
        occurrence["metadata"] = _json(occurrence.pop("metadata_json", "{}"))
        source = occurrence.pop(
            "source_title" if occurrence["metadata"].get("span_basis") == "title" else "source_text",
        ) or ""
        start, end = occurrence.get("span_start"), occurrence.get("span_end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(source):
            occurrence["span_text"] = source[start:end]
        occurrence.pop("source_text", None)
        occurrence.pop("source_title", None)
        occurrence["source_span"] = {
            "start": occurrence["span_start"],
            "end": occurrence["span_end"],
        }
        occurrences_by_edge[str(occurrence["edge_id"])].append(occurrence)
    for edge in edges:
        occurrences = occurrences_by_edge.get(str(edge.get("id") or ""), [])
        if occurrences:
            edge.setdefault("metadata", {})["reference_occurrences"] = occurrences


def _attach_unresolved_reader_occurrences(
    conn: sqlite3.Connection,
    source_ids: list[str],
    nodes_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    if not source_ids:
        return
    for start in range(0, len(source_ids), 750):
        batch = source_ids[start:start + 750]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT occurrence_id,group_id,source_node_id,target_node_id,edge_id,
                   relationship_type,citation_kind,citation_text,group_text,
                   instrument_id,provision_path,qualifier,span_start,span_end,
                   status,source_method,confidence,context_text,metadata_json
            FROM reference_occurrence
            WHERE source_node_id IN ({placeholders})
              AND status IN ('unresolved','ambiguous')
            ORDER BY source_node_id,span_start,occurrence_id
            """,
            batch,
        ):
            occurrence = dict(row)
            occurrence["metadata"] = _json(occurrence.pop("metadata_json", "{}"))
            occurrence["source_span"] = {"start": occurrence["span_start"], "end": occurrence["span_end"]}
            target_id = str(occurrence.get("target_node_id") or f"unresolved:{occurrence['occurrence_id']}")
            occurrence["target_node_id"] = target_id
            if target_id not in nodes_by_id and target_id != f"unresolved:{occurrence['occurrence_id']}":
                target_row = conn.execute(
                    "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?",
                    (target_id,),
                ).fetchone()
                if target_row:
                    nodes_by_id[target_id] = row_to_node(target_row)
            nodes_by_id.setdefault(target_id, {
                "id": target_id,
                "node_type": "external_reference" if row["status"] == "unresolved" else "rule_reference",
                "title": occurrence.get("citation_text") or "Unresolved citation",
                "text": "",
                "url": "",
                "metadata": {"resolution_status": row["status"], "citation_kind": row["citation_kind"]},
            })
            edges.append({
                "id": f"reader-occurrence:{occurrence['occurrence_id']}",
                "from_node_id": occurrence["source_node_id"],
                "to_node_id": target_id,
                "edge_type": {
                    "REF": "references", "DEF": "uses_defined_term",
                    "AMEND": "amends", "AMENDS": "amends",
                }.get(str(occurrence.get("relationship_type") or "").upper(), "references"),
                "source_method": occurrence["source_method"],
                "confidence": occurrence["confidence"],
                "evidence_text": occurrence["citation_text"],
                "source_url": "",
                "metadata": {"reference_occurrences": [occurrence], "resolution_status": row["status"]},
            })


def _attach_detached_materialized_reader_occurrences(
    conn: sqlite3.Connection,
    source_ids: list[str],
    nodes_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Keep citations readable when their stored edge association has drifted.

    Occurrence-specific edges exist only in this reader response. They preserve
    the recorded source, target and wording without guessing which of several
    graph assertions should own the occurrence.
    """
    if not source_ids or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_occurrence'"
    ).fetchone():
        return
    attached = {
        occurrence.get("occurrence_id")
        for edge in edges
        for occurrence in (edge.get("metadata") or {}).get("reference_occurrences", [])
    }
    for start in range(0, len(source_ids), 750):
        batch = source_ids[start:start + 750]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT o.occurrence_id,o.group_id,o.source_node_id,o.target_node_id,o.edge_id,
                   o.relationship_type,o.citation_kind,o.citation_text,o.group_text,
                   o.instrument_id,o.provision_path,o.qualifier,o.span_start,o.span_end,
                   o.status,o.source_method,o.confidence,o.context_text,o.metadata_json
            FROM reference_occurrence o
            LEFT JOIN edge e ON e.id=o.edge_id
            WHERE o.source_node_id IN ({placeholders})
              AND o.status='materialized'
              AND (e.id IS NULL OR e.from_node_id<>o.source_node_id
                   OR e.to_node_id<>o.target_node_id
                   OR e.edge_type<>CASE UPPER(o.relationship_type)
                     WHEN 'REF' THEN 'references'
                     WHEN 'DEF' THEN 'uses_defined_term'
                     WHEN 'AMEND' THEN 'amends'
                     WHEN 'AMENDS' THEN 'amends'
                     ELSE '' END)
              AND o.relationship_type IN ('REF','DEF','AMEND','AMENDS')
            ORDER BY o.source_node_id,o.span_start,o.occurrence_id
            """,
            batch,
        )
        for row in rows:
            occurrence_id = str(row["occurrence_id"])
            if occurrence_id in attached:
                continue
            occurrence = dict(row)
            occurrence["metadata"] = _json(occurrence.pop("metadata_json", "{}"))
            occurrence["source_span"] = {"start": occurrence["span_start"], "end": occurrence["span_end"]}
            target_id = str(occurrence.get("target_node_id") or f"unresolved:{occurrence_id}")
            occurrence["target_node_id"] = target_id
            if target_id not in nodes_by_id:
                target_row = conn.execute(
                    "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?",
                    (target_id,),
                ).fetchone()
                if target_row:
                    nodes_by_id[target_id] = row_to_node(target_row)
            nodes_by_id.setdefault(target_id, {
                "id": target_id,
                "node_type": "external_reference",
                "title": occurrence.get("citation_text") or "Referenced material",
                "text": "",
                "url": "",
                "metadata": {"resolution_status": "target_unavailable", "citation_kind": row["citation_kind"]},
            })
            edges.append({
                "id": f"reader-occurrence:{occurrence_id}",
                "from_node_id": occurrence["source_node_id"],
                "to_node_id": target_id,
                "edge_type": {
                    "REF": "references", "DEF": "uses_defined_term",
                    "AMEND": "amends", "AMENDS": "amends",
                }.get(str(occurrence.get("relationship_type") or "").upper(), "references"),
                "source_method": occurrence["source_method"],
                "confidence": occurrence["confidence"],
                "evidence_text": occurrence["citation_text"],
                "source_url": "",
                "metadata": {
                    "reference_occurrences": [occurrence],
                    "association_status": "detached_occurrence",
                },
            })
            attached.add(occurrence_id)


def _attach_html_link_occurrences(
    conn: sqlite3.Connection,
    edges: list[dict[str, Any]],
    source_nodes: list[dict[str, Any]],
) -> None:
    """Project each source-page anchor into a reader citation occurrence.

    The graph deliberately keeps one relationship per target for most derived
    links.  The reader needs the lexical anchor occurrences as well, especially
    when several anchors share one href.  Reading these occurrences from the
    cached source HTML keeps the graph identity compact while preserving every
    clickable citation in the reader.
    """

    if not edges or not source_nodes or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_source'"
    ).fetchone():
        return

    import re
    from urllib.parse import urljoin, urlparse

    from bs4 import BeautifulSoup, Tag

    def clean(value: str) -> str:
        return " ".join((value or "").split()).strip()

    def href_key(value: str) -> str:
        parsed = urlparse(urljoin("https://www.prarulebook.co.uk", value or ""))
        return (
            f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
            f"{parsed.path}?{parsed.query}#{parsed.fragment}"
        ).rstrip("?#")

    def sibling_tail(anchor: Tag) -> str:
        for sibling in anchor.next_siblings:
            if isinstance(sibling, Tag) and sibling.name == "a":
                break
            text = sibling if isinstance(sibling, str) else sibling.get_text(" ", strip=True)
            text = clean(str(text))
            if text:
                return text
        return ""

    def citation_text(anchor: Tag) -> str:
        title = clean(anchor.get("title") or "")
        text = clean(anchor.get_text(" ", strip=True))
        value = title or text
        numeric = re.fullmatch(r"\d+(?:\.\d+)*[A-Za-z]?", value)
        if numeric:
            suffix = re.match(r"^(\(\d{1,3}\)(?:\([A-Za-z]\))?)", sibling_tail(anchor))
            if suffix:
                value = f"{value} {suffix.group(1)}"
        value = re.sub(r"^(\d+(?:\.\d+)*[A-Za-z]?)(?=\()", r"\1 ", value)
        return clean(value)

    def source_html(source_node: dict[str, Any], source_edges: list[dict[str, Any]]) -> str:
        metadata = source_node.get("metadata") or {}
        source_page_id = metadata.get("source_page_id")
        row = None
        if source_page_id:
            row = conn.execute(
                "SELECT raw_html FROM document_source WHERE id=?",
                (source_page_id,),
            ).fetchone()
        urls = [str(source_node.get("url") or "").split("#", 1)[0]]
        urls.extend(str(edge.get("source_url") or "").split("#", 1)[0] for edge in source_edges)
        if row is None:
            for url in dict.fromkeys(url for url in urls if url):
                row = conn.execute(
                    "SELECT raw_html FROM document_source WHERE url=?",
                    (url,),
                ).fetchone()
                if row is not None:
                    break
        return str(row[0] or "") if row is not None else ""

    edges_by_source: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        if edge.get("edge_type") != "references":
            continue
        if edge.get("source_method") not in {"html_link", "html_anchor_resolved"}:
            continue
        edges_by_source[str(edge.get("from_node_id") or "")].append(edge)

    parsed_sources: dict[str, Any] = {}
    for source_node in source_nodes:
        source_id = str(source_node.get("id") or "")
        source_edges = edges_by_source.get(source_id, [])
        if not source_edges:
            continue
        source_metadata = source_node.get("metadata") or {}
        source_key = str(
            source_metadata.get("source_page_id")
            or str(source_node.get("url") or "").split("#", 1)[0]
            or source_edges[0].get("source_url")
            or source_id
        )
        if source_key not in parsed_sources:
            raw_html = source_html(source_node, source_edges)
            parsed_sources[source_key] = BeautifulSoup(raw_html, "lxml") if raw_html else None
        soup = parsed_sources[source_key]
        if soup is None:
            continue
        html_id = source_metadata.get("html_id") or urlparse(source_node.get("url") or "").fragment
        container = soup.find(id=html_id) if html_id else None
        if container is None:
            continue
        body = container.select_one(".div-row__col-2") or container
        edges_by_href: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in source_edges:
            edge_href = (edge.get("metadata") or {}).get("href")
            if edge_href:
                edges_by_href[href_key(str(edge_href))].append(edge)
        occurrence_index: defaultdict[str, int] = defaultdict(int)
        search_offset: defaultdict[str, int] = defaultdict(int)
        source_text = str(source_node.get("text") or "")
        for anchor in body.find_all("a", href=True):
            matches = edges_by_href.get(href_key(anchor.get("href", "")), [])
            if not matches:
                continue
            edge = sorted(
                matches,
                key=lambda item: (
                    item.get("source_method") != "html_anchor_resolved",
                    -float(item.get("confidence") or 0),
                    str(item.get("id") or ""),
                ),
            )[0]
            citation = citation_text(anchor)
            if not citation:
                continue
            start = source_text.find(citation, search_offset[citation])
            if start < 0:
                anchor_text = clean(anchor.get_text(" ", strip=True))
                start = source_text.find(anchor_text, search_offset[citation])
            if start < 0:
                start = 0
            end = start + len(citation)
            search_offset[citation] = max(search_offset[citation], end)
            edge_key = str(edge.get("id") or "")
            index = occurrence_index[edge_key]
            occurrence_index[edge_key] += 1
            occurrence_id = f"html-link:{source_id}:{edge_key}:{index}"
            existing = edge.setdefault("metadata", {}).setdefault("reference_occurrences", [])
            if any(item.get("occurrence_id") == occurrence_id for item in existing):
                continue
            existing.append({
                "occurrence_id": occurrence_id,
                "group_id": occurrence_id,
                "source_node_id": source_id,
                "target_node_id": edge.get("to_node_id"),
                "edge_id": edge.get("id"),
                "relationship_type": "REF",
                "citation_kind": "html_link",
                "citation_text": citation,
                "group_text": citation,
                "span_start": start,
                "span_end": end,
                "status": "materialized",
                "source_method": "html_link_occurrence",
                "confidence": float(edge.get("confidence") or 1.0),
                "context_text": clean(anchor.parent.get_text(" ", strip=True)) if anchor.parent else citation,
                "metadata": {
                    "href": urljoin("https://www.prarulebook.co.uk", anchor.get("href", "")),
                    "anchor_text": clean(anchor.get_text(" ", strip=True)),
                    "anchor_title": clean(anchor.get("title") or ""),
                    "source_html_id": html_id,
                    "group_span": {"start": start, "end": end},
                },
            })


def _roll_up_guidance_reference_nodes(conn: sqlite3.Connection, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reference_node_ids = {
        endpoint
        for edge in edges
        if edge.get("edge_type") == "references"
        for endpoint in (edge.get("from_node_id"), edge.get("to_node_id"))
        if endpoint
    }
    rollups = {node_id: doc for node_id in reference_node_ids if (doc := _guidance_document_ancestor(conn, node_id)) and doc.get("id") != node_id}
    if not rollups:
        return nodes, edges

    nodes_by_id = {node["id"]: node for node in nodes}
    for doc in rollups.values():
        nodes_by_id[doc["id"]] = doc

    deduped_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    replaced_node_ids: set[str] = set()
    for edge in edges:
        edge = {**edge, "metadata": dict(edge.get("metadata") or {})}
        rolled_from: list[str] = []
        directional_rollups: dict[str, list[str]] = {}
        if edge.get("edge_type") == "references":
            for field in ("from_node_id", "to_node_id"):
                original = edge.get(field)
                doc = rollups.get(original)
                if doc:
                    edge[field] = doc["id"]
                    rolled_from.append(original)
                    directional_rollups.setdefault(
                        f"rolled_up_from_{field.split('_', 1)[0]}_node_ids",
                        [],
                    ).append(original)
                    replaced_node_ids.add(original)
        if edge.get("from_node_id") == edge.get("to_node_id"):
            continue
        key = (edge.get("from_node_id") or "", edge.get("to_node_id") or "", edge.get("edge_type") or "")
        if key in deduped_edges:
            existing = deduped_edges[key]
            existing["confidence"] = max(float(existing.get("confidence") or 0), float(edge.get("confidence") or 0))
            existing_meta = existing.setdefault("metadata", {})
            existing_rollups = existing_meta.setdefault("rolled_up_from_node_ids", [])
            for node_id in rolled_from or edge.get("metadata", {}).get("rolled_up_from_node_ids", []):
                if node_id not in existing_rollups:
                    existing_rollups.append(node_id)
            for key, node_ids in directional_rollups.items():
                existing_directional = existing_meta.setdefault(key, [])
                for node_id in node_ids:
                    if node_id not in existing_directional:
                        existing_directional.append(node_id)
            existing_occurrences = existing_meta.setdefault(
                "reference_occurrences",
                [],
            )
            known_occurrence_ids = {
                item.get("occurrence_id")
                for item in existing_occurrences
            }
            for occurrence in edge.get("metadata", {}).get(
                "reference_occurrences",
                [],
            ):
                if occurrence.get("occurrence_id") not in known_occurrence_ids:
                    existing_occurrences.append(occurrence)
                    known_occurrence_ids.add(occurrence.get("occurrence_id"))
            continue
        if rolled_from:
            edge["metadata"]["rolled_up_from_node_ids"] = rolled_from
            edge["metadata"].update(directional_rollups)
        deduped_edges[key] = edge

    kept_node_ids = {edge["from_node_id"] for edge in deduped_edges.values()} | {edge["to_node_id"] for edge in deduped_edges.values()}
    kept_node_ids |= {node["id"] for node in nodes if node["id"] not in replaced_node_ids}
    return [node for node_id, node in nodes_by_id.items() if node_id in kept_node_ids], list(deduped_edges.values())


def _guidance_document_ancestor(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?", (node_id,)).fetchone()
    node = row_to_node(row)
    if not node or node.get("node_type") not in {"guidance_document", "guidance_section", "guidance_paragraph"}:
        return None
    if node.get("node_type") == "guidance_document":
        return node if _is_statement_guidance(node) else None
    current_id = node_id
    for _ in range(8):
        parent_row = conn.execute(
            """
            SELECT p.id,p.node_type,p.stable_key,p.title,p.text,p.url,p.metadata_json
            FROM edge e JOIN node p ON p.id=e.from_node_id
            WHERE e.to_node_id=? AND e.edge_type='contains'
            ORDER BY CASE p.node_type WHEN 'guidance_document' THEN 0 WHEN 'guidance_section' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (current_id,),
        ).fetchone()
        parent = row_to_node(parent_row)
        if not parent:
            return None
        if parent.get("node_type") == "guidance_document" and _is_statement_guidance(parent):
            return parent
        current_id = parent["id"]
    return None


def _is_statement_guidance(node: dict[str, Any]) -> bool:
    if node.get("node_type") not in {"guidance_document", "guidance_section", "guidance_paragraph"}:
        return False
    meta = node.get("metadata") or {}
    doc_type = (meta.get("document_type") or "").lower()
    url = (node.get("url") or "").lower()
    return doc_type in {"supervisory_statement", "statement_of_policy"} or "/supervisory-statements/" in url or "/statements-of-policy/" in url


def shortest_path(conn: sqlite3.Connection, source: str, target: str, *, max_edges: int = 200000) -> dict[str, Any]:
    """Bidirectional BFS over indexed adjacency lookups.

    Uses idx_edge_from / idx_edge_to to expand frontiers without loading the
    whole edge table. Returns as soon as forward and backward frontiers meet.
    """
    if source == target:
        node = get_node(conn, source)
        if not node or node.get("node_type") in RETIRED_NODE_TYPES:
            raise ValueError(f"node {source!r} not found")
        return {"node_ids": [source], "nodes": [node], "edges": [], "length": 0}

    source_row = conn.execute("SELECT node_type FROM node WHERE id=?", (source,)).fetchone()
    if not source_row or source_row["node_type"] in RETIRED_NODE_TYPES:
        raise ValueError(f"source node {source!r} not found")
    target_row = conn.execute("SELECT node_type FROM node WHERE id=?", (target,)).fetchone()
    if not target_row or target_row["node_type"] in RETIRED_NODE_TYPES:
        raise ValueError(f"target node {target!r} not found")

    visited_fwd: dict[str, int] = {source: 0}
    visited_bwd: dict[str, int] = {target: 0}
    parent_fwd: dict[str, str] = {}
    parent_bwd: dict[str, str] = {}
    frontier_fwd = [source]
    frontier_bwd = [target]
    meet_node: str | None = None
    edges_scanned = 0

    while frontier_fwd and frontier_bwd and meet_node is None:
        expand_forward = len(frontier_fwd) <= len(frontier_bwd)
        current = frontier_fwd if expand_forward else frontier_bwd
        nxt: list[str] = []
        for nid in current:
            rows = conn.execute(
                "SELECT edge.from_node_id,edge.to_node_id FROM edge "
                "WHERE (edge.from_node_id=? OR edge.to_node_id=?) "
                "AND edge.edge_type NOT IN (%s) " % ",".join("?" for _ in RETIRED_EDGE_TYPES)
                + "AND NOT EXISTS(SELECT 1 FROM node n WHERE n.id=edge.from_node_id AND n.node_type IN (%s)) " % ",".join("?" for _ in RETIRED_NODE_TYPES)
                + "AND NOT EXISTS(SELECT 1 FROM node n WHERE n.id=edge.to_node_id AND n.node_type IN (%s)) LIMIT 500" % ",".join("?" for _ in RETIRED_NODE_TYPES),
                (nid, nid, *sorted(RETIRED_EDGE_TYPES), *sorted(RETIRED_NODE_TYPES), *sorted(RETIRED_NODE_TYPES)),
            ).fetchall()
            edges_scanned += len(rows)
            if edges_scanned > max_edges:
                raise ValueError("path search exceeded edge budget")
            for row in rows:
                neighbour = row["to_node_id"] if row["from_node_id"] == nid else row["from_node_id"]
                other_visited = visited_bwd if expand_forward else visited_fwd
                own_visited = visited_fwd if expand_forward else visited_bwd
                own_parent = parent_fwd if expand_forward else parent_bwd
                if neighbour in own_visited:
                    continue
                own_visited[neighbour] = -1
                own_parent[neighbour] = nid
                if neighbour in other_visited:
                    meet_node = neighbour
                    break
                nxt.append(neighbour)
            if meet_node:
                break
        if expand_forward:
            frontier_fwd = nxt
        else:
            frontier_bwd = nxt

    if meet_node is None:
        raise ValueError(f"no path between {source!r} and {target!r}")

    fwd_half: list[str] = []
    cursor = meet_node
    while cursor != source:
        fwd_half.append(cursor)
        cursor = parent_fwd[cursor]
    fwd_half.append(source)
    fwd_half.reverse()

    bwd_half: list[str] = []
    cursor = meet_node
    while cursor != target:
        cursor = parent_bwd[cursor]
        bwd_half.append(cursor)

    node_ids = fwd_half + bwd_half

    edges = []
    for a, b in zip(node_ids, node_ids[1:]):
        row = conn.execute(
            "SELECT id,edge_type,source_method,confidence FROM edge "
            "WHERE ((from_node_id=? AND to_node_id=?) OR (from_node_id=? AND to_node_id=?)) "
            "AND edge_type NOT IN (%s)" % ",".join("?" for _ in RETIRED_EDGE_TYPES),
            (a, b, b, a, *sorted(RETIRED_EDGE_TYPES)),
        ).fetchone()
        if row:
            edges.append({"id": row["id"], "edge_type": row["edge_type"], "source_method": row["source_method"], "confidence": row["confidence"]})

    nodes = [row_to_node(conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?", (nid,)
    ).fetchone()) for nid in node_ids]

    return {"node_ids": node_ids, "nodes": nodes, "edges": edges, "length": len(edges)}


def centrality(conn: sqlite3.Connection, *, limit: int = 25) -> dict[str, Any]:
    graph = _load_nx_graph(conn, analysis=True)
    degree = Counter(dict(graph.degree()))
    top = degree.most_common(limit)
    nodes_by_id = {
        r["id"]: row_to_node(r)
        for r in conn.execute(
            "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id IN (%s)" % ",".join("?" for _ in top),
            [nid for nid, _ in top],
        )
    } if top else {}
    return {"degree": [{"node": nodes_by_id.get(nid), "degree": deg} for nid, deg in top]}


def contents_tree(conn: sqlite3.Connection, node_id: str, *, max_depth: int = 4, max_children: int = 1000) -> dict[str, Any]:
    root_row = conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?", (node_id,)).fetchone()
    if not root_row:
        raise ValueError("Node not found")
    if root_row["node_type"] == "rulebook":
        max_depth = min(max_depth, 1)

    def children(parent_id: str, depth: int) -> list[dict[str, Any]]:
        if depth >= max_depth:
            return []
        edge_types = ("contains", "has_version") if parent_id == node_id and root_row["node_type"] == "provision" else ("contains",)
        edge_placeholders = ",".join("?" for _ in edge_types)
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json
            FROM edge e JOIN node n ON n.id=e.to_node_id
            JOIN canonical_node cn ON cn.id=n.id AND cn.is_canonical=1
            WHERE e.from_node_id=? AND e.edge_type IN ({edge_placeholders})
            ORDER BY
              CASE n.node_type WHEN 'chapter' THEN 0 WHEN 'rule' THEN 1 WHEN 'guidance_section' THEN 2 WHEN 'guidance_paragraph' THEN 3 ELSE 9 END,
              n.title
            LIMIT ?
            """,
            (parent_id, *edge_types, max_children),
        ).fetchall()
        out = []
        for row in rows:
            node = row_to_node(row)
            node["children"] = children(node["id"], depth + 1)
            out.append(node)
        return _natural_sort_content(out)

    root = row_to_node(root_row)
    return {"root": root, "children": children(node_id, 0)}


def reader_bundle(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    reference_depth: int = 1,
    max_depth: int = 8,
    max_children: int = 5000,
) -> dict[str, Any]:
    """Return a complete reading spine and every direct link made by that spine.

    A graph neighbourhood is centred on one node, so it cannot represent the
    citations made by contained rules without increasing graph depth and also
    admitting references-of-references.  The reader has different semantics:
    every contained provision is part of the fixed source spine, and only
    outgoing reading relationships from those source provisions are included.
    """

    root_type = conn.execute(
        "SELECT node_type FROM node WHERE id=?",
        (node_id,),
    ).fetchone()
    if root_type and root_type["node_type"] == "rulebook":
        raise ValueError("Open a Part or chapter to read provisions inline.")

    contents = contents_tree(
        conn,
        node_id,
        max_depth=max_depth,
        max_children=max_children,
    )
    spine_nodes: list[dict[str, Any]] = []
    reading_source_nodes: list[dict[str, Any]] = []
    seen_spine_ids: set[str] = set()
    structural_types = {
        "rulebook",
        "part",
        "chapter",
        "guidance_document",
        "guidance_section",
    }

    def collect(node: dict[str, Any]) -> None:
        if node["id"] not in seen_spine_ids:
            seen_spine_ids.add(node["id"])
            flat_node = {key: value for key, value in node.items() if key != "children"}
            spine_nodes.append(flat_node)
            children = node.get("children") or []
            if (
                str(node.get("text") or "").strip()
                and not (children and node.get("node_type") in structural_types)
            ):
                reading_source_nodes.append(flat_node)
        for child in node.get("children") or []:
            collect(child)

    collect({**contents["root"], "children": contents["children"]})
    reference_depth = max(1, min(int(reference_depth), 3))
    source_ids = [node["id"] for node in reading_source_nodes]
    edges_by_id: dict[str, dict[str, Any]] = {}
    available: Counter[str] = Counter()
    edge_types = ("references", "uses_defined_term", "amends")

    # Expand only outgoing links at each requested level. This keeps reference
    # depth independent from the hierarchy depth of the fixed reading spine.
    preferred_date = _reader_preferred_version_date(contents["root"], reading_source_nodes)
    # Structural headings can carry references even when they have no body
    # text. Include the whole spine in the query; paragraph placement remains
    # limited to text-bearing provisions in the frontend.
    frontier = set(seen_spine_ids)
    expanded_sources: set[str] = set()
    reader_source_ids = set(seen_spine_ids)
    version_nodes_by_id: dict[str, dict[str, Any]] = {}
    for _level in range(reference_depth):
        frontier -= expanded_sources
        if not frontier:
            break
        next_frontier: set[str] = set()
        ordered_frontier = sorted(frontier)
        # Stay below conservative SQLite variable limits for very large Parts.
        version_aliases: dict[str, list[str]] = {}
        version_rows: dict[str, dict[str, Any]] = {}
        canonical_by_version_id: dict[str, str] = {}
        for start in range(0, len(ordered_frontier), 400):
            batch = ordered_frontier[start:start + 400]
            aliases = _reader_version_aliases(conn, batch, preferred_date)
            batch_version_ids = []
            for canonical_id, version in aliases.items():
                version_aliases.setdefault(canonical_id, []).append(version["id"])
                canonical_by_version_id[version["id"]] = canonical_id
                version_rows[version["id"]] = version
                batch_version_ids.append(version["id"])
            query_sources = [*batch, *batch_version_ids]
            reader_source_ids.update(query_sources)
            placeholders = ",".join("?" for _ in query_sources)
            rows = conn.execute(
                f"""
                SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,
                       evidence_text,source_url,metadata_json
                FROM edge
                WHERE from_node_id IN ({placeholders})
                  AND edge_type IN (?,?,?)
                ORDER BY from_node_id,edge_type,confidence DESC,id
                """,
                [*query_sources, *edge_types],
            ).fetchall()
            for row in rows:
                edge = row_to_edge(row)
                canonical_source = canonical_by_version_id.get(edge["from_node_id"])
                if canonical_source:
                    edge.setdefault("metadata", {})["rolled_up_from_from_node_ids"] = [
                        canonical_source, edge["from_node_id"]
                    ]
                if edge["id"] not in edges_by_id:
                    edges_by_id[edge["id"]] = edge
                    available[edge["edge_type"]] += 1
                if edge.get("to_node_id"):
                    next_frontier.add(edge["to_node_id"])
        version_nodes_by_id.update(version_rows)
        expanded_sources.update(frontier)
        frontier = next_frontier

    edges = list(edges_by_id.values())
    _attach_reference_occurrences(conn, edges)
    endpoint_ids = set(source_ids)
    endpoint_ids.update(
        endpoint
        for edge in edges
        for endpoint in (edge.get("from_node_id"), edge.get("to_node_id"))
        if endpoint
    )
    nodes_by_id = {node["id"]: node for node in spine_nodes}
    nodes_by_id.update(version_nodes_by_id)
    missing_ids = sorted(endpoint_ids - set(nodes_by_id))
    for start in range(0, len(missing_ids), 750):
        batch = missing_ids[start:start + 750]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT id,node_type,stable_key,title,text,url,metadata_json
            FROM node WHERE id IN ({placeholders})
            """,
            batch,
        ):
            node = row_to_node(row)
            nodes_by_id[node["id"]] = node

    reader_source_nodes = [nodes_by_id[source_id] for source_id in sorted(reader_source_ids) if source_id in nodes_by_id]
    _attach_html_link_occurrences(conn, edges, reader_source_nodes)
    _attach_detached_materialized_reader_occurrences(conn, sorted(reader_source_ids), nodes_by_id, edges)
    _attach_unresolved_reader_occurrences(conn, sorted(reader_source_ids), nodes_by_id, edges)
    _resolve_reader_provision_versions(
        conn,
        nodes_by_id,
        spine_node_ids=set(seen_spine_ids),
        preferred_date=preferred_date,
    )
    nodes, edges = _roll_up_guidance_reference_nodes(
        conn,
        list(nodes_by_id.values()),
        edges,
    )
    return {
        "contents": contents,
        "graph": {
            "nodes": nodes,
            "edges": edges,
            "available_edge_types": dict(sorted(available.items())),
        },
        "spine_node_count": len(spine_nodes),
        "source_provision_count": len(reading_source_nodes),
        "reference_level": reference_depth,
        "reference_depth": reference_depth,
    }


def _reader_preferred_version_date(
    root: dict[str, Any],
    source_nodes: list[dict[str, Any]],
) -> str | None:
    """Choose the dated source version a reader should show for canonical links."""

    candidates = [root, *source_nodes]
    for node in candidates:
        value = (node.get("metadata") or {}).get("rulebook_date")
        if value:
            return str(value)
    return None


def _resolve_reader_provision_versions(
    conn: sqlite3.Connection,
    nodes_by_id: dict[str, dict[str, Any]],
    *,
    spine_node_ids: set[str],
    preferred_date: str | None,
) -> None:
    """Give canonical reference targets the relevant dated text for reading.

    Canonical provision IDs remain the graph target.  The text-bearing version
    is only projected into the reader response, so opening a citation is useful
    without changing graph identity or mutating the database.
    """

    for node in nodes_by_id.values():
        if node.get("node_type") != "provision" or node.get("id") in spine_node_ids:
            continue
        if str(node.get("text") or "").strip():
            continue
        version = _reader_version_aliases(conn, [node["id"]], preferred_date).get(node["id"])
        if not version:
            continue
        version_meta = version.get("metadata") or {}
        node["text"] = version.get("text") or ""
        node["url"] = version.get("url") or node.get("url") or ""
        node["resolved_version"] = {
            "id": version["id"],
            "stable_key": version["stable_key"],
            "version_date": version_meta.get("rulebook_date"),
            "snapshot_id": version_meta.get("snapshot_id"),
        }
        node["metadata"] = {
            **(node.get("metadata") or {}),
            "resolved_version_id": version["id"],
            "resolved_version_key": version["stable_key"],
            "resolved_version_date": version_meta.get("rulebook_date"),
            "resolved_snapshot_id": version_meta.get("snapshot_id"),
        }


def _reader_version_aliases(
    conn: sqlite3.Connection,
    provision_ids: list[str],
    preferred_date: str | None,
) -> dict[str, dict[str, Any]]:
    """Choose exact or most recent non-future text for each canonical provision."""
    result: dict[str, dict[str, Any]] = {}
    if not provision_ids:
        return result
    for start in range(0, len(provision_ids), 750):
        batch = provision_ids[start:start + 750]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT id,node_type,stable_key,title,text,url,metadata_json,
                   json_extract(metadata_json,'$.canonical_provision_id') AS canonical_id,
                   json_extract(metadata_json,'$.rulebook_date') AS rulebook_date
            FROM node
            WHERE node_type='rule' AND json_extract(metadata_json,'$.canonical_provision_id') IN ({placeholders})
            ORDER BY canonical_id,rulebook_date,id
            """,
            batch,
        ).fetchall()
        grouped: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[str(row["canonical_id"])].append(row)
        for canonical_id, versions in grouped.items():
            if preferred_date:
                eligible = [row for row in versions if str(row["rulebook_date"] or "") <= preferred_date]
                exact = [row for row in eligible if str(row["rulebook_date"] or "") == preferred_date]
                choices = exact or eligible
            else:
                choices = versions
            if choices:
                result[canonical_id] = row_to_node(choices[-1])
    return result


def _natural_sort_content(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import re
    def key(item: dict[str, Any]) -> tuple:
        meta = item.get("metadata") or {}
        title = str(item.get("title") or "")
        raw = str(meta.get("chapter_number") or meta.get("article_number") or meta.get("rule_number") or title)
        lower = title.lower()
        article = re.match(r"article\s+(\d+)([a-z]*)\b", raw.lower()) or re.match(r"article\s+(\d+)([a-z]*)\b", lower)
        annex = re.match(r"annex\s+([ivxlcdm]+)\b", raw.lower()) or re.match(r"annex\s+([ivxlcdm]+)\b", lower)
        nums = tuple(int(x) for x in re.findall(r"\d+", raw)[:4])
        if lower.startswith("annex") or annex:
            structure_rank = 3
            ordinal = (_roman_to_int(annex.group(1)) if annex else 9999,)
        elif lower.startswith("rules on standards"):
            structure_rank = 1
            ordinal = (nums[0] if nums else 9999,)
        elif lower.startswith("article") or article:
            structure_rank = 2
            ordinal = (int(article.group(1)) if article else (nums[0] if nums else 9999), article.group(2) if article else "")
        else:
            structure_rank = 0
            ordinal = nums or (9999,)
        type_rank = {"chapter": 0, "rule": 1, "guidance_section": 2, "guidance_paragraph": 3}.get(item.get("node_type"), 9)
        return (type_rank, structure_rank, ordinal, raw.lower())


    return sorted(items, key=key)


def _roman_to_int(value: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    prev = 0
    for char in reversed(value.lower()):
        cur = values.get(char, 0)
        total += -cur if cur < prev else cur
        prev = max(prev, cur)
    return total or 9999


def _snippet(text: str, q: str, size: int = 240) -> str:
    lower = text.lower(); idx = lower.find(q.lower().split()[0]) if q else -1
    if idx < 0:
        return text[:size]
    start = max(0, idx - size // 3)
    return text[start:start + size]


def _json(value: str) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def components(conn: sqlite3.Connection, *, limit: int = 20, max_edges: int = 250000) -> dict[str, Any]:
    graph = _load_nx_graph(conn, max_edges=max_edges, analysis=True)
    comps = sorted(nx.connected_components(graph), key=len, reverse=True)
    return {"component_count": len(comps), "largest_size": len(comps[0]) if comps else 0, "components": [{"size": len(c), "sample": _nodes_for_ids(conn, list(c)[:8])} for c in comps[:limit]]}


def betweenness(conn: sqlite3.Connection, *, limit: int = 25, k: int = 250, max_edges: int = 250000, max_nodes: int = 4000) -> dict[str, Any]:
    graph = _load_nx_graph(conn, max_edges=max_edges, analysis=True)
    if graph.number_of_nodes() == 0:
        return {"results": []}
    # Full betweenness over the whole corpus graph is too slow for an interactive
    # endpoint. Use a top-degree induced subgraph and sampled source nodes. This
    # is deliberately an exploratory bridge ranking, not an authoritative metric.
    if graph.number_of_nodes() > max_nodes:
        keep = [nid for nid, _ in sorted(graph.degree, key=lambda item: item[1], reverse=True)[:max_nodes]]
        graph = graph.subgraph(keep).copy()
    sample_k = min(k, graph.number_of_nodes())
    scores = nx.betweenness_centrality(graph, k=sample_k, seed=42, normalized=True)
    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    nodes = {n["id"]: n for n in _nodes_for_ids(conn, [nid for nid, _ in top])}
    return {"sample_k": sample_k, "graph_nodes": graph.number_of_nodes(), "graph_edges": graph.number_of_edges(), "results": [{"node": nodes.get(nid), "betweenness": score} for nid, score in top]}


def communities(conn: sqlite3.Connection, *, limit: int = 20, max_edges: int = 250000, max_nodes: int = 2500) -> dict[str, Any]:
    graph = _load_nx_graph(conn, max_edges=max_edges, analysis=True)
    if graph.number_of_nodes() == 0:
        return {"community_count": 0, "communities": []}
    if graph.number_of_nodes() > max_nodes:
        keep = [nid for nid, _ in sorted(graph.degree, key=lambda item: item[1], reverse=True)[:max_nodes]]
        graph = graph.subgraph(keep).copy()
    comms = list(nx.algorithms.community.greedy_modularity_communities(graph))
    comms = sorted(comms, key=len, reverse=True)
    return {"community_count": len(comms), "graph_nodes": graph.number_of_nodes(), "graph_edges": graph.number_of_edges(), "communities": [{"size": len(c), "sample": _nodes_for_ids(conn, list(c)[:10])} for c in comms[:limit]]}


def common_neighbours(conn: sqlite3.Connection, source: str, target: str, *, limit: int = 50) -> dict[str, Any]:
    graph = _load_nx_graph(conn)
    common = list(nx.common_neighbors(graph, source, target)) if graph.has_node(source) and graph.has_node(target) else []
    # Rank common neighbours by graph degree so the most explanatory bridges appear first.
    common = sorted(common, key=lambda nid: graph.degree(nid), reverse=True)[:limit]
    return {"source": source, "target": target, "count": len(common), "nodes": _nodes_for_ids(conn, common)}



def _load_nx_graph(conn: sqlite3.Connection, *, max_edges: int = 250000, analysis: bool = False) -> nx.Graph:
    graph = nx.Graph()
    identity_map = _analysis_identity_map(conn) if analysis else {}
    excluded_types_set = set(RETIRED_EDGE_TYPES)
    if analysis:
        excluded_types_set.update({"has_version", "sourced_from"})
    excluded_types = sorted(excluded_types_set)
    rows = conn.execute(
        f"""
        SELECT e.id,e.from_node_id,e.to_node_id,e.edge_type,e.source_method,e.confidence
        FROM edge e WHERE e.edge_type NOT IN ({','.join('?' for _ in excluded_types)})
          AND NOT EXISTS (
            SELECT 1 FROM node n WHERE n.id=e.from_node_id
              AND n.node_type IN ({','.join('?' for _ in RETIRED_NODE_TYPES)})
          )
          AND NOT EXISTS (
            SELECT 1 FROM node n WHERE n.id=e.to_node_id
              AND n.node_type IN ({','.join('?' for _ in RETIRED_NODE_TYPES)})
          )
        LIMIT ?
        """,
        [*excluded_types, *sorted(RETIRED_NODE_TYPES), *sorted(RETIRED_NODE_TYPES), max_edges],
    )
    for row in rows:
        from_id = identity_map.get(row["from_node_id"], row["from_node_id"])
        to_id = identity_map.get(row["to_node_id"], row["to_node_id"])
        if from_id == to_id:
            continue
        graph.add_edge(from_id, to_id, id=row["id"], edge_type=row["edge_type"], source_method=row["source_method"], confidence=row["confidence"])
    return graph


def _analysis_identity_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Map version node IDs to canonical provision IDs for analysis only."""

    mapping: dict[str, str] = {}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node'").fetchone():
        return mapping
    for row in conn.execute("SELECT id,metadata_json FROM node"):
        metadata = _json(row["metadata_json"])
        target = metadata.get("canonical_provision_id")
        if metadata.get("identity_type") == "provision_version" and target:
            mapping[row["id"]] = str(target)
    return mapping


def _nodes_for_ids(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id IN (%s)" % ",".join("?" for _ in ids),
        ids,
    ).fetchall()
    by_id = {r["id"]: row_to_node(r) for r in rows}
    return [by_id[nid] for nid in ids if nid in by_id]


def _summary_node(row: sqlite3.Row) -> dict[str, Any]:
    node = row_to_node(row)
    metadata = node.get("metadata") or {}
    return {
        "id": node["id"],
        "node_type": node["node_type"],
        "stable_key": node["stable_key"],
        "title": node["title"],
        "url": node["url"],
        "metadata": {
            "firm_categories": metadata["firm_categories"],
        } if isinstance(metadata.get("firm_categories"), list) else {},
    }


def list_nodes(
    conn: sqlite3.Connection,
    *,
    node_types: list[str] | None = None,
    limit: int = 500,
    offset: int = 0,
    summary: bool = False,
) -> list[dict[str, Any]]:
    type_clause = "WHERE cn.is_canonical=1 AND n.node_type NOT IN (%s)" % ",".join("?" for _ in RETIRED_NODE_TYPES)
    params: list[Any] = sorted(RETIRED_NODE_TYPES)
    if node_types:
        type_clause += " AND node_type IN (%s)" % ",".join("?" for _ in node_types)
        params.extend(node_types)
    rows = conn.execute(
        f"""
        SELECT n.id,n.node_type,n.stable_key,n.title,{'' if summary else 'n.text,'}n.url,n.metadata_json
        FROM node n JOIN canonical_node cn ON cn.id=n.id {type_clause}
        ORDER BY n.title COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [_summary_node(r) if summary else row_to_node(r) for r in rows]
