from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from .db import DEFAULT_DB, connect, ensure_indexes, get_node
from .graph import betweenness, centrality, common_neighbours, communities, components, interesting, list_nodes, neighbourhood, search, shortest_path, stats

app = FastAPI(title="PRA Rulebook Explorer API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = DEFAULT_DB


@app.on_event("startup")
def startup() -> None:
    conn = connect(DB_PATH)
    ensure_indexes(conn)
    conn.close()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": str(DB_PATH), "exists": DB_PATH.exists()}


@app.get("/stats")
def api_stats() -> dict:
    conn = connect(DB_PATH)
    return stats(conn)


@app.get("/search")
def api_search(q: str, types: Annotated[list[str] | None, Query()] = None, limit: int = 25) -> dict:
    conn = connect(DB_PATH)
    return {"query": q, "results": search(conn, q, node_types=types, limit=min(limit, 100))}


@app.get("/nodes")
def api_nodes(types: Annotated[list[str] | None, Query()] = None, limit: int = 500, offset: int = 0) -> dict:
    conn = connect(DB_PATH)
    return {"results": list_nodes(conn, node_types=types, limit=min(limit, 1000), offset=max(offset, 0))}


@app.get("/node/{node_id}")
def api_node(node_id: str) -> dict:
    conn = connect(DB_PATH)
    node = get_node(conn, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@app.get("/node/{node_id}/neighbourhood")
def api_neighbourhood(
    node_id: str,
    depth: int = 1,
    limit: int = 250,
    edge_types: Annotated[list[str] | None, Query()] = None,
    explicit_only: bool = False,
) -> dict:
    conn = connect(DB_PATH)
    if not get_node(conn, node_id):
        raise HTTPException(status_code=404, detail="Node not found")
    return neighbourhood(conn, node_id, depth=min(depth, 3), limit=min(limit, 1000), edge_types=edge_types, explicit_only=explicit_only)


@app.get("/path")
def api_path(request: Request) -> dict:
    source = request.query_params.get("from") or request.query_params.get("from_id")
    target = request.query_params.get("to") or request.query_params.get("to_id")
    if not source or not target:
        raise HTTPException(status_code=400, detail="Provide from/to or from_id/to_id query parameters")
    conn = connect(DB_PATH)
    try:
        return shortest_path(conn, source, target)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Path not found: {exc}")


@app.get("/interesting")
def api_interesting(limit: int = 50) -> dict:
    conn = connect(DB_PATH)
    return {"results": interesting(conn, limit=min(limit, 200))}


@app.get("/centrality")
def api_centrality(limit: int = 25) -> dict:
    conn = connect(DB_PATH)
    return centrality(conn, limit=min(limit, 100))


@app.get("/analysis/betweenness")
def api_betweenness(limit: int = 25, k: int = 100, max_nodes: int = 1000) -> dict:
    conn = connect(DB_PATH)
    return betweenness(conn, limit=min(limit, 100), k=min(k, 300), max_nodes=min(max_nodes, 2000))


@app.get("/analysis/components")
def api_components(limit: int = 20) -> dict:
    conn = connect(DB_PATH)
    return components(conn, limit=min(limit, 100))


@app.get("/analysis/communities")
def api_communities(limit: int = 20, max_nodes: int = 2500) -> dict:
    conn = connect(DB_PATH)
    return communities(conn, limit=min(limit, 100), max_nodes=min(max_nodes, 5000))


@app.get("/analysis/common-neighbours")
def api_common_neighbours(from_id: str, to_id: str, limit: int = 50) -> dict:
    conn = connect(DB_PATH)
    return common_neighbours(conn, from_id, to_id, limit=min(limit, 200))
