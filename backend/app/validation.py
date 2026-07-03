from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .graph import EXPLICIT_METHODS

HARD_STATUSES = ("direct_text", "html_structure", "document_metadata")


def validation_dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    total_nodes = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    total_edges = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    checks = [
        _duplicate_logical_nodes(conn),
        _missing_evidence(conn),
        _self_loops(conn),
        _unresolved_references(conn),
        _hard_soft_split(conn),
    ]
    return {
        "generated_from": "rulebook.sqlite3",
        "totals": {"nodes": total_nodes, "edges": total_edges},
        "checks": checks,
        "reporting": _reporting_quality(conn),
        "edges_by_type_method": _edges_by_type_method(conn),
        "high_degree_nodes": _high_degree_nodes(conn),
        "unresolved_reference_samples": _unresolved_reference_samples(conn),
        "unresolved_reference_patterns": _unresolved_reference_patterns(conn),
        "suspect_403_reference_samples": _suspect_403_reference_samples(),
        "near_self_loop_samples": _near_self_loop_samples(conn),
        "obligations_by_normative_force": _obligations_by_normative_force(conn),
    }


def _fetch_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _suspect_403_reference_samples(limit: int = 500) -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parents[2] / "outputs/broken-reference-check/unresolved-external-link-check.csv"
    decisions = _load_suspect_403_review_decisions()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("classification") == "suspect" and row.get("status") == "403":
                rows.append({
                    "review_id": f"403-{len(rows)+1:04d}",
                    "status": row.get("status", ""),
                    "reason": row.get("reason", ""),
                    "title": row.get("title", ""),
                    "url": row.get("url", ""),
                    "final_url": row.get("final_url", ""),
                    "live_edges": int(row.get("live_edges") or 0),
                    "target_id": row.get("target_id", ""),
                    "stable_key": row.get("stable_key", ""),
                    "review_decision": decisions.get(row.get("target_id", ""), {}).get("decision", ""),
                    "review_note": decisions.get(row.get("target_id", ""), {}).get("note", ""),
                })
                if len(rows) >= limit:
                    break
    return rows


def _suspect_403_review_path() -> Path:
    return Path(__file__).resolve().parents[2] / "outputs/broken-reference-check/suspect-403-review-decisions.json"


