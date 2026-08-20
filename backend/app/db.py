from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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

CREATE TABLE IF NOT EXISTS reference_occurrence (
  occurrence_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT,
  edge_id TEXT,
  relationship_type TEXT NOT NULL DEFAULT 'REF',
  citation_kind TEXT NOT NULL,
  citation_text TEXT NOT NULL,
  group_text TEXT NOT NULL,
  instrument_id TEXT,
  provision_path TEXT,
  qualifier TEXT DEFAULT '',
  span_start INTEGER NOT NULL,
  span_end INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'materialized','unresolved','ambiguous','not_reference'
  )),
  source_method TEXT NOT NULL,
  confidence REAL NOT NULL,
  context_text TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK(span_start >= 0 AND span_end >= span_start),
  CHECK(confidence >= 0.0 AND confidence <= 1.0),
  CHECK(json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS idx_reference_occurrence_source
  ON reference_occurrence(source_node_id,span_start,span_end);
CREATE INDEX IF NOT EXISTS idx_reference_occurrence_target
  ON reference_occurrence(target_node_id);
CREATE INDEX IF NOT EXISTS idx_reference_occurrence_edge
  ON reference_occurrence(edge_id);
CREATE INDEX IF NOT EXISTS idx_reference_occurrence_status
  ON reference_occurrence(status);

CREATE TABLE IF NOT EXISTS document_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  raw_html TEXT NOT NULL,
  raw_text TEXT DEFAULT '',
  UNIQUE(url, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_document_snapshot_source
  ON document_snapshot(source_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_document_snapshot_url
  ON document_snapshot(url, fetched_at);

CREATE TABLE IF NOT EXISTS node_alias (
  node_id TEXT NOT NULL,
  alias_type TEXT NOT NULL,
  alias_value TEXT NOT NULL,
  PRIMARY KEY(node_id, alias_type, alias_value)
);

CREATE INDEX IF NOT EXISTS idx_node_alias_node ON node_alias(node_id);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    return configure_connection(conn)


def configure_connection(conn: sqlite3.Connection, *, wal: bool = True) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if wal:
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
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_projection_state'").fetchone():
        conn.execute(
            "UPDATE search_projection_state SET dirty=0,refreshed_at=? WHERE singleton=1",
            (datetime.now(timezone.utc).isoformat(),),
        )
    conn.commit()


def search_projections_dirty(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT dirty FROM search_projection_state WHERE singleton=1"
    ).fetchone()
    return row is None or bool(row[0])


def row_to_node(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    meta = d.pop("metadata_json", "{}") or "{}"
    d["metadata"] = json.loads(meta)
    identity = identity_projection(d.get("id", ""), d["metadata"])
    if identity:
        d["identity"] = identity
    return d


def identity_projection(node_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Expose stable legal identity fields without duplicating source text."""

    identity_type = metadata.get("identity_type")
    if not identity_type and not any(
        metadata.get(key)
        for key in ("canonical_provision_id", "source_page_id", "snapshot_id")
    ):
        return {}
    identity: dict[str, Any] = {"identity_type": identity_type or ""}
    if identity_type == "canonical_provision":
        identity["canonical_provision_id"] = node_id
    for source_key, output_key in (
        ("canonical_provision_id", "canonical_provision_id"),
        ("canonical_provision_key", "canonical_provision_key"),
        ("rulebook_date", "version_date"),
        ("source_page_id", "source_page_id"),
        ("source_page_key", "source_page_key"),
        ("snapshot_id", "snapshot_id"),
        ("canonical_part_key", "canonical_part_key"),
        ("version_key", "version_key"),
    ):
        if metadata.get(source_key) not in (None, ""):
            identity[output_key] = metadata[source_key]
    if identity_type == "source_page":
        identity.setdefault("source_page_id", node_id)
    return identity


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
