from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from .db import DEFAULT_DB, connect, get_node
from .feedback import create_feedback, list_feedback, process_feedback_queue
from .graph import betweenness, centrality, common_neighbours, communities, components, contents_tree, interesting, list_nodes, neighbourhood, search, semantic_map, shortest_path, stats
from .reporting import (
    datapoint_detail,
    list_returns,
    list_templates,
    relationship_evidence,
    reporting_neighbourhood,
    reporting_overview_graph,
    reporting_stats,
    return_detail,
    return_references,
    returns_relying_on,
    search_reporting_nodes,
    search_datapoints,
    template_detail,
)
from .unified import unified_edge, unified_edges, unified_neighbourhood, unified_node, unified_nodes, unified_schema, unified_search, unified_stats, unified_table_rows
from .validation import validation_dashboard

DB_PATH = DEFAULT_DB
PROJECT_ROOT = Path(__file__).resolve().parents[2]
app = FastAPI(title="PRA Rulebook Explorer API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _limit(value: int, cap: int, *, minimum: int = 1) -> int:
    """Clamp user-supplied LIMIT values so SQLite never sees negative LIMITs."""
    return max(minimum, min(value, cap))


def _offset(value: int) -> int:
    return max(value, 0)


@app.on_event("startup")
def startup() -> None:
    # Search/FTS indexes are maintained by the explicit build-indexes command.
    # Avoid rebuilding them during service startup because long embedding rebuilds
    # can hold the SQLite database busy for extended periods.
    return None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": str(DB_PATH), "exists": DB_PATH.exists()}


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


@app.post("/feedback/process")
async def api_process_feedback_queue(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    limit = _limit(int(payload.get("limit", 3)), 10)
    return process_feedback_queue(PROJECT_ROOT, limit=limit)


@app.post("/validation/suspect-403-review")
async def api_suspect_403_review(request: Request) -> dict:
    payload = await request.json()
    target_id = str(payload.get("target_id", "")).strip()
    review_id = str(payload.get("review_id", "")).strip()
    decision = str(payload.get("decision", "")).strip()
    note = str(payload.get("note", "")).strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="target_id is required")
    if decision not in {"valid", "broken", "needs_url_fix", "unsure"}:
        raise HTTPException(status_code=400, detail="decision must be valid, broken, needs_url_fix, or unsure")
    path = PROJECT_ROOT / "outputs/broken-reference-check/suspect-403-review-decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        existing = {}
    existing[target_id] = {"target_id": target_id, "review_id": review_id, "decision": decision, "note": note}
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    return {"ok": True, "target_id": target_id, "decision": decision, "saved": len(existing)}


@app.post("/validation/unresolved-reference-review")
async def api_unresolved_reference_review(request: Request) -> dict:
    payload = await request.json()
    target_id = str(payload.get("target_id", "")).strip()
    edge_id = str(payload.get("edge_id", "")).strip()
    sample_id = str(payload.get("sample_id", "")).strip()
    decision = str(payload.get("decision", "")).strip()
    replacement_url = str(payload.get("replacement_url", "")).strip()
    rulebook_target = str(payload.get("rulebook_target", "")).strip()
    note = str(payload.get("note", "")).strip()
    allowed = {"outdated", "irrelevant", "dead", "rulebook_target", "keep_external"}
    if not target_id:
        raise HTTPException(status_code=400, detail="target_id is required")
    if decision not in allowed:
        raise HTTPException(status_code=400, detail="decision must be outdated, irrelevant, dead, rulebook_target, or keep_external")
    path = PROJECT_ROOT / "outputs/broken-reference-check/unresolved-reference-review-decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        existing = {}
    existing[target_id] = {
        "target_id": target_id,
        "edge_id": edge_id,
        "sample_id": sample_id,
        "decision": decision,
        "replacement_url": replacement_url,
        "rulebook_target": rulebook_target,
        "note": note,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    return {"ok": True, "target_id": target_id, "decision": decision, "saved": len(existing)}


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
    limit: int = 80,
    child_limit: int = 900,
    include_datapoints: bool = False,
) -> dict:
    conn = connect(DB_PATH)
    return reporting_overview_graph(
        conn,
        q=q,
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
    return centrality(conn, limit=_limit(limit, 100))


@app.get("/analysis/semantic-map")
def api_semantic_map(level: str = "part", clusters: int = 12, edge_limit: int = 700) -> dict:
    conn = connect(DB_PATH)
    try:
        return semantic_map(conn, level=level, clusters=_limit(clusters, 50), edge_limit=_limit(edge_limit, 5000, minimum=0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/analysis/betweenness")
def api_betweenness(limit: int = 25, k: int = 100, max_nodes: int = 1000) -> dict:
    conn = connect(DB_PATH)
    return betweenness(conn, limit=_limit(limit, 100), k=_limit(k, 300), max_nodes=_limit(max_nodes, 2000))


@app.get("/analysis/components")
def api_components(limit: int = 20) -> dict:
    conn = connect(DB_PATH)
    return components(conn, limit=_limit(limit, 100))


@app.get("/analysis/communities")
def api_communities(limit: int = 20, max_nodes: int = 2500) -> dict:
    conn = connect(DB_PATH)
    return communities(conn, limit=_limit(limit, 100), max_nodes=_limit(max_nodes, 5000))



@app.get("/analysis/common-neighbours")
def api_common_neighbours(from_id: str, to_id: str, limit: int = 50) -> dict:
    conn = connect(DB_PATH)
    return common_neighbours(conn, from_id, to_id, limit=_limit(limit, 200))