def _load_suspect_403_review_decisions() -> dict[str, Any]:
    path = _suspect_403_review_path()
    if not path.exists():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _unresolved_reference_patterns(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT target.id AS target_id, target.node_type AS target_type, target.title AS target_title,
               target.url AS target_url, target.stable_key AS stable_key,
               COUNT(e.id) AS live_edges,
               MIN(source.title) AS example_source_title
        FROM node target
        JOIN edge e ON e.to_node_id = target.id AND e.edge_type = 'references'
        LEFT JOIN node source ON source.id = e.from_node_id
        WHERE target.node_type IN ('external_reference','rule_reference')
          AND json_extract(target.metadata_json,'$.placeholder') = 1
        GROUP BY target.id
        """
    ).fetchall()
    buckets: dict[str, dict[str, Any]] = {}

    def add(name: str, row: sqlite3.Row) -> None:
        bucket = buckets.setdefault(name, {"pattern": name, "targets": 0, "live_edges": 0, "examples": []})
        bucket["targets"] += 1
        bucket["live_edges"] += int(row["live_edges"] or 0)
        if len(bucket["examples"]) < 12:
            bucket["examples"].append(
                {
                    "target_id": row["target_id"],
                    "target_type": row["target_type"],
                    "target_title": row["target_title"],
                    "target_url": row["target_url"],
                    "stable_key": row["stable_key"],
                    "live_edges": int(row["live_edges"] or 0),
                    "example_source_title": row["example_source_title"],
                }
            )

    for row in rows:
        title = (row["target_title"] or "").strip()
        haystack = " ".join([title, row["target_url"] or "", row["stable_key"] or ""]).lower()
        host = urlparse((row["target_url"] or "").replace("external:", "").replace("url:", "")).netloc.lower()
        if row["target_type"] == "rule_reference" and "pra-rules/" in haystack:
            add("Internal PRA rule URL needing resolver context", row)
        elif "guidance/supervisory-statements" in haystack or re.search(r"\bss\d+[-/]\d+\b", haystack):
            add("PRA supervisory statement/guidance reference", row)
        elif "guidance/statements-of-policy" in haystack or re.search(r"\bsop\d*[-/]?\d*\b", haystack):
            add("PRA statement of policy reference", row)
        elif title.lower() in {"here", "click here"}:
            add("Generic link text", row)
        elif title.lower().startswith(("http://", "https://", "www.")):
            add("Raw URL title", row)
        elif re.fullmatch(r"\d+(\.\d+)*[a-z]?([()\w–-]+)?", title, re.I):
            add("Bare paragraph/rule number", row)
        elif re.match(r"^(chapter|article|section|annex|appendix|part)\b", title, re.I):
            add("Generic structural label", row)
        elif host:
            add(f"External site: {host}", row)
        else:
            add("Other unresolved pattern", row)
    return sorted(buckets.values(), key=lambda r: (-r["targets"], r["pattern"]))
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _duplicate_logical_nodes(conn: sqlite3.Connection) -> dict[str, Any]:
    exact_html_groups = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT json_extract(metadata_json,'$.document_title') doc,
                 json_extract(metadata_json,'$.paragraph_number') para,
                 json_extract(metadata_json,'$.html_id') html_id,
                 text,
                 COUNT(*) c
          FROM node
          WHERE node_type='guidance_paragraph'
            AND coalesce(json_extract(metadata_json,'$.paragraph_number'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.html_id'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction'
          GROUP BY doc,para,html_id,text
          HAVING c>1
        )
        """
    ).fetchone()[0]
    html_id_key_pairs = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT json_extract(metadata_json,'$.document_title') doc,
                 json_extract(metadata_json,'$.paragraph_number') para,
                 json_extract(metadata_json,'$.html_id') html_id,
                 SUM(CASE WHEN stable_key LIKE '%:'||json_extract(metadata_json,'$.paragraph_number') THEN 1 ELSE 0 END) para_key_nodes,
                 SUM(CASE WHEN stable_key LIKE '%:'||json_extract(metadata_json,'$.html_id') THEN 1 ELSE 0 END) html_key_nodes,
                 COUNT(*) c
          FROM node
          WHERE node_type='guidance_paragraph'
            AND coalesce(json_extract(metadata_json,'$.paragraph_number'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.html_id'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction'
          GROUP BY doc,para,html_id
          HAVING c>1 AND para_key_nodes>0 AND html_key_nodes>0
        )
        """
    ).fetchone()[0]
    ambiguous_doc_para_groups = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT json_extract(metadata_json,'$.document_title') doc,
                 json_extract(metadata_json,'$.paragraph_number') para,
                 COUNT(*) c,
                 COUNT(DISTINCT text) distinct_texts
          FROM node
          WHERE node_type='guidance_paragraph'
            AND coalesce(json_extract(metadata_json,'$.paragraph_number'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction'
          GROUP BY doc,para
          HAVING c>1 AND distinct_texts>1
        )
        """
    ).fetchone()[0]
    return {
        "check": "duplicate logical nodes",
        "purpose": "Catch stable-key/canonicalisation issues in guidance paragraph identity.",
        "status": "pass" if exact_html_groups == 0 and html_id_key_pairs == 0 else "fail",
        "metrics": {
            "exact_html_duplicate_groups": exact_html_groups,
            "paragraph_vs_html_id_key_pairs": html_id_key_pairs,
            "ambiguous_doc_paragraph_groups_not_auto_merged": ambiguous_doc_para_groups,
        },
    }


