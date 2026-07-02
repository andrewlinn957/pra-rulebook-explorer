from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime

DB = Path(__file__).resolve().parents[1] / "backend/data/rulebook.sqlite3"


def stable_suffix(stable_key: str) -> str:
    return (stable_key or "").rsplit(":", 1)[-1]


def main() -> None:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = DB.with_name(f"rulebook.sqlite3.bak-guidance-alias-{ts}")
    shutil.copy2(DB, backup)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_aliases (
          node_id TEXT NOT NULL,
          alias_type TEXT NOT NULL,
          alias_value TEXT NOT NULL,
          UNIQUE(node_id, alias_type, alias_value)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_node_aliases_value ON node_aliases(alias_type, alias_value)")

    groups = conn.execute(
        """
        SELECT
          json_extract(metadata_json,'$.document_title') AS doc,
          json_extract(metadata_json,'$.paragraph_number') AS para,
          json_extract(metadata_json,'$.html_id') AS html_id,
          text,
          COUNT(*) AS n
        FROM node
        WHERE node_type='guidance_paragraph'
          AND coalesce(json_extract(metadata_json,'$.paragraph_number'),'')<>''
          AND coalesce(json_extract(metadata_json,'$.html_id'),'')<>''
          AND coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction'
        GROUP BY doc, para, html_id, text
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    merged_nodes = 0
    updated_edges = 0
    deleted_edges = 0
    alias_rows = 0

    for g in groups:
        rows = conn.execute(
            """
            SELECT id, stable_key, url, metadata_json
            FROM node
            WHERE node_type='guidance_paragraph'
              AND json_extract(metadata_json,'$.document_title') IS ?
              AND json_extract(metadata_json,'$.paragraph_number') IS ?
              AND json_extract(metadata_json,'$.html_id') IS ?
              AND text IS ?
              AND coalesce(json_extract(metadata_json,'$.source'),'')<>'pdf_text_extraction'
            ORDER BY
              CASE WHEN stable_key LIKE '%' || ':' || json_extract(metadata_json,'$.paragraph_number') THEN 0 ELSE 1 END,
              stable_key
            """,
            (g["doc"], g["para"], g["html_id"], g["text"]),
        ).fetchall()
        if len(rows) < 2:
            continue
        canonical = rows[0]
        duplicate_rows = rows[1:]
        meta = json.loads(canonical["metadata_json"] or "{}")
        aliases = [
            (canonical["id"], "paragraph_number", g["para"]),
            (canonical["id"], "html_id", g["html_id"]),
            (canonical["id"], "url", canonical["url"] or ""),
            (canonical["id"], "legacy_key", canonical["stable_key"]),
        ]
        for dup in duplicate_rows:
            aliases.extend([
                (canonical["id"], "legacy_id", dup["id"]),
                (canonical["id"], "legacy_key", dup["stable_key"]),
                (canonical["id"], "url", dup["url"] or ""),
            ])
        for node_id, alias_type, alias_value in aliases:
            if alias_value:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO node_aliases(node_id, alias_type, alias_value) VALUES (?,?,?)",
                    (node_id, alias_type, alias_value),
                )
                alias_rows += cur.rowcount

        for dup in duplicate_rows:
            cur = conn.execute("UPDATE edge SET from_node_id=? WHERE from_node_id=?", (canonical["id"], dup["id"]))
            updated_edges += cur.rowcount
            cur = conn.execute("UPDATE edge SET to_node_id=? WHERE to_node_id=?", (canonical["id"], dup["id"]))
            updated_edges += cur.rowcount
            conn.execute("DELETE FROM embedding WHERE node_id=?", (dup["id"],))
            conn.execute("DELETE FROM node_fts WHERE id=?", (dup["id"],))
            conn.execute("DELETE FROM canonical_node WHERE id=?", (dup["id"],))
            conn.execute("DELETE FROM canonical_guidance_paragraph WHERE id=?", (dup["id"],))
            conn.execute("DELETE FROM node WHERE id=?", (dup["id"],))
            merged_nodes += 1

    # Remove self-loops created by canonical merging.
    cur = conn.execute("DELETE FROM edge WHERE from_node_id=to_node_id")
    deleted_edges += cur.rowcount

    # Do not run a global duplicate-edge cleanup here: the edge table is large and
    # the important correctness fix is node identity/remapping. A narrower edge
    # dedupe can be added later if duplicate parallel evidence becomes noisy.

    conn.commit()
    print(json.dumps({
        "backup": str(backup),
        "duplicate_groups": len(groups),
        "merged_nodes": merged_nodes,
        "updated_edges": updated_edges,
        "deleted_edges": deleted_edges,
        "alias_rows_inserted": alias_rows,
    }, indent=2))


if __name__ == "__main__":
    main()
