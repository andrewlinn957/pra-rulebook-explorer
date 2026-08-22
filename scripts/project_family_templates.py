#!/usr/bin/env python3
"""Project every official PRA, RFB and LVR template into the Cells viewer.

The reporting catalogue is edition-centred, while the cell explorer consumes
legacy/graph ``Template`` nodes.  Older package extraction only projected
worksheets that happened to contain an easily recognised row-code pattern.
That left complete official workbooks downloaded but invisible in Cells.

This projection is deliberately additive:

* every visible worksheet in an official XLSX/XLSM/XLTX template artifact gets
  one exact-source Template projection;
* every official PDF-only template artifact gets one document Template;
* existing templates and datapoints are reused and never deleted;
* USES_TEMPLATE/CONTAINS edges retain source-span evidence.

Run without ``--apply`` for an audit-only dry run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import DEFAULT_DB, configure_connection
from backend.app.xlsx_layout import _select_sheet, _sheet_targets
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect


MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
WORKBOOK_TYPES = {"xlsx", "xlsm", "xltx"}
TARGET_CODE = re.compile(r"^(?:PRA|RFB|LVR)\d{3}$", re.I)


def stable(prefix: str, *parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:100] or "template"


def normalise_sheet_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def sheet_from_template_id(
    template_id: str,
    sheet_names: list[str],
) -> str | None:
    local_id = template_id.split(":", 2)[-1]
    normalised_id = normalise_sheet_name(local_id)
    matches = [
        sheet_name
        for sheet_name in sheet_names
        if normalise_sheet_name(sheet_name)
        and normalised_id.endswith(normalise_sheet_name(sheet_name))
    ]
    if not matches:
        return None
    return max(matches, key=lambda name: len(normalise_sheet_name(name)))


def workbook_sheets(path: Path, *, include_hidden: bool = False) -> list[str]:
    return [
        name
        for name, state in workbook_sheet_states(path)
        if include_hidden or state == "visible"
    ]


def workbook_sheet_states(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    return [
        (
            sheet.attrib.get("name", "Sheet"),
            sheet.attrib.get("state", "visible"),
        )
        for sheet in workbook.findall(f"{MAIN}sheets/{MAIN}sheet")
    ]


def artifact_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT r.return_code,a.artifact_id,a.display_title,a.url,
               lower(a.file_type) AS file_type,
               sd.source_id,sd.local_path,sd.title AS source_title
        FROM reporting_return_catalog r
        JOIN reporting_return_artifact ra
          ON ra.return_id=r.return_id AND ra.relationship='template'
        JOIN reporting_artifact a ON a.artifact_id=ra.artifact_id
        JOIN source_document sd ON sd.url=a.url
        WHERE (
          upper(r.return_code) GLOB 'PRA[0-9][0-9][0-9]'
          OR upper(r.return_code) GLOB 'RFB[0-9][0-9][0-9]'
          OR upper(r.return_code) GLOB 'LVR[0-9][0-9][0-9]'
        )
          AND lower(a.file_type) IN ('xlsx','xlsm','xltx','pdf')
          AND COALESCE(sd.local_path,'')<>''
        ORDER BY r.return_code,a.display_title,sd.source_id
        """
    ).fetchall()