def _missing_evidence(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics = {
        "missing_source_method": conn.execute("SELECT COUNT(*) FROM edge WHERE coalesce(source_method,'')=''").fetchone()[0],
        "missing_confidence": conn.execute("SELECT COUNT(*) FROM edge WHERE confidence IS NULL").fetchone()[0],
        "missing_source_url": conn.execute("SELECT COUNT(*) FROM edge WHERE coalesce(source_url,'')=''").fetchone()[0],
        "missing_evidence_text": conn.execute("SELECT COUNT(*) FROM edge WHERE coalesce(evidence_text,'')=''").fetchone()[0],
        "missing_extraction_run_id": conn.execute("SELECT COUNT(*) FROM edge WHERE json_extract(metadata_json,'$.extraction_run_id') IS NULL").fetchone()[0],
        "missing_evidence_status": conn.execute("SELECT COUNT(*) FROM edge WHERE json_extract(metadata_json,'$.evidence_status') IS NULL").fetchone()[0],
        "hard_edges_missing_evidence_or_url": conn.execute(
            """
            SELECT COUNT(*) FROM edge
            WHERE json_extract(metadata_json,'$.evidence_status') IN ('direct_text','html_structure','document_metadata')
              AND (coalesce(evidence_text,'')='' OR coalesce(source_url,'')='')
            """
        ).fetchone()[0],
    }
    return {
        "check": "missing evidence/source URL",
        "purpose": "Ensure every edge has auditable provenance, especially hard legal edges.",
        "status": "pass" if sum(metrics.values()) == 0 else "fail",
        "metrics": metrics,
    }


def _self_loops(conn: sqlite3.Connection) -> dict[str, Any]:
    self_loops = conn.execute("SELECT COUNT(*) FROM edge WHERE from_node_id=to_node_id").fetchone()[0]
    near_sample = _near_self_loop_samples(conn, limit=41)
    near = len(near_sample)
    return {
        "check": "self-loops / near-self-loops",
        "purpose": "Catch canonicalisation bugs and duplicate logical nodes expressed as relationships.",
        "status": "pass" if self_loops == 0 and near == 0 else "warn",
        "metrics": {"self_loops": self_loops, "near_self_loop_sample_rows": near, "near_self_loop_sample_capped": near >= 41},
    }


def _unresolved_references(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        WITH placeholder AS (
          SELECT id,node_type
          FROM node
          WHERE node_type IN ('external_reference','rule_reference')
            AND json_extract(metadata_json,'$.placeholder')=1
        ), live_reference_targets AS (
          SELECT DISTINCT to_node_id
          FROM edge
          WHERE edge_type='references'
        ), any_targets AS (
          SELECT DISTINCT to_node_id
          FROM edge
        )
        SELECT
          p.node_type,
          COUNT(*) AS all_placeholders,
          SUM(CASE WHEN lrt.to_node_id IS NOT NULL THEN 1 ELSE 0 END) AS live_placeholders,
          SUM(CASE WHEN at.to_node_id IS NULL THEN 1 ELSE 0 END) AS orphan_placeholders,
          SUM(CASE WHEN lrt.to_node_id IS NULL AND at.to_node_id IS NOT NULL THEN 1 ELSE 0 END) AS non_reference_placeholders
        FROM placeholder p
        LEFT JOIN live_reference_targets lrt ON lrt.to_node_id=p.id
        LEFT JOIN any_targets at ON at.to_node_id=p.id
        GROUP BY p.node_type
        """
    ).fetchall()
    live_by_type = {row[0]: row[2] for row in rows}
    live_total = sum(row[2] for row in rows)
    orphan_total = sum(row[3] for row in rows)
    non_reference_total = sum(row[4] for row in rows)
    all_total = sum(row[1] for row in rows)
    return {
        "check": "unresolved references",
        "purpose": "Identify live cross-references that still point to placeholders rather than resolved legal nodes.",
        "status": "warn" if live_total else "pass",
        "metrics": {"live_placeholder_reference_nodes": live_total, "all_placeholder_reference_nodes": all_total, "orphan_placeholder_reference_nodes": orphan_total, "non_reference_placeholder_nodes": non_reference_total, **live_by_type},
    }


def _hard_soft_split(conn: sqlite3.Connection) -> dict[str, Any]:
    explicit_methods = tuple(sorted(EXPLICIT_METHODS))
    placeholders = ",".join("?" for _ in explicit_methods)
    hard = conn.execute(f"SELECT COUNT(*) FROM edge WHERE source_method IN ({placeholders})", explicit_methods).fetchone()[0]
    soft = conn.execute(f"SELECT COUNT(*) FROM edge WHERE source_method NOT IN ({placeholders})", explicit_methods).fetchone()[0]
    evidence_status = dict(conn.execute("SELECT json_extract(metadata_json,'$.evidence_status'),COUNT(*) FROM edge GROUP BY 1").fetchall())
    return {
        "check": "hard vs soft edge split",
        "purpose": "Separate direct/legal relationships from inferred analytical links so the legal graph stays clean.",
        "status": "pass",
        "metrics": {"hard_explicit_edges": hard, "soft_inferred_edges": soft, **{f"evidence_status_{k}": v for k, v in evidence_status.items()}},
    }


def _edges_by_type_method(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _fetch_dicts(
        conn,
        """
        SELECT edge_type, source_method,
               json_extract(metadata_json,'$.evidence_status') AS evidence_status,
               COUNT(*) AS edges,
               ROUND(AVG(confidence),3) AS avg_confidence,
               ROUND(MIN(confidence),3) AS min_confidence,
               ROUND(MAX(confidence),3) AS max_confidence
        FROM edge
        GROUP BY edge_type,source_method,evidence_status
        ORDER BY edges DESC, edge_type, source_method
        """,
    )


def _high_degree_nodes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _fetch_dicts(
        conn,
        """
        WITH d AS (
          SELECT from_node_id AS id, COUNT(*) AS out_degree, 0 AS in_degree FROM edge GROUP BY from_node_id
          UNION ALL
          SELECT to_node_id AS id, 0 AS out_degree, COUNT(*) AS in_degree FROM edge GROUP BY to_node_id
        ), agg AS (
          SELECT id, SUM(out_degree) out_degree, SUM(in_degree) in_degree, SUM(out_degree+in_degree) degree
          FROM d GROUP BY id
        )
        SELECT n.id,n.node_type,n.title,n.stable_key,agg.degree,agg.out_degree,agg.in_degree
        FROM agg JOIN node n ON n.id=agg.id
        ORDER BY agg.degree DESC
        LIMIT 40
        """,
    )


def _unresolved_reference_samples(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _fetch_dicts(
        conn,
        """
        SELECT
          printf('UR-%04d', ROW_NUMBER() OVER (
            ORDER BY target.node_type,target.title,
              CASE WHEN length(trim(COALESCE(NULLIF(original.text,''), source.text, ''))) > 0 THEN 0 ELSE 1 END,
              source.title,
              e.id
          )) AS sample_id,
          e.id AS edge_id,
          target.id AS target_id,
          target.node_type AS target_type,
          target.title AS target_title,
          target.url AS target_url,
          COALESCE(original.id, source.id) AS source_id,
          COALESCE(original.node_type, source.node_type) AS source_type,
          COALESCE(original.title, source.title) AS source_title,
          COALESCE(original.url, source.url) AS source_url,
          COALESCE(NULLIF(original.text,''), source.text, '') AS source_text,
          CASE WHEN original.id IS NOT NULL THEN source.title ELSE '' END AS source_container_title,
          e.edge_type,
          e.source_method,
          json_extract(e.metadata_json,'$.original_source_method') AS original_source_method,
          e.evidence_text,
          e.confidence
        FROM node target
        JOIN edge e ON e.to_node_id=target.id AND e.edge_type='references'
        LEFT JOIN node source ON source.id=e.from_node_id
        LEFT JOIN node original ON original.id=json_extract(e.metadata_json,'$.rolled_up_from_node_id')
        WHERE target.node_type IN ('external_reference','rule_reference')
          AND json_extract(target.metadata_json,'$.placeholder')=1
        ORDER BY
          target.node_type,
          target.title,
          CASE WHEN length(trim(COALESCE(NULLIF(original.text,''), source.text, ''))) > 0 THEN 0 ELSE 1 END,
          source.title
        LIMIT 2000
        """,
    )
    reviews = _load_unresolved_reference_reviews()
    for row in rows:
        review = reviews.get(row.get("target_id")) or reviews.get(row.get("edge_id")) or {}
        row["review_decision"] = review.get("decision", "")
        row["review_replacement_url"] = review.get("replacement_url", "")
        row["review_rulebook_target"] = review.get("rulebook_target", "")
        row["review_note"] = review.get("note", "")
        row["review_updated_at"] = review.get("updated_at", "")
    return rows


def _unresolved_reference_review_path() -> Path:
    return Path(__file__).resolve().parents[2] / "outputs/broken-reference-check/unresolved-reference-review-decisions.json"


def _load_unresolved_reference_reviews() -> dict[str, Any]:
    path = _unresolved_reference_review_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _near_self_loop_samples(conn: sqlite3.Connection, limit: int = 40) -> list[dict[str, Any]]:
    return _fetch_dicts(
        conn,
        """
        SELECT e.id,e.edge_type,e.source_method,s.id AS source_id,t.id AS target_id,s.node_type,s.title
        FROM edge e
        JOIN node s ON s.id=e.from_node_id
        JOIN node t ON t.id=e.to_node_id
        WHERE e.from_node_id<>e.to_node_id
          AND s.node_type=t.node_type
          AND s.title=t.title
          AND length(trim(coalesce(s.text,''))) > 0
          AND coalesce(s.text,'')=coalesce(t.text,'')
          AND e.edge_type NOT LIKE 'shares_%'
          AND e.source_method NOT LIKE 'derived_%'
        LIMIT ?
        """,
        (limit,),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _count_table(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _reporting_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "graph_node") or not _table_exists(conn, "graph_edge"):
        return {
            "totals": {},
            "checks": [],
            "edges_by_type_method": [],
            "samples": {},
        }

    totals = {
        "data_items": conn.execute("SELECT COUNT(*) FROM graph_node WHERE node_type='DataItem'").fetchone()[0],
        "templates": conn.execute("SELECT COUNT(*) FROM graph_node WHERE node_type='Template'").fetchone()[0],
        "datapoints": _count_table(conn, "datapoint"),
        "obligations": _count_table(conn, "reporting_obligation"),
        "source_documents": conn.execute("SELECT COUNT(*) FROM graph_node WHERE node_type='SourceDocument'").fetchone()[0],
        "reporting_reference_edges": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE extraction_method='reporting_llm_reference'").fetchone()[0],
    }
    checks = [_reporting_coverage(conn), _reporting_reference_evidence(conn)]
    if _table_exists(conn, "reporting_llm_reference_resolution"):
        checks.append(_reporting_llm_resolution(conn))
    return {
        "totals": totals,
        "checks": checks,
        "edges_by_type_method": _reporting_edges_by_type_method(conn),
        "samples": {
            "data_items_without_templates": _reporting_data_items_without(conn, "USES_TEMPLATE"),
            "data_items_without_source_documents": _reporting_data_items_without(conn, "EVIDENCED_BY"),
            "templates_without_datapoints": _reporting_templates_without_datapoints(conn),
        },
    }


def _reporting_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics = {
        "data_items_without_templates": _graph_nodes_missing_out_edge(conn, "DataItem", "USES_TEMPLATE"),
        "data_items_without_source_documents": _graph_nodes_missing_out_edge(conn, "DataItem", "EVIDENCED_BY"),
        "templates_without_datapoints": _templates_without_datapoints_count(conn),
        "obligations_without_data_item_node": conn.execute(
            """
            SELECT COUNT(*) FROM reporting_obligation ro
            WHERE NOT EXISTS (
              SELECT 1 FROM graph_node di
              WHERE di.node_type='DataItem'
                AND (UPPER(di.label)=UPPER(ro.data_item_code) OR UPPER(di.node_id)=UPPER('DataItem:'||ro.data_item_code))
            )
            """
        ).fetchone()[0] if _table_exists(conn, "reporting_obligation") else 0,
    }
    return {
        "check": "reporting coverage",
        "purpose": "Check that reporting data items connect to templates, source documents, datapoints and obligations.",
        "status": "warn" if any(metrics.values()) else "pass",
        "metrics": metrics,
    }


def _reporting_reference_evidence(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics = {
        "reporting_reference_edges": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE extraction_method='reporting_llm_reference'").fetchone()[0],
        "missing_confidence": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE extraction_method='reporting_llm_reference' AND confidence IS NULL").fetchone()[0],
        "missing_evidence_span": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE extraction_method='reporting_llm_reference' AND coalesce(evidence_span_id,'')=''").fetchone()[0],
        "low_confidence_under_60pct": conn.execute("SELECT COUNT(*) FROM graph_edge WHERE extraction_method='reporting_llm_reference' AND confidence < 0.6").fetchone()[0],
    }
    return {
        "check": "reporting reference evidence",
        "purpose": "Check extracted reporting-rule references have confidence and source-span evidence.",
        "status": "warn" if metrics["missing_confidence"] or metrics["missing_evidence_span"] or metrics["low_confidence_under_60pct"] else "pass",
        "metrics": metrics,
    }


def _reporting_llm_resolution(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics = {
        "extracted_references": conn.execute("SELECT COUNT(*) FROM reporting_llm_reference_resolution").fetchone()[0],
        "unresolved_references": conn.execute("SELECT COUNT(*) FROM reporting_llm_reference_resolution WHERE coalesce(target_node_id,'')='' AND coalesce(added_edge_id,'')=''").fetchone()[0],
        "resolved_without_added_edge": conn.execute("SELECT COUNT(*) FROM reporting_llm_reference_resolution WHERE coalesce(target_node_id,'')<>'' AND coalesce(added_edge_id,'')='' ").fetchone()[0],
    }
    return {
        "check": "reporting reference resolution",
        "purpose": "Check LLM-extracted reporting references have been resolved into graph links where possible.",
        "status": "warn" if metrics["unresolved_references"] or metrics["resolved_without_added_edge"] else "pass",
        "metrics": metrics,
    }


def _reporting_edges_by_type_method(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _fetch_dicts(
        conn,
        """
        SELECT edge_type, extraction_method, review_status,
               COUNT(*) AS edges,
               ROUND(AVG(confidence),3) AS avg_confidence,
               ROUND(MIN(confidence),3) AS min_confidence,
               ROUND(MAX(confidence),3) AS max_confidence
        FROM graph_edge
        WHERE edge_type IN ('USES_TEMPLATE','HAS_DATAPOINT','EVIDENCED_BY','USES_INSTRUCTIONS','REFERENCES_RULE','REFERENCES_SOURCE','REFERENCES_EXTERNAL','REFERENCES_RETURN','REFERENCES_TEMPLATE')
        GROUP BY edge_type, extraction_method, review_status
        ORDER BY edges DESC, edge_type, extraction_method
        """,
    )


def _graph_nodes_missing_out_edge(conn: sqlite3.Connection, node_type: str, edge_type: str) -> int:
    return conn.execute(
        """
        WITH linked AS (
          SELECT DISTINCT source_node_id FROM graph_edge WHERE edge_type=?
        )
        SELECT COUNT(*)
        FROM graph_node n
        LEFT JOIN linked ON linked.source_node_id=n.node_id
        WHERE n.node_type=? AND linked.source_node_id IS NULL
        """,
        (edge_type, node_type),
    ).fetchone()[0]


def _templates_without_datapoints_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "datapoint"):
        return 0
    return conn.execute(
        """
        WITH edge_templates AS (
          SELECT DISTINCT source_node_id FROM graph_edge WHERE edge_type='HAS_DATAPOINT'
        ), datapoint_templates AS (
          SELECT DISTINCT template_id FROM datapoint
        )
        SELECT COUNT(*)
        FROM graph_node t
        LEFT JOIN edge_templates et ON et.source_node_id=t.node_id
        LEFT JOIN datapoint_templates dt ON dt.template_id=COALESCE(t.source_pk,t.node_id)
        WHERE t.node_type='Template' AND et.source_node_id IS NULL AND dt.template_id IS NULL
        """
    ).fetchone()[0]


def _reporting_data_items_without(conn: sqlite3.Connection, edge_type: str) -> list[dict[str, Any]]:
    return _fetch_dicts(
        conn,
        """
        WITH linked AS (
          SELECT DISTINCT source_node_id FROM graph_edge WHERE edge_type=?
        )
        SELECT di.node_id,di.label,di.source_table,di.source_pk
        FROM graph_node di
        LEFT JOIN linked ON linked.source_node_id=di.node_id
        WHERE di.node_type='DataItem' AND linked.source_node_id IS NULL
        ORDER BY di.label
        LIMIT 80
        """,
        (edge_type,),
    )


def _reporting_templates_without_datapoints(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "datapoint"):
        return []
    return _fetch_dicts(
        conn,
        """
        WITH edge_templates AS (
          SELECT DISTINCT source_node_id FROM graph_edge WHERE edge_type='HAS_DATAPOINT'
        ), datapoint_templates AS (
          SELECT DISTINCT template_id FROM datapoint
        )
        SELECT t.node_id,t.label,t.source_table,t.source_pk
        FROM graph_node t
        LEFT JOIN edge_templates et ON et.source_node_id=t.node_id
        LEFT JOIN datapoint_templates dt ON dt.template_id=COALESCE(t.source_pk,t.node_id)
        WHERE t.node_type='Template' AND et.source_node_id IS NULL AND dt.template_id IS NULL
        ORDER BY t.label
        LIMIT 80
        """,
    )


def _obligations_by_normative_force(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _fetch_dicts(
        conn,
        """
        SELECT lower(coalesce(json_extract(metadata_json,'$.modal'),'unspecified')) AS normative_force,
               COUNT(*) AS obligations,
               ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
        FROM node
        WHERE node_type='obligation_statement'
        GROUP BY normative_force
        ORDER BY obligations DESC, normative_force
        """,
    )
