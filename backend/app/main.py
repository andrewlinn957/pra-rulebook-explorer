from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .db import DEFAULT_DB, connect, ensure_indexes, get_node
from .feedback import create_feedback, list_feedback
from .analysis_cache import get_or_compute
from .graph import common_neighbours, contents_tree, interesting, list_nodes, neighbourhood, reader_bundle, search, semantic_map, shortest_path, stats
from .reporting import (
    datapoint_detail,
    list_returns,
    list_templates,
    relationship_evidence,
    reporting_neighbourhood,
    reporting_catalog,
    reporting_catalog_cells,
    reporting_catalog_return,
    reporting_change_impact,
    reporting_overview_graph,
    reporting_stats,
    reporting_template_document_path,
    reporting_template_layout,
    return_detail,
    return_references,
    returns_relying_on,
    search_reporting_nodes,
    search_datapoints,
    template_detail,
)
from .unified import unified_edge, unified_edges, unified_neighbourhood, unified_node, unified_nodes, unified_schema, unified_search, unified_stats, unified_table_rows
from .validation import validation_dashboard
from .migrations import apply_migrations, schema_version

DB_PATH = DEFAULT_DB
PROJECT_ROOT = Path(__file__).resolve().parents[2]
app = FastAPI(title="PRA Rulebook Explorer API", version="0.2.0")
allowed_origins = [
    origin.strip()
    for origin in os.environ.get(
        "PRA_RULEBOOK_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


def _limit(value: int, cap: int, *, minimum: int = 1) -> int:
    """Clamp user-supplied LIMIT values so SQLite never sees negative LIMITs."""
    return max(minimum, min(value, cap))


def _offset(value: int) -> int:
    return max(value, 0)


@app.on_event("startup")
def startup() -> None:
    conn = connect(DB_PATH)
    try:
        apply_migrations(conn)
        dirty_row = conn.execute(
            "SELECT dirty FROM search_projection_state WHERE singleton=1"
        ).fetchone()
        if dirty_row is None or dirty_row[0]:
            ensure_indexes(conn)
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    conn = connect(DB_PATH)
    try:
        version = schema_version(conn)
    finally:
        conn.close()
    return {"ok": True, "db": str(DB_PATH), "exists": DB_PATH.exists(), "schema_version": version}


@app.get("/stats")
def api_stats() -> dict:
    conn = connect(DB_PATH)
    return stats(conn)


@app.get("/validation/dashboard")
def api_validation_dashboard() -> dict:
    conn = connect(DB_PATH)
    return validation_dashboard(conn)


@app.get("/feedback")
def api_feedback_queue() -> dict:
    return list_feedback(PROJECT_ROOT)


@app.post("/feedback/node")
async def api_node_feedback(request: Request) -> dict:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    node = payload.get("node") or {}
    feedback = str(payload.get("feedback", ""))
    page_url = str(payload.get("page_url", ""))
    try:
        item = create_feedback(PROJECT_ROOT, node=node, feedback=feedback, page_url=page_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@app.get("/unified/stats")
def api_unified_stats() -> dict:
    conn = connect(DB_PATH)
    return unified_stats(conn)


@app.get("/unified/schema")
def api_unified_schema() -> dict:
    conn = connect(DB_PATH)
    return unified_schema(conn)


@app.get("/unified/tables/{table}")
def api_unified_table(table: str, q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    conn = connect(DB_PATH)
    result = unified_table_rows(conn, table, q=q, limit=_limit(limit, 1000), offset=_offset(offset))
    if result is None:
        raise HTTPException(status_code=404, detail="Table not found")
    return result


@app.get("/unified/search")
def api_unified_search(
    q: str,
    sources: Annotated[list[str] | None, Query()] = None,
    types: Annotated[list[str] | None, Query()] = None,
    limit: int = 50,
) -> dict:
    conn = connect(DB_PATH)
    return {"query": q, "results": unified_search(conn, q, sources=sources, node_types=types, limit=_limit(limit, 200))}


@app.get("/unified/nodes")
def api_unified_nodes(
    sources: Annotated[list[str] | None, Query()] = None,
    types: Annotated[list[str] | None, Query()] = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    conn = connect(DB_PATH)
    return {"results": unified_nodes(conn, sources=sources, node_types=types, q=q, limit=_limit(limit, 1000), offset=_offset(offset))}


@app.get("/unified/nodes/{node_id:path}")
def api_unified_node(node_id: str, source: str | None = None) -> dict:
    conn = connect(DB_PATH)
    result = unified_node(conn, node_id, source=source)
    if not result:
        raise HTTPException(status_code=404, detail="Node not found")
    return result


@app.get("/unified/edges")
def api_unified_edges(
    node_id: str | None = None,
    sources: Annotated[list[str] | None, Query()] = None,
    edge_types: Annotated[list[str] | None, Query()] = None,
    direction: str = "both",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    if direction not in {"in", "out", "both"}:
        raise HTTPException(status_code=400, detail="direction must be one of: in, out, both")
    conn = connect(DB_PATH)
    return {"results": unified_edges(conn, node_id=node_id, sources=sources, edge_types=edge_types, direction=direction, limit=_limit(limit, 1000), offset=_offset(offset))}


@app.get("/unified/edges/{edge_id:path}")
def api_unified_edge(edge_id: str, source: str | None = None) -> dict:
    conn = connect(DB_PATH)
    result = unified_edge(conn, edge_id, source=source)
    if not result:
        raise HTTPException(status_code=404, detail="Edge not found")
    return result


@app.get("/unified/neighbourhood/{node_id:path}")
def api_unified_neighbourhood(
    node_id: str,
    source: str | None = None,
    depth: int = 1,
    limit: int = 250,
    edge_types: Annotated[list[str] | None, Query()] = None,
) -> dict:
    conn = connect(DB_PATH)
    result = unified_neighbourhood(conn, node_id, source=source, depth=_limit(depth, 3), limit=_limit(limit, 1000), edge_types=edge_types)
    if not result:
        raise HTTPException(status_code=404, detail="Node not found")
    return result



@app.get("/reporting/stats")
def api_reporting_stats() -> dict:
    conn = connect(DB_PATH)
    return reporting_stats(conn)


@app.get("/reporting/catalog")
def api_reporting_catalog(
    q: str | None = None,
    estate: str | None = None,
    include_historic: bool = False,
) -> dict:
    conn = connect(DB_PATH)
    try:
        return reporting_catalog(conn, q=q, estate=estate, include_historic=include_historic)
    finally:
        conn.close()


@app.get("/reporting/catalog/{return_id}")
def api_reporting_catalog_return(return_id: str) -> dict:
    conn = connect(DB_PATH)
    try:
        result = reporting_catalog_return(conn, return_id)
    finally:
        conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Reporting catalogue entry not found")
    return result


@app.get("/reporting/catalog/{return_id}/cells")
def api_reporting_catalog_cells(
    return_id: str,
    q: str | None = None,
    template_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    conn = connect(DB_PATH)
    try:
        result = reporting_catalog_cells(
            conn,
            return_id,
            q=q,
            template_id=template_id,
            limit=_limit(limit, 500),
            offset=_offset(offset),
        )
    finally:
        conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Reporting catalogue entry not found")
    return result


@app.get("/reporting/templates/{template_id}/layout")
def api_reporting_template_layout(template_id: str) -> dict:
    conn = connect(DB_PATH)
    try:
        result = reporting_template_layout(
            conn,
            template_id,
            project_root=PROJECT_ROOT,
        )
    finally:
        conn.close()
    if result is None:
        raise HTTPException(status_code=404, detail="Workbook layout not available")
    return result


@app.get("/reporting/templates/{template_id}/document")
def api_reporting_template_document(template_id: str) -> FileResponse:
    conn = connect(DB_PATH)
    try:
        path = reporting_template_document_path(
            conn,
            template_id,
            project_root=PROJECT_ROOT,
        )
    finally:
        conn.close()
    if path is None or path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="PDF template not available")
    return FileResponse(
        path,
        media_type="application/pdf",
        content_disposition_type="inline",
        filename=path.name,
    )


@app.get("/reporting/impact/{target_node_id:path}")
def api_reporting_change_impact(
    target_node_id: str,
    include_historic: bool = False,
    sample_cells: int = 8,
    limit: int = 200,
) -> dict:
    conn = connect(DB_PATH)
    try:
        result = reporting_change_impact(
            conn,
            target_node_id,
            include_historic=include_historic,
            sample_cells=_limit(sample_cells, 50),
            limit=_limit(limit, 1000),
        )
    finally:
        conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Rulebook or reporting graph node not found")
    return result


@app.get("/reporting/returns")
def api_reporting_returns(q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    conn = connect(DB_PATH)
    return {"results": list_returns(conn, q=q, limit=_limit(limit, 500), offset=_offset(offset))}


@app.get("/reporting/returns/{code}")
def api_reporting_return(code: str) -> dict:
    conn = connect(DB_PATH)
    result = return_detail(conn, code)
    if not result:
        raise HTTPException(status_code=404, detail="Reporting return/data item not found")
    return result


@app.get("/reporting/returns/{code}/references")
def api_reporting_return_references(
    code: str,
    edge_types: Annotated[list[str] | None, Query()] = None,
    limit: int = 500,
) -> dict:
    conn = connect(DB_PATH)
    result = return_references(conn, code, edge_types=edge_types, limit=_limit(limit, 2000))
    if result is None:
        raise HTTPException(status_code=404, detail="Reporting return/data item not found")
    return result


@app.get("/reporting/templates")
def api_reporting_templates(q: str | None = None, data_item: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    conn = connect(DB_PATH)
    return {"results": list_templates(conn, q=q, data_item=data_item, limit=_limit(limit, 500), offset=_offset(offset))}


@app.get("/reporting/templates/{template_id:path}")
def api_reporting_template(template_id: str) -> dict:
    conn = connect(DB_PATH)
    result = template_detail(conn, template_id)
    if not result:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@app.get("/reporting/nodes/search")
def api_reporting_node_search(q: str, types: Annotated[list[str] | None, Query()] = None, limit: int = 50) -> dict:
    conn = connect(DB_PATH)
    return {"query": q, "results": search_reporting_nodes(conn, q=q, node_types=types, limit=_limit(limit, 200))}


@app.get("/reporting/datapoints/search")
def api_reporting_datapoint_search(q: str, template: str | None = None, data_item: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    conn = connect(DB_PATH)
    return {"query": q, "results": search_datapoints(conn, q=q, template=template, data_item=data_item, limit=_limit(limit, 200), offset=_offset(offset))}


@app.get("/reporting/datapoints/{datapoint_id:path}")
def api_reporting_datapoint(datapoint_id: str) -> dict:
    conn = connect(DB_PATH)
    result = datapoint_detail(conn, datapoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Data point not found")
    return result


@app.get("/reporting/references-to/{target_node_id:path}/returns")
def api_reporting_returns_relying_on(target_node_id: str, limit: int = 200) -> dict:
    conn = connect(DB_PATH)
    return returns_relying_on(conn, target_node_id, limit=_limit(limit, 1000))


@app.get("/reporting/rules/{target_node_id:path}/returns")
@app.get("/reporting/provisions/{target_node_id:path}/returns")
def api_reporting_rule_returns(target_node_id: str, limit: int = 200) -> dict:
    conn = connect(DB_PATH)
    return returns_relying_on(conn, target_node_id, limit=_limit(limit, 1000))


@app.get("/reporting/relationships/{edge_id}/evidence")
def api_reporting_relationship_evidence(edge_id: str) -> dict:
    conn = connect(DB_PATH)
    result = relationship_evidence(conn, edge_id)
    if not result:
        raise HTTPException(status_code=404, detail="Reporting relationship not found")
    return result


@app.get("/reporting/graph/overview")
def api_reporting_graph_overview(
    q: str | None = None,
    selected_return: str | None = None,
    limit: int = 80,
    child_limit: int = 900,
    include_datapoints: bool = False,
) -> dict:
    conn = connect(DB_PATH)
    return reporting_overview_graph(
        conn,
        q=q,
        selected_return=selected_return,
        limit=_limit(limit, 200),
        child_limit=_limit(child_limit, 2000),
        include_datapoints=include_datapoints,
    )


@app.get("/reporting/graph/neighbourhood/{node_id:path}")
def api_reporting_graph_neighbourhood(
    node_id: str,
    depth: int = 1,
    limit: int = 250,
    edge_types: Annotated[list[str] | None, Query()] = None,
) -> dict:
    conn = connect(DB_PATH)
    result = reporting_neighbourhood(conn, node_id, depth=_limit(depth, 3), limit=_limit(limit, 1000), edge_types=edge_types)
    if not result:
        raise HTTPException(status_code=404, detail="Reporting graph node not found")
    return result


@app.get("/search")
def api_search(q: str, types: Annotated[list[str] | None, Query()] = None, limit: int = 25) -> dict:
    conn = connect(DB_PATH)
    return {"query": q, "results": search(conn, q, node_types=types, limit=_limit(limit, 100))}


@app.get("/nodes")
def api_nodes(types: Annotated[list[str] | None, Query()] = None, limit: int = 500, offset: int = 0) -> dict:
    conn = connect(DB_PATH)
    return {"results": list_nodes(conn, node_types=types, limit=_limit(limit, 1000), offset=_offset(offset))}


@app.get("/node/{node_id}")
def api_node(node_id: str) -> dict:
    conn = connect(DB_PATH)
    node = get_node(conn, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@app.get("/node/{node_id}/contents")
def api_contents(node_id: str) -> dict:
    conn = connect(DB_PATH)
    try:
        return contents_tree(conn, node_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Node not found")


@app.get("/node/{node_id}/reader")
def api_reader(node_id: str, reference_depth: int = 1) -> dict:
    conn = connect(DB_PATH)
    try:
        return reader_bundle(
            conn,
            node_id,
            reference_depth=_limit(reference_depth, 3),
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Node not found")


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
    return neighbourhood(conn, node_id, depth=_limit(depth, 3), limit=_limit(limit, 1000), edge_types=edge_types, explicit_only=explicit_only)


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
    return {"results": interesting(conn, limit=_limit(limit, 200))}


@app.get("/centrality")
def api_centrality(limit: int = 25) -> dict:
    conn = connect(DB_PATH)
    payload = get_or_compute(conn, "centrality")
    return {"degree": payload["degree"][:_limit(limit, 100)]}


@app.get("/analysis/semantic-map")
def api_semantic_map(level: str = "part", clusters: int = 12, edge_limit: int = 700) -> dict:
    conn = connect(DB_PATH)
    try:
        return semantic_map(conn, level=level, clusters=_limit(clusters, 50), edge_limit=_limit(edge_limit, 5000, minimum=0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/analysis/betweenness")
def api_betweenness(limit: int = 25) -> dict:
    conn = connect(DB_PATH)
    payload = get_or_compute(conn, "betweenness")
    return {**{k2: v2 for k2, v2 in payload.items() if k2 != "results"}, "results": payload["results"][:_limit(limit, 100)]}


@app.get("/analysis/components")
def api_components(limit: int = 20) -> dict:
    conn = connect(DB_PATH)
    payload = get_or_compute(conn, "components")
    return {"component_count": payload["component_count"], "largest_size": payload["largest_size"], "components": payload["components"][:_limit(limit, 100)]}


@app.get("/analysis/communities")
def api_communities(limit: int = 20) -> dict:
    conn = connect(DB_PATH)
    payload = get_or_compute(conn, "communities")
    return {**{k2: v2 for k2, v2 in payload.items() if k2 != "communities"}, "communities": payload["communities"][:_limit(limit, 100)]}



@app.get("/analysis/common-neighbours")
def api_common_neighbours(from_id: str, to_id: str, limit: int = 50) -> dict:
    conn = connect(DB_PATH)
    return common_neighbours(conn, from_id, to_id, limit=_limit(limit, 200))
