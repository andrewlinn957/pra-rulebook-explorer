from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Edge, Node

SCHEMA = """
CREATE TABLE IF NOT EXISTS document_source (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  raw_html TEXT NOT NULL,
  raw_text TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS node (
  id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  stable_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  text TEXT DEFAULT '',
  url TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edge (
  id TEXT PRIMARY KEY,
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  source_method TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_text TEXT DEFAULT '',
  source_url TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{}'
);
"""


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_source(conn: sqlite3.Connection, *, source_type: str, url: str, fetched_at: str, raw_html: str, raw_text: str = "") -> str:
    source_id = sha1(url)
    conn.execute(
        """
        INSERT INTO document_source (id, source_type, url, fetched_at, content_hash, raw_html, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET fetched_at=excluded.fetched_at,
          content_hash=excluded.content_hash, raw_html=excluded.raw_html, raw_text=excluded.raw_text
        """,
        (source_id, source_type, url, fetched_at, sha1(raw_html), raw_html, raw_text),
    )
    return source_id


def upsert_nodes(conn: sqlite3.Connection, nodes: Iterable[Node]) -> None:
    conn.executemany(
        """
        INSERT INTO node (id, node_type, stable_key, title, text, url, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stable_key) DO UPDATE SET node_type=excluded.node_type,
          title=excluded.title, text=excluded.text, url=excluded.url, metadata_json=excluded.metadata_json
        """,
        [(n.id, n.node_type, n.stable_key, n.title, n.text, n.url, json.dumps(n.metadata, ensure_ascii=False)) for n in nodes],
    )


def upsert_edges(conn: sqlite3.Connection, edges: Iterable[Edge]) -> None:
    conn.executemany(
        """
        INSERT INTO edge (id, from_node_id, to_node_id, edge_type, source_method, confidence, evidence_text, source_url, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET from_node_id=excluded.from_node_id, to_node_id=excluded.to_node_id,
          edge_type=excluded.edge_type, source_method=excluded.source_method, confidence=excluded.confidence,
          evidence_text=excluded.evidence_text, source_url=excluded.source_url, metadata_json=excluded.metadata_json
        """,
        [(e.id, e.from_node_id, e.to_node_id, e.edge_type, e.source_method, e.confidence, e.evidence_text, e.source_url, json.dumps(e.metadata, ensure_ascii=False)) for e in edges],
    )


def backfill_placeholder_targets(conn: sqlite3.Connection) -> None:
    """Create lightweight placeholder nodes for linked targets not yet parsed.

    This keeps graph exports structurally valid while preserving that the node is
    unresolved. Later parsers can upsert the same stable_key with full content.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT e.to_node_id, e.evidence_text, e.metadata_json
        FROM edge e LEFT JOIN node n ON e.to_node_id = n.id
        WHERE n.id IS NULL
        """
    ).fetchall()
    for to_node_id, evidence_text, metadata_json in rows:
        metadata = json.loads(metadata_json or "{}")
        stable_key = metadata.get("target_key") or f"unresolved:{to_node_id}"
        if stable_key.startswith("defined_term:") or stable_key.startswith("glossary-term:"):
            node_type = "defined_term"
        elif stable_key.startswith("url:pra-rules"):
            node_type = "rule_reference"
        else:
            node_type = "external_reference"
        title = evidence_text or stable_key.rsplit(":", 1)[-1]
        conn.execute(
            """
            INSERT INTO node (id, node_type, stable_key, title, text, url, metadata_json)
            VALUES (?, ?, ?, ?, '', ?, ?)
            ON CONFLICT(stable_key) DO NOTHING
            """,
            (to_node_id, node_type, stable_key, title, metadata.get("href", ""), json.dumps({"placeholder": True, **metadata}, ensure_ascii=False)),
        )


def export_json(conn: sqlite3.Connection, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    nodes = [
        {**dict(zip(["id", "node_type", "stable_key", "title", "text", "url"], row[:6])), "metadata": json.loads(row[6] or "{}")}
        for row in conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node ORDER BY node_type,title")
    ]
    edges = [
        {**dict(zip(["id", "from_node_id", "to_node_id", "edge_type", "source_method", "confidence", "evidence_text", "source_url"], row[:8])), "metadata": json.loads(row[8] or "{}")}
        for row in conn.execute("SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json FROM edge ORDER BY edge_type,id")
    ]
    out_path.write_text(json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=2), encoding="utf-8")