def template_rows_for_source(
    conn: sqlite3.Connection,
    source_id: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT DISTINCT n.node_id,n.label,n.source_table,
               COALESCE(t.template_code,
                        json_extract(n.properties_json,'$.data_item_code'),
                        n.label) AS template_code,
               COALESCE(t.title,n.label) AS title,
               n.properties_json,
               (SELECT COUNT(*) FROM datapoint d
                WHERE d.template_id=COALESCE(t.template_id,n.node_id)) AS cell_count
        FROM graph_node n
        LEFT JOIN template t
          ON t.template_id=n.source_pk OR t.template_id=n.node_id
        WHERE n.node_type='Template'
          AND (
            t.source_id=?
            OR (
              t.template_id IS NULL
              AND json_extract(n.properties_json,'$.source_id')=?
            )
          )
        ORDER BY cell_count DESC,n.node_id
        """,
        (source_id, source_id),
    ).fetchall()


def resolved_template_assignments(
    conn: sqlite3.Connection,
    source_id: str,
    path: Path,
) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        sheets = _sheet_targets(archive)
    sheet_names = [name for name, _ in sheets]
    result: dict[str, str] = {}
    for row in template_rows_for_source(conn, source_id):
        try:
            properties = json.loads(row["properties_json"] or "{}")
        except json.JSONDecodeError:
            properties = {}
        explicit_sheet = properties.get("sheet_name")
        if (
            properties.get("projection_method")
            == "official_template_projection"
            and explicit_sheet in sheet_names
        ):
            result[row["node_id"]] = explicit_sheet
            continue
        id_sheet = sheet_from_template_id(row["node_id"], sheet_names)
        if id_sheet is not None:
            result[row["node_id"]] = id_sheet
            continue
        selected = _select_sheet(
            sheets,
            template_id=row["node_id"],
            template_code=row["template_code"] or "",
            title=row["title"] or "",
        )
        if selected is not None:
            result[row["node_id"]] = selected[0]
    return result


def existing_sheet_templates(
    conn: sqlite3.Connection,
    source_id: str,
    path: Path,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for template_id, sheet_name in resolved_template_assignments(
        conn,
        source_id,
        path,
    ).items():
        result.setdefault(sheet_name, template_id)
    return result


def evidence_span(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    sheet_name: str | None = None,
) -> str:
    row = None
    if sheet_name:
        row = conn.execute(
            """
            SELECT span_id FROM source_span
            WHERE source_id=? AND sheet_name=?
            ORDER BY CASE span_type WHEN 'xlsx_sheet' THEN 0 ELSE 1 END,
                     row_number,column_number,span_id
            LIMIT 1
            """,
            (source_id, sheet_name),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT span_id FROM source_span
            WHERE source_id=?
            ORDER BY CASE span_type
                       WHEN 'xlsx_workbook' THEN 0
                       WHEN 'pdf_page' THEN 1
                       WHEN 'document_anchor' THEN 2
                       ELSE 3
                     END,
                     page_number,row_number,span_id
            LIMIT 1
            """,
            (source_id,),
        ).fetchone()
    if row is not None:
        return row["span_id"]
    source = conn.execute(
        "SELECT title FROM source_document WHERE source_id=?",
        (source_id,),
    ).fetchone()
    title = source["title"] if source else source_id
    span_id = stable("span", source_id, "document_anchor", title)
    conn.execute(
        """
        INSERT OR IGNORE INTO source_span(
          span_id,source_id,span_type,anchor,raw_text,normalised_text
        ) VALUES (?,?,?,?,?,?)
        """,
        (span_id, source_id, "document_anchor", "document", title, title),
    )
    return span_id


