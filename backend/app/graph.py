from __future__ import annotations

import json
import sqlite3
from collections import Counter, deque
from typing import Any

import networkx as nx
import numpy as np
from sklearn.cluster import KMeans

from .db import row_to_edge, row_to_node

EXPLICIT_METHODS = {"site_structure", "html_link", "html_glossary_link", "glossary_source", "crr_terms_source", "legal_instrument_listing"}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "nodes": conn.execute("SELECT COUNT(*) FROM node").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0],
        "nodes_by_type": dict(conn.execute("SELECT node_type, COUNT(*) FROM node GROUP BY node_type ORDER BY node_type").fetchall()),
        "edges_by_type": dict(conn.execute("SELECT edge_type, COUNT(*) FROM edge GROUP BY edge_type ORDER BY edge_type").fetchall()),
        "edge_methods": dict(conn.execute("SELECT source_method, COUNT(*) FROM edge GROUP BY source_method ORDER BY source_method").fetchall()),
        "missing_edge_targets": conn.execute("SELECT COUNT(*) FROM edge e LEFT JOIN node n ON e.to_node_id=n.id WHERE n.id IS NULL").fetchone()[0],
    }


def search(conn: sqlite3.Connection, q: str, *, node_types: list[str] | None = None, limit: int = 25) -> list[dict[str, Any]]:
    params: list[Any] = []
    type_clause = ""
    if node_types:
        type_clause = " AND n.node_type IN (%s)" % ",".join("?" for _ in node_types)
        params.extend(node_types)
    try:
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json,
                   bm25(node_fts) AS score
            FROM node_fts f JOIN node n ON n.id=f.id
            WHERE node_fts MATCH ? {type_clause}
            ORDER BY score LIMIT ?
            """,
            [q, *params, limit],
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json, 0 AS score
            FROM node n
            WHERE (n.title LIKE ? OR n.text LIKE ?) {type_clause}
            LIMIT ?
            """,
            [f"%{q}%", f"%{q}%", *params, limit],
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
    edge_type_clause = ""
    params_extra: list[Any] = []
    if edge_types:
        edge_type_clause = " AND edge_type IN (%s)" % ",".join("?" for _ in edge_types)
        params_extra.extend(edge_types)

    available = dict(conn.execute(
        f"""
        SELECT edge_type, COUNT(*) FROM edge
        WHERE (from_node_id=? OR to_node_id=?) {method_clause}
        GROUP BY edge_type ORDER BY edge_type
        """,
        [node_id, node_id, *method_params],
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
            WHERE (from_node_id=? OR to_node_id=?) {edge_type_clause} {method_clause}
            ORDER BY confidence DESC
            LIMIT ?
            """,
            [current, current, *params_extra, *method_params, limit],
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
    return {"nodes": nodes, "edges": list(seen_edges.values()), "available_edge_types": available}


def shortest_path(conn: sqlite3.Connection, source: str, target: str, *, max_edges: int = 200000) -> dict[str, Any]:
    graph = nx.Graph()
    for row in conn.execute("SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence FROM edge LIMIT ?", (max_edges,)):
        graph.add_edge(row["from_node_id"], row["to_node_id"], id=row["id"], edge_type=row["edge_type"], source_method=row["source_method"], confidence=row["confidence"])
    path = nx.shortest_path(graph, source, target)
    edges = []
    for a, b in zip(path, path[1:]):
        edges.append(graph[a][b])
    nodes = [row_to_node(conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?", (nid,)).fetchone()) for nid in path]
    return {"node_ids": path, "nodes": nodes, "edges": edges, "length": len(edges)}


def centrality(conn: sqlite3.Connection, *, limit: int = 25) -> dict[str, Any]:
    degree = Counter()
    for a, b in conn.execute("SELECT from_node_id,to_node_id FROM edge"):
        degree[a] += 1
        degree[b] += 1
    top = degree.most_common(limit)
    nodes_by_id = {
        r["id"]: row_to_node(r)
        for r in conn.execute(
            "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id IN (%s)" % ",".join("?" for _ in top),
            [nid for nid, _ in top],
        )
    } if top else {}
    return {"degree": [{"node": nodes_by_id.get(nid), "degree": deg} for nid, deg in top]}


def interesting(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.id,e.from_node_id,e.to_node_id,e.edge_type,e.source_method,e.confidence,e.evidence_text,e.source_url,e.metadata_json,
               a.title AS from_title, a.node_type AS from_type, a.metadata_json AS from_meta,
               b.title AS to_title, b.node_type AS to_type, b.metadata_json AS to_meta
        FROM edge e
        JOIN node a ON a.id=e.from_node_id
        JOIN node b ON b.id=e.to_node_id
        WHERE e.edge_type IN ('similar_to','shares_defined_term','shares_defined_term_with_guidance','resolves_to_part')
        ORDER BY
          CASE e.edge_type WHEN 'similar_to' THEN 0 WHEN 'shares_defined_term_with_guidance' THEN 1 ELSE 2 END,
          e.confidence DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        edge = row_to_edge(r)
        from_meta = _json(r["from_meta"])
        to_meta = _json(r["to_meta"])
        edge["from_title"] = r["from_title"]
        edge["to_title"] = r["to_title"]
        edge["from_type"] = r["from_type"]
        edge["to_type"] = r["to_type"]
        edge["why"] = _why(edge, from_meta, to_meta)
        out.append(edge)
    return out


def contents_tree(conn: sqlite3.Connection, node_id: str, *, max_depth: int = 4, max_children: int = 1000) -> dict[str, Any]:
    root_row = conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?", (node_id,)).fetchone()
    if not root_row:
        raise ValueError("Node not found")

    def children(parent_id: str, depth: int) -> list[dict[str, Any]]:
        if depth >= max_depth:
            return []
        rows = conn.execute(
            """
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json
            FROM edge e JOIN node n ON n.id=e.to_node_id
            WHERE e.from_node_id=? AND e.edge_type='contains'
            ORDER BY
              CASE n.node_type WHEN 'chapter' THEN 0 WHEN 'rule' THEN 1 WHEN 'guidance_section' THEN 2 WHEN 'guidance_paragraph' THEN 3 ELSE 9 END,
              n.title
            LIMIT ?
            """,
            (parent_id, max_children),
        ).fetchall()
        out = []
        for row in rows:
            node = row_to_node(row)
            node["children"] = children(node["id"], depth + 1)
            out.append(node)
        return _natural_sort_content(out)

    root = row_to_node(root_row)
    return {"root": root, "children": children(node_id, 0)}


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
            ordinal = (_roman_to_int(annex.group(1)) if annex else 9999, "")
        elif lower.startswith("rules on standards"):
            structure_rank = 1
            ordinal = (nums[0] if nums else 9999, "")
        elif lower.startswith("article") or article:
            structure_rank = 2
            ordinal = (int(article.group(1)) if article else (nums[0] if nums else 9999), article.group(2) if article else "")
        else:
            structure_rank = 0
            ordinal = (nums[0] if nums else 9999, "")
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


def _why(edge: dict[str, Any], from_meta: dict[str, Any], to_meta: dict[str, Any]) -> str:
    if edge["edge_type"] == "similar_to":
        return f"Semantic similarity score {edge['confidence']:.2f}."
    if edge["edge_type"].startswith("shares_defined_term"):
        return f"Both nodes use defined term: {edge.get('evidence_text')}."
    if edge["edge_type"] == "resolves_to_part":
        return "Dated Part reference resolved to current parsed Part by title match."
    return edge.get("source_method", "")


def components(conn: sqlite3.Connection, *, limit: int = 20, max_edges: int = 250000) -> dict[str, Any]:
    graph = _load_nx_graph(conn, max_edges=max_edges)
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


def semantic_map(conn: sqlite3.Connection, *, level: str = "part", clusters: int = 12, edge_limit: int = 700) -> dict[str, Any]:
    """Whole-Rulebook semantic map.

    Part-level for now: each Part vector is the average embedding of its rule
    provisions. Edges aggregate lower-level semantic, reference, term and
    obligation links between Parts.
    """
    if level != "part":
        raise ValueError("Only level='part' is currently supported")

    part_rows = conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE node_type='part' ORDER BY title"
    ).fetchall()
    parts_by_key = {r["stable_key"]: row_to_node(r) for r in part_rows}
    node_to_part: dict[str, str] = {}
    vectors_by_part: dict[str, list[np.ndarray]] = {p["id"]: [] for p in parts_by_key.values()}

    for row in conn.execute(
        """
        SELECT n.id,n.stable_key,emb.vector_json
        FROM node n JOIN embedding emb ON emb.node_id=n.id
        WHERE n.node_type='rule'
        """
    ):
        part_key = _part_key_from_stable(row["stable_key"])
        part = parts_by_key.get(part_key or "")
        if not part:
            continue
        node_to_part[row["id"]] = part["id"]
        vectors_by_part[part["id"]].append(np.array(json.loads(row["vector_json"]), dtype="float32"))

    nodes: list[dict[str, Any]] = []
    part_vectors: list[np.ndarray] = []
    for part in parts_by_key.values():
        vectors = vectors_by_part.get(part["id"]) or []
        vector = np.mean(np.vstack(vectors), axis=0) if vectors else np.zeros(256, dtype="float32")
        part_vectors.append(vector)
        part["metadata"] = {**(part.get("metadata") or {}), "provision_count": len(vectors)}
        nodes.append(part)

    matrix = np.vstack(part_vectors) if part_vectors else np.zeros((0, 2), dtype="float32")
    coords = _project_2d(matrix)
    labels = _cluster_labels(matrix, min(clusters, max(2, len(nodes) // 4))) if len(nodes) >= 3 else [0] * len(nodes)

    pair_weights: Counter[tuple[str, str]] = Counter()
    pair_methods: dict[tuple[str, str], Counter[str]] = {}
    for row in conn.execute(
        """
        SELECT from_node_id,to_node_id,edge_type,source_method,confidence
        FROM edge
        WHERE edge_type IN ('similar_to','references','uses_defined_term','has_topic','has_topic_cluster','has_obligation_pattern','shares_obligation_pattern')
          AND source_method <> 'site_structure'
        """
    ):
        a = node_to_part.get(row["from_node_id"])
        b = node_to_part.get(row["to_node_id"])
        if not a or not b or a == b:
            continue
        key = tuple(sorted((a, b)))
        kind_weight = {"similar_to": 1.4, "references": 1.2, "uses_defined_term": 0.45, "has_topic": 0.25, "has_topic_cluster": 0.35, "has_obligation_pattern": 0.35, "shares_obligation_pattern": 0.8}.get(row["edge_type"], 0.4)
        pair_weights[key] += float(row["confidence"] or 0.5) * kind_weight
        pair_methods.setdefault(key, Counter())[row["edge_type"]] += 1

    weighted_degree: Counter[str] = Counter()
    edges: list[dict[str, Any]] = []
    for i, ((a, b), weight) in enumerate(pair_weights.most_common(edge_limit)):
        weighted_degree[a] += weight
        weighted_degree[b] += weight
        methods = pair_methods.get((a, b), Counter())
        dominant = methods.most_common(1)[0][0] if methods else "similar_to"
        edges.append({
            "id": f"semantic-map:{i}:{a}:{b}",
            "from_node_id": a,
            "to_node_id": b,
            "edge_type": dominant,
            "source_method": "part_level_aggregate",
            "confidence": min(1.0, weight / 12),
            "evidence_text": ", ".join(f"{k}: {v}" for k, v in methods.most_common(4)),
            "source_url": "",
            "metadata": {"weight": weight, "counts": dict(methods)},
        })

    for i, node in enumerate(nodes):
        node["x"] = float(coords[i, 0])
        node["y"] = float(coords[i, 1])
        node["degree"] = float(weighted_degree[node["id"]])
        node["metadata"] = {**(node.get("metadata") or {}), "semantic_cluster": int(labels[i]), "weighted_degree": float(weighted_degree[node["id"]])}
    return {"level": level, "nodes": nodes, "edges": edges, "clusters": len(set(labels)), "available_edge_types": dict(Counter(e["edge_type"] for e in edges))}


def _part_key_from_stable(stable_key: str) -> str | None:
    parts = (stable_key or "").split(":")
    if len(parts) >= 3 and parts[1] == "part":
        return f"part:{parts[2]}"
    return None


def _project_2d(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return np.zeros((0, 2), dtype="float32")
    centred = matrix - matrix.mean(axis=0, keepdims=True)
    try:
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        coords = centred @ vt[:2].T
    except np.linalg.LinAlgError:
        coords = centred[:, :2]
    if coords.shape[1] < 2:
        coords = np.pad(coords, ((0, 0), (0, 2 - coords.shape[1])))
    mins = coords.min(axis=0); maxs = coords.max(axis=0); span = np.where(maxs - mins == 0, 1, maxs - mins)
    norm = (coords - mins) / span
    return np.column_stack((120 + norm[:, 0] * 960, 100 + norm[:, 1] * 620)).astype("float32")


def _cluster_labels(matrix: np.ndarray, clusters: int) -> list[int]:
    if matrix.size == 0 or len(matrix) < clusters:
        return [0] * len(matrix)
    try:
        return [int(x) for x in KMeans(n_clusters=clusters, random_state=42, n_init="auto").fit_predict(matrix)]
    except Exception:
        return [0] * len(matrix)


def _load_nx_graph(conn: sqlite3.Connection, *, max_edges: int = 250000, analysis: bool = False) -> nx.Graph:
    graph = nx.Graph()
    if analysis:
        rows = conn.execute(
            """
            SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence
            FROM edge
            WHERE edge_type NOT IN ('shares_defined_term','has_obligation_pattern')
            LIMIT ?
            """,
            (max_edges,),
        )
    else:
        rows = conn.execute("SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence FROM edge LIMIT ?", (max_edges,))
    for row in rows:
        graph.add_edge(row["from_node_id"], row["to_node_id"], id=row["id"], edge_type=row["edge_type"], source_method=row["source_method"], confidence=row["confidence"])
    return graph


def _nodes_for_ids(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id IN (%s)" % ",".join("?" for _ in ids),
        ids,
    ).fetchall()
    by_id = {r["id"]: row_to_node(r) for r in rows}
    return [by_id[nid] for nid in ids if nid in by_id]


def list_nodes(conn: sqlite3.Connection, *, node_types: list[str] | None = None, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    type_clause = ""
    params: list[Any] = []
    if node_types:
        type_clause = "WHERE node_type IN (%s)" % ",".join("?" for _ in node_types)
        params.extend(node_types)
    rows = conn.execute(
        f"""
        SELECT id,node_type,stable_key,title,text,url,metadata_json
        FROM node {type_clause}
        ORDER BY title COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [row_to_node(r) for r in rows]
