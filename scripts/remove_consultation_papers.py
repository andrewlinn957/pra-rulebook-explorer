#!/usr/bin/env python3
"""Remove consultation-paper sources and derived rows from the PRA Rulebook dataset.

The predicate is deliberately URL-slug based. Do not use a broad "CP" match:
legitimate reporting templates use codes such as cp01.00. The slug
"consultation-paper" catches both media-file CP paths and Bank publication
landing pages for occasional consultation papers.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "backend" / "data" / "rulebook.sqlite3"
CP_URL_PATTERN = "%consultation-paper%"


def count(cur: sqlite3.Cursor, sql: str) -> int:
    return int(cur.execute(sql).fetchone()[0])


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("PRAGMA busy_timeout = 60000")

    cur.executescript(
        f"""
        DROP TABLE IF EXISTS temp_cp_sources;
        DROP TABLE IF EXISTS temp_cp_spans;
        DROP TABLE IF EXISTS temp_cp_templates;
        DROP TABLE IF EXISTS temp_cp_graph_nodes;
        DROP TABLE IF EXISTS temp_cp_nodes;

        CREATE TEMP TABLE temp_cp_sources AS
          SELECT source_id FROM source_document
          WHERE lower(url) LIKE '{CP_URL_PATTERN}';
        CREATE UNIQUE INDEX temp_idx_cp_sources ON temp_cp_sources(source_id);

        CREATE TEMP TABLE temp_cp_spans AS
          SELECT span_id FROM source_span
          WHERE source_id IN (SELECT source_id FROM temp_cp_sources);
        CREATE UNIQUE INDEX temp_idx_cp_spans ON temp_cp_spans(span_id);

        CREATE TEMP TABLE temp_cp_templates AS
          SELECT template_id FROM template
          WHERE source_id IN (SELECT source_id FROM temp_cp_sources);
        CREATE UNIQUE INDEX temp_idx_cp_templates ON temp_cp_templates(template_id);

        CREATE TEMP TABLE temp_cp_graph_nodes AS
          SELECT node_id FROM graph_node
          WHERE (source_table='source_document' AND source_pk IN (SELECT source_id FROM temp_cp_sources))
             OR (source_table='source_span' AND source_pk IN (SELECT span_id FROM temp_cp_spans))
             OR (source_table='template' AND source_pk IN (SELECT template_id FROM temp_cp_templates));
        CREATE UNIQUE INDEX temp_idx_cp_graph_nodes ON temp_cp_graph_nodes(node_id);

        CREATE TEMP TABLE temp_cp_nodes AS
          SELECT id FROM node
          WHERE lower(url) LIKE '{CP_URL_PATTERN}'
             OR lower(metadata_json) LIKE '{CP_URL_PATTERN}';
        CREATE UNIQUE INDEX temp_idx_cp_nodes ON temp_cp_nodes(id);
        """
    )

    before = {
        "source_document": count(cur, "SELECT count(*) FROM temp_cp_sources"),
        "source_span": count(cur, "SELECT count(*) FROM temp_cp_spans"),
        "template": count(cur, "SELECT count(*) FROM temp_cp_templates"),
        "graph_node": count(cur, "SELECT count(*) FROM temp_cp_graph_nodes"),
        "node": count(cur, "SELECT count(*) FROM temp_cp_nodes"),
        "graph_edge_evidence_or_node": count(cur, """
            SELECT count(*) FROM graph_edge
            WHERE evidence_span_id IN (SELECT span_id FROM temp_cp_spans)
               OR source_node_id IN (SELECT node_id FROM temp_cp_graph_nodes)
               OR target_node_id IN (SELECT node_id FROM temp_cp_graph_nodes)
        """),
    }

    deletes = [
        ("reporting_llm_reference_resolution", "DELETE FROM reporting_llm_reference_resolution WHERE source_id IN (SELECT source_id FROM temp_cp_sources) OR span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("reporting_llm_reference_extraction", "DELETE FROM reporting_llm_reference_extraction WHERE source_id IN (SELECT source_id FROM temp_cp_sources) OR span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("graph_edge", "DELETE FROM graph_edge WHERE evidence_span_id IN (SELECT span_id FROM temp_cp_spans) OR source_node_id IN (SELECT node_id FROM temp_cp_graph_nodes) OR target_node_id IN (SELECT node_id FROM temp_cp_graph_nodes)"),
        ("graph_node", "DELETE FROM graph_node WHERE node_id IN (SELECT node_id FROM temp_cp_graph_nodes)"),
        ("edge", "DELETE FROM edge WHERE lower(source_url) LIKE '%/prudential-regulation/consultation-paper/%' OR lower(metadata_json) LIKE '%/prudential-regulation/consultation-paper/%' OR from_node_id IN (SELECT id FROM temp_cp_nodes) OR to_node_id IN (SELECT id FROM temp_cp_nodes)"),
        ("llm_reference_resolution", "DELETE FROM llm_reference_resolution WHERE source_node_id IN (SELECT id FROM temp_cp_nodes) OR target_node_id IN (SELECT id FROM temp_cp_nodes)"),
        ("llm_reference_extraction", "DELETE FROM llm_reference_extraction WHERE node_id IN (SELECT id FROM temp_cp_nodes)"),
        ("node_aliases", "DELETE FROM node_aliases WHERE node_id IN (SELECT id FROM temp_cp_nodes)"),
        ("embedding", "DELETE FROM embedding WHERE node_id IN (SELECT id FROM temp_cp_nodes)"),
        ("canonical_node", "DELETE FROM canonical_node WHERE id IN (SELECT id FROM temp_cp_nodes)"),
        ("node_fts", "DELETE FROM node_fts WHERE id IN (SELECT id FROM temp_cp_nodes)"),
        ("node", "DELETE FROM node WHERE id IN (SELECT id FROM temp_cp_nodes)"),
        ("instruction", "DELETE FROM instruction WHERE source_span_id IN (SELECT span_id FROM temp_cp_spans) OR applies_to_id IN (SELECT template_id FROM temp_cp_templates)"),
        ("reporting_obligation", "DELETE FROM reporting_obligation WHERE source_span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("validation_rule", "DELETE FROM validation_rule WHERE source_id IN (SELECT source_id FROM temp_cp_sources) OR source_span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("calculation_rule", "DELETE FROM calculation_rule WHERE source_span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("datapoint", "DELETE FROM datapoint WHERE template_id IN (SELECT template_id FROM temp_cp_templates) OR source_span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("template_column", "DELETE FROM template_column WHERE template_id IN (SELECT template_id FROM temp_cp_templates) OR source_span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("template_row", "DELETE FROM template_row WHERE template_id IN (SELECT template_id FROM temp_cp_templates) OR source_span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("template", "DELETE FROM template WHERE template_id IN (SELECT template_id FROM temp_cp_templates)"),
        ("source_span", "DELETE FROM source_span WHERE span_id IN (SELECT span_id FROM temp_cp_spans)"),
        ("source_document", "DELETE FROM source_document WHERE source_id IN (SELECT source_id FROM temp_cp_sources)"),
    ]

    deleted: dict[str, int] = {}
    cur.execute("BEGIN")
    for label, sql in deletes:
        cur.execute(sql)
        deleted[label] = deleted.get(label, 0) + cur.rowcount
    con.commit()

    after = {
        "remaining_cp_sources": count(cur, f"SELECT count(*) FROM source_document WHERE lower(url) LIKE '{CP_URL_PATTERN}'"),
        "remaining_cp_spans": count(cur, f"""
            SELECT count(*) FROM source_span s JOIN source_document d ON d.source_id=s.source_id
            WHERE lower(d.url) LIKE '{CP_URL_PATTERN}'
        """),
        "remaining_cp_nodes": count(cur, f"SELECT count(*) FROM node WHERE lower(url) LIKE '{CP_URL_PATTERN}' OR lower(metadata_json) LIKE '{CP_URL_PATTERN}'"),
        "remaining_cp_edges": count(cur, f"SELECT count(*) FROM edge WHERE lower(source_url) LIKE '{CP_URL_PATTERN}' OR lower(metadata_json) LIKE '{CP_URL_PATTERN}'"),
    }

    out = {"database": str(DB), "predicate": CP_URL_PATTERN, "before": before, "deleted": deleted, "after": after}
    print(json.dumps(out, indent=2))
    con.close()


if __name__ == "__main__":
    main()
