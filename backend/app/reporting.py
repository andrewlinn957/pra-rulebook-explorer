from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .xlsx_layout import _select_sheet, parse_xlsx_layout

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
REPORTING_ONTOLOGY_EDGE_TYPES = {
    "HAS_REGIME", "HAS_COLLECTION", "BELONGS_TO_REGIME", "BELONGS_TO_COLLECTION",
    "HAS_EDITION", "SUPERSEDES", "HAS_TEMPLATE_RESOURCE", "HAS_INSTRUCTION_RESOURCE",
    "HAS_RESOURCE", "CONTAINS_SHEET", "IMPLEMENTS_TEMPLATE", "SUPPORTED_BY_TAXONOMY",
    "CONTAINS_INSTRUCTION_SECTION", "HAS_TAXONOMY_RESOURCE", "HAS_ENTRY_POINT",
    "ENCODES_REQUIREMENT", "REFERENCES_RULE", "REFERENCES_SOURCE", "REFERENCES_EXTERNAL",
}


def reporting_catalog(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    estate: str | None = None,
    include_historic: bool = False,
) -> dict[str, Any]:
    """Return the normalized, user-facing reporting estate.

    This endpoint intentionally omits ingestion/audit fields.  The official
    source page and direct artifact links are the useful provenance for normal
    users; internal hashes, model names and extraction methods remain in the
    database for maintainers.
    """
    where = ["1=1"]
    params: list[Any] = []
    if not include_historic:
        where.append("r.status <> 'historic'")
    if estate:
        where.append("r.estate=?")
        params.append(estate)
    if q:
        where.append("(r.return_code LIKE ? OR r.name LIKE ? OR r.description LIKE ? OR r.family LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle, needle])
    rows = conn.execute(
        f"""
        SELECT r.*,oe.edition_id,oq.requirement_id,oq.requirement_type,
               oc.collection_id,oc.name AS collection_name,
               og.regime_id,og.name AS regime_name,
               oen.resolved_display_name AS edition_display_name,
               oen.display_name_source AS edition_display_name_source,
               COUNT(DISTINCT CASE WHEN ra.relationship='template' THEN ra.artifact_id END) AS template_count,
               COUNT(DISTINCT CASE WHEN ra.relationship='instructions' THEN ra.artifact_id END) AS instruction_count
        FROM reporting_return_catalog r
        LEFT JOIN reporting_return_artifact ra ON ra.return_id=r.return_id
        LEFT JOIN reporting_requirement_edition oe ON oe.legacy_return_id=r.return_id
        LEFT JOIN reporting_requirement oq ON oq.requirement_id=oe.requirement_id
        LEFT JOIN reporting_collection oc ON oc.collection_id=oq.collection_id
        LEFT JOIN reporting_regime og ON og.regime_id=oc.regime_id
        LEFT JOIN reporting_edition_names oen ON oen.edition_id=oe.edition_id
        WHERE {' AND '.join(where)}
        GROUP BY r.return_id
        ORDER BY CASE r.estate WHEN 'supervisory_reporting' THEN 1 WHEN 'pillar3_disclosure' THEN 2 ELSE 9 END,
                 r.family,r.return_code,r.effective_from
        """,
        params,
    ).fetchall()
    returns = [_public_catalog_return(row) for row in rows]
    technical = conn.execute(
        """
        SELECT artifact_id,url,display_title,artifact_role,estate,file_type,
               sheet_names_json,description,taxonomy_version
        FROM reporting_artifact
        WHERE estate='technical'
        ORDER BY artifact_role,display_title
        """
    ).fetchall()
    return {
        "source_page_url": rows[0]["source_page_url"] if rows else "",
        "returns": returns,
        "technical_artifacts": [_public_artifact(row) for row in technical],
        "counts": {
            "returns": len(returns),
            "supervisory_reporting": sum(r["estate"] == "supervisory_reporting" for r in returns),
            "pillar3_disclosure": sum(r["estate"] == "pillar3_disclosure" for r in returns),
            "technical_artifacts": len(technical),
        },
    }


def reporting_catalog_return(conn: sqlite3.Connection, return_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT r.*,oe.edition_id,oq.requirement_id,oq.requirement_type,
                  oc.collection_id,oc.name AS collection_name,
                  og.regime_id,og.name AS regime_name,
                  oen.resolved_display_name AS edition_display_name,
                  oen.display_name_source AS edition_display_name_source
           FROM reporting_return_catalog r
           LEFT JOIN reporting_requirement_edition oe ON oe.legacy_return_id=r.return_id
           LEFT JOIN reporting_requirement oq ON oq.requirement_id=oe.requirement_id
           LEFT JOIN reporting_collection oc ON oc.collection_id=oq.collection_id
           LEFT JOIN reporting_regime og ON og.regime_id=oc.regime_id
           LEFT JOIN reporting_edition_names oen ON oen.edition_id=oe.edition_id
           WHERE r.return_id=? OR oe.edition_id=?""",
        (return_id, return_id),
    ).fetchone()
    if not row:
        return None
    result = _public_catalog_return(row)
    artifacts = conn.execute(
        """
        SELECT a.artifact_id,a.url,a.display_title,a.artifact_role,a.estate,a.file_type,
               a.sheet_names_json,a.description,a.taxonomy_version,
               ra.relationship,ra.is_primary,ra.display_order,
               ores.resource_id,orn.resolved_display_name,orn.display_name_source,
               orn.inherited_requirement_name
        FROM reporting_return_artifact ra
        JOIN reporting_artifact a ON a.artifact_id=ra.artifact_id
        LEFT JOIN reporting_resource ores ON ores.legacy_artifact_id=a.artifact_id
        LEFT JOIN reporting_requirement_edition oe ON oe.legacy_return_id=ra.return_id
        LEFT JOIN reporting_edition_resource_names orn
          ON orn.edition_id=oe.edition_id AND orn.resource_id=ores.resource_id
        WHERE ra.return_id=?
        ORDER BY ra.display_order,a.display_title
        """,
        (row["return_id"],),
    ).fetchall()
    result["artifacts"] = [_public_artifact(artifact) for artifact in artifacts]
    code = result["return_code"]
    references = return_references(conn, code, limit=250)
    result["rulebook_references"] = (references or {}).get("references", [])
    result["reference_summary"] = (references or {}).get("summary", {})
    return result


def reporting_catalog_cells(
    conn: sqlite3.Connection,
    return_id: str,
    *,
    q: str | None = None,
    template_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Return the parsed template cells associated with a catalogue edition.

    The normalized catalogue is edition-centred, while the existing cell
    corpus is attached to the older ``DataItem -> Template`` projection.  This
    read model is the deliberate bridge between those layers: callers use a
    catalogue return or edition id and never need to know the legacy graph id.
    Coverage is explicit because not every official workbook has been parsed
    into cells yet.
    """
    catalog_row = conn.execute(
        """
        SELECT r.return_id,r.return_code,r.name,r.status,r.estate,r.source_page_url,
               oe.edition_id
        FROM reporting_return_catalog r
        LEFT JOIN reporting_requirement_edition oe ON oe.legacy_return_id=r.return_id
        WHERE r.return_id=? OR oe.edition_id=?
        LIMIT 1
        """,
        (return_id, return_id),
    ).fetchone()
    if not catalog_row:
        return None

    data_item_id = _data_item_id(catalog_row["return_code"])
    artifact_urls = [
        row["url"]
        for row in conn.execute(
            """
            SELECT DISTINCT a.url
            FROM reporting_return_artifact ra
            JOIN reporting_artifact a ON a.artifact_id=ra.artifact_id
            WHERE ra.return_id=? AND ra.relationship='template'
              AND COALESCE(a.url,'')<>''
            """,
            (catalog_row["return_id"],),
        )
    ]
    artifact_sheet_names: dict[str, list[str]] = {}
    for row in conn.execute(
        """
        SELECT a.url,a.sheet_names_json
        FROM reporting_return_artifact ra
        JOIN reporting_artifact a ON a.artifact_id=ra.artifact_id
        WHERE ra.return_id=? AND ra.relationship='template'
          AND lower(a.file_type) IN ('xlsx','xlsm','xltx')
          AND COALESCE(a.url,'')<>''
        """,
        (catalog_row["return_id"],),
    ):
        try:
            sheet_names = json.loads(row["sheet_names_json"] or "[]")
        except json.JSONDecodeError:
            sheet_names = []
        if isinstance(sheet_names, list) and sheet_names:
            artifact_sheet_names[row["url"]] = [
                str(name) for name in sheet_names if str(name).strip()
            ]
    direct_data_item = conn.execute(
        """
        SELECT node_id FROM graph_node
        WHERE node_id=? AND node_type='DataItem'
        """,
        (data_item_id,),
    ).fetchone()
    artifact_routed_data_items = {
        row["data_item_id"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT di.node_id AS data_item_id
            FROM graph_node di
            JOIN graph_edge evidence
              ON evidence.source_node_id=di.node_id
             AND evidence.edge_type='EVIDENCED_BY'
            JOIN graph_node source_node
              ON source_node.node_id=evidence.target_node_id
             AND source_node.node_type='SourceDocument'
            JOIN source_document sd ON sd.source_id=source_node.source_pk
            WHERE di.node_type='DataItem'
              AND sd.url IN ({','.join('?' for _ in artifact_urls)})
            """,
            artifact_urls,
        )
    } if artifact_urls else set()
    routed_data_items = set(artifact_routed_data_items)
    direct_matches_artifact = False
    if direct_data_item and artifact_urls:
        direct_matches_artifact = bool(
            conn.execute(
                f"""
                SELECT 1
                FROM graph_edge uses
                JOIN graph_node n
                  ON n.node_id=uses.target_node_id AND n.node_type='Template'
                JOIN template t
                  ON t.template_id=n.source_pk OR t.template_id=n.node_id
                JOIN source_document sd ON sd.source_id=t.source_id
                WHERE uses.source_node_id=?
                  AND uses.edge_type='USES_TEMPLATE'
                  AND sd.url IN ({','.join('?' for _ in artifact_urls)})
                LIMIT 1
                """,
                [data_item_id, *artifact_urls],
            ).fetchone()
        )
    if direct_matches_artifact:
        # Prefer the edition's own data-item route over broader aggregate
        # packages that happen to cite the same workbook.
        routed_data_items = {data_item_id}
    elif direct_data_item and not artifact_urls:
        routed_data_items.add(data_item_id)

    exact_artifact_template = False
    if artifact_urls:
        exact_artifact_template = bool(
            conn.execute(
                f"""
                SELECT 1
                FROM graph_node n
                LEFT JOIN template t
                  ON t.template_id=n.node_id OR t.template_id=n.source_pk
                LEFT JOIN source_document relational_source
                  ON relational_source.source_id=t.source_id
                LEFT JOIN source_document graph_source
                  ON graph_source.source_id=json_extract(
                    n.properties_json,'$.source_id'
                  )
                WHERE n.node_type='Template'
                  AND (
                    relational_source.url IN (
                      {','.join('?' for _ in artifact_urls)}
                    )
                    OR (
                      t.template_id IS NULL
                      AND graph_source.url IN (
                        {','.join('?' for _ in artifact_urls)}
                      )
                    )
                  )
                LIMIT 1
                """,
                [*artifact_urls, *artifact_urls],
            ).fetchone()
        )

    # An edition's official template URL is the strongest available join key.
    # Read templates directly from that exact source even when the older
    # DataItem projection omitted USES_TEMPLATE/EVIDENCED_BY edges. Fall back
    # to the edition's own legacy DataItem only when no exact-source template
    # exists. Never fall back through an aggregate DataItem discovered from
    # artifact evidence: that is what previously leaked unrelated FINREP and
    # COR011 workbooks into an edition.
    constrain_to_artifacts = exact_artifact_template
    route_ids = sorted(routed_data_items)
    relational_route_where = "1=0"
    relational_route_params: list[Any] = []
    graph_route_where = "1=0"
    graph_route_params: list[Any] = []
    if constrain_to_artifacts:
        artifact_slots = ",".join("?" for _ in artifact_urls)
        relational_route_where = (
            f"(sd.url IN ({artifact_slots}) "
            f"OR (sd.source_id IS NULL "
            f"AND graph_sd.url IN ({artifact_slots})))"
        )
        relational_route_params.extend([*artifact_urls, *artifact_urls])
        graph_route_where = f"sd.url IN ({artifact_slots})"
        graph_route_params.extend(artifact_urls)
    elif direct_data_item and not artifact_urls:
        relational_route_where = graph_route_where = (
            "EXISTS ("
            "SELECT 1 FROM graph_edge uses "
            "WHERE uses.target_node_id=n.node_id "
            "AND uses.edge_type='USES_TEMPLATE' "
            "AND uses.source_node_id=?"
            ")"
        )
        relational_route_params.append(data_item_id)
        graph_route_params.append(data_item_id)
    scoped_family_code = str(catalog_row["return_code"] or "").upper()
    if re.fullmatch(r"(?:PRA|RFB|LVR)\d{3}", scoped_family_code):
        family_scope = (
            "UPPER(COALESCE("
            "json_extract(n.properties_json,'$.data_item_code'),?"
            "))=?"
        )
        relational_route_where = (
            f"({relational_route_where}) AND {family_scope}"
        )
        graph_route_where = f"({graph_route_where}) AND {family_scope}"
        relational_route_params.extend(
            [scoped_family_code, scoped_family_code]
        )
        graph_route_params.extend([scoped_family_code, scoped_family_code])

    template_rows = conn.execute(
        f"""
        SELECT n.node_id,t.template_id,
               COALESCE(
                 json_extract(n.properties_json,'$.template_code'),
                 t.template_code
               ) AS template_code,
               COALESCE(
                 json_extract(n.properties_json,'$.template_title'),
                 t.title
               ) AS title,
               t.annex,
               COALESCE(sd.url,graph_sd.url) AS source_url,
               COALESCE(sd.title,graph_sd.title) AS source_title,
               (SELECT COUNT(*) FROM datapoint dp WHERE dp.template_id=t.template_id) AS cell_count,
               (SELECT COUNT(*) FROM template_row tr WHERE tr.template_id=t.template_id) AS row_count,
               (SELECT COUNT(*) FROM template_column tc WHERE tc.template_id=t.template_id) AS column_count
        FROM graph_node n
        JOIN template t
          ON t.template_id=n.source_pk OR t.template_id=n.node_id
        LEFT JOIN source_document sd ON sd.source_id=t.source_id
        LEFT JOIN source_document graph_sd
          ON graph_sd.source_id=json_extract(n.properties_json,'$.source_id')
        WHERE n.node_type='Template'
          AND {relational_route_where}
          AND COALESCE(
                json_extract(n.properties_json,'$.sheet_state'),
                'visible'
              )='visible'
        GROUP BY n.node_id,t.template_id
        ORDER BY t.template_code,t.title,t.template_id
        """,
        relational_route_params,
    ).fetchall()
    templates = _dedupe_template_summaries([
        {
            "node_id": row["node_id"],
            "template_id": row["template_id"],
            "template_code": row["template_code"],
            "title": row["title"],
            "annex": row["annex"],
            "source_url": row["source_url"],
            "source_title": row["source_title"],
            "cell_count": int(row["cell_count"] or 0),
            "row_count": int(row["row_count"] or 0),
            "column_count": int(row["column_count"] or 0),
        }
        for row in template_rows
    ], preferred_code=catalog_row["return_code"])
    graph_template_rows = conn.execute(
        f"""
        SELECT n.node_id,n.label,n.properties_json,
               sd.url AS source_url,sd.title AS source_title,
               (SELECT COUNT(DISTINCT e.target_node_id) FROM graph_edge e
                WHERE e.source_node_id=n.node_id
                  AND e.edge_type='HAS_DATAPOINT') AS cell_count,
               (SELECT COUNT(*) FROM graph_edge e
                WHERE e.source_node_id=n.node_id
                  AND e.edge_type='HAS_ROW') AS row_count,
               (SELECT COUNT(*) FROM graph_edge e
                WHERE e.source_node_id=n.node_id
                  AND e.edge_type='HAS_COLUMN') AS column_count
        FROM graph_node n
        LEFT JOIN source_document sd
          ON sd.source_id=json_extract(n.properties_json,'$.source_id')
        WHERE n.node_type='Template'
          AND {graph_route_where}
          AND COALESCE(
                json_extract(n.properties_json,'$.sheet_state'),
                'visible'
              )='visible'
          AND NOT EXISTS (
            SELECT 1 FROM template t
            WHERE t.template_id=n.node_id OR t.template_id=n.source_pk
          )
        GROUP BY n.node_id
        ORDER BY n.label,n.node_id
        """,
        graph_route_params,
    ).fetchall()
    seen_template_keys = {
        _template_identity_key(template["template_code"])
        for template in templates
    }
    graph_template_ids: set[str] = set()
    graph_template_metadata: dict[str, dict[str, Any]] = {}
    for row in graph_template_rows:
        summary = _graph_template_summary(row)
        identity_key = _template_identity_key(summary["template_code"])
        if identity_key and identity_key in seen_template_keys:
            continue
        if identity_key:
            seen_template_keys.add(identity_key)
        graph_template_ids.add(summary["template_id"])
        graph_template_metadata[summary["template_id"]] = summary
        templates.append(summary)
    if constrain_to_artifacts and artifact_sheet_names:
        # A workbook URL can have stale legacy templates attached to it.  The
        # catalogue's inspected worksheet list is the authoritative scope for
        # this return (for example NSFR declares Index and sheets 80-84, not
        # the unrelated C71 template that once pointed at the same URL).
        templates = [
            template
            for template in templates
            if not artifact_sheet_names.get(template.get("source_url") or "")
            or _select_sheet(
                [
                    (sheet_name, "")
                    for sheet_name in artifact_sheet_names[
                        template.get("source_url") or ""
                    ]
                ],
                template_id=template["template_id"],
                template_code=template.get("template_code") or "",
                title=template.get("title") or "",
            )
            is not None
        ]
        retained_template_ids = {
            template["template_id"] for template in templates
        }
        graph_template_ids.intersection_update(retained_template_ids)
        graph_template_metadata = {
            template_id: metadata
            for template_id, metadata in graph_template_metadata.items()
            if template_id in retained_template_ids
        }
    templates.sort(
        key=lambda row: (
            str(row.get("template_code") or ""),
            str(row.get("title") or ""),
            str(row.get("template_id") or ""),
        )
    )
    relational_template_ids = {
        row["template_id"] for row in templates
        if row["template_id"] not in graph_template_ids
    }
    available_template_ids = {row["template_id"] for row in templates}
    selected_template_id = template_id if template_id in available_template_ids else None
    filtered_template_ids = [selected_template_id] if selected_template_id else sorted(available_template_ids)
    selected_relational_ids = [
        value for value in filtered_template_ids
        if value in relational_template_ids
    ]
    selected_graph_ids = [
        value for value in filtered_template_ids
        if value in graph_template_ids
    ]
    needle = f"%{q}%" if q else ""
    cell_offset = offset
    graph_total_from_metadata: int | None = None
    if not q and not selected_relational_ids and selected_graph_ids:
        # The graph-native corpus can contain tens of thousands of cells.
        # Counting and sorting every one just to return the first page made
        # the default explorer load take several seconds. Template summaries
        # already hold distinct cell counts, so use those for the unfiltered
        # total and only query the templates that intersect the requested page.
        graph_total_from_metadata = sum(
            int(graph_template_metadata[value]["cell_count"] or 0)
            for value in selected_graph_ids
        )
        page_graph_ids: list[str] = []
        cumulative = 0
        page_capacity = 0
        for value in selected_graph_ids:
            template_count = int(
                graph_template_metadata[value]["cell_count"] or 0
            )
            if cumulative + template_count <= offset:
                cumulative += template_count
                continue
            if not page_graph_ids:
                cell_offset = max(0, offset - cumulative)
                page_capacity = template_count - cell_offset
            else:
                page_capacity += template_count
            page_graph_ids.append(value)
            cumulative += template_count
            if page_capacity >= limit:
                break
        selected_graph_ids = page_graph_ids

    total = 0
    cell_selects: list[str] = []
    cell_params: list[Any] = []
    if selected_relational_ids:
        relational_where = [
            f"d.template_id IN ({','.join('?' for _ in selected_relational_ids)})"
        ]
        relational_params: list[Any] = list(selected_relational_ids)
        if q:
            relational_where.append(
                "(d.datapoint_id LIKE ? OR d.concept_label LIKE ? OR "
                "tr.row_code LIKE ? OR tr.label LIKE ? OR "
                "tc.column_code LIKE ? OR tc.label LIKE ? OR "
                "t.template_code LIKE ? OR t.title LIKE ?)"
            )
            relational_params.extend([needle] * 8)
        total += conn.execute(
            f"""
            SELECT COUNT(*)
            FROM datapoint d
            JOIN template t ON t.template_id=d.template_id
            LEFT JOIN template_row tr ON tr.row_id=d.row_id
            LEFT JOIN template_column tc ON tc.column_id=d.column_id
            WHERE {' AND '.join(relational_where)}
            """,
            relational_params,
        ).fetchone()[0]
        cell_selects.append(
            f"""
            SELECT t.template_code AS sort_template,
                   COALESCE(tr.row_order,999999) AS sort_row,
                   COALESCE(tc.column_order,999999) AS sort_column,
                   d.datapoint_id,d.template_id,d.row_id,d.column_id,
                   d.data_type,d.unit_type,d.concept_label,d.source_span_id,
                   t.template_code,t.title AS template_title,
                   tr.row_code,tr.label AS row_label,tr.row_order,
                   tc.column_code,tc.label AS column_label,tc.column_order,
                   gn.node_id,gn.label AS node_label,gn.properties_json
            FROM datapoint d
            JOIN template t ON t.template_id=d.template_id
            LEFT JOIN template_row tr ON tr.row_id=d.row_id
            LEFT JOIN template_column tc ON tc.column_id=d.column_id
            LEFT JOIN graph_node gn ON gn.node_id=d.datapoint_id
            WHERE {' AND '.join(relational_where)}
            """
        )
        cell_params.extend(relational_params)

    if graph_total_from_metadata is not None:
        total += graph_total_from_metadata
    if selected_graph_ids:
        graph_where = [
            f"h.source_node_id IN ({','.join('?' for _ in selected_graph_ids)})",
            "h.edge_type='HAS_DATAPOINT'",
            "dp.node_type='DataPoint'",
        ]
        graph_params: list[Any] = list(selected_graph_ids)
        if q:
            graph_where.append(
                "(dp.node_id LIKE ? OR dp.label LIKE ? OR "
                "json_extract(dp.properties_json,'$.row_code') LIKE ? OR "
                "gr.label LIKE ? OR "
                "json_extract(dp.properties_json,'$.column_code') LIKE ? OR "
                "gc.label LIKE ? OR gt.node_id LIKE ? OR gt.label LIKE ?)"
            )
            graph_params.extend([needle] * 8)
        graph_joins = """
            FROM graph_edge h
            JOIN graph_node gt
              ON gt.node_id=h.source_node_id AND gt.node_type='Template'
            JOIN graph_node dp ON dp.node_id=h.target_node_id
            LEFT JOIN graph_node gr
              ON gr.node_id=(
                'row:' || gt.node_id || ':' ||
                json_extract(dp.properties_json,'$.row_code')
              )
            LEFT JOIN graph_node gc
              ON gc.node_id=(
                'column:' || gt.node_id || ':' ||
                json_extract(dp.properties_json,'$.column_code')
              )
        """
        if graph_total_from_metadata is None:
            total += conn.execute(
                f"""
                SELECT COUNT(DISTINCT dp.node_id)
                {graph_joins}
                WHERE {' AND '.join(graph_where)}
                """,
                graph_params,
            ).fetchone()[0]
        cell_selects.append(
            f"""
            SELECT DISTINCT gt.label AS sort_template,
                   COALESCE(
                     CAST(json_extract(dp.properties_json,'$.row_code') AS INTEGER),
                     999999
                   ) AS sort_row,
                   COALESCE(
                     CAST(json_extract(dp.properties_json,'$.column_code') AS INTEGER),
                     999999
                   ) AS sort_column,
                   dp.node_id AS datapoint_id,
                   gt.node_id AS template_id,
                   gr.node_id AS row_id,
                   gc.node_id AS column_id,
                   json_extract(dp.properties_json,'$.data_type') AS data_type,
                   json_extract(dp.properties_json,'$.unit_type') AS unit_type,
                   dp.label AS concept_label,
                   json_extract(dp.properties_json,'$.source_span_id') AS source_span_id,
                   gt.label AS template_code,
                   gt.label AS template_title,
                   json_extract(dp.properties_json,'$.row_code') AS row_code,
                   gr.label AS row_label,
                   NULL AS row_order,
                   json_extract(dp.properties_json,'$.column_code') AS column_code,
                   gc.label AS column_label,
                   NULL AS column_order,
                   dp.node_id AS node_id,
                   dp.label AS node_label,
                   dp.properties_json
            {graph_joins}
            WHERE {' AND '.join(graph_where)}
            """
        )
        cell_params.extend(graph_params)

    cells: list[sqlite3.Row] = []
    if cell_selects:
        cells = conn.execute(
            f"""
            SELECT * FROM ({' UNION ALL '.join(cell_selects)})
            ORDER BY sort_template,sort_row,sort_column,datapoint_id
            LIMIT ? OFFSET ?
            """,
            [*cell_params, limit, cell_offset],
        ).fetchall()
    result_relational_ids = sorted({
        row["template_id"] for row in cells
        if row["template_id"] in relational_template_ids
    })
    row_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    column_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    if result_relational_ids:
        slots = ",".join("?" for _ in result_relational_ids)
        for row in conn.execute(
            f"""
            SELECT template_id,row_id,row_code,row_order,label
            FROM template_row
            WHERE template_id IN ({slots})
            """,
            result_relational_ids,
        ):
            row_metadata[(row["template_id"], str(row["row_code"] or ""))] = dict(row)
        for row in conn.execute(
            f"""
            SELECT template_id,column_id,column_code,column_order,label
            FROM template_column
            WHERE template_id IN ({slots})
            """,
            result_relational_ids,
        ):
            column_metadata[(row["template_id"], str(row["column_code"] or ""))] = dict(row)
    cell_results: list[dict[str, Any]] = []
    for row in cells:
        result = _datapoint_result(row)
        graph_template = graph_template_metadata.get(result["template_id"])
        if graph_template:
            result["template_code"] = graph_template["template_code"]
            result["template_title"] = graph_template["title"]
        _fill_graph_coordinate(result)
        row_meta = row_metadata.get((
            str(result.get("template_id") or ""),
            str(result.get("row_code") or ""),
        ))
        if row_meta:
            result["row_id"] = result.get("row_id") or row_meta["row_id"]
            result["row_label"] = result.get("row_label") or row_meta["label"]
            result["row_order"] = row_meta["row_order"]
        column_meta = column_metadata.get((
            str(result.get("template_id") or ""),
            str(result.get("column_code") or ""),
        ))
        if column_meta:
            result["column_id"] = result.get("column_id") or column_meta["column_id"]
            result["column_label"] = result.get("column_label") or column_meta["label"]
            result["column_order"] = column_meta["column_order"]
        cell_results.append(result)

    cell_count = sum(row["cell_count"] for row in templates)
    coverage = (
        "available"
        if cell_count
        else "template_layout_available"
        if templates
        else "return_not_mapped"
    )
    return {
        "return": {
            "return_id": catalog_row["return_id"],
            "edition_id": catalog_row["edition_id"],
            "return_code": catalog_row["return_code"],
            "name": catalog_row["name"],
            "status": catalog_row["status"],
            "estate": catalog_row["estate"],
            "source_page_url": catalog_row["source_page_url"],
            "data_item_id": route_ids[0] if route_ids else data_item_id,
            "data_item_ids": route_ids,
            "cell_mapping_basis": (
                "official_template_source"
                if constrain_to_artifacts
                else "direct_return_code"
                if direct_data_item and not artifact_urls
                else "unmapped"
            ),
        },
        "templates": templates,
        "cells": cell_results,
        "counts": {
            "templates": len(templates),
            "cells": cell_count,
            "matched_cells": int(total),
        },
        "coverage": coverage,
        "query": q or "",
        "selected_template_id": selected_template_id,
        "limit": limit,
        "offset": offset,
    }


def _reporting_template_source(
    conn: sqlite3.Connection,
    template_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT t.template_id,t.template_code,t.title,t.source_id,
               sd.local_path,sd.url AS source_url,sd.file_type,
               (
                 SELECT json_extract(n.properties_json,'$.sheet_name')
                 FROM graph_node n
                 WHERE n.node_id=t.template_id
                 LIMIT 1
               ) AS sheet_name
        FROM template t
        LEFT JOIN source_document sd ON sd.source_id=t.source_id
        WHERE t.template_id=?
        LIMIT 1
        """,
        (template_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT n.node_id AS template_id,
                   COALESCE(
                     json_extract(n.properties_json,'$.data_item_code'),
                     n.label
                   ) AS template_code,
                   n.label AS title,
                   json_extract(n.properties_json,'$.source_id') AS source_id,
                   sd.local_path,sd.url AS source_url,sd.file_type,
                   json_extract(n.properties_json,'$.sheet_name') AS sheet_name
            FROM graph_node n
            LEFT JOIN source_document sd
              ON sd.source_id=json_extract(n.properties_json,'$.source_id')
            WHERE n.node_id=? AND n.node_type='Template'
            LIMIT 1
            """,
            (template_id,),
        ).fetchone()
    return row


def reporting_template_document_path(
    conn: sqlite3.Connection,
    template_id: str,
    *,
    project_root: Path,
) -> Path | None:
    row = _reporting_template_source(conn, template_id)
    if row is None or not row["local_path"]:
        return None
    root = project_root.resolve()
    source_path = (root / row["local_path"]).resolve()
    if root not in source_path.parents:
        return None
    if not source_path.exists():
        return None
    return source_path


def reporting_template_layout(
    conn: sqlite3.Connection,
    template_id: str,
    *,
    project_root: Path,
) -> dict[str, Any] | None:
    row = _reporting_template_source(conn, template_id)
    if row is None:
        return None
    root = project_root.resolve()
    source_path = reporting_template_document_path(
        conn,
        template_id,
        project_root=project_root,
    )
    if source_path is None:
        return None
    if str(row["file_type"] or "").lower() == "pdf":
        try:
            page_count = len(PdfReader(source_path).pages)
        except Exception:
            return None
        return {
            "template_id": row["template_id"],
            "template_code": row["template_code"],
            "source_url": row["source_url"],
            "format": "pdf",
            "sheet_name": "PDF",
            "dimension": f"{page_count} page{'s' if page_count != 1 else ''}",
            "page_count": page_count,
            "rows": [],
            "columns": [],
        }
    layout = parse_xlsx_layout(
        source_path,
        template_id=row["template_id"],
        template_code=row["sheet_name"] or row["template_code"] or "",
        title=row["title"] or "",
    )
    layout_source_url = row["source_url"]
    if layout is None:
        sheet_hints = {row["template_code"] or ""}
        code_match = re.search(r"(\d{1,3})(?:\.\d+)?", row["template_code"] or "")
        if code_match:
            sheet_hints.add(str(int(code_match.group(1))))
        fallback_rows = conn.execute(
            f"""
            SELECT DISTINCT sd.local_path,sd.url AS source_url
            FROM source_span sp
            JOIN source_document sd ON sd.source_id=sp.source_id
            WHERE sp.sheet_name IN ({','.join('?' for _ in sheet_hints)})
              AND sd.file_type IN ('xlsx','xlsm','xltx')
              AND COALESCE(sd.local_path,'')<>''
            ORDER BY sd.local_path
            """,
            sorted(sheet_hints),
        ).fetchall()
        for fallback in fallback_rows:
            fallback_path = (root / fallback["local_path"]).resolve()
            if root not in fallback_path.parents:
                continue
            layout = parse_xlsx_layout(
                fallback_path,
                template_id=row["template_id"],
                template_code=row["sheet_name"] or row["template_code"] or "",
                title=row["title"] or "",
            )
            if layout is not None:
                layout_source_url = fallback["source_url"]
                break
    if layout is None:
        return None
    return {
        "template_id": row["template_id"],
        "template_code": row["template_code"],
        "source_url": layout_source_url,
        **layout,
    }


def reporting_change_impact(
    conn: sqlite3.Connection,
    target_node_id: str,
    *,
    include_historic: bool = False,
    sample_cells: int = 8,
    limit: int = 200,
) -> dict[str, Any] | None:
    """Trace a changed rule to instruction evidence and candidate cell scope.

    A reference edge proves that an instruction source mentions the changed
    provision. It does *not* prove that every cell in the associated return
    changes. The response keeps those evidence tiers separate so downstream
    applications can review the direct instruction passages before narrowing
    candidate templates and cells.
    """
    target = get_graph_node(conn, target_node_id)
    if not target:
        return None
    reference_rows = conn.execute(
        f"""
        SELECT ref.edge_id,ref.edge_type,ref.confidence,ref.extraction_method,
               ref.evidence_span_id,ref.properties_json,
               di.node_id AS data_item_id,di.label AS return_label,
               sdn.node_id AS source_node_id,
               sd.source_id,sd.title AS source_title,sd.url AS source_url,
               sd.file_type,sp.raw_text AS evidence_text,
               sp.heading_path,sp.page_number,sp.sheet_name,sp.row_number
        FROM graph_edge ref
        JOIN graph_node sdn
          ON sdn.node_id=ref.source_node_id AND sdn.node_type='SourceDocument'
        JOIN graph_edge ev
          ON ev.target_node_id=sdn.node_id AND ev.edge_type='EVIDENCED_BY'
        JOIN graph_node di
          ON di.node_id=ev.source_node_id AND di.node_type='DataItem'
        LEFT JOIN source_document sd
          ON sd.source_id=sdn.source_pk OR sd.source_id=sdn.node_id
        LEFT JOIN source_span sp ON sp.span_id=ref.evidence_span_id
        WHERE ref.target_node_id=?
          AND ref.edge_type IN ({','.join('?' for _ in REPORTING_REFERENCE_EDGE_TYPES)})
        ORDER BY di.label,sd.title,ref.confidence DESC,ref.edge_id
        """,
        [target_node_id, *sorted(REPORTING_REFERENCE_EDGE_TYPES)],
    ).fetchall()

    by_return: dict[str, dict[str, Any]] = {}

    def ensure_return(
        data_item_id: str,
        *,
        return_label: str | None = None,
    ) -> dict[str, Any]:
        return_code = str(data_item_id).removeprefix("data_item:")
        return by_return.setdefault(
            data_item_id,
            {
                "data_item_id": data_item_id,
                "return_code": return_code,
                "return_label": return_label or return_code,
                "references": [],
                "reference_count": 0,
                "instruction_sources": {},
                "direct_coordinates": [],
                "direct_coordinate_count": 0,
                "direct_coordinate_instruction_ids": set(),
                "materialized_direct_cell_count": 0,
                "instruction_defined_coordinate_count": 0,
                "direct_coordinates_seen": set(),
            },
        )

    for row in reference_rows:
        entry = ensure_return(
            row["data_item_id"],
            return_label=row["return_label"],
        )
        entry["reference_count"] += 1
        if len(entry["references"]) < limit:
            entry["references"].append(
                {
                    "edge_id": row["edge_id"],
                    "edge_type": row["edge_type"],
                    "confidence": row["confidence"],
                    "extraction_method": row["extraction_method"],
                    "evidence_span_id": row["evidence_span_id"],
                    "evidence_text": row["evidence_text"],
                    "heading_path": row["heading_path"],
                    "page_number": row["page_number"],
                    "sheet_name": row["sheet_name"],
                    "row_number": row["row_number"],
                    "source_node_id": row["source_node_id"],
                    "source_id": row["source_id"],
                    "source_title": row["source_title"],
                    "source_url": row["source_url"],
                }
            )
        source_key = row["source_id"] or row["source_node_id"]
        entry["instruction_sources"][source_key] = {
            "source_node_id": row["source_node_id"],
            "source_id": row["source_id"],
            "title": row["source_title"],
            "url": row["source_url"],
            "file_type": row["file_type"],
            "relationship": "direct_reference",
        }

    direct_coordinate_rows = conn.execute(
        """
        SELECT DISTINCT
               legal.edge_id AS legal_edge_id,
               legal.confidence AS legal_confidence,
               legal.properties_json AS legal_properties_json,
               coordinate.edge_id AS coordinate_edge_id,
               coordinate.confidence AS coordinate_confidence,
               coordinate.properties_json AS coordinate_properties_json,
               coordinate.evidence_span_id,
               ip.node_id AS instruction_node_id,
               ip.label AS instruction_label,
               ip.properties_json AS instruction_properties_json,
               target.node_id AS coordinate_node_id,
               target.node_type AS coordinate_node_type,
               target.label AS coordinate_node_label,
               target.properties_json AS coordinate_node_properties_json,
               di.node_id AS data_item_id,
               di.label AS return_label,
               t.template_id,t.template_code,t.title AS template_title,
               tsd.url AS template_source_url,
               tr.label AS row_label,
               tc.label AS column_label,
               dp.datapoint_id,dp.concept_label,
               sp.raw_text AS evidence_text,
               sp.heading_path,sp.page_number,sp.sheet_name,sp.row_number,
               sdn.node_id AS source_node_id,
               sd.source_id,sd.title AS source_title,sd.url AS source_url,
               sd.file_type
        FROM graph_edge legal
        JOIN graph_node ip
          ON ip.node_id=legal.source_node_id
         AND ip.node_type='InstructionProvision'
        JOIN graph_edge coordinate
          ON coordinate.source_node_id=ip.node_id
         AND coordinate.edge_type='INSTRUCTS'
         AND json_extract(
               coordinate.properties_json,'$.coordinate_relation'
             )='normative_reporting_coordinate'
         AND COALESCE(coordinate.evidence_span_id,'')
             =COALESCE(legal.evidence_span_id,'')
        JOIN graph_node target ON target.node_id=coordinate.target_node_id
        JOIN template t
          ON t.template_id=json_extract(
               coordinate.properties_json,'$.template_id'
             )
        JOIN graph_edge uses
          ON uses.target_node_id=t.template_id
         AND uses.edge_type='USES_TEMPLATE'
        JOIN graph_node di
          ON di.node_id=uses.source_node_id AND di.node_type='DataItem'
        LEFT JOIN source_document tsd ON tsd.source_id=t.source_id
        LEFT JOIN datapoint dp ON dp.datapoint_id=target.node_id
        LEFT JOIN template_row tr
          ON tr.template_id=t.template_id
         AND tr.row_code=json_extract(
               coordinate.properties_json,'$.row_code'
             )
        LEFT JOIN template_column tc
          ON tc.template_id=t.template_id
         AND tc.column_code=json_extract(
               coordinate.properties_json,'$.column_code'
             )
        LEFT JOIN source_span sp
          ON sp.span_id=coordinate.evidence_span_id
        LEFT JOIN graph_edge instruction_evidence
          ON instruction_evidence.source_node_id=ip.node_id
         AND instruction_evidence.edge_type='EVIDENCED_BY'
        LEFT JOIN graph_node sdn
          ON sdn.node_id=instruction_evidence.target_node_id
         AND sdn.node_type='SourceDocument'
        LEFT JOIN source_document sd
          ON sd.source_id=sdn.source_pk
        WHERE legal.target_node_id=?
          AND legal.edge_type='REFERENCES_RULE'
          AND legal.extraction_method='instruction_legal_reference_projection'
        ORDER BY di.label,t.template_code,
                 json_extract(coordinate.properties_json,'$.row_code'),
                 json_extract(coordinate.properties_json,'$.column_code'),
                 ip.node_id
        """,
        (target_node_id,),
    ).fetchall()

    for row in direct_coordinate_rows:
        entry = ensure_return(
            row["data_item_id"],
            return_label=row["return_label"],
        )
        evidence_key = (
            row["instruction_node_id"],
            row["coordinate_node_id"],
            row["legal_edge_id"],
        )
        if evidence_key in entry["direct_coordinates_seen"]:
            continue
        entry["direct_coordinates_seen"].add(evidence_key)
        entry["direct_coordinate_count"] += 1
        entry["direct_coordinate_instruction_ids"].add(row["instruction_node_id"])

        edge_properties = _json(row["coordinate_properties_json"])
        target_properties = _json(row["coordinate_node_properties_json"])
        instruction_properties = _json(row["instruction_properties_json"])
        legal_properties = _json(row["legal_properties_json"])
        if row["coordinate_node_type"] == "DataPoint":
            coverage_status = "materialized_datapoint"
            entry["materialized_direct_cell_count"] += 1
        elif row["coordinate_node_type"] == "ReportingCoordinate":
            coverage_status = (
                target_properties.get("coverage_status")
                or "instruction_defined_not_materialized"
            )
            entry["instruction_defined_coordinate_count"] += 1
        else:
            coverage_status = "instruction_defined_row_scope"
            entry["instruction_defined_coordinate_count"] += 1

        if len(entry["direct_coordinates"]) < limit:
            entry["direct_coordinates"].append(
                {
                    "instruction_node_id": row["instruction_node_id"],
                    "instruction_label": row["instruction_label"],
                    "instruction_text": (
                        instruction_properties.get("text")
                        or row["instruction_label"]
                    ),
                    "legal_edge_id": row["legal_edge_id"],
                    "legal_confidence": row["legal_confidence"],
                    "legal_reference": (
                        legal_properties.get("reference_label")
                        or legal_properties.get("canonical_key")
                    ),
                    "coordinate_edge_id": row["coordinate_edge_id"],
                    "coordinate_confidence": row["coordinate_confidence"],
                    "coordinate_node_id": row["coordinate_node_id"],
                    "coordinate_node_type": row["coordinate_node_type"],
                    "coordinate_node_label": row["coordinate_node_label"],
                    "coordinate_evidence": edge_properties.get(
                        "coordinate_evidence"
                    ),
                    "template_id": row["template_id"],
                    "template_code": row["template_code"],
                    "template_title": row["template_title"],
                    "template_source_url": row["template_source_url"],
                    "row_code": edge_properties.get("row_code"),
                    "row_label": row["row_label"],
                    "column_code": edge_properties.get("column_code"),
                    "column_label": row["column_label"],
                    "datapoint_id": row["datapoint_id"],
                    "concept_label": row["concept_label"],
                    "coverage_status": coverage_status,
                    "evidence_span_id": row["evidence_span_id"],
                    "evidence_text": (
                        row["evidence_text"]
                        or instruction_properties.get("text")
                    ),
                    "heading_path": row["heading_path"],
                    "page_number": row["page_number"],
                    "sheet_name": row["sheet_name"],
                    "row_number": row["row_number"],
                    "source_node_id": row["source_node_id"],
                    "source_id": row["source_id"],
                    "source_title": row["source_title"],
                    "source_url": row["source_url"],
                    "impact_tier": "direct_coordinate_evidence",
                    "review_note": (
                        "The same instruction passage names this rule and "
                        "reporting coordinate. Review the passage before "
                        "treating the coordinate as a required edit."
                    ),
                }
            )
        if row["source_id"] or row["source_node_id"]:
            source_key = row["source_id"] or row["source_node_id"]
            entry["instruction_sources"][source_key] = {
                "source_node_id": row["source_node_id"],
                "source_id": row["source_id"],
                "title": row["source_title"],
                "url": row["source_url"],
                "file_type": row["file_type"],
                "relationship": "instruction_provision_evidence",
            }

    return_codes = sorted({str(entry["return_code"]) for entry in by_return.values() if entry["return_code"]})
    catalog_by_code: dict[str, list[dict[str, Any]]] = {}
    if return_codes:
        historic_clause = "" if include_historic else " AND r.status <> 'historic'"
        catalog_rows = conn.execute(
            f"""
            SELECT r.return_id,r.return_code,r.name,r.description,r.estate,r.family,
                   r.status,r.effective_from,r.effective_to,r.effective_text,
                   oe.edition_id
            FROM reporting_return_catalog r
            LEFT JOIN reporting_requirement_edition oe ON oe.legacy_return_id=r.return_id
            WHERE UPPER(r.return_code) IN ({','.join('?' for _ in return_codes)})
              {historic_clause}
            ORDER BY r.return_code,r.effective_from,r.return_id
            """,
            [code.upper() for code in return_codes],
        ).fetchall()
        for row in catalog_rows:
            catalog_by_code.setdefault(row["return_code"].upper(), []).append(dict(row))

    impacted_returns: list[dict[str, Any]] = []
    for entry in by_return.values():
        template_rows = conn.execute(
            """
            SELECT DISTINCT n.node_id,t.template_id,t.template_code,t.title,t.annex,
                   sd.url AS source_url,
                   (SELECT COUNT(*) FROM datapoint d WHERE d.template_id=t.template_id) AS cell_count,
                   (SELECT COUNT(*) FROM instruction i
                    WHERE i.applies_to_type='template'
                      AND i.applies_to_id=t.template_id) AS instruction_count
            FROM graph_edge uses
            JOIN graph_node n
              ON n.node_id=uses.target_node_id AND n.node_type='Template'
            JOIN template t
              ON t.template_id=n.source_pk OR t.template_id=n.node_id
            LEFT JOIN source_document sd ON sd.source_id=t.source_id
            WHERE uses.source_node_id=? AND uses.edge_type='USES_TEMPLATE'
            ORDER BY t.template_code,t.title,t.template_id
            """,
            (entry["data_item_id"],),
        ).fetchall()
        templates = [
            {
                "node_id": row["node_id"],
                "template_id": row["template_id"],
                "template_code": row["template_code"],
                "title": row["title"],
                "annex": row["annex"],
                "source_url": row["source_url"],
                "cell_count": int(row["cell_count"] or 0),
                "instruction_count": int(row["instruction_count"] or 0),
                "impact_tier": "candidate_scope",
            }
            for row in template_rows
        ]
        template_ids = [row["template_id"] for row in templates]
        cell_samples: list[dict[str, Any]] = []
        if template_ids and sample_cells:
            sample_rows = conn.execute(
                f"""
                SELECT d.*,t.template_code,t.title AS template_title,
                       tr.row_code,tr.label AS row_label,tr.row_order,
                       tc.column_code,tc.label AS column_label,tc.column_order,
                       gn.node_id,gn.label AS node_label,gn.properties_json
                FROM datapoint d
                JOIN template t ON t.template_id=d.template_id
                LEFT JOIN template_row tr ON tr.row_id=d.row_id
                LEFT JOIN template_column tc ON tc.column_id=d.column_id
                LEFT JOIN graph_node gn ON gn.node_id=d.datapoint_id
                WHERE d.template_id IN ({','.join('?' for _ in template_ids)})
                ORDER BY t.template_code,COALESCE(tr.row_order,999999),
                         COALESCE(tc.column_order,999999),d.datapoint_id
                LIMIT ?
                """,
                [*template_ids, sample_cells],
            ).fetchall()
            cell_samples = [
                _datapoint_result(row) | {"impact_tier": "candidate_scope"}
                for row in sample_rows
            ]
        entry["instruction_sources"] = list(entry["instruction_sources"].values())
        entry["direct_coordinate_instruction_count"] = len(
            entry.pop("direct_coordinate_instruction_ids")
        )
        entry["direct_coordinates_truncated"] = (
            entry["direct_coordinate_count"] > len(entry["direct_coordinates"])
        )
        entry.pop("direct_coordinates_seen")
        entry["catalog_entries"] = catalog_by_code.get(str(entry["return_code"]).upper(), [])
        entry["templates"] = templates
        entry["candidate_cell_count"] = sum(row["cell_count"] for row in templates)
        entry["candidate_cell_samples"] = cell_samples
        entry["instruction_record_count"] = sum(row["instruction_count"] for row in templates)
        entry["references_truncated"] = entry["reference_count"] > len(entry["references"])
        entry["impact_tier"] = "direct_instruction_reference"
        impacted_returns.append(entry)

    impacted_returns.sort(key=lambda row: (str(row["return_code"]), row["data_item_id"]))
    return {
        "target": _ui_reporting_node(target),
        "impact_model": {
            "direct_instruction_reference": (
                "The reporting instruction source expressly references the changed node."
            ),
            "candidate_scope": (
                "Templates and cells belong to an affected return, but the database "
                "does not yet prove that each item changes."
            ),
            "direct_coordinate_evidence": (
                "The same reporting-instruction passage expressly names the changed "
                "rule and this row, column or cell. This sharply narrows review, but "
                "does not by itself prove that the coordinate must be edited."
            ),
        },
        "returns": impacted_returns,
        "counts": {
            "affected_returns": len(impacted_returns),
            "direct_references": sum(row["reference_count"] for row in impacted_returns),
            "direct_coordinates": sum(
                row["direct_coordinate_count"] for row in impacted_returns
            ),
            "direct_coordinate_instructions": sum(
                row["direct_coordinate_instruction_count"]
                for row in impacted_returns
            ),
            "materialized_direct_cells": sum(
                row["materialized_direct_cell_count"] for row in impacted_returns
            ),
            "instruction_defined_coordinates": sum(
                row["instruction_defined_coordinate_count"]
                for row in impacted_returns
            ),
            "instruction_sources": sum(len(row["instruction_sources"]) for row in impacted_returns),
            "candidate_templates": sum(len(row["templates"]) for row in impacted_returns),
            "candidate_cells": sum(row["candidate_cell_count"] for row in impacted_returns),
        },
        "limitations": [
            "A source-level rule reference is direct evidence for reviewing the instruction source.",
            "A direct coordinate means the same instruction passage names the rule and coordinate; it is review evidence, not an automatically confirmed edit.",
            "Template and other cell results remain candidate scope when no instruction provision links them to a precise row, column or cell.",
            "Instruction-defined coordinates identify explicit row and column codes even where the workbook parser has not materialized a DataPoint.",
        ],
    }


def _public_catalog_return(row: sqlite3.Row) -> dict[str, Any]:
    fields = {
        "return_id", "return_code", "name", "description", "estate", "family",
        "effective_from", "effective_to", "effective_text", "status", "source_page_url",
        "template_count", "instruction_count", "edition_id", "requirement_id", "requirement_type",
        "collection_id", "collection_name", "regime_id", "regime_name",
        "edition_display_name", "edition_display_name_source",
    }
    return {key: row[key] for key in fields if key in row.keys()}


def _public_artifact(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        key: row[key]
        for key in (
            "artifact_id", "url", "display_title", "artifact_role", "estate", "file_type",
            "description", "taxonomy_version", "relationship", "is_primary", "display_order",
            "resource_id", "resolved_display_name", "display_name_source", "inherited_requirement_name",
        )
        if key in row.keys()
    }
    if "sheet_names_json" in row.keys():
        try:
            parsed = json.loads(row["sheet_names_json"] or "[]")
            result["sheet_names"] = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            result["sheet_names"] = []
    else:
        result["sheet_names"] = []
    return result


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

    Return and disclosure-set nodes are the top-level parents. The default graph includes their
    main reporting artefacts and source-document cross-references, but avoids
    the full datapoint explosion unless explicitly requested.
    """
    ensure_reporting_graph_indexes(conn)
    roots = _reporting_root_data_items(conn, q=selected_return or q, limit=limit, exact=bool(selected_return))
    if selected_return and roots and roots[0].get("node_type") == "RequirementEdition":
        return _reporting_ontology_graph(conn, roots[0], limit=child_limit)
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
    _ensure_reporting_node_source_urls(nodes, edges)
    return {
        "level": "reporting_overview",
        "root_count": len(roots),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "available_edge_types": available,
    }


def _reporting_ontology_graph(conn: sqlite3.Connection, root: dict[str, Any], *, limit: int) -> dict[str, Any]:
    """Build the edition-centred graph from the normalized reporting ontology."""
    root_id = root["node_id"]
    nodes: dict[str, dict[str, Any]] = {root_id: _ui_reporting_node(root, role="requirement_edition")}
    edges: dict[str, dict[str, Any]] = {}

    first = _reporting_edges_for_sources(conn, [root_id], sorted(REPORTING_ONTOLOGY_EDGE_TYPES), limit)
    first += _reporting_edges_for_targets(conn, [root_id], ["HAS_EDITION"], limit)
    _add_reporting_edges(conn, first, nodes, edges)

    requirement_ids = [edge["source_node_id"] for edge in first if edge["edge_type"] == "HAS_EDITION"]
    resource_ids = [
        edge["target_node_id"] for edge in first
        if edge["edge_type"] in {"HAS_TEMPLATE_RESOURCE", "HAS_INSTRUCTION_RESOURCE", "HAS_RESOURCE", "SUPPORTED_BY_TAXONOMY"}
    ]
    second_sources = sorted(set(requirement_ids + resource_ids))
    if second_sources:
        second = _reporting_edges_for_sources(
            conn,
            second_sources,
            sorted(REPORTING_ONTOLOGY_EDGE_TYPES),
            limit,
        )
        _add_reporting_edges(conn, second, nodes, edges)
        component_ids = [
            edge["target_node_id"] for edge in second
            if edge["edge_type"] in {"CONTAINS_SHEET", "CONTAINS_INSTRUCTION_SECTION", "HAS_ENTRY_POINT"}
        ]
        if component_ids:
            third = _reporting_edges_for_sources(conn, component_ids, ["IMPLEMENTS_TEMPLATE", "ENCODES_REQUIREMENT"], limit)
            _add_reporting_edges(conn, third, nodes, edges)

    available = _count_by(list(edges.values()), "edge_type")
    _ensure_reporting_node_source_urls(nodes, edges)
    return {
        "level": "reporting_ontology",
        "root_count": 1,
        "centre_id": root_id,
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
    where = "WHERE n.node_type IN ('RequirementEdition','DataItem','ReportingReturn','DisclosureSet')"
    if q:
        if exact:
            where += " AND (n.node_id=? OR n.node_id=? OR n.node_id=? OR n.node_id=? OR n.label=? OR n.source_pk=?)"
            code = q.removeprefix("data_item:")
            params.extend([q, f"data_item:{code}", f"return_version:{q}", f"disclosure_set:{q}", code, q])
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


def _reporting_edges_for_targets(conn: sqlite3.Connection, target_ids: list[str], edge_types: list[str], limit: int) -> list[dict[str, Any]]:
    if not target_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT edge_id,source_node_id,target_node_id,edge_type,properties_json,evidence_span_id,confidence,extraction_method,review_status
        FROM graph_edge
        WHERE target_node_id IN ({','.join('?' for _ in target_ids)})
          AND edge_type IN ({','.join('?' for _ in edge_types)})
        ORDER BY edge_type,source_node_id
        LIMIT ?
        """,
        [*target_ids, *edge_types, limit],
    ).fetchall()
    return [_graph_edge(row) for row in rows]


def _add_reporting_edges(conn: sqlite3.Connection, rows: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], edges: dict[str, dict[str, Any]]) -> None:
    rows = [
        row for row in rows
        if row.get("source_node_id") != row.get("target_node_id")
    ]
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

    Some PRA reporting pages retain historical PDFs and taxonomy packages
    alongside the current one. Showing every superseded file creates duplicate
    evidence nodes and makes the graph look contradictory. Keep the highest
    explicit Q&A version and the latest explicit taxonomy package for XML/XSD
    artefacts, then drop older versions from the visible reporting graph.
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

    node_meta: dict[str, dict[str, Any]] = {}
    families: dict[str, list[tuple[int, str]]] = {}
    for row in meta_rows:
        props = _json(row["properties_json"] or "{}")
        title = str(row["source_title"] or row["label"] or props.get("title") or "")
        url = str(row["source_url"] or props.get("url") or "")
        file_type = str(row["source_file_type"] or props.get("file_type") or "").lower()
        node_meta[row["node_id"]] = {"title": title, "url": url, "file_type": file_type}
        family = _versioned_q_and_a_family(title, url)
        if family:
            families.setdefault(family, []).append((_source_document_version(url), row["node_id"]))

    drop: set[str] = set()
    represented_source_urls = _represented_reporting_source_urls(conn, rows)
    if represented_source_urls:
        for node_id, meta in node_meta.items():
            if _normalise_reporting_source_url(meta.get("url", "")) in represented_source_urls:
                drop.add(node_id)

    for versions in families.values():
        if len(versions) <= 1:
            continue
        current_version = max(version for version, _ in versions)
        current_ids = {node_id for version, node_id in versions if version == current_version}
        drop.update(node_id for _, node_id in versions if node_id not in current_ids)

    taxonomy_versions_by_source: dict[str, list[tuple[tuple[int, int, int, int], str]]] = {}
    for row in rows:
        if row.get("edge_type") != "EVIDENCED_BY":
            continue
        meta = node_meta.get(row.get("target_node_id"))
        if not meta or meta["file_type"] not in {"xml", "xsd"}:
            continue
        version = _taxonomy_package_version(meta["url"])
        if version:
            taxonomy_versions_by_source.setdefault(row["source_node_id"], []).append((version, row["target_node_id"]))
    for versions in taxonomy_versions_by_source.values():
        if len(versions) <= 1:
            continue
        current_version = max(version for version, _ in versions)
        current_ids = {node_id for version, node_id in versions if version == current_version}
        drop.update(node_id for _, node_id in versions if node_id not in current_ids)

    if not drop:
        return rows
    return [r for r in rows if r.get("target_node_id") not in drop and r.get("source_node_id") not in drop]


def _represented_reporting_source_urls(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> set[str]:
    """Return source-document URLs already represented by semantic artefact nodes.

    The selected-return graph often has both a higher-level node such as
    ``instruction_set:COREP-CCR`` and its provenance source document
    ``Annex XXVI (PDF)``. If both point to the same PDF, showing both as graph
    nodes is duplicate navigation noise. Keep the higher-level artefact node
    and suppress the duplicate SourceDocument node.
    """
    artefact_ids = sorted({
        r.get("target_node_id")
        for r in rows
        if r.get("edge_type") in {"USES_TEMPLATE", "USES_INSTRUCTIONS"}
        and r.get("target_node_id")
    })
    if not artefact_ids:
        return set()
    fetched = conn.execute(
        f"""
        SELECT node_id,properties_json
        FROM graph_node
        WHERE node_id IN ({','.join('?' for _ in artefact_ids)})
        """,
        artefact_ids,
    ).fetchall()
    source_ids: set[str] = set()
    for row in fetched:
        props = _json(row["properties_json"] or "{}")
        for source_id in props.get("source_document_ids") or []:
            if source_id:
                source_ids.add(str(source_id))
    if not source_ids:
        return set()
    docs = conn.execute(
        f"""
        SELECT url
        FROM source_document
        WHERE source_id IN ({','.join('?' for _ in source_ids)})
        """,
        sorted(source_ids),
    ).fetchall()
    return {_normalise_reporting_source_url(row["url"]) for row in docs if row["url"]}


def _normalise_reporting_source_url(url: str) -> str:
    return re.sub(r"/$", "", re.sub(r"[?#].*$", "", str(url or "").strip().lower()))


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


def _taxonomy_package_version(url: str) -> tuple[int, int, int, int] | None:
    text = url.lower()
    match = re.search(r"banking[_-](\d+)\.(\d+)\.(\d+)", text)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1 if "hotfix" in text else 0)
    match = re.search(r"banking[-_]?v?(\d)(\d)(\d)", text)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1 if "hotfix" in text else 0)
    return None


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
            "url": nodes.get(template_id, {}).get("url") or "",
            "metadata": {
                "reporting_role": "datapoint_summary",
                "template_id": template_id,
                "source_url": nodes.get(template_id, {}).get("url") or "",
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
            "source_url": nodes.get(template_id, {}).get("url") or "",
            "metadata": {"datapoint_count": count, "sample_datapoints": labels},
        }


def _ensure_reporting_node_source_urls(nodes: dict[str, dict[str, Any]], edges: dict[str, dict[str, Any]]) -> None:
    """Populate a source URL on every node emitted to the reporting UI.

    Reporting graph nodes are a mixture of canonical source documents, parsed
    templates, return-level roll-ups and materialised references. Many of the
    roll-up/reference nodes do not have their own URL in graph_node, but in the
    reporting UI every visible node must still take the user back to the source
    evidence that put it in the graph. Prefer a node's own URL, then metadata
    URLs, then connected source-document/evidence neighbours.
    """
    for node in nodes.values():
        _set_reporting_node_url(node, _node_direct_source_url(node))

    source_document_urls = {
        node_id: node.get("url")
        for node_id, node in nodes.items()
        if node.get("node_type") == "SourceDocument" and _is_http_url(node.get("url"))
    }

    for edge in edges.values():
        source_id = edge.get("from_node_id")
        target_id = edge.get("to_node_id")
        source_node = nodes.get(source_id)
        target_node = nodes.get(target_id)
        edge_url = _first_http_url(edge.get("source_url"), (edge.get("metadata") or {}).get("source_url"), (edge.get("metadata") or {}).get("url"))

        if edge.get("edge_type") == "EVIDENCED_BY" and target_id in source_document_urls:
            evidence_url = source_document_urls[target_id]
            _set_reporting_node_url(source_node, evidence_url)
            edge["source_url"] = evidence_url
        elif edge_url:
            _set_reporting_node_url(source_node, edge_url)
            _set_reporting_node_url(target_node, edge_url)

    for edge in edges.values():
        source_node = nodes.get(edge.get("from_node_id"))
        target_node = nodes.get(edge.get("to_node_id"))
        if source_node and target_node:
            if not _is_http_url(target_node.get("url")):
                _set_reporting_node_url(target_node, source_node.get("url"))
            if not _is_http_url(source_node.get("url")):
                _set_reporting_node_url(source_node, target_node.get("url"))
        if not _is_http_url(edge.get("source_url")):
            edge["source_url"] = _first_http_url(source_node.get("url") if source_node else None, target_node.get("url") if target_node else None) or ""


def _set_reporting_node_url(node: dict[str, Any] | None, url: Any) -> None:
    if not node or not _is_http_url(url) or _is_http_url(node.get("url")):
        return
    text = str(url)
    node["url"] = text
    metadata = node.setdefault("metadata", {})
    metadata.setdefault("source_url", text)


def _node_direct_source_url(node: dict[str, Any]) -> str:
    metadata = node.get("metadata") or {}
    return _first_http_url(
        node.get("url"),
        metadata.get("url"),
        metadata.get("source_url"),
        metadata.get("source_parent_url"),
        metadata.get("parent_url"),
        metadata.get("document_url"),
        metadata.get("target_url"),
        metadata.get("original_url"),
    ) or ""


def _first_http_url(*values: Any) -> str:
    for value in values:
        if _is_http_url(value):
            return str(value)
    return ""


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


FORBIDDEN_REPORTING_METADATA_KEYS = {
    "audit_cleanup",
    "cleanup_decision",
    "cleanup_reason",
    "decision",
    "decision_reason",
    "model",
    "model_name",
    "prompt_version",
    "template_enrichment_model",
    "template_enrichment_prompt_version",
    "template_enrichment_input_hash",
}


def _public_reporting_metadata(props: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in props.items()
        if key not in FORBIDDEN_REPORTING_METADATA_KEYS
        and not key.endswith("_model")
        and not key.endswith("_prompt_version")
        and not key.endswith("_input_hash")
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
    node_ids = sorted({n.get("node_id") for n in nodes if n.get("node_id")})

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

    template_enrichment = _latest_template_enrichment(conn, template_ids)

    explicit_source_document_ids = sorted({
        str(source_id)
        for node in nodes
        for source_id in (node.get("properties") or {}).get("source_document_ids", [])
        if source_id
    })
    template_source_ids = sorted({
        str(source_id)
        for node in nodes
        if node.get("node_type") == "Template"
        for source_id in [
            template_meta.get(node.get("source_pk") or node.get("node_id"), {}).get("source_id")
            or (node.get("properties") or {}).get("source_id")
        ]
        if source_id
    })
    source_ids = sorted(set(source_ids) | set(template_source_ids) | set(explicit_source_document_ids))

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
            canonical = template_meta.get(node.get("source_pk") or node.get("node_id"), {})
            enrichment = template_enrichment.get(node.get("source_pk") or node.get("node_id"), {})
            source_id = canonical.get("source_id") or props.get("source_id")
            source = source_meta.get(source_id, {}) if source_id else {}
            props = source | canonical | enrichment | props
        elif node.get("node_type") == "SourceDocument":
            props = source_meta.get(node.get("source_pk") or node.get("node_id"), {}) | props
        else:
            explicit_ids = [str(source_id) for source_id in props.get("source_document_ids", []) if source_id]
            explicit_sources = [source_meta[source_id] for source_id in explicit_ids if source_id in source_meta]
            if explicit_sources:
                props = explicit_sources[0] | props
        node["properties"] = props
    evidence_urls = _template_source_urls(conn, node_ids) | _evidence_source_urls(conn, node_ids)
    for node in nodes:
        url = evidence_urls.get(node.get("node_id"))
        if not url:
            continue
        props = dict(node.get("properties") or {})
        props.setdefault("source_url", url)
        node["properties"] = props
    return nodes


def _latest_template_enrichment(conn: sqlite3.Connection, template_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not template_ids:
        return {}
    try:
        rows = conn.execute(
            f"""
            SELECT template_id,model,prompt_version,input_hash,purpose,contents,summary,key_rows_json,quality_notes,updated_at
            FROM reporting_template_enrichment
            WHERE template_id IN ({','.join('?' for _ in template_ids)})
              AND status='ok'
            """,
            template_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    enrichment: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            key_rows = json.loads(row["key_rows_json"] or "[]")
        except json.JSONDecodeError:
            key_rows = []
        if not isinstance(key_rows, list):
            key_rows = []
        enrichment[row["template_id"]] = {
            "template_purpose": row["purpose"],
            "template_contents": row["contents"],
            "template_summary": row["summary"],
            "template_key_rows": [str(x) for x in key_rows if x],
            "template_quality_notes": row["quality_notes"],
            "template_enriched_at": row["updated_at"],
        }
    return enrichment


def _evidence_source_urls(conn: sqlite3.Connection, node_ids: list[str]) -> dict[str, str]:
    if not node_ids:
        return {}
    try:
        rows = conn.execute(
            f"""
            SELECT e.source_node_id,sd.url,sd.parent_url
            FROM graph_edge e
            JOIN graph_node n ON n.node_id=e.target_node_id
            LEFT JOIN source_document sd ON sd.source_id=n.source_pk OR sd.source_id=n.node_id
            WHERE e.source_node_id IN ({','.join('?' for _ in node_ids)})
              AND e.edge_type='EVIDENCED_BY'
              AND n.node_type='SourceDocument'
              AND COALESCE(sd.url,'') <> ''
            ORDER BY e.source_node_id,
              CASE LOWER(COALESCE(sd.file_type,'')) WHEN 'html' THEN 0 WHEN 'xlsx' THEN 1 WHEN 'xls' THEN 1 WHEN 'pdf' THEN 2 ELSE 3 END,
              sd.url
            """,
            node_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    urls: dict[str, str] = {}
    for row in rows:
        urls.setdefault(row["source_node_id"], row["url"] or row["parent_url"] or "")
    return {node_id: url for node_id, url in urls.items() if _is_http_url(url)}


def _template_source_urls(conn: sqlite3.Connection, node_ids: list[str]) -> dict[str, str]:
    if not node_ids:
        return {}
    try:
        rows = conn.execute(
            f"""
            SELECT e.source_node_id,sd.url,sd.parent_url
            FROM graph_edge e
            JOIN graph_node template_node ON template_node.node_id=e.target_node_id
            JOIN template t ON t.template_id=template_node.source_pk OR t.template_id=template_node.node_id
            JOIN source_document sd ON sd.source_id=t.source_id
            WHERE e.source_node_id IN ({','.join('?' for _ in node_ids)})
              AND e.edge_type='USES_TEMPLATE'
              AND COALESCE(sd.url,'') <> ''
            ORDER BY e.source_node_id,sd.url
            """,
            node_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    urls: dict[str, str] = {}
    for row in rows:
        urls.setdefault(row["source_node_id"], row["url"] or row["parent_url"] or "")
    return {node_id: url for node_id, url in urls.items() if _is_http_url(url)}


def _ui_reporting_node(node: dict[str, Any], *, role: str | None = None) -> dict[str, Any]:
    props = _public_reporting_metadata(dict(node.get("properties") or {}))
    text_parts = [props.get("description"), props.get("title"), props.get("reporting_domain"), props.get("submission_system")]
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


def _template_identity_key(value: str | None) -> str:
    # Whitespace and import-only underscores are cosmetic, but punctuation
    # within an official template code is not: FINREP 1.1 and FINREP 11 are
    # different templates.
    return re.sub(r"[\s_]+", "", (value or "").lower())


def _template_projection_key(template: dict[str, Any]) -> tuple[str, str]:
    """Identify duplicate DataItem projections of one workbook sheet."""
    template_id = str(template.get("template_id") or "")
    template_code = str(template.get("template_code") or "")
    suffix = template_id.split(":", 2)[-1].strip("_")
    prefix = f"{template_code}_"
    if template_code and suffix.upper().startswith(prefix.upper()):
        suffix = suffix[len(prefix):].strip("_")
    return (
        _normalise_reporting_source_url(str(template.get("source_url") or "")),
        _template_identity_key(suffix or template_code),
    )


def _dedupe_template_summaries(
    templates: list[dict[str, Any]],
    *,
    preferred_code: str,
) -> list[dict[str, Any]]:
    by_projection: dict[tuple[str, str], dict[str, Any]] = {}
    preferred = preferred_code.upper()
    for template in templates:
        key = _template_projection_key(template)
        current = by_projection.get(key)
        if current is None:
            by_projection[key] = template
            continue
        candidate_score = (
            str(template.get("template_code") or "").upper() == preferred,
            int(template.get("cell_count") or 0),
        )
        current_score = (
            str(current.get("template_code") or "").upper() == preferred,
            int(current.get("cell_count") or 0),
        )
        if candidate_score > current_score:
            by_projection[key] = template
    return list(by_projection.values())


def _graph_template_summary(row: sqlite3.Row) -> dict[str, Any]:
    properties = _json(row["properties_json"])
    data_item_code = str(properties.get("data_item_code") or "")
    suffix = str(row["node_id"]).split(":", 2)[-1].strip("_")
    prefix = f"{data_item_code}_"
    if data_item_code and suffix.upper().startswith(prefix.upper()):
        suffix = suffix[len(prefix):].strip("_")
    template_code = str(properties.get("template_code") or "").strip()
    if not template_code:
        template_code = re.sub(r"_+", " ", suffix).strip() or row["label"] or row["node_id"]
    title = str(properties.get("template_title") or "").strip() or f"Template {template_code}"
    return {
        "node_id": row["node_id"],
        "template_id": row["node_id"],
        "template_code": template_code,
        "title": title,
        "annex": None,
        "source_url": row["source_url"],
        "source_title": row["source_title"],
        "cell_count": int(row["cell_count"] or 0),
        "row_count": int(row["row_count"] or 0),
        "column_count": int(row["column_count"] or 0),
    }


def _fill_graph_coordinate(result: dict[str, Any]) -> None:
    match = re.search(
        r":r([^:]+):c([^:]+)$",
        str(result.get("datapoint_id") or ""),
    )
    if match:
        result["row_code"] = result.get("row_code") or match.group(1)
        result["column_code"] = result.get("column_code") or match.group(2)
    for axis in ("row", "column"):
        code = str(result.get(f"{axis}_code") or "")
        label = str(result.get(f"{axis}_label") or "")
        if code and label:
            result[f"{axis}_label"] = re.sub(
                rf"^.*?\b{axis}\s+{re.escape(code)}\s*",
                "",
                label,
                count=1,
                flags=re.I,
            ) or label


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
        "row_order": d.get("row_order"),
        "column_id": d.get("column_id"),
        "column_code": d.get("column_code"),
        "column_label": d.get("column_label"),
        "column_order": d.get("column_order"),
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
