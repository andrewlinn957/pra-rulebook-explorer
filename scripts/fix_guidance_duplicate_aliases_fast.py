from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime

DB = Path(__file__).resolve().parents[1] / "backend/data/rulebook.sqlite3"


def main() -> None:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = DB.with_name(f"rulebook.sqlite3.bak-guidance-alias-fast-{ts}")
    shutil.copy2(DB, backup)

    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")

    conn.executescript(
        r"""
        CREATE TABLE IF NOT EXISTS node_aliases (
          node_id TEXT NOT NULL,
          alias_type TEXT NOT NULL,
          alias_value TEXT NOT NULL,
          UNIQUE(node_id, alias_type, alias_value)
        );
        CREATE INDEX IF NOT EXISTS idx_node_aliases_value ON node_aliases(alias_type, alias_value);

        DROP TABLE IF EXISTS temp.dup_ranked;
        CREATE TEMP TABLE dup_ranked AS
        WITH base AS (
          SELECT
            id,
            stable_key,
            url,
            json_extract(metadata_json,'$.document_title') AS doc,
            json_extract(metadata_json,'$.paragraph_number') AS para,
            json_extract(metadata_json,'$.html_id') AS html_id,
            text
          FROM node
          WHERE node_type='guidance_paragraph'
            AND coalesce(json_extract(metadata_json,'$.paragraph_number'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.html_id'),'')<>''
            AND coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction'
        ), ranked AS (
          SELECT
            *,
            COUNT(*) OVER (PARTITION BY doc, para, html_id, text) AS group_count,
            FIRST_VALUE(id) OVER (
              PARTITION BY doc, para, html_id, text
              ORDER BY CASE WHEN stable_key LIKE '%:' || para THEN 0 ELSE 1 END, stable_key
            ) AS canon_id,
            FIRST_VALUE(stable_key) OVER (
              PARTITION BY doc, para, html_id, text
              ORDER BY CASE WHEN stable_key LIKE '%:' || para THEN 0 ELSE 1 END, stable_key
            ) AS canon_stable_key
          FROM base
        )
        SELECT * FROM ranked WHERE group_count > 1;

        DROP TABLE IF EXISTS temp.dup_map;
        CREATE TEMP TABLE dup_map AS
        SELECT id AS dup_id, stable_key AS dup_stable_key, url AS dup_url, canon_id, para, html_id
        FROM dup_ranked
        WHERE id <> canon_id;
        CREATE INDEX dup_map_dup_idx ON dup_map(dup_id);
        CREATE INDEX dup_map_canon_idx ON dup_map(canon_id);
        """
    )

    duplicate_groups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT doc,para,html_id,text FROM dup_ranked GROUP BY doc,para,html_id,text)"
    ).fetchone()[0]
    merged_nodes = conn.execute("SELECT COUNT(*) FROM dup_map").fetchone()[0]

    alias_rows = 0
    alias_inserts = [
        "INSERT OR IGNORE INTO node_aliases(node_id,alias_type,alias_value) SELECT DISTINCT canon_id,'legacy_id',dup_id FROM dup_map",
        "INSERT OR IGNORE INTO node_aliases(node_id,alias_type,alias_value) SELECT DISTINCT canon_id,'legacy_key',dup_stable_key FROM dup_map",
        "INSERT OR IGNORE INTO node_aliases(node_id,alias_type,alias_value) SELECT DISTINCT canon_id,'url',dup_url FROM dup_map WHERE coalesce(dup_url,'')<>''",
        "INSERT OR IGNORE INTO node_aliases(node_id,alias_type,alias_value) SELECT DISTINCT canon_id,'paragraph_number',para FROM dup_map WHERE coalesce(para,'')<>''",
        "INSERT OR IGNORE INTO node_aliases(node_id,alias_type,alias_value) SELECT DISTINCT canon_id,'html_id',html_id FROM dup_map WHERE coalesce(html_id,'')<>''",
        "INSERT OR IGNORE INTO node_aliases(node_id,alias_type,alias_value) SELECT DISTINCT canon_id,'legacy_key',canon_stable_key FROM dup_ranked WHERE id=canon_id",
    ]
    for sql in alias_inserts:
        cur = conn.execute(sql)
        alias_rows += cur.rowcount

    cur = conn.execute("UPDATE edge SET from_node_id=(SELECT canon_id FROM dup_map WHERE dup_id=edge.from_node_id) WHERE from_node_id IN (SELECT dup_id FROM dup_map)")
    updated_from = cur.rowcount
    cur = conn.execute("UPDATE edge SET to_node_id=(SELECT canon_id FROM dup_map WHERE dup_id=edge.to_node_id) WHERE to_node_id IN (SELECT dup_id FROM dup_map)")
    updated_to = cur.rowcount

    deleted = {}
    for table, col in [("embedding", "node_id"), ("node_fts", "id"), ("canonical_node", "id"), ("canonical_guidance_paragraph", "id"), ("node", "id")]:
        cur = conn.execute(f"DELETE FROM {table} WHERE {col} IN (SELECT dup_id FROM dup_map)")
        deleted[table] = cur.rowcount
    cur = conn.execute("DELETE FROM edge WHERE from_node_id=to_node_id")
    deleted_self_edges = cur.rowcount

    conn.commit()
    print(json.dumps({
        "backup": str(backup),
        "duplicate_groups": duplicate_groups,
        "merged_nodes": merged_nodes,
        "updated_edges": updated_from + updated_to,
        "alias_rows_inserted": alias_rows,
        "deleted": deleted,
        "deleted_self_edges": deleted_self_edges,
    }, indent=2))


if __name__ == "__main__":
    main()
