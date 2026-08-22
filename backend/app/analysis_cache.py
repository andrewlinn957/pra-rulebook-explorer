"""Materialised graph-analysis cache.

Expensive NetworkX computations (centrality, betweenness, components,
communities) are precomputed into the analysis_cache table during
``backend.app.cli stabilize``.  API endpoints read from the cache; if a
requested metric is missing or the graph has changed since it was computed,
the endpoint rebuilds it on demand under a process-wide lock so only one
request ever pays the compute cost.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .graph import betweenness as _betweenness
from .graph import centrality as _centrality
from .graph import communities as _communities
from .graph import components as _components

CACHE_KEYS = ("centrality", "betweenness", "components", "communities")

_lock = threading.Lock()


def graph_fingerprint(conn: sqlite3.Connection) -> str:
    """Cheap fingerprint of the analysis-relevant edge set."""
    row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(length(id)),0)
        FROM edge
        WHERE edge_type NOT IN ('shares_defined_term','has_obligation_pattern','has_version','sourced_from')
        """
    ).fetchone()
    raw = f"{row[0]}:{row[1]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_row(conn: sqlite3.Connection, key: str, fingerprint: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT payload_json, computed_at, node_count, edge_count FROM analysis_cache WHERE cache_key=?",
        (key,),
    ).fetchone()


def read_cached(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM analysis_cache WHERE cache_key=?", (key,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def write_cached(
    conn: sqlite3.Connection,
    key: str,
    payload: dict[str, Any],
    *,
    node_count: int = 0,
    edge_count: int = 0,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO analysis_cache (cache_key, payload_json, computed_at, node_count, edge_count)
        VALUES (?,?,?,?,?)
        ON CONFLICT(cache_key) DO UPDATE SET
          payload_json=excluded.payload_json,
          computed_at=excluded.computed_at,
          node_count=excluded.node_count,
          edge_count=excluded.edge_count
        """,
        (key, json.dumps(payload, ensure_ascii=False, sort_keys=True), now, node_count, edge_count),
    )
    conn.commit()


def precompute_all(conn: sqlite3.Connection) -> dict[str, Any]:
    """Recompute and store every analysis metric. Called from stabilize."""
    results: dict[str, Any] = {}
    results["centrality"] = _centrality(conn, limit=100)
    results["betweenness"] = _betweenness(conn, limit=100, k=250, max_nodes=2000)
    results["components"] = _components(conn, limit=100)
    results["communities"] = _communities(conn, limit=100, max_nodes=2500)
    for key, payload in results.items():
        node_count = payload.get("graph_nodes", 0) or 0
        edge_count = payload.get("graph_edges", 0) or 0
        write_cached(conn, key, payload, node_count=node_count, edge_count=edge_count)
    return {key: {"computed_at": datetime.now(timezone.utc).isoformat()} for key in results}


def get_or_compute(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """Return cached result; rebuild under lock only if absent."""
    cached = read_cached(conn, key)
    if cached is not None:
        return cached

    with _lock:
        # Double-check after acquiring lock: another thread may have finished.
        cached = read_cached(conn, key)
        if cached is not None:
            return cached
        if key == "centrality":
            payload = _centrality(conn, limit=100)
        elif key == "betweenness":
            payload = _betweenness(conn, limit=100, k=250, max_nodes=2000)
        elif key == "components":
            payload = _components(conn, limit=100)
        elif key == "communities":
            payload = _communities(conn, limit=100, max_nodes=2500)
        else:
            raise ValueError(f"unknown analysis key: {key}")
        write_cached(conn, key, payload)
        return payload


def invalidate_all(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM analysis_cache")
    conn.commit()
