#!/usr/bin/env python3
"""Validate source-backed evidence routes for reporting graph sources."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect as connect_db

DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"


def source_node_id(source_id: str) -> str:
    return f"source_document:{source_id}"


def has_instruction_evidence(conn: sqlite3.Connection, source_id: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM graph_edge e
            JOIN graph_node src ON src.node_id=e.source_node_id
            JOIN graph_node tgt ON tgt.node_id=e.target_node_id
            WHERE e.edge_type='EVIDENCED_BY'
              AND src.node_type='InstructionSet'
              AND tgt.node_id=?
            LIMIT 1
            """,
            (source_node_id(source_id),),
        ).fetchone()
    )


def has_template_route(conn: sqlite3.Connection, source_id: str) -> bool:
    sid = source_node_id(source_id)
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM graph_edge e
            JOIN graph_node n
              ON n.node_id=CASE
                WHEN e.source_node_id=? THEN e.target_node_id
                ELSE e.source_node_id
              END
            WHERE (e.source_node_id=? OR e.target_node_id=?)
              AND n.node_type IN ('Template','TemplateSet','Worksheet','LogicalTemplate','ReportingResource','DataItem','ReportingReturn','RequirementEdition')
            LIMIT 1
            """,
            (sid, sid, sid),
        ).fetchone()
    )


def has_taxonomy_route(conn: sqlite3.Connection, source_id: str) -> bool:
    sid = source_node_id(source_id)
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM graph_edge e
            JOIN graph_node n
              ON n.node_id=CASE
                WHEN e.source_node_id=? THEN e.target_node_id
                ELSE e.source_node_id
              END
            WHERE (e.source_node_id=? OR e.target_node_id=?)
              AND (
                n.node_type IN ('TaxonomyRelease','TaxonomyEntryPoint','ReportingResource','ReportingReturn','RequirementEdition','DataItem')
                OR e.edge_type IN ('SUPPORTED_BY_TAXONOMY','HAS_TAXONOMY_RESOURCE','HAS_ENTRY_POINT','ENCODES_REQUIREMENT')
              )
            LIMIT 1
            """,
            (sid, sid, sid),
        ).fetchone()
    )


def validate_source_evidence(
    db_path: Path = DB_PATH,
    *,
    root: Path = ROOT,
    report_path: Path | None = None,
) -> dict[str, Any]:
    conn = connect_db(db_path)
    rows = conn.execute(
        """
        SELECT sd.source_id,sd.title,sd.url,sd.local_path,sd.file_type,c.source_kind
        FROM source_document_cleanup c
        JOIN source_document sd ON sd.source_id=c.source_id
        WHERE c.decision IN ('canonical','duplicate_candidate')
        ORDER BY c.source_kind,sd.source_id
        """
    ).fetchall()
    findings: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        source_kind = row["source_kind"] or "unknown"
        counts[source_kind] = counts.get(source_kind, 0) + 1
        base = {
            "source_id": row["source_id"],
            "source_kind": source_kind,
            "title": row["title"],
            "url": row["url"],
            "local_path": row["local_path"],
            "path_exists": bool(row["local_path"] and (root / row["local_path"]).exists()),
        }
        if source_kind == "instruction_pdf" and not has_instruction_evidence(conn, row["source_id"]):
            findings.append(
                {
                    **base,
                    "severity": "error",
                    "finding": "instruction_pdf_without_instruction_evidence_edge",
                    "expected": "InstructionSet -> EVIDENCED_BY -> SourceDocument",
                }
            )
        elif source_kind in {"template_pdf", "template_workbook"} and not has_template_route(conn, row["source_id"]):
            findings.append(
                {
                    **base,
                    "severity": "warning",
                    "finding": "template_source_without_template_route",
                    "expected": "Template, TemplateSet, ReportingResource or return route",
                }
            )
        elif source_kind == "taxonomy_package" and not has_taxonomy_route(conn, row["source_id"]):
            findings.append(
                {
                    **base,
                    "severity": "warning",
                    "finding": "taxonomy_source_without_taxonomy_route",
                    "expected": "TaxonomyRelease, TaxonomyEntryPoint, ReportingResource or supported-by-taxonomy route",
                }
            )
    conn.close()
    errors = sum(1 for finding in findings if finding["severity"] == "error")
    warnings = sum(1 for finding in findings if finding["severity"] == "warning")
    result = {
        "status": "fail" if errors else "pass",
        "sources": len(rows),
        "counts_by_source_kind": counts,
        "errors": errors,
        "warnings": warnings,
        "findings": findings,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    result = validate_source_evidence(args.db, root=args.root, report_path=args.report)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
