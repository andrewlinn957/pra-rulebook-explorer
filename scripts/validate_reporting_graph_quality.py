#!/usr/bin/env python3
"""Quality gate for the PRA reporting graph.

The gate is intentionally deterministic: it checks graph model invariants,
cleanup state and source-dedupe safety without exposing audit metadata through
graph nodes or the frontend API.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect as connect_db

DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"


@dataclass(frozen=True)
class SqlCheck:
    check_id: str
    description: str
    sql: str
    severity: str = "error"
    expect: str = "zero_count"


SQL_CHECKS = [
    SqlCheck(
        "graph_nodes_no_audit_cleanup",
        "Graph node properties must not expose audit cleanup metadata.",
        """
        select count(*) as graph_nodes_with_audit_cleanup
        from graph_node
        where json_type(properties_json,'$.audit_cleanup') is not null
        """,
    ),
    SqlCheck(
        "audit_cleanup_has_no_unresolved_decisions",
        "Audit cleanup decisions must be fully resolved.",
        """
        select count(*) as unresolved_audit
        from reporting_node_cleanup
        where decision not in ('implemented','discarded')
        """,
    ),
    SqlCheck(
        "edges_do_not_point_to_duplicate_sources",
        "Graph edges must not point to source nodes marked duplicate_rewired.",
        """
        select count(*) as edges_to_duplicate_sources
        from graph_edge e
        join source_document_cleanup c
          on c.decision='duplicate_rewired'
         and (
           e.source_node_id='source_document:'||c.source_id
           or e.target_node_id='source_document:'||c.source_id
         )
        """,
    ),
    SqlCheck(
        "duplicate_source_graph_nodes_have_no_edges",
        "Duplicate source graph nodes must be removed or orphaned after rewiring.",
        """
        select n.node_id
        from source_document_cleanup c
        join graph_node n on n.node_id='source_document:'||c.source_id
        where c.decision='duplicate_rewired'
          and exists (
            select 1 from graph_edge e
            where e.source_node_id=n.node_id or e.target_node_id=n.node_id
          )
        limit 50
        """,
        expect="no_rows",
    ),
    SqlCheck(
        "taxonomy_children_not_collapsed_to_parent_packages",
        "Taxonomy child files must not be deduped into parent packages or siblings by inherited URL/checksum.",
        """
        select c.source_id,c.canonical_source_id,sd.file_type,sd.parent_url,sd.url,c.decision_reason
        from source_document_cleanup c
        join source_document sd on sd.source_id=c.source_id
        join source_document canon on canon.source_id=c.canonical_source_id
        where sd.file_type in ('xml','xsd','xbrl')
          and c.source_id <> c.canonical_source_id
          and (sd.parent_url is not null or instr(sd.url,'#') > 0)
          and c.decision <> 'canonical'
          and lower(coalesce(c.decision_reason,'')) not like '%exact child path%'
        limit 50
        """,
        expect="no_rows",
    ),
    SqlCheck(
        "provisions_not_legal_instruments",
        "Provision nodes must not be retyped as LegalInstrument nodes.",
        """
        select node_id,node_type,label
        from graph_node
        where node_type='LegalInstrument'
          and (
            node_id like 'provision:%'
            or source_table='provision'
          )
        limit 50
        """,
        expect="no_rows",
    ),
    SqlCheck(
        "policy_statements_not_legal_instruments",
        "Policy statements must not be retyped as LegalInstrument nodes.",
        """
        select node_id,node_type,label
        from graph_node
        where node_type='LegalInstrument'
          and (
            node_id like 'policy_statement:%'
            or lower(label) glob 'ps[0-9]*/*'
            or lower(label) glob 'cp[0-9]*/*'
            or lower(label) glob 'ss[0-9]*/*'
          )
        limit 50
        """,
        expect="no_rows",
    ),
    SqlCheck(
        "current_reporting_roots_have_evidence",
        "Current reporting roots must have a direct or normalised evidence route.",
        """
        with roots as (
          select node_id,label,node_type
          from graph_node
          where node_type in ('DataItem','ReportingReturn','RequirementEdition','DisclosureSet')
            and coalesce(effective_to,'')=''
        ),
        evidence as (
          select distinct source_node_id as node_id
          from graph_edge
          where edge_type in (
            'EVIDENCED_BY','HAS_TEMPLATE_RESOURCE','HAS_INSTRUCTION_RESOURCE',
            'HAS_TAXONOMY_RESOURCE','SUPPORTED_BY_TAXONOMY','USES_TEMPLATE','USES_INSTRUCTIONS'
          )
        )
        select r.node_id,r.node_type,r.label
        from roots r
        left join evidence e on e.node_id=r.node_id
        where e.node_id is null
        limit 100
        """,
        severity="warning",
        expect="no_rows",
    ),
    SqlCheck(
        "data_items_have_obligation_or_reporting_relationship",
        "DataItem nodes should have an obligation, source, template or instruction relationship.",
        """
        select di.node_id,di.label
        from graph_node di
        where di.node_type='DataItem'
          and not exists (
            select 1
            from graph_edge e
            join graph_node n on n.node_id=e.source_node_id
            where e.target_node_id=di.node_id
              and e.edge_type in ('CONTAINS','APPLIES_TO')
              and n.node_type='ReportingObligation'
          )
          and not exists (
            select 1
            from graph_edge e
            join graph_node n on n.node_id=e.target_node_id
            where e.source_node_id=di.node_id
              and e.edge_type in ('LEGAL_BASIS','USES_TEMPLATE','USES_INSTRUCTIONS','EVIDENCED_BY')
              and n.node_type in ('Provision','Template','TemplateSet','InstructionSet','SourceDocument')
          )
        limit 100
        """,
        severity="warning",
        expect="no_rows",
    ),
    SqlCheck(
        "templates_used_by_returns_belong_to_template_sets",
        "Templates used by returns should belong to a TemplateSet where source evidence supports it.",
        """
        select t.node_id,t.label
        from graph_node t
        where t.node_type='Template'
          and exists (
            select 1
            from graph_edge e
            join graph_node s on s.node_id=e.source_node_id
            where e.target_node_id=t.node_id
              and e.edge_type='USES_TEMPLATE'
              and s.node_type in ('DataItem','ReportingObligation','ReportingReturn','RequirementEdition')
          )
          and not exists (
            select 1
            from graph_edge e
            join graph_node ts on ts.node_id=e.source_node_id
            where e.target_node_id=t.node_id
              and e.edge_type='CONTAINS'
              and ts.node_type='TemplateSet'
          )
        limit 100
        """,
        severity="warning",
        expect="no_rows",
    ),
    SqlCheck(
        "uses_instructions_edges_have_valid_types",
        "USES_INSTRUCTIONS edges must point from reporting/template nodes to InstructionSet nodes.",
        """
        select e.source_node_id,e.target_node_id
        from graph_edge e
        left join graph_node s on s.node_id=e.source_node_id
        left join graph_node t on t.node_id=e.target_node_id
        where e.edge_type='USES_INSTRUCTIONS'
          and (
            s.node_type not in ('Template','TemplateSet','DataItem','ReportingObligation','ReportingReturn','RequirementEdition')
            or t.node_type <> 'InstructionSet'
          )
        limit 50
        """,
        expect="no_rows",
    ),
    SqlCheck(
        "instruction_sets_have_source_evidence",
        "InstructionSets used by graph nodes should have source evidence where source evidence exists.",
        """
        select i.node_id,i.label
        from graph_node i
        where i.node_type='InstructionSet'
          and exists (
            select 1 from graph_edge u
            where u.target_node_id=i.node_id
              and u.edge_type='USES_INSTRUCTIONS'
          )
          and not exists (
            select 1
            from graph_edge e
            join graph_node sd on sd.node_id=e.target_node_id
            where e.source_node_id=i.node_id
              and e.edge_type='EVIDENCED_BY'
              and sd.node_type in ('SourceDocument','ReportingResource')
          )
        limit 100
        """,
        severity="warning",
        expect="no_rows",
    ),
    SqlCheck(
        "source_document_cleanup_covers_all_sources",
        "Every source_document row must have a deterministic source cleanup classification.",
        """
        select count(*) as unclassified_sources
        from source_document sd
        left join source_document_cleanup c on c.source_id=sd.source_id
        where c.source_id is null
          or c.source_kind is null
          or c.source_kind=''
        """,
    ),
    SqlCheck(
        "source_cleanup_decisions_are_valid",
        "Source cleanup decisions must use the recognised decision vocabulary.",
        """
        select decision,count(*) as count
        from source_document_cleanup
        where decision not in ('canonical','duplicate_rewired','duplicate_candidate')
        group by decision
        """,
        expect="no_rows",
    ),
]


def _rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "select 1 from sqlite_master where type in ('table','view') and name=?",
            (table,),
        ).fetchone()
    )


def _referenced_tables(sql: str) -> set[str]:
    import re

    cte_names = {
        match.group(1)
        for match in re.finditer(r"(?:\bwith|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", sql, re.I)
    }
    tables = {
        match.group(2)
        for match in re.finditer(r"\b(from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.I)
    }
    return tables - cte_names


def run_sql_check(conn: sqlite3.Connection, check: SqlCheck) -> dict[str, Any]:
    missing_tables = sorted(table for table in _referenced_tables(check.sql) if not _table_exists(conn, table))
    if missing_tables:
        status = "fail" if check.severity == "error" else "warning"
        return {
            "check_id": check.check_id,
            "description": check.description,
            "severity": check.severity,
            "status": status,
            "row_count": len(missing_tables),
            "sample_rows": [{"missing_table": table} for table in missing_tables],
        }

    rows = conn.execute(check.sql).fetchall()
    if check.expect == "zero_count":
        count = int(rows[0][0]) if rows else 0
        sample_rows: list[dict[str, Any]] = _rows_as_dicts(rows[:5]) if count else []
    else:
        count = len(rows)
        sample_rows = _rows_as_dicts(rows[:5])
    failed = count != 0
    return {
        "check_id": check.check_id,
        "description": check.description,
        "severity": check.severity,
        "status": "fail" if failed and check.severity == "error" else "warning" if failed else "pass",
        "row_count": count,
        "sample_rows": sample_rows,
    }


def _node_contains_forbidden_metadata(node: dict[str, Any]) -> bool:
    raw = json.dumps(node, sort_keys=True).lower()
    forbidden = ("audit_cleanup", "cleanup decision", "prompt_version", "model_name", '"model"')
    return any(term in raw for term in forbidden)


def run_api_smoke_checks(api_base: str, *, selected_returns: list[str] | None = None) -> list[dict[str, Any]]:
    selected_returns = selected_returns or ["PRA101", "COR011", "PRA110", "COREP-CREDIT-RISK"]
    checks: list[dict[str, Any]] = []
    for selected_return in selected_returns:
        url = (
            api_base.rstrip("/")
            + "/reporting/graph/overview"
            + f"?selected_return={selected_return}&limit=20&child_limit=200&include_datapoints=false"
        )
        try:
            with urlopen(url, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            checks.append(
                {
                    "check_id": f"api_{selected_return}_loads",
                    "description": f"{selected_return} reporting graph API smoke check.",
                    "severity": "error",
                    "status": "fail",
                    "row_count": 1,
                    "sample_rows": [{"url": url, "error": str(exc)}],
                }
            )
            continue
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        node_ids = {node.get("id") for node in nodes}
        missing_endpoints = [
            edge
            for edge in edges
            if edge.get("from_node_id") not in node_ids or edge.get("to_node_id") not in node_ids
        ]
        forbidden_nodes = [node for node in nodes if _node_contains_forbidden_metadata(node)]
        problems = []
        if not nodes:
            problems.append({"problem": "empty_nodes"})
        if missing_endpoints:
            problems.append({"problem": "missing_edge_endpoints", "count": len(missing_endpoints)})
        if forbidden_nodes:
            problems.append({"problem": "forbidden_node_metadata", "count": len(forbidden_nodes)})
        if selected_return == "PRA101" and (len(nodes) < 15 or len(edges) < 17):
            problems.append({"problem": "pra101_below_baseline", "nodes": len(nodes), "edges": len(edges)})
        checks.append(
            {
                "check_id": f"api_{selected_return}_loads",
                "description": f"{selected_return} reporting graph API smoke check.",
                "severity": "error",
                "status": "fail" if problems else "pass",
                "row_count": len(problems),
                "sample_rows": problems[:5],
            }
        )
    return checks


def run_quality_gate(
    db_path: Path = DB_PATH,
    *,
    report_path: Path | None = None,
    api_base: str | None = None,
    raw_root: Path | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    conn = connect_db(db_path)
    checks = [run_sql_check(conn, check) for check in SQL_CHECKS]
    conn.close()

    if api_base:
        checks.extend(run_api_smoke_checks(api_base))

    if raw_root:
        checks.append(
            {
                "check_id": "raw_root_exists",
                "description": "Raw reporting source root exists for source evidence validation.",
                "severity": "error" if strict else "warning",
                "status": "pass" if raw_root.exists() else "fail" if strict else "warning",
                "row_count": 0 if raw_root.exists() else 1,
                "sample_rows": [] if raw_root.exists() else [{"raw_root": str(raw_root)}],
            }
        )
        if raw_root.exists():
            from scripts.validate_reporting_source_evidence import validate_source_evidence

            evidence = validate_source_evidence(db_path, root=raw_root)
            checks.append(
                {
                    "check_id": "source_evidence_validation",
                    "description": "Source documents have the expected semantic graph evidence routes.",
                    "severity": "error",
                    "status": "fail" if evidence["errors"] else "pass",
                    "row_count": evidence["errors"],
                    "sample_rows": [finding for finding in evidence["findings"] if finding["severity"] == "error"][:5],
                    "warnings": evidence["warnings"],
                }
            )

    failures = [check for check in checks if check["status"] == "fail" and check["severity"] == "error"]
    result = {
        "status": "fail" if failures else "pass",
        "db": str(db_path),
        "checks": checks,
        "summary": {
            "checks": len(checks),
            "failures": len(failures),
            "warnings": sum(1 for check in checks if check["status"] == "warning"),
        },
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--raw-root", type=Path)
    ap.add_argument("--api-base")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    result = run_quality_gate(
        args.db,
        report_path=args.report,
        api_base=args.api_base,
        raw_root=args.raw_root,
        strict=args.strict,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