def template_set_for_code(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute(
        """
        SELECT n.node_id
        FROM graph_edge e
        JOIN graph_node n
          ON n.node_id=e.target_node_id AND n.node_type='TemplateSet'
        WHERE e.source_node_id=? AND e.edge_type='USES_TEMPLATE'
        ORDER BY n.node_id
        LIMIT 1
        """,
        (f"data_item:{code}",),
    ).fetchone()
    if row:
        return row["node_id"]
    template_set_id = f"template_set:{code}"
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_node(
          node_id,node_type,label,properties_json,review_status
        ) VALUES (?,?,?,?,?)
        """,
        (
            template_set_id,
            "TemplateSet",
            f"{code} template set",
            json.dumps({"data_item_code": code}),
            "candidate",
        ),
    )
    return template_set_id


def ensure_edge(
    conn: sqlite3.Connection,
    source_node_id: str,
    edge_type: str,
    target_node_id: str,
    span_id: str,
    explanation: str,
) -> None:
    method = "official_template_projection"
    edge_id = stable(
        "edge",
        source_node_id,
        edge_type,
        target_node_id,
        span_id,
        method,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_edge(
          edge_id,source_node_id,target_node_id,edge_type,properties_json,
          evidence_span_id,confidence,extraction_method,review_status
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            edge_id,
            source_node_id,
            target_node_id,
            edge_type,
            json.dumps({"explanation": explanation}),
            span_id,
            1.0,
            method,
            "accepted_candidate",
        ),
    )


def available_template_id(
    conn: sqlite3.Connection,
    code: str,
    label: str,
    source_id: str,
) -> str:
    base = f"template:{code}:{code}_{slug(label)}"
    row = conn.execute(
        """
        SELECT COALESCE(t.source_id,json_extract(n.properties_json,'$.source_id'))
        FROM graph_node n
        LEFT JOIN template t
          ON t.template_id=n.source_pk OR t.template_id=n.node_id
        WHERE n.node_id=?
        """,
        (base,),
    ).fetchone()
    if row is None or row[0] == source_id:
        return base
    return f"{base}_{slug(source_id)[-12:]}"


def merge_properties(raw: str | None, additions: dict[str, Any]) -> str:
    try:
        properties = json.loads(raw or "{}")
    except json.JSONDecodeError:
        properties = {}
    properties.update(additions)
    return json.dumps(properties, ensure_ascii=False, sort_keys=True)


def ensure_template(
    conn: sqlite3.Connection,
    *,
    template_id: str,
    code: str,
    template_code: str,
    title: str,
    source_id: str,
    properties: dict[str, Any],
) -> None:
    properties = {
        "template_code": template_code,
        "template_title": title,
        **properties,
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO template(
          template_id,template_code,title,annex,source_id
        ) VALUES (?,?,?,?,?)
        """,
        (
            template_id,
            template_code,
            title,
            "official template artifact",
            source_id,
        ),
    )
    existing = conn.execute(
        "SELECT properties_json FROM graph_node WHERE node_id=?",
        (template_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO graph_node(
          node_id,node_type,label,source_table,source_pk,properties_json,
          review_status
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(node_id) DO UPDATE SET
          node_type='Template',
          label=excluded.label,
          source_table=COALESCE(graph_node.source_table,excluded.source_table),
          source_pk=COALESCE(graph_node.source_pk,excluded.source_pk),
          properties_json=excluded.properties_json
        """,
        (
            template_id,
            "Template",
            f"{code} — {template_code}",
            "template",
            template_id,
            merge_properties(
                existing["properties_json"] if existing else None,
                properties,
            ),
            "accepted_candidate",
        ),
    )


def link_template(
    conn: sqlite3.Connection,
    *,
    code: str,
    template_id: str,
    span_id: str,
) -> None:
    data_item_id = f"data_item:{code}"
    if not conn.execute(
        "SELECT 1 FROM graph_node WHERE node_id=?",
        (data_item_id,),
    ).fetchone():
        raise RuntimeError(f"Missing reporting DataItem node: {data_item_id}")
    template_set_id = template_set_for_code(conn, code)
    ensure_edge(
        conn,
        data_item_id,
        "USES_TEMPLATE",
        template_set_id,
        span_id,
        "Official template artifact belongs to this reporting data item.",
    )
    ensure_edge(
        conn,
        data_item_id,
        "USES_TEMPLATE",
        template_id,
        span_id,
        "Official workbook worksheet or PDF template for this reporting item.",
    )
    ensure_edge(
        conn,
        template_set_id,
        "CONTAINS",
        template_id,
        span_id,
        "Official template set contains this worksheet or PDF template.",
    )


def project(
    conn: sqlite3.Connection,
    *,
    include_hidden: bool = False,
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    coverage: list[dict[str, Any]] = []
    for artifact in artifact_sources(conn):
        code = artifact["return_code"].upper()
        if not TARGET_CODE.fullmatch(code):
            continue
        path = (ROOT / artifact["local_path"]).resolve()
        if ROOT not in path.parents or not path.exists():
            counts["missing_files"] += 1
            continue
        file_type = artifact["file_type"]
        if file_type in WORKBOOK_TYPES:
            states = dict(workbook_sheet_states(path))
            sheets = [
                name
                for name, state in states.items()
                if include_hidden or state == "visible"
            ]
            assignments = resolved_template_assignments(
                conn,
                artifact["source_id"],
                path,
            )
            for template_id, sheet_name in assignments.items():
                node = conn.execute(
                    """
                    SELECT properties_json FROM graph_node WHERE node_id=?
                    """,
                    (template_id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE graph_node SET properties_json=? WHERE node_id=?
                    """,
                    (
                        merge_properties(
                            node["properties_json"] if node else None,
                            {
                                "sheet_name": sheet_name,
                                "sheet_state": states.get(
                                    sheet_name,
                                    "visible",
                                ),
                                "template_code": sheet_name,
                                "template_title": sheet_name,
                            },
                        ),
                        template_id,
                    ),
                )
            existing: dict[str, str] = {}
            for template_id, sheet_name in assignments.items():
                if sheet_name in sheets:
                    existing.setdefault(sheet_name, template_id)
            created = 0
            for sheet_name in sheets:
                template_id = existing.get(sheet_name)
                if template_id is None:
                    template_id = available_template_id(
                        conn,
                        code,
                        sheet_name,
                        artifact["source_id"],
                    )
                    ensure_template(
                        conn,
                        template_id=template_id,
                        code=code,
                        template_code=sheet_name,
                        title=sheet_name,
                        source_id=artifact["source_id"],
                        properties={
                            "data_item_code": code,
                            "file_type": file_type,
                            "projection_method": "official_template_projection",
                            "sheet_name": sheet_name,
                            "source_id": artifact["source_id"],
                        },
                    )
                    existing[sheet_name] = template_id
                    created += 1
                    counts["workbook_templates_created"] += 1
                else:
                    counts["workbook_templates_reused"] += 1
                link_template(
                    conn,
                    code=code,
                    template_id=template_id,
                    span_id=evidence_span(
                        conn,
                        artifact["source_id"],
                        sheet_name=sheet_name,
                    ),
                )
            coverage.append(
                {
                    "code": code,
                    "source_id": artifact["source_id"],
                    "file_type": file_type,
                    "expected_templates": len(sheets),
                    "created": created,
                    "resolved_templates": len(existing),
                }
            )
            counts["workbooks"] += 1
            counts["visible_worksheets"] += len(sheets)
        elif file_type == "pdf":
            page_count = len(PdfReader(path).pages)
            existing_rows = template_rows_for_source(
                conn,
                artifact["source_id"],
            )
            if existing_rows:
                template_id = existing_rows[0]["node_id"]
                counts["pdf_templates_reused"] += 1
                created = 0
            else:
                template_id = available_template_id(
                    conn,
                    code,
                    "PDF",
                    artifact["source_id"],
                )
                ensure_template(
                    conn,
                    template_id=template_id,
                    code=code,
                    template_code=code,
                    title=artifact["display_title"] or f"{code} PDF template",
                    source_id=artifact["source_id"],
                    properties={
                        "data_item_code": code,
                        "file_type": "pdf",
                        "page_count": page_count,
                        "projection_method": "official_template_projection",
                        "source_id": artifact["source_id"],
                    },
                )
                counts["pdf_templates_created"] += 1
                created = 1
            link_template(
                conn,
                code=code,
                template_id=template_id,
                span_id=evidence_span(conn, artifact["source_id"]),
            )
            coverage.append(
                {
                    "code": code,
                    "source_id": artifact["source_id"],
                    "file_type": "pdf",
                    "expected_templates": 1,
                    "created": created,
                    "resolved_templates": 1,
                    "pages": page_count,
                }
            )
            counts["pdf_documents"] += 1
            counts["pdf_pages"] += page_count
    return {"counts": dict(counts), "coverage": coverage}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    conn = configure_connection(connect(args.database))
    try:
        result = project(conn, include_hidden=args.include_hidden)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
        result["applied"] = args.apply
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()