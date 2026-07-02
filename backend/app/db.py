from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import rebuild_canonical_guidance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "backend" / "data" / "rulebook.sqlite3"

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding (
  node_id TEXT PRIMARY KEY,
  model_name TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_node_type ON node(node_type);
CREATE INDEX IF NOT EXISTS idx_edge_from ON edge(from_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_to ON edge(to_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_type ON edge(edge_type);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEX_SCHEMA)
    rebuild_canonical_guidance(conn)
    # Recreate FTS from scratch so schema changes are harmless and refreshes are exact.
    conn.execute("DROP TABLE IF EXISTS node_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE node_fts USING fts5(
          id UNINDEXED,
          title,
          text,
          node_type UNINDEXED
        )
        """
    )
    conn.execute(
        """
        INSERT INTO node_fts (id, title, text, node_type)
        SELECT id, title, COALESCE(text,''), node_type
        FROM node
        WHERE COALESCE(title,'') || COALESCE(text,'') <> ''
        """
    )
    conn.commit()


def row_to_node(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    meta = d.pop("metadata_json", "{}") or "{}"
    d["metadata"] = json.loads(meta)
    return d


def row_to_edge(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    meta = d.pop("metadata_json", "{}") or "{}"
    d["metadata"] = json.loads(meta)
    return d


def get_node(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?",
        (node_id,),
    ).fetchone()
    node = row_to_node(row)
    if node and not (node.get("text") or "").strip():
        children = conn.execute(
            """
            SELECT child.id, child.node_type, child.title, child.text, child.url
            FROM edge e JOIN node child ON child.id=e.to_node_id
            WHERE e.from_node_id=? AND e.edge_type='contains'
              AND (COALESCE(child.text,'') <> '' OR COALESCE(child.title,'') <> '')
            ORDER BY child.title
            LIMIT 30
            """,
            (node_id,),
        ).fetchall()
        if children:
            node["child_content"] = [dict(r) for r in children]
            node["text"] = "\n\n".join(
                f"{r['title']}\n{r['text']}" if r["text"] else r["title"]
                for r in children
            )
            node["metadata"]["text_derived_from_children"] = True
    return node


def get_edge(conn: sqlite3.Connection, edge_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json FROM edge WHERE id=?",
        (edge_id,),
    ).fetchone()
    return row_to_edge(row)
