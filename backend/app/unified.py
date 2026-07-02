from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from typing import Any


def unified_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    legacy_nodes = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    reporting_nodes = conn.execute("SELECT COUNT(*) FROM graph_node").fetchone()[0]
    legacy_edges = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    reporting_edges = conn.execute("SELECT COUNT(*) FROM graph_edge").fetchone()[0]
    return {
        "nodes": legacy_nodes + reporting_nodes,
        "edges": legacy_edges + reporting_edges,
        "sources": {
            "rulebook": {
                "nodes": legacy_nodes,
                "edges": legacy_edges,
                "nodes_by_type": dict(conn.execute("SELECT node_type, COUNT(*) FROM node GROUP BY node_type ORDER BY node_type").fetchall()),
                "edges_by_type": dict(conn.execute("SELECT edge_type, COUNT(*) FROM edge GROUP BY edge_type ORDER BY edge_type").fetchall()),
            },
            "reporting": {
                "nodes": reporting_nodes,
                "edges": reporting_edges,
                "nodes_by_type": dict(conn.execute("SELECT node_type, COUNT(*) FROM graph_node GROUP BY node_type ORDER BY node_type").fetchall()),
                "edges_by_type": dict(conn.execute("SELECT edge_type, COUNT(*) FROM graph_edge GROUP BY edge_type ORDER BY edge_type").fetchall()),
            },
        },
    }


def unified_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = []
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        table = row["name"]
        columns = [dict(c) for c in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()]
        count = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
        tables.append({"table": table, "rows": count, "columns": columns})
    return {"tables": tables}


def unified_table_rows(conn: sqlite3.Connection, table: str, *, q: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any] | None:
    if not _valid_table(conn, table):
        return None
    quoted = _quote_ident(table)
    columns = [dict(c) for c in conn.execute(f"PRAGMA table_info({quoted})").fetchall()]
    where = ""
    params: list[Any] = []
    if q:
        text_columns = [c["name"] for c in columns if str(c["type"] or "").upper() in {"", "TEXT", "VARCHAR", "CHAR", "CLOB"}]
        if text_columns:
            where = " WHERE " + " OR ".join(f"{_quote_ident(c)} LIKE ?" for c in text_columns)
            params.extend([f"%{q}%"] * len(text_columns))
    rows = conn.execute(f"SELECT * FROM {quoted}{where} LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM {quoted}{where}", params).fetchone()[0]
    return {"table": table, "total": total, "limit": limit, "offset": offset, "columns": columns, "rows": [dict(r) for r in rows]}


def unified_search(
    conn: sqlite3.Connection,
    q: str,
    *,
    sources: list[str] | None = None,
    node_types: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    wanted = _sources(sources)
    per_source = max(1, limit)
    results: list[dict[str, Any]] = []
    if "rulebook" in wanted:
        results.extend(_search_rulebook(conn, q, node_types=node_types, limit=per_source))
    if "reporting" in wanted:
        results.extend(_search_reporting(conn, q, node_types=node_types, limit=per_source))
    return sorted(results, key=lambda x: (x.get("score", 0), x["source"], x.get("label") or ""))[:limit]


def unified_nodes(
    conn: sqlite3.Connection,
    *,
    sources: list[str] | None = None,
    node_types: list[str] | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    wanted = _sources(sources)
    results: list[dict[str, Any]] = []
    per_source_limit = limit if len(wanted) == 1 else limit + offset
    if "rulebook" in wanted:
        results.extend(_list_rulebook_nodes(conn, node_types=node_types, q=q, limit=per_source_limit, offset=0 if len(wanted) > 1 else offset))
    if "reporting" in wanted:
        results.extend(_list_reporting_nodes(conn, node_types=node_types, q=q, limit=per_source_limit, offset=0 if len(wanted) > 1 else offset))
    if len(wanted) > 1:
        results = sorted(results, key=lambda x: (x["source"], x.get("node_type") or "", x.get("label") or ""))[offset : offset + limit]
    return results[:limit]


def unified_node(conn: sqlite3.Connection, node_id: str, *, source: str | None = None) -> dict[str, Any] | None:
    wanted = _sources([source] if source else None)
    if "reporting" in wanted:
        row = conn.execute(
            "SELECT node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status FROM graph_node WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if row:
            return _reporting_node(row)
    if "rulebook" in wanted:
        row = conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?", (node_id,)).fetchone()
        if row:
            return _rulebook_node(row)
    return None


def unified_edges(
    conn: sqlite3.Connection,
    *,
    node_id: str | None = None,
    sources: list[str] | None = None,
    edge_types: list[str] | None = None,
    direction: str = "both",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    wanted = _sources(sources)
    results: list[dict[str, Any]] = []
    per_source_limit = limit if len(wanted) == 1 else limit + offset
    if "rulebook" in wanted:
        results.extend(_list_rulebook_edges(conn, node_id=node_id, edge_types=edge_types, direction=direction, limit=per_source_limit, offset=0 if len(wanted) > 1 else offset))
    if "reporting" in wanted:
        results.extend(_list_reporting_edges(conn, node_id=node_id, edge_types=edge_types, direction=direction, limit=per_source_limit, offset=0 if len(wanted) > 1 else offset))
    if len(wanted) > 1:
        results = sorted(results, key=lambda x: (x["source"], x.get("edge_type") or "", x.get("edge_id") or ""))[offset : offset + limit]
    return results[:limit]


def unified_edge(conn: sqlite3.Connection, edge_id: str, *, source: str | None = None) -> dict[str, Any] | None:
    wanted = _sources([source] if source else None)
    if "reporting" in wanted:
        row = conn.execute(
            "SELECT edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status,effective_from,effective_to FROM graph_edge WHERE edge_id=?",
            (edge_id,),
        ).fetchone()
        if row:
            return _reporting_edge(row)
    if "rulebook" in wanted:
        row = conn.execute(
            "SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json FROM edge WHERE id=?",
            (edge_id,),
        ).fetchone()
        if row:
            return _rulebook_edge(row)
    return None


def unified_neighbourhood(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    source: str | None = None,
    depth: int = 1,
    limit: int = 250,
    edge_types: list[str] | None = None,
) -> dict[str, Any] | None:
    root_source = source or _detect_node_source(conn, node_id)
    if not root_source:
        return None
    if root_source == "rulebook":
        return _rulebook_neighbourhood(conn, node_id, depth=depth, limit=limit, edge_types=edge_types)
    return _reporting_neighbourhood(conn, node_id, depth=depth, limit=limit, edge_types=edge_types)


def _search_rulebook(conn: sqlite3.Connection, q: str, *, node_types: list[str] | None, limit: int) -> list[dict[str, Any]]:
    params: list[Any] = []
    type_clause = ""
    if node_types:
        type_clause = f" AND n.node_type IN ({','.join('?' for _ in node_types)})"
        params.extend(node_types)
    try:
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json,
                   CASE WHEN n.title LIKE ? THEN -1000000.0 ELSE bm25(node_fts) END AS score
            FROM node_fts f JOIN node n ON n.id=f.id
            JOIN canonical_node cn ON cn.id=n.id AND cn.is_canonical=1
            WHERE node_fts MATCH ? {type_clause}
            ORDER BY score LIMIT ?
            """,
            [f"%{q}%", q, *params, limit],
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            f"""
            SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json,
                   CASE WHEN n.title LIKE ? THEN -1000000.0 ELSE 0 END AS score
            FROM node n
            JOIN canonical_node cn ON cn.id=n.id AND cn.is_canonical=1
            WHERE (n.title LIKE ? OR n.text LIKE ? OR n.stable_key LIKE ?) {type_clause}
            ORDER BY score, n.title LIMIT ?
            """,
            [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", *params, limit],
        ).fetchall()
    return [_rulebook_node(row) | {"score": row["score"]} for row in rows]


def _search_reporting(conn: sqlite3.Connection, q: str, *, node_types: list[str] | None, limit: int) -> list[dict[str, Any]]:
    params: list[Any] = [f"%{q}%", f"%{q}%", f"%{q}%"]
    type_clause = ""
    if node_types:
        type_clause = f" AND node_type IN ({','.join('?' for _ in node_types)})"
        params.extend(node_types)
    rows = conn.execute(
        f"""
        SELECT node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status,
               CASE WHEN label LIKE ? THEN -1 ELSE 0 END AS score
        FROM graph_node
        WHERE (node_id LIKE ? OR label LIKE ? OR properties_json LIKE ?) {type_clause}
        ORDER BY score,label LIMIT ?
        """,
        [f"%{q}%", *params, limit],
    ).fetchall()
    return [_reporting_node(row) | {"score": row["score"]} for row in rows]


def _list_rulebook_nodes(conn: sqlite3.Connection, *, node_types: list[str] | None, q: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    where = "WHERE cn.is_canonical=1"
    params: list[Any] = []
    if node_types:
        where += f" AND n.node_type IN ({','.join('?' for _ in node_types)})"
        params.extend(node_types)
    if q:
        where += " AND (n.id LIKE ? OR n.stable_key LIKE ? OR n.title LIKE ? OR n.text LIKE ?)"
        needle = f"%{q}%"
        params.extend([needle, needle, needle, needle])
    rows = conn.execute(
        f"SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json FROM node n JOIN canonical_node cn ON cn.id=n.id {where} ORDER BY n.node_type,n.title LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_rulebook_node(row) for row in rows]


def _list_reporting_nodes(conn: sqlite3.Connection, *, node_types: list[str] | None, q: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    where = "WHERE 1=1"
    params: list[Any] = []
    if node_types:
        where += f" AND node_type IN ({','.join('?' for _ in node_types)})"
        params.extend(node_types)
    if q:
        where += " AND (node_id LIKE ? OR label LIKE ? OR properties_json LIKE ?)"
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    rows = conn.execute(
        f"SELECT node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status FROM graph_node {where} ORDER BY node_type,label LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_reporting_node(row) for row in rows]


def _list_rulebook_edges(conn: sqlite3.Connection, *, node_id: str | None, edge_types: list[str] | None, direction: str, limit: int, offset: int) -> list[dict[str, Any]]:
    where = "WHERE 1=1"
    params: list[Any] = []
    if node_id:
        if direction == "out":
            where += " AND from_node_id=?"
            params.append(node_id)
        elif direction == "in":
            where += " AND to_node_id=?"
            params.append(node_id)
        else:
            where += " AND (from_node_id=? OR to_node_id=?)"
            params.extend([node_id, node_id])
    if edge_types:
        where += f" AND edge_type IN ({','.join('?' for _ in edge_types)})"
        params.extend(edge_types)
    rows = conn.execute(
        f"SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json FROM edge {where} ORDER BY edge_type,id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_rulebook_edge(row) for row in rows]


def _list_reporting_edges(conn: sqlite3.Connection, *, node_id: str | None, edge_types: list[str] | None, direction: str, limit: int, offset: int) -> list[dict[str, Any]]:
    where = "WHERE 1=1"
    params: list[Any] = []
    if node_id:
        if direction == "out":
            where += " AND source_node_id=?"
            params.append(node_id)
        elif direction == "in":
            where += " AND target_node_id=?"
            params.append(node_id)
        else:
            where += " AND (source_node_id=? OR target_node_id=?)"
            params.extend([node_id, node_id])
    if edge_types:
        where += f" AND edge_type IN ({','.join('?' for _ in edge_types)})"
        params.extend(edge_types)
    rows = conn.execute(
        f"SELECT edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status,effective_from,effective_to FROM graph_edge {where} ORDER BY edge_type,edge_id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_reporting_edge(row) for row in rows]


def _rulebook_neighbourhood(conn: sqlite3.Connection, node_id: str, *, depth: int, limit: int, edge_types: list[str] | None) -> dict[str, Any]:
    seen_nodes = {node_id}
    seen_edges: dict[str, dict[str, Any]] = {}
    q: deque[tuple[str, int]] = deque([(node_id, 0)])
    while q and len(seen_nodes) < limit:
        current, dist = q.popleft()
        if dist >= depth:
            continue
        edges = _list_rulebook_edges(conn, node_id=current, edge_types=edge_types, direction="both", limit=limit, offset=0)
        for edge in edges:
            seen_edges[edge["edge_id"]] = edge
            other = edge["target_node_id"] if edge["source_node_id"] == current else edge["source_node_id"]
            if other not in seen_nodes and len(seen_nodes) < limit:
                seen_nodes.add(other)
                q.append((other, dist + 1))
    nodes = [unified_node(conn, nid, source="rulebook") for nid in seen_nodes]
    return {"root": node_id, "source": "rulebook", "nodes": [n for n in nodes if n], "edges": list(seen_edges.values())}


def _reporting_neighbourhood(conn: sqlite3.Connection, node_id: str, *, depth: int, limit: int, edge_types: list[str] | None) -> dict[str, Any]:
    seen_nodes = {node_id}
    seen_edges: dict[str, dict[str, Any]] = {}
    q: deque[tuple[str, int]] = deque([(node_id, 0)])
    while q and len(seen_nodes) < limit:
        current, dist = q.popleft()
        if dist >= depth:
            continue
        edges = _list_reporting_edges(conn, node_id=current, edge_types=edge_types, direction="both", limit=limit, offset=0)
        for edge in edges:
            seen_edges[edge["edge_id"]] = edge
            other = edge["target_node_id"] if edge["source_node_id"] == current else edge["source_node_id"]
            if other not in seen_nodes and len(seen_nodes) < limit:
                seen_nodes.add(other)
                q.append((other, dist + 1))
    nodes = [unified_node(conn, nid, source="reporting") for nid in seen_nodes]
    return {"root": node_id, "source": "reporting", "nodes": [n for n in nodes if n], "edges": list(seen_edges.values())}


def _detect_node_source(conn: sqlite3.Connection, node_id: str) -> str | None:
    if conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (node_id,)).fetchone():
        return "reporting"
    if conn.execute("SELECT 1 FROM node WHERE id=?", (node_id,)).fetchone():
        return "rulebook"
    if not node_id.startswith("data_item:") and conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (f"data_item:{node_id.upper()}",)).fetchone():
        return "reporting"
    return None


def _sources(sources: list[str] | None) -> set[str]:
    if not sources:
        return {"rulebook", "reporting"}
    normalised = {s.lower() for s in sources if s}
    if "all" in normalised:
        return {"rulebook", "reporting"}
    return {s for s in normalised if s in {"rulebook", "reporting"}} or {"rulebook", "reporting"}


def _valid_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table)) and bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    )


def _quote_ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid SQLite identifier: {value}")
    return '"' + value.replace('"', '""') + '"'


def _rulebook_node(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": "rulebook",
        "node_id": row["id"],
        "node_type": row["node_type"],
        "stable_key": row["stable_key"],
        "label": row["title"],
        "title": row["title"],
        "text": row["text"],
        "url": row["url"],
        "properties": _json(row["metadata_json"]),
    }


def _reporting_node(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": "reporting",
        "node_id": row["node_id"],
        "node_type": row["node_type"],
        "stable_key": row["source_pk"] or row["node_id"],
        "label": row["label"],
        "title": row["label"],
        "source_table": row["source_table"],
        "source_pk": row["source_pk"],
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
        "review_status": row["review_status"],
        "properties": _json(row["properties_json"]),
    }


def _rulebook_edge(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": "rulebook",
        "edge_id": row["id"],
        "source_node_id": row["from_node_id"],
        "target_node_id": row["to_node_id"],
        "edge_type": row["edge_type"],
        "method": row["source_method"],
        "confidence": row["confidence"],
        "evidence_text": row["evidence_text"],
        "source_url": row["source_url"],
        "properties": _json(row["metadata_json"]),
    }


def _reporting_edge(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": "reporting",
        "edge_id": row["edge_id"],
        "source_node_id": row["source_node_id"],
        "target_node_id": row["target_node_id"],
        "edge_type": row["edge_type"],
        "method": row["extraction_method"],
        "confidence": row["confidence"],
        "evidence_span_id": row["evidence_span_id"],
        "review_status": row["review_status"],
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
        "properties": _json(row["properties_json"]),
    }


def _json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {}
