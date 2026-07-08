from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from typing import Any

REPORTING_REFERENCE_EDGE_TYPES = {
    "REFERENCES_RULE",
    "REFERENCES_SOURCE",
    "REFERENCES_EXTERNAL",
    "REFERENCES_RETURN",
    "REFERENCES_TEMPLATE",
}

REPORTING_OVERVIEW_CHILD_EDGES = {
    "USES_TEMPLATE",
    "USES_INSTRUCTIONS",
    "EVIDENCED_BY",
    "LEGAL_BASIS",
    "APPLIES_TO",
    "HAS_SCOPE_RULE",
    "MAY_BE_AFFECTED_BY_PERMISSION",
}

REPORTING_OVERVIEW_REFERENCE_EDGES = REPORTING_REFERENCE_EDGE_TYPES | {"REFERENCES_TEMPLATE"}


def reporting_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "nodes_by_type": dict(conn.execute("SELECT node_type, COUNT(*) FROM graph_node GROUP BY node_type ORDER BY node_type").fetchall()),
        "edges_by_type": dict(conn.execute("SELECT edge_type, COUNT(*) FROM graph_edge GROUP BY edge_type ORDER BY edge_type").fetchall()),
        "reporting_reference_edges": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE extraction_method='reporting_llm_reference'").fetchone()[0],
        "llm_reference_resolution": dict(
            conn.execute(
                "SELECT resolver_method, COUNT(*) FROM reporting_llm_reference_resolution GROUP BY resolver_method ORDER BY resolver_method"
            ).fetchall()
        ),
    }


def reporting_overview_graph(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    selected_return: str | None = None,
    limit: int = 80,
    child_limit: int = 900,
    include_datapoints: bool = False,
) -> dict[str, Any]:
    """Build a reporting-first graph for the UI.

    DataItem nodes are the top-level parents. The default graph includes their
    main reporting artefacts and source-document cross-references, but avoids
    the full datapoint explosion unless explicitly requested.
    """
    ensure_reporting_graph_indexes(conn)
    roots = _reporting_root_data_items(conn, q=selected_return or q, limit=limit, exact=bool(selected_return))
    root_ids = [r["node_id"] for r in roots]
    nodes: dict[str, dict[str, Any]] = {r["node_id"]: _ui_reporting_node(r, role="return") for r in roots}
    edges: dict[str, dict[str, Any]] = {}
    if root_ids and selected_return:
        child_edges = _reporting_edges_for_sources(conn, root_ids, sorted(REPORTING_OVERVIEW_CHILD_EDGES), child_limit)
        child_edges = _filter_current_reporting_source_documents(conn, child_edges)
        _add_reporting_edges(conn, child_edges, nodes, edges)

        source_ids = [e["target_node_id"] for e in child_edges if e["edge_type"] == "EVIDENCED_BY"]
        if source_ids:
            reference_edges = _reporting_edges_for_sources(conn, source_ids, sorted(REPORTING_OVERVIEW_REFERENCE_EDGES), child_limit)
            _add_reporting_edges(conn, reference_edges, nodes, edges)

        if include_datapoints:
            template_ids = [e["target_node_id"] for e in child_edges if e["edge_type"] == "USES_TEMPLATE"]
            if template_ids:
                _add_grouped_datapoints(conn, template_ids, nodes, edges)

    available: dict[str, int] = {}
    for edge in edges.values():
        available[edge["edge_type"]] = available.get(edge["edge_type"], 0) + 1
    return {
        "level": "reporting_overview",
        "root_count": len(roots),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "available_edge_types": available,
    }


