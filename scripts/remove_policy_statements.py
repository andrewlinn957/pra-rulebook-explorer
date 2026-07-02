#!/usr/bin/env python3
"""Remove PRA Policy Statement publication sources and derived rows.

This deliberately preserves Rulebook/Guidance "Statements of Policy" (SoP):
- excludes URLs containing statements-of-policy
- excludes titles containing statement of policy
- targets Policy Statement file paths and PRA publication landing pages whose
  title starts with a PS number.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "backend" / "data" / "rulebook.sqlite3"

SOURCE_WHERE = """
(
  lower(url) LIKE '%/prudential-regulation/policy-statement/%'
  OR (
    lower(url) LIKE '%/prudential-regulation/publication/%'
    AND lower(title) REGEXP '(^|[^a-z])ps[0-9]+/[0-9]+'
  )
)
AND lower(url) NOT LIKE '%statements-of-policy%'
AND lower(coalesce(title,'')) NOT LIKE '%statement of policy%'
"""
NODE_WHERE = """
(
  lower(url) LIKE '%/prudential-regulation/policy-statement/%'
  OR lower(metadata_json) LIKE '%/prudential-regulation/policy-statement/%'
  OR (
    lower(url) LIKE '%/prudential-regulation/publication/%'
    AND lower(title) REGEXP '(^|[^a-z])ps[0-9]+/[0-9]+'
  )
)
AND lower(url) NOT LIKE '%statements-of-policy%'
AND lower(coalesce(title,'')) NOT LIKE '%statement of policy%'
"""
EDGE_WHERE = """
lower(source_url) LIKE '%/prudential-regulation/policy-statement/%'
OR lower(metadata_json) LIKE '%/prudential-regulation/policy-statement/%'
"""


def count(cur: sqlite3.Cursor, sql: str) -> int:
    return int(cur.execute(sql).fetchone()[0])


def main() -> None:
    con = sqlite3.connect(DB)
    con.create_function("REGEXP", 2, lambda pattern, value: 1 if re.search(pattern, value or "") else 0)
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("PRAGMA busy_timeout = 60000")

    cur.executescript(
        f"""
        DROP TABLE IF EXISTS temp_ps_sources;
        DROP TABLE IF EXISTS temp_ps_spans;
        DROP TABLE IF EXISTS temp_ps_templates;
        DROP TABLE IF EXISTS temp_ps_graph_nodes;
        DROP TABLE IF EXISTS temp_ps_nodes;

        CREATE TEMP TABLE temp_ps_sources AS
          SELECT source_id FROM source_document WHERE {SOURCE_WHERE};
        CREATE UNIQUE INDEX temp_idx_ps_sources ON temp_ps_sources(source_id);

        CREATE TEMP TABLE temp_ps_spans AS
          SELECT span_id FROM source_span
          WHERE source_id IN (SELECT source_id FROM temp_ps_sources);
        CREATE UNIQUE INDEX temp_idx_ps_spans ON temp_ps_spans(span_id);

        CREATE TEMP TABLE temp_ps_templates AS
          SELECT template_id FROM template
          WHERE source_id IN (SELECT source_id FROM temp_ps_sources);
        CREATE UNIQUE INDEX temp_idx_ps_templates ON temp_ps_templates(template_id);

        CREATE TEMP TABLE temp_ps_graph_nodes AS
          SELECT node_id FROM graph_node
          WHERE (source_table='source_document' AND source_pk IN (SELECT source_id FROM temp_ps_sources))
             OR (source_table='source_span' AND source_pk IN (SELECT span_id FROM temp_ps_spans))
             OR (source_table='template' AND source_pk IN (SELECT template_id FROM temp_ps_templates));
        CREATE UNIQUE INDEX temp_idx_ps_graph_nodes ON temp_ps_graph_nodes(node_id);

        CREATE TEMP TABLE temp_ps_nodes AS
          SELECT id FROM node WHERE {NODE_WHERE};
        CREATE UNIQUE INDEX temp_idx_ps_nodes ON temp_ps_nodes(id);
        """
    )

    before = {
        "source_document": count(cur, "SELECT count(*) FROM temp_ps_sources"),
        "source_span": count(cur, "SELECT count(*) FROM temp_ps_spans"),
        "template": count(cur, "SELECT count(*) FROM temp_ps_templates"),
        "graph_node": count(cur, "SELECT count(*) FROM temp_ps_graph_nodes"),
        "node": count(cur, "SELECT count(*) FROM temp_ps_nodes"),
        "graph_edge_evidence_or_node": count(cur, """
            SELECT count(*) FROM graph_edge
            WHERE evidence_span_id IN (SELECT span_id FROM temp_ps_spans)
               OR source_node_id IN (SELECT node_id FROM temp_ps_graph_nodes)
               OR target_node_id IN (SELECT node_id FROM temp_ps_graph_nodes)
        """),
    }

    deletes = [
        ("reporting_llm_reference_resolution", "DELETE FROM reporting_llm_reference_resolution WHERE source_id IN (SELECT source_id FROM temp_ps_sources) OR span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("reporting_llm_reference_extraction", "DELETE FROM reporting_llm_reference_extraction WHERE source_id IN (SELECT source_id FROM temp_ps_sources) OR span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("graph_edge", "DELETE FROM graph_edge WHERE evidence_span_id IN (SELECT span_id FROM temp_ps_spans) OR source_node_id IN (SELECT node_id FROM temp_ps_graph_nodes) OR target_node_id IN (SELECT node_id FROM temp_ps_graph_nodes)"),
        ("graph_node", "DELETE FROM graph_node WHERE node_id IN (SELECT node_id FROM temp_ps_graph_nodes)"),
        ("edge", f"DELETE FROM edge WHERE {EDGE_WHERE} OR from_node_id IN (SELECT id FROM temp_ps_nodes) OR to_node_id IN (SELECT id FROM temp_ps_nodes)"),
        ("llm_reference_resolution", "DELETE FROM llm_reference_resolution WHERE source_node_id IN (SELECT id FROM temp_ps_nodes) OR target_node_id IN (SELECT id FROM temp_ps_nodes)"),
        ("llm_reference_extraction", "DELETE FROM llm_reference_extraction WHERE node_id IN (SELECT id FROM temp_ps_nodes)"),
        ("node_aliases", "DELETE FROM node_aliases WHERE node_id IN (SELECT id FROM temp_ps_nodes)"),
        ("embedding", "DELETE FROM embedding WHERE node_id IN (SELECT id FROM temp_ps_nodes)"),
        ("canonical_node", "DELETE FROM canonical_node WHERE id IN (SELECT id FROM temp_ps_nodes)"),
        ("node_fts", "DELETE FROM node_fts WHERE id IN (SELECT id FROM temp_ps_nodes)"),
        ("node", "DELETE FROM node WHERE id IN (SELECT id FROM temp_ps_nodes)"),
        ("instruction", "DELETE FROM instruction WHERE source_span_id IN (SELECT span_id FROM temp_ps_spans) OR applies_to_id IN (SELECT template_id FROM temp_ps_templates)"),
        ("reporting_obligation", "DELETE FROM reporting_obligation WHERE source_span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("validation_rule", "DELETE FROM validation_rule WHERE source_id IN (SELECT source_id FROM temp_ps_sources) OR source_span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("calculation_rule", "DELETE FROM calculation_rule WHERE source_span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("datapoint", "DELETE FROM datapoint WHERE template_id IN (SELECT template_id FROM temp_ps_templates) OR source_span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("template_column", "DELETE FROM template_column WHERE template_id IN (SELECT template_id FROM temp_ps_templates) OR source_span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("template_row", "DELETE FROM template_row WHERE template_id IN (SELECT template_id FROM temp_ps_templates) OR source_span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("template", "DELETE FROM template WHERE template_id IN (SELECT template_id FROM temp_ps_templates)"),
        ("source_span", "DELETE FROM source_span WHERE span_id IN (SELECT span_id FROM temp_ps_spans)"),
        ("source_document", "DELETE FROM source_document WHERE source_id IN (SELECT source_id FROM temp_ps_sources)"),
    ]

    deleted: dict[str, int] = {}
    cur.execute("BEGIN")
    for label, sql in deletes:
        cur.execute(sql)
        deleted[label] = deleted.get(label, 0) + cur.rowcount
    con.commit()

    after = {
        "remaining_ps_sources": count(cur, f"SELECT count(*) FROM source_document WHERE {SOURCE_WHERE}"),
        "remaining_sop_guidance_docs": count(cur, "SELECT count(*) FROM canonical_guidance_document WHERE lower(document_type)='statement_of_policy'"),
        "remaining_sop_nodes": count(cur, "SELECT count(*) FROM node WHERE lower(url) LIKE '%statements-of-policy%'"),
        "remaining_ps_nodes": count(cur, f"SELECT count(*) FROM node WHERE {NODE_WHERE}"),
        "remaining_ps_edges": count(cur, f"SELECT count(*) FROM edge WHERE {EDGE_WHERE}"),
    }

    print(json.dumps({"database": str(DB), "before": before, "deleted": deleted, "after": after}, indent=2))
    con.close()


if __name__ == "__main__":
    main()
