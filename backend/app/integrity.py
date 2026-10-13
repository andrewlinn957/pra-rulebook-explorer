from __future__ import annotations

import sqlite3
from typing import Any


def integrity_report(conn: sqlite3.Connection) -> dict[str, Any]:
    # FTS5 UNINDEXED columns cannot support efficient point lookups. Materialise
    # only the small id set for this check so integrity verification stays linear.
    conn.execute("DROP TABLE IF EXISTS temp.integrity_fts_id")
    conn.execute("CREATE TEMP TABLE integrity_fts_id(id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO integrity_fts_id(id) SELECT id FROM node_fts")
    has_occurrences = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_occurrence'"
    ).fetchone())
    metrics = {
        "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        "legacy_edges_missing_endpoint": conn.execute(
            """
            SELECT COUNT(*) FROM edge e
            WHERE NOT EXISTS(SELECT 1 FROM node n WHERE n.id=e.from_node_id)
               OR NOT EXISTS(SELECT 1 FROM node n WHERE n.id=e.to_node_id)
            """
        ).fetchone()[0],
        "reporting_edges_missing_endpoint": conn.execute(
            """
            SELECT COUNT(*) FROM graph_edge e
            WHERE NOT EXISTS(SELECT 1 FROM graph_node n WHERE n.node_id=e.source_node_id)
               OR NOT EXISTS(SELECT 1 FROM graph_node n WHERE n.node_id=e.target_node_id)
            """
        ).fetchone()[0],
        "materialized_occurrence_missing_endpoint": conn.execute(
            """
            SELECT COUNT(*) FROM reference_occurrence o
            LEFT JOIN node s ON s.id=o.source_node_id
            LEFT JOIN node t ON t.id=o.target_node_id
            WHERE o.status='materialized' AND (s.id IS NULL OR t.id IS NULL)
            """
        ).fetchone()[0] if has_occurrences else 0,
        "materialized_occurrence_edge_mismatch": conn.execute(
            """
            SELECT COUNT(*) FROM reference_occurrence o
            LEFT JOIN edge e ON e.id=o.edge_id
            WHERE o.status='materialized'
              AND (e.id IS NULL OR e.from_node_id<>o.source_node_id
                   OR e.to_node_id<>o.target_node_id
                   OR e.edge_type<>CASE UPPER(o.relationship_type)
                     WHEN 'REF' THEN 'references'
                     WHEN 'DEF' THEN 'uses_defined_term'
                     WHEN 'AMEND' THEN 'amends'
                     WHEN 'AMENDS' THEN 'amends'
                     ELSE '' END)
            """
        ).fetchone()[0] if has_occurrences else 0,
        "enrichments_missing_graph_template": conn.execute(
            """
            SELECT COUNT(*) FROM reporting_template_enrichment e
            WHERE NOT EXISTS(SELECT 1 FROM graph_node n WHERE n.node_id=e.graph_node_id AND n.node_type='Template')
            """
        ).fetchone()[0],
        "canonical_missing_live_node": conn.execute(
            "SELECT COUNT(*) FROM node n WHERE NOT EXISTS(SELECT 1 FROM canonical_node c WHERE c.id=n.id)"
        ).fetchone()[0],
        "canonical_orphan": conn.execute(
            "SELECT COUNT(*) FROM canonical_node c WHERE NOT EXISTS(SELECT 1 FROM node n WHERE n.id=c.id)"
        ).fetchone()[0],
        "fts_missing_live_node": conn.execute(
            """
            SELECT COUNT(*) FROM node n
            WHERE COALESCE(n.title,'')||COALESCE(n.text,'')<>''
              AND NOT EXISTS(SELECT 1 FROM integrity_fts_id f WHERE f.id=n.id)
            """
        ).fetchone()[0],
        "fts_orphan": conn.execute(
            "SELECT COUNT(*) FROM integrity_fts_id f WHERE NOT EXISTS(SELECT 1 FROM node n WHERE n.id=f.id)"
        ).fetchone()[0],
    }
    conn.execute("DROP TABLE temp.integrity_fts_id")
    return {"ok": all(value == 0 for value in metrics.values()), "metrics": metrics}