def ensure_reporting_graph_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edge_source_type ON graph_edge(source_node_id, edge_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edge_target_type ON graph_edge(target_node_id, edge_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_node_type_label ON graph_node(node_type, label)")


def _reporting_root_data_items(conn: sqlite3.Connection, *, q: str | None, limit: int, exact: bool = False) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = "WHERE n.node_type='DataItem'"
    if q:
        if exact:
            where += " AND (n.node_id=? OR n.node_id=? OR n.label=? OR n.source_pk=?)"
            code = q.removeprefix("data_item:")
            params.extend([q, f"data_item:{code}", code, q])
        else:
            where += " AND (n.node_id LIKE ? OR n.label LIKE ? OR n.properties_json LIKE ?)"
            needle = f"%{q}%"
            params.extend([needle, needle, needle])
    rows = conn.execute(
        f"""
        SELECT n.node_id,n.node_type,n.label,n.source_table,n.source_pk,n.properties_json,n.effective_from,n.effective_to,n.review_status,
               COUNT(DISTINCT CASE WHEN e.edge_type='USES_TEMPLATE' THEN e.target_node_id END) AS template_count,
               COUNT(DISTINCT CASE WHEN e.edge_type='USES_INSTRUCTIONS' THEN e.target_node_id END) AS instruction_count,
               COUNT(DISTINCT CASE WHEN e.edge_type='EVIDENCED_BY' THEN e.target_node_id END) AS source_document_count
        FROM graph_node n
        LEFT JOIN graph_edge e ON e.source_node_id=n.node_id
        {where}
        GROUP BY n.node_id
        ORDER BY n.label
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return _enrich_reporting_nodes(conn, [_graph_node(row) for row in rows])


def _reporting_edges_for_sources(conn: sqlite3.Connection, source_ids: list[str], edge_types: list[str], limit: int) -> list[dict[str, Any]]:
    if not source_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status
        FROM graph_edge
        WHERE source_node_id IN ({','.join('?' for _ in source_ids)})
          AND edge_type IN ({','.join('?' for _ in edge_types)})
        ORDER BY CASE edge_type
          WHEN 'USES_TEMPLATE' THEN 1 WHEN 'USES_INSTRUCTIONS' THEN 2 WHEN 'EVIDENCED_BY' THEN 3
          WHEN 'LEGAL_BASIS' THEN 4 WHEN 'REFERENCES_RULE' THEN 5 WHEN 'REFERENCES_RETURN' THEN 6 ELSE 20 END,
          confidence DESC, target_node_id
        LIMIT ?
        """,
        [*source_ids, *edge_types, limit],
    ).fetchall()
    return [_graph_edge(row) for row in rows]


def _add_reporting_edges(conn: sqlite3.Connection, rows: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], edges: dict[str, dict[str, Any]]) -> None:
    missing_ids = sorted({node_id for row in rows for node_id in (row["source_node_id"], row["target_node_id"]) if node_id not in nodes})
    if missing_ids:
        fetched = _get_graph_nodes(conn, missing_ids)
        for node in fetched:
            nodes[node["node_id"]] = _ui_reporting_node(node)
    for row in rows:
        if row["source_node_id"] in nodes and row["target_node_id"] in nodes:
            edges[row["edge_id"]] = _ui_reporting_edge(row)


def _filter_current_reporting_source_documents(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hide superseded source-document versions from selected-return graphs.

    Some PRA reporting pages retain historical PDFs alongside the current one.
    For versioned Q&A documents, showing every superseded file creates duplicate
    evidence nodes and makes the graph look contradictory. Keep the highest
    explicit version in each Q&A document family and drop unversioned/older
    versions from the visible reporting graph.
    """
    source_ids = sorted({r["target_node_id"] for r in rows if r.get("edge_type") == "EVIDENCED_BY"})
    if not source_ids:
        return rows
    try:
        meta_rows = conn.execute(
            f"""
            SELECT n.node_id,n.label,n.properties_json,sd.title AS source_title,sd.url AS source_url,sd.file_type AS source_file_type
            FROM graph_node n
            LEFT JOIN source_document sd ON sd.source_id=n.source_pk OR sd.source_id=n.node_id
            WHERE n.node_id IN ({','.join('?' for _ in source_ids)})
              AND n.node_type='SourceDocument'
            """,
            source_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return rows

    families: dict[str, list[tuple[int, str]]] = {}
    for row in meta_rows:
        props = _json(row["properties_json"] or "{}")
        title = str(row["source_title"] or row["label"] or props.get("title") or "")
        url = str(row["source_url"] or props.get("url") or "")
        family = _versioned_q_and_a_family(title, url)
        if not family:
            continue
        families.setdefault(family, []).append((_source_document_version(url), row["node_id"]))

    drop: set[str] = set()
    for versions in families.values():
        if len(versions) <= 1:
            continue
        current_version = max(version for version, _ in versions)
        current_ids = {node_id for version, node_id in versions if version == current_version}
        drop.update(node_id for _, node_id in versions if node_id not in current_ids)
    if not drop:
        return rows
    return [r for r in rows if r.get("target_node_id") not in drop and r.get("source_node_id") not in drop]


def _versioned_q_and_a_family(title: str, url: str) -> str:
    hay = f"{title} {url}".lower().replace("&amp;", "&")
    if "q&a" not in hay and "q-and-a" not in hay and "q-and-as" not in hay:
        return ""
    path = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    basename = path.rsplit("/", 1)[-1].lower()
    if not basename:
        return re.sub(r"\s+", " ", title.lower()).strip()
    return re.sub(r"-v\d+(?=\.[a-z0-9]+$)", "", basename)


def _source_document_version(url: str) -> int:
    match = re.search(r"-v(\d+)(?=\.[a-z0-9]+(?:[?#]|$))", url.lower())
    return int(match.group(1)) if match else 0


def _get_graph_nodes(conn: sqlite3.Connection, node_ids: list[str]) -> list[dict[str, Any]]:
    if not node_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status
        FROM graph_node
        WHERE node_id IN ({','.join('?' for _ in node_ids)})
        """,
        node_ids,
    ).fetchall()
    return _enrich_reporting_nodes(conn, [_graph_node(row) for row in rows])


def _add_grouped_datapoints(conn: sqlite3.Connection, template_ids: list[str], nodes: dict[str, dict[str, Any]], edges: dict[str, dict[str, Any]]) -> None:
    if not template_ids:
        return
    rows = conn.execute(
        f"""
        SELECT e.source_node_id AS template_id,
               COUNT(*) AS datapoint_count
        FROM graph_edge e
        WHERE e.source_node_id IN ({','.join('?' for _ in template_ids)})
          AND e.edge_type='HAS_DATAPOINT'
        GROUP BY e.source_node_id
        """,
        template_ids,
    ).fetchall()
    for row in rows:
        count = int(row["datapoint_count"] or 0)
        if count <= 0:
            continue
        template_id = row["template_id"]
        group_id = f"datapoint_group:{template_id}"
        labels = _sample_datapoint_labels(conn, template_id, limit=8)
        nodes[group_id] = {
            "id": group_id,
            "node_type": "DataPointGroup",
            "stable_key": group_id,
            "title": f"{count:,} datapoints",
            "text": f"Datapoints reported through {nodes.get(template_id, {}).get('title', template_id)}",
            "url": "",
            "metadata": {
                "reporting_role": "datapoint_summary",
                "template_id": template_id,
                "datapoint_count": count,
                "sample_datapoints": labels,
            },
            "degree": max(1, min(50, count)),
        }
        edge_id = f"summary:{template_id}:datapoints"
        edges[edge_id] = {
            "id": edge_id,
            "from_node_id": template_id,
            "to_node_id": group_id,
            "edge_type": "SUMMARISES_DATAPOINTS",
            "source_method": "reporting_datapoint_summary",
            "confidence": 1,
            "evidence_text": f"{count:,} datapoints grouped for screen readability",
            "source_url": "",
            "metadata": {"datapoint_count": count, "sample_datapoints": labels},
        }


def _sample_datapoint_labels(conn: sqlite3.Connection, template_id: str, *, limit: int = 8) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT dp.label
        FROM graph_edge e
        JOIN graph_node dp ON dp.node_id=e.target_node_id
        WHERE e.source_node_id=? AND e.edge_type='HAS_DATAPOINT'
          AND COALESCE(dp.label,'') <> ''
        ORDER BY dp.label
        LIMIT ?
        """,
        (template_id, limit),
    ).fetchall()
    return [row["label"] for row in rows]


def _enrich_reporting_nodes(conn: sqlite3.Connection, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach canonical reporting source metadata to graph nodes.

    The graph_node table is deliberately generic and some imported reporting
    nodes have empty properties_json. Source template URLs live in the canonical
    template/source_document tables, so the reporting API must join them back in
    before building the UI graph.
    """
    if not nodes:
        return nodes
    template_ids = sorted({(n.get("source_pk") or n.get("node_id")) for n in nodes if n.get("node_type") == "Template"})
    source_ids = sorted({(n.get("source_pk") or n.get("node_id")) for n in nodes if n.get("node_type") == "SourceDocument"})

    template_meta: dict[str, dict[str, Any]] = {}
    if template_ids:
        try:
            rows = conn.execute(
                f"""
                SELECT t.template_id,t.template_code,t.title,t.annex,t.source_id,
                       sd.title AS source_title,sd.url AS source_url,sd.local_path AS source_local_path,
                       sd.file_type AS source_file_type,sd.parent_url AS source_parent_url,
                       sd.source_status,sd.downloaded_at,sd.publication_date
                FROM template t
                LEFT JOIN source_document sd ON sd.source_id=t.source_id
                WHERE t.template_id IN ({','.join('?' for _ in template_ids)})
                """,
                template_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            d = dict(row)
            template_meta[d["template_id"]] = {k: v for k, v in d.items() if v is not None}

    source_meta: dict[str, dict[str, Any]] = {}
    if source_ids:
        try:
            rows = conn.execute(
                f"""
                SELECT source_id,title AS source_title,url AS source_url,local_path AS source_local_path,
                       file_type AS source_file_type,parent_url AS source_parent_url,
                       source_status,downloaded_at,publication_date
                FROM source_document
                WHERE source_id IN ({','.join('?' for _ in source_ids)})
                """,
                source_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            d = dict(row)
            source_meta[d["source_id"]] = {k: v for k, v in d.items() if v is not None}

    for node in nodes:
        props = dict(node.get("properties") or {})
        if node.get("node_type") == "Template":
            props = template_meta.get(node.get("source_pk") or node.get("node_id"), {}) | props
        elif node.get("node_type") == "SourceDocument":
            props = source_meta.get(node.get("source_pk") or node.get("node_id"), {}) | props
        node["properties"] = props
    return nodes


def _ui_reporting_node(node: dict[str, Any], *, role: str | None = None) -> dict[str, Any]:
    props = node.get("properties") or {}
    text_parts = [props.get("title"), props.get("reporting_domain"), props.get("submission_system")]
    return {
        "id": node["node_id"],
        "node_type": node["node_type"],
        "stable_key": node.get("source_pk") or node["node_id"],
        "title": node.get("label") or node["node_id"],
        "text": " · ".join(str(p) for p in text_parts if p),
        "url": props.get("url") or props.get("source_url") or props.get("source_parent_url") or "",
        "metadata": props | {
            "source_table": node.get("source_table"),
            "source_pk": node.get("source_pk"),
            "reporting_role": role or node["node_type"],
        },
        "degree": int((node.get("template_count") or 0) + (node.get("instruction_count") or 0) + (node.get("source_document_count") or 0) or 1),
    }


def _ui_reporting_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": edge["edge_id"],
        "from_node_id": edge["source_node_id"],
        "to_node_id": edge["target_node_id"],
        "edge_type": edge["edge_type"],
        "source_method": edge.get("extraction_method") or "reporting_graph",
        "confidence": edge.get("confidence") if edge.get("confidence") is not None else 1,
        "evidence_text": (edge.get("properties") or {}).get("evidence_quote") or "",
        "source_url": "",
        "metadata": edge.get("properties") or {},
    }


def list_returns(conn: sqlite3.Connection, *, q: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    where = "WHERE n.node_type='DataItem'"
    params: list[Any] = []
    if q:
        where += " AND (n.node_id LIKE ? OR n.label LIKE ? OR n.properties_json LIKE ?)"
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    rows = conn.execute(
        f"""
        SELECT n.node_id,n.node_type,n.label,n.source_table,n.source_pk,n.properties_json,
               COUNT(DISTINCT CASE WHEN e.edge_type='USES_TEMPLATE' THEN e.target_node_id END) AS template_count,
               COUNT(DISTINCT CASE WHEN e.edge_type='EVIDENCED_BY' THEN e.target_node_id END) AS source_document_count
        FROM graph_node n
        LEFT JOIN graph_edge e ON e.source_node_id=n.node_id
        {where}
        GROUP BY n.node_id
        ORDER BY n.label
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [_graph_node(row) for row in rows]


def return_detail(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    data_item_id = _data_item_id(code)
    data_item = get_graph_node(conn, data_item_id)
    if not data_item:
        return None
    obligations = [dict(r) for r in conn.execute("SELECT * FROM reporting_obligation WHERE UPPER(data_item_code)=UPPER(?) ORDER BY title", (code,)).fetchall()]
    templates = _adjacent_nodes(conn, data_item_id, edge_types=["USES_TEMPLATE"], direction="out", limit=500)
    instructions = _adjacent_nodes(conn, data_item_id, edge_types=["USES_INSTRUCTIONS"], direction="out", limit=100)
    source_documents = _adjacent_nodes(conn, data_item_id, edge_types=["EVIDENCED_BY"], direction="out", limit=500)
    return {
        "data_item": data_item,
        "reporting_obligations": obligations,
        "templates": templates,
        "instruction_sets": instructions,
        "source_documents": source_documents,
        "reference_summary": _return_reference_summary(conn, data_item_id),
    }


def list_templates(conn: sqlite3.Connection, *, q: str | None = None, data_item: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = "WHERE n.node_type='Template'"
    if q:
        where += " AND (n.node_id LIKE ? OR n.label LIKE ? OR n.properties_json LIKE ?)"
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    if data_item:
        where += " AND EXISTS (SELECT 1 FROM graph_edge e WHERE e.edge_type='USES_TEMPLATE' AND e.target_node_id=n.node_id AND e.source_node_id=?)"
        params.append(_data_item_id(data_item))
    rows = conn.execute(
        f"""
        SELECT n.node_id,n.node_type,n.label,n.source_table,n.source_pk,n.properties_json,
               COUNT(DISTINCT dp.target_node_id) AS datapoint_count,
               COUNT(DISTINCT r.source_node_id) AS data_item_count
        FROM graph_node n
        LEFT JOIN graph_edge dp ON dp.source_node_id=n.node_id AND dp.edge_type='HAS_DATAPOINT'
        LEFT JOIN graph_edge r ON r.target_node_id=n.node_id AND r.edge_type='USES_TEMPLATE'
        {where}
        GROUP BY n.node_id
        ORDER BY n.label
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [_graph_node(row) for row in rows]


def search_reporting_nodes(conn: sqlite3.Connection, *, q: str, node_types: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = "WHERE (node_id LIKE ? OR label LIKE ? OR properties_json LIKE ?)"
    needle = f"%{q}%"
    params.extend([needle, needle, needle])
    if node_types:
        where += f" AND node_type IN ({','.join('?' for _ in node_types)})"
        params.extend(node_types)
    rows = conn.execute(
        f"""
        SELECT node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status
        FROM graph_node
        {where}
        ORDER BY CASE node_type
          WHEN 'DataItem' THEN 1 WHEN 'ReportingObligation' THEN 2 WHEN 'Template' THEN 3
          WHEN 'DataPoint' THEN 4 WHEN 'Provision' THEN 5 WHEN 'SourceDocument' THEN 6 ELSE 20 END,
          label
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [_graph_node(row) for row in rows]


def template_detail(conn: sqlite3.Connection, template_id_or_code: str) -> dict[str, Any] | None:
    template_id = _template_id(conn, template_id_or_code)
    node = get_graph_node(conn, template_id)
    if not node:
        return None
    template_row = conn.execute("SELECT * FROM template WHERE template_id=?", (node.get("source_pk") or template_id,)).fetchone()
    rows = [dict(r) for r in conn.execute("SELECT * FROM template_row WHERE template_id=? ORDER BY COALESCE(row_order, 999999), row_code LIMIT 1000", (node.get("source_pk") or template_id,)).fetchall()]
    columns = [dict(r) for r in conn.execute("SELECT * FROM template_column WHERE template_id=? ORDER BY COALESCE(column_order, 999999), column_code LIMIT 1000", (node.get("source_pk") or template_id,)).fetchall()]
    datapoints = _template_datapoints(conn, node["node_id"], limit=200)
    data_items = _adjacent_nodes(conn, node["node_id"], edge_types=["USES_TEMPLATE"], direction="in", limit=200)
    return {
        "template": node,
        "template_record": dict(template_row) if template_row else None,
        "data_items": data_items,
        "rows": rows,
        "columns": columns,
        "datapoints_sample": datapoints,
        "counts": {
            "rows": len(rows),
            "columns": len(columns),
            "datapoints": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE source_node_id=? AND edge_type='HAS_DATAPOINT'", (node["node_id"],)).fetchone()[0],
        },
    }


def search_datapoints(conn: sqlite3.Connection, *, q: str, template: str | None = None, data_item: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = "WHERE 1=1"
    if q:
        where += " AND (d.datapoint_id LIKE ? OR d.concept_label LIKE ? OR tr.label LIKE ? OR tc.label LIKE ? OR t.template_code LIKE ? OR t.title LIKE ?)"
        needle = f"%{q}%"
        params.extend([needle, needle, needle, needle, needle, needle])
    if template:
        where += " AND d.template_id=?"
        template_node_id = _template_id(conn, template)
        template_node = get_graph_node(conn, template_node_id)
        params.append((template_node or {}).get("source_pk") or template_node_id)
    if data_item:
        where += " AND EXISTS (SELECT 1 FROM graph_edge e WHERE e.edge_type='USES_TEMPLATE' AND e.source_node_id=? AND e.target_node_id=d.template_id)"
        params.append(_data_item_id(data_item))
    rows = conn.execute(
        f"""
        SELECT d.*, t.template_code, t.title AS template_title,
               tr.row_code, tr.label AS row_label,
               tc.column_code, tc.label AS column_label,
               gn.node_id, gn.label AS node_label, gn.properties_json
        FROM datapoint d
        LEFT JOIN template t ON t.template_id=d.template_id
        LEFT JOIN template_row tr ON tr.row_id=d.row_id
        LEFT JOIN template_column tc ON tc.column_id=d.column_id
        LEFT JOIN graph_node gn ON gn.node_id=d.datapoint_id
        {where}
        ORDER BY t.template_code, tr.row_order, tc.column_order, d.datapoint_id
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [_datapoint_result(row) for row in rows]


def datapoint_detail(conn: sqlite3.Connection, datapoint_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT d.*, t.template_code, t.title AS template_title,
               tr.row_code, tr.label AS row_label,
               tc.column_code, tc.label AS column_label,
               gn.node_id, gn.node_type, gn.label AS node_label, gn.source_table, gn.source_pk, gn.properties_json
        FROM datapoint d
        LEFT JOIN template t ON t.template_id=d.template_id
        LEFT JOIN template_row tr ON tr.row_id=d.row_id
        LEFT JOIN template_column tc ON tc.column_id=d.column_id
        LEFT JOIN graph_node gn ON gn.node_id=d.datapoint_id
        WHERE d.datapoint_id=?
        """,
        (datapoint_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "datapoint": _datapoint_result(row),
        "reports_concepts": _adjacent_nodes(conn, datapoint_id, edge_types=["REPORTS_CONCEPT"], direction="out", limit=100),
        "permissions": _adjacent_nodes(conn, datapoint_id, edge_types=["MAY_BE_AFFECTED_BY_PERMISSION"], direction="out", limit=100),
    }


def return_references(conn: sqlite3.Connection, code: str, *, edge_types: list[str] | None = None, limit: int = 500) -> dict[str, Any] | None:
    data_item_id = _data_item_id(code)
    if not get_graph_node(conn, data_item_id):
        return None
    source_ids = [r[0] for r in conn.execute("SELECT target_node_id FROM graph_edge WHERE source_node_id=? AND edge_type='EVIDENCED_BY'", (data_item_id,)).fetchall()]
    if not source_ids:
        return {"data_item_id": data_item_id, "references": [], "summary": {}}
    allowed = edge_types or sorted(REPORTING_REFERENCE_EDGE_TYPES)
    rows = conn.execute(
        f"""
        SELECT e.edge_id,e.source_node_id,e.target_node_id,e.edge_type,e.properties_json,e.evidence_span_id,e.confidence,e.extraction_method,e.review_status,
               s.label AS source_label, s.node_type AS source_type, s.properties_json AS source_properties_json,
               t.label AS target_label, t.node_type AS target_type, t.properties_json AS target_properties_json,
               sp.raw_text AS evidence_text, sp.heading_path AS evidence_heading, sp.page_number, sp.sheet_name, sp.row_number
        FROM graph_edge e
        JOIN graph_node s ON s.node_id=e.source_node_id
        JOIN graph_node t ON t.node_id=e.target_node_id
        LEFT JOIN source_span sp ON sp.span_id=e.evidence_span_id
        WHERE e.source_node_id IN ({','.join('?' for _ in source_ids)})
          AND e.edge_type IN ({','.join('?' for _ in allowed)})
        ORDER BY e.edge_type, t.label
        LIMIT ?
        """,
        [*source_ids, *allowed, limit],
    ).fetchall()
    refs = [_relationship(row) for row in rows]
    return {"data_item_id": data_item_id, "references": refs, "summary": _count_by(refs, "edge_type")}


def returns_relying_on(conn: sqlite3.Connection, target_node_id: str, *, limit: int = 200) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT di.node_id AS data_item_node_id, di.label AS data_item_code, di.properties_json AS data_item_properties_json,
               COUNT(DISTINCT ref.edge_id) AS relationship_count,
               COUNT(DISTINCT sd.node_id) AS source_document_count,
               GROUP_CONCAT(DISTINCT ref.edge_type) AS edge_types
        FROM graph_edge ref
        JOIN graph_node sd ON sd.node_id=ref.source_node_id AND sd.node_type='SourceDocument'
        JOIN graph_edge ev ON ev.target_node_id=sd.node_id AND ev.edge_type='EVIDENCED_BY'
        JOIN graph_node di ON di.node_id=ev.source_node_id AND di.node_type='DataItem'
        WHERE ref.target_node_id=?
        GROUP BY di.node_id
        ORDER BY relationship_count DESC, di.label
        LIMIT ?
        """,
        (target_node_id, limit),
    ).fetchall()
    return {
        "target": get_graph_node(conn, target_node_id),
        "returns": [
            {
                "node_id": r["data_item_node_id"],
                "data_item_code": r["data_item_code"],
                "properties": _json(r["data_item_properties_json"]),
                "relationship_count": r["relationship_count"],
                "source_document_count": r["source_document_count"],
                "edge_types": (r["edge_types"] or "").split(",") if r["edge_types"] else [],
            }
            for r in rows
        ],
    }


def relationship_evidence(conn: sqlite3.Connection, edge_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT e.edge_id,e.source_node_id,e.target_node_id,e.edge_type,e.properties_json,e.evidence_span_id,e.confidence,e.extraction_method,e.review_status,
               s.label AS source_label, s.node_type AS source_type, s.properties_json AS source_properties_json,
               t.label AS target_label, t.node_type AS target_type, t.properties_json AS target_properties_json,
               sp.*, sd.title AS source_document_title, sd.url AS source_document_url
        FROM graph_edge e
        LEFT JOIN graph_node s ON s.node_id=e.source_node_id
        LEFT JOIN graph_node t ON t.node_id=e.target_node_id
        LEFT JOIN source_span sp ON sp.span_id=e.evidence_span_id
        LEFT JOIN source_document sd ON sd.source_id=sp.source_id
        WHERE e.edge_id=?
        """,
        (edge_id,),
    ).fetchone()
    if not row:
        return None
    rel = _relationship(row)
    rel["source_document"] = {"source_id": row["source_id"], "title": row["source_document_title"], "url": row["source_document_url"]} if row["source_id"] else None
    return rel


def reporting_neighbourhood(conn: sqlite3.Connection, node_id_or_code: str, *, depth: int = 1, limit: int = 250, edge_types: list[str] | None = None) -> dict[str, Any] | None:
    node_id = _resolve_graph_node_id(conn, node_id_or_code)
    if not node_id:
        return None
    allowed = set(edge_types or [])
    seen_nodes = {node_id}
    seen_edges: dict[str, dict[str, Any]] = {}
    q: deque[tuple[str, int]] = deque([(node_id, 0)])
    while q and len(seen_nodes) < limit:
        current, dist = q.popleft()
        if dist >= depth:
            continue
        params: list[Any] = [current, current]
        clause = ""
        if allowed:
            clause = f" AND edge_type IN ({','.join('?' for _ in allowed)})"
            params.extend(sorted(allowed))
        rows = conn.execute(
            f"""
            SELECT edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status
            FROM graph_edge
            WHERE (source_node_id=? OR target_node_id=?) {clause}
            ORDER BY confidence DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        for row in rows:
            edge = _graph_edge(row)
            seen_edges[edge["edge_id"]] = edge
            other = edge["target_node_id"] if edge["source_node_id"] == current else edge["source_node_id"]
            if other not in seen_nodes and len(seen_nodes) < limit:
                seen_nodes.add(other)
                q.append((other, dist + 1))
    nodes = [get_graph_node(conn, nid) for nid in seen_nodes]
    return {"root": node_id, "nodes": [n for n in nodes if n], "edges": list(seen_edges.values())}


def get_graph_node(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT node_id,node_type,label,source_table,source_pk,properties_json,effective_from,effective_to,review_status FROM graph_node WHERE node_id=?", (node_id,)).fetchone()
    if not row:
        return None
    nodes = _enrich_reporting_nodes(conn, [_graph_node(row)])
    return nodes[0] if nodes else None


def _data_item_id(code: str) -> str:
    return code if code.startswith("data_item:") else f"data_item:{code.upper()}"


def _template_id(conn: sqlite3.Connection, value: str) -> str:
    if value.startswith("template:"):
        return value
    row = conn.execute("SELECT node_id FROM graph_node WHERE node_type='Template' AND (UPPER(node_id)=UPPER(?) OR UPPER(label)=UPPER(?) OR UPPER(source_pk)=UPPER(?)) LIMIT 1", (f"template:{value}", value, f"template:{value}")).fetchone()
    return row[0] if row else f"template:{value}"


def _resolve_graph_node_id(conn: sqlite3.Connection, value: str) -> str | None:
    if get_graph_node(conn, value):
        return value
    candidates = []
    if not value.startswith("data_item:"):
        candidates.append(_data_item_id(value))
    if not value.startswith("template:"):
        candidates.append(_template_id(conn, value))
    for candidate in candidates:
        if get_graph_node(conn, candidate):
            return candidate
    row = conn.execute("SELECT node_id FROM graph_node WHERE UPPER(label)=UPPER(?) LIMIT 1", (value,)).fetchone()
    return row[0] if row else None


def _return_reference_summary(conn: sqlite3.Connection, data_item_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT ref.edge_type, COUNT(*)
        FROM graph_edge ev
        JOIN graph_edge ref ON ref.source_node_id=ev.target_node_id
        WHERE ev.source_node_id=? AND ev.edge_type='EVIDENCED_BY'
          AND ref.edge_type IN ('REFERENCES_RULE','REFERENCES_SOURCE','REFERENCES_EXTERNAL','REFERENCES_RETURN','REFERENCES_TEMPLATE')
        GROUP BY ref.edge_type ORDER BY ref.edge_type
        """,
        (data_item_id,),
    ).fetchall()
    return dict(rows)


def _adjacent_nodes(conn: sqlite3.Connection, node_id: str, *, edge_types: list[str], direction: str, limit: int) -> list[dict[str, Any]]:
    if direction == "out":
        join = "n.node_id=e.target_node_id"
        where = "e.source_node_id=?"
    else:
        join = "n.node_id=e.source_node_id"
        where = "e.target_node_id=?"
    rows = conn.execute(
        f"""
        SELECT n.node_id,n.node_type,n.label,n.source_table,n.source_pk,n.properties_json,n.effective_from,n.effective_to,n.review_status,
               e.edge_id,e.edge_type,e.confidence
        FROM graph_edge e JOIN graph_node n ON {join}
        WHERE {where} AND e.edge_type IN ({','.join('?' for _ in edge_types)})
        ORDER BY n.node_type,n.label
        LIMIT ?
        """,
        [node_id, *edge_types, limit],
    ).fetchall()
    nodes = _enrich_reporting_nodes(conn, [_graph_node(row) for row in rows])
    by_id = {node["node_id"]: node for node in nodes}
    return [by_id[row["node_id"]] | {"via_edge": {"edge_id": row["edge_id"], "edge_type": row["edge_type"], "confidence": row["confidence"]}} for row in rows if row["node_id"] in by_id]


def _template_datapoints(conn: sqlite3.Connection, template_node_id: str, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.*, tr.row_code, tr.label AS row_label, tc.column_code, tc.label AS column_label,
               gn.node_id, gn.label AS node_label, gn.properties_json
        FROM graph_edge e
        JOIN datapoint d ON d.datapoint_id=e.target_node_id
        LEFT JOIN template_row tr ON tr.row_id=d.row_id
        LEFT JOIN template_column tc ON tc.column_id=d.column_id
        LEFT JOIN graph_node gn ON gn.node_id=d.datapoint_id
        WHERE e.source_node_id=? AND e.edge_type='HAS_DATAPOINT'
        ORDER BY tr.row_order, tc.column_order, d.datapoint_id
        LIMIT ?
        """,
        (template_node_id, limit),
    ).fetchall()
    return [_datapoint_result(row) for row in rows]


def _datapoint_result(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    props = _json(d.pop("properties_json", "{}"))
    return {
        "datapoint_id": d.get("datapoint_id"),
        "node_id": d.get("node_id") or d.get("datapoint_id"),
        "label": d.get("node_label") or d.get("concept_label"),
        "template_id": d.get("template_id"),
        "template_code": d.get("template_code"),
        "template_title": d.get("template_title"),
        "row_id": d.get("row_id"),
        "row_code": d.get("row_code"),
        "row_label": d.get("row_label"),
        "column_id": d.get("column_id"),
        "column_code": d.get("column_code"),
        "column_label": d.get("column_label"),
        "concept_label": d.get("concept_label"),
        "data_type": d.get("data_type"),
        "unit_type": d.get("unit_type"),
        "source_span_id": d.get("source_span_id"),
        "properties": props,
    }


def _relationship(row: sqlite3.Row) -> dict[str, Any]:
    props = _json(row["properties_json"] if "properties_json" in row.keys() else "{}")
    evidence_text = None
    if "evidence_text" in row.keys():
        evidence_text = row["evidence_text"]
    elif "raw_text" in row.keys():
        evidence_text = row["raw_text"]
    llm_ref = props.get("llm_reference") if isinstance(props, dict) else None
    return {
        "edge_id": row["edge_id"],
        "edge_type": row["edge_type"],
        "source": {"node_id": row["source_node_id"], "node_type": row["source_type"], "label": row["source_label"], "properties": _json(row["source_properties_json"] or "{}")},
        "target": {"node_id": row["target_node_id"], "node_type": row["target_type"], "label": row["target_label"], "properties": _json(row["target_properties_json"] or "{}")},
        "confidence": row["confidence"],
        "extraction_method": row["extraction_method"],
        "review_status": row["review_status"],
        "properties": props,
        "llm_reference": llm_ref,
        "evidence": {
            "span_id": row["evidence_span_id"],
            "quote": (llm_ref or {}).get("evidence_quote") if isinstance(llm_ref, dict) else None,
            "text": evidence_text,
            "heading_path": row["evidence_heading"] if "evidence_heading" in row.keys() else row["heading_path"] if "heading_path" in row.keys() else None,
            "page_number": row["page_number"] if "page_number" in row.keys() else None,
            "sheet_name": row["sheet_name"] if "sheet_name" in row.keys() else None,
            "row_number": row["row_number"] if "row_number" in row.keys() else None,
        },
    }


def _graph_node(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    props = _json(d.pop("properties_json", "{}"))
    d["properties"] = props
    return d


def _graph_edge(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["properties"] = _json(d.pop("properties_json", "{}"))
    return d


def _json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {}


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[str(item.get(key) or "")]=out.get(str(item.get(key) or ""),0)+1
    return out
