from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

from .fetch import BASE_URL, normalise_url
from .legal_identity import snapshot_id, source_page_key
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

CREATE TABLE IF NOT EXISTS node_alias (
  node_id TEXT NOT NULL,
  alias_type TEXT NOT NULL,
  alias_value TEXT NOT NULL,
  PRIMARY KEY(node_id, alias_type, alias_value)
);

CREATE INDEX IF NOT EXISTS idx_node_alias_node ON node_alias(node_id);

CREATE TABLE IF NOT EXISTS ingestion_run (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
  command TEXT NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '{}',
  error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingestion_run_scope (
  run_id TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
  snapshot_id TEXT,
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  error TEXT DEFAULT '',
  started_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(run_id, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_scope_key
  ON ingestion_run_scope(scope_key, status, completed_at);

CREATE TABLE IF NOT EXISTS ingestion_output (
  scope_key TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN ('node','edge')),
  object_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope_key, object_type, object_id)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_output_object
  ON ingestion_output(object_type, object_id, scope_key);
CREATE INDEX IF NOT EXISTS idx_ingestion_output_run
  ON ingestion_output(run_id);
"""


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_source(conn: sqlite3.Connection, *, source_type: str, url: str, fetched_at: str, raw_html: str, raw_text: str = "") -> str:
    source_id = sha1(url)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_snapshot (
          snapshot_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          url TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          raw_html TEXT NOT NULL,
          raw_text TEXT DEFAULT '',
          UNIQUE(url, content_hash)
        )
        """
    )
    content_hash = sha1(raw_html)
    conn.execute(
        """
        INSERT OR IGNORE INTO document_snapshot
          (snapshot_id,source_id,url,fetched_at,content_hash,raw_html,raw_text)
        VALUES (?,?,?,?,?,?,?)
        """,
        (snapshot_id(url, raw_html), source_id, url, fetched_at, content_hash, raw_html, raw_text),
    )
    conn.execute(
        """
        INSERT INTO document_source (id, source_type, url, fetched_at, content_hash, raw_html, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET fetched_at=excluded.fetched_at,
          content_hash=excluded.content_hash, raw_html=excluded.raw_html, raw_text=excluded.raw_text
        """,
        (source_id, source_type, url, fetched_at, content_hash, raw_html, raw_text),
    )
    return source_id


def source_scope_key(source_type: str, url: str) -> str:
    """Return the stable manifest key for one fetched source."""

    return f"source:{source_type}:{normalise_url(url)}"


def derived_scope_key(name: str) -> str:
    """Return the stable manifest key for one deterministic derived pass."""

    return f"derived:{name}"


def start_ingestion_run(conn: sqlite3.Connection, *, command: str, scope: dict[str, Any] | None = None) -> str:
    run_id = f"ingestion:{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO ingestion_run(run_id,started_at,status,command,scope_json)
        VALUES (?,?,?,?,?)
        """,
        (run_id, _utc_now(), "running", command, json.dumps(scope or {}, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()
    return run_id


def record_ingestion_scope_started(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    scope_key: str,
    source_url: str = "",
    source_type: str = "",
) -> None:
    _record_ingestion_scope_started(
        conn,
        run_id=run_id,
        scope_key=scope_key,
        source_url=source_url,
        source_type=source_type,
    )
    conn.commit()


def record_ingestion_scope_failure(conn: sqlite3.Connection, *, run_id: str, scope_key: str, error: str) -> None:
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO ingestion_run_scope(
          run_id,scope_key,source_url,source_type,status,error,started_at,completed_at
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,scope_key) DO UPDATE SET status='failed',error=excluded.error,completed_at=excluded.completed_at
        """,
        (run_id, scope_key, "", "", "failed", str(error)[:2000], now, now),
    )
    conn.commit()


def finish_ingestion_run(conn: sqlite3.Connection, *, run_id: str) -> str:
    statuses = [
        row[0]
        for row in conn.execute(
            "SELECT status FROM ingestion_run_scope WHERE run_id=? ORDER BY scope_key",
            (run_id,),
        )
    ]
    if not statuses:
        status = "completed"
    elif any(value == "running" for value in statuses):
        status = "failed"
    elif all(value == "succeeded" for value in statuses):
        status = "completed"
    elif any(value == "succeeded" for value in statuses):
        status = "partial"
    else:
        status = "failed"
    conn.execute(
        "UPDATE ingestion_run SET status=?,completed_at=? WHERE run_id=?",
        (status, _utc_now(), run_id),
    )
    conn.commit()
    return status


def reconcile_source_output(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_url: str,
    source_type: str,
    fetched_at: str = "",
    raw_html: str = "",
    raw_text: str = "",
    nodes: Iterable[Node] = (),
    edges: Iterable[Edge] = (),
) -> dict[str, Any]:
    """Replace one successful source scope with its complete parsed output."""

    full_url = normalise_url(source_url)
    return _reconcile_scope(
        conn,
        run_id=run_id,
        scope_key=source_scope_key(source_type, full_url),
        source_url=full_url,
        source_type=source_type,
        fetched_at=fetched_at,
        raw_html=raw_html,
        raw_text=raw_text,
        nodes=list(nodes),
        edges=list(edges),
    )


def reconcile_derived_output(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    name: str,
    edges: Iterable[Edge],
    legacy_source_methods: Iterable[str] = (),
) -> dict[str, Any]:
    """Replace one deterministic derived edge scope after it completes."""

    return _reconcile_scope(
        conn,
        run_id=run_id,
        scope_key=derived_scope_key(name),
        source_url="",
        source_type="derived",
        fetched_at="",
        raw_html=None,
        raw_text="",
        nodes=[],
        edges=list(edges),
        legacy_source_methods=tuple(legacy_source_methods),
    )


def _reconcile_scope(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    scope_key: str,
    source_url: str,
    source_type: str,
    fetched_at: str,
    raw_html: str | None,
    raw_text: str,
    nodes: list[Node],
    edges: list[Edge],
    legacy_source_methods: tuple[str, ...] = (),
) -> dict[str, Any]:
    now = _utc_now()
    with conn:
        _record_ingestion_scope_started(
            conn,
            run_id=run_id,
            scope_key=scope_key,
            source_url=source_url,
            source_type=source_type,
        )
        old_membership = _scope_membership(conn, scope_key)
        if not old_membership and not _scope_has_success(conn, scope_key):
            old_membership = _bootstrap_legacy_membership(
                conn,
                scope_key=scope_key,
                source_url=source_url,
                source_type=source_type,
                legacy_source_methods=legacy_source_methods,
            )

        snapshot = None
        if raw_html is not None:
            snapshot = upsert_source(
                conn,
                source_type=source_type,
                url=source_url,
                fetched_at=fetched_at or now,
                raw_html=raw_html,
                raw_text=raw_text,
            )

        old_node_hashes = {
            object_id: payload_hash
            for object_type, object_id, payload_hash in old_membership
            if object_type == "node"
        }
        old_node_ids = set(old_node_hashes)
        # Listing/index pages emit shared catalogue nodes with less provenance
        # than a detail page. Preserve richer detail metadata when those
        # shared nodes are refreshed from an index, while detail scopes replace
        # their own complete payload so removed fields do not linger.
        replace_metadata = source_type not in {"index", "guidance_index"}
        upsert_nodes(conn, nodes, replace_metadata=replace_metadata)
        live_node_ids, node_hashes, parsed_node_id_map = _resolve_live_node_ids(conn, nodes)
        live_edges = [_remap_edge(edge, parsed_node_id_map) for edge in edges]
        upsert_edges(conn, live_edges)
        # Placeholder creation is part of the same source transaction. If it
        # fails, the source manifest and graph upserts roll back together.
        backfill_placeholder_targets(conn)

        current_edges = {edge.id for edge in live_edges}
        current_manifest = {
            ("node", node_id): node_hash
            for node_id, node_hash in node_hashes.items()
        }
        current_manifest.update(
            {("edge", edge.id): _edge_payload_hash(edge) for edge in live_edges}
        )

        changed_nodes = {
            node_id
            for node_id, payload_hash in node_hashes.items()
            if node_id in old_node_ids and old_node_hashes.get(node_id) != payload_hash
        }
        if changed_nodes:
            _delete_occurrences_for_source_nodes(conn, changed_nodes)

        old_edges = {object_id for object_type, object_id, _ in old_membership if object_type == "edge"}
        old_nodes = {object_id for object_type, object_id, _ in old_membership if object_type == "node"}
        stale_edges = old_edges - current_edges
        stale_nodes = old_nodes - live_node_ids

        conn.execute("DELETE FROM ingestion_output WHERE scope_key=?", (scope_key,))
        conn.executemany(
            """
            INSERT INTO ingestion_output(scope_key,object_type,object_id,payload_hash,run_id,updated_at)
            VALUES (?,?,?,?,?,?)
            """,
            [
                (scope_key, object_type, object_id, payload_hash, run_id, now)
                for (object_type, object_id), payload_hash in current_manifest.items()
            ],
        )

        removed_edges = 0
        for edge_id_ in sorted(stale_edges):
            if _has_other_owner(conn, "edge", edge_id_, scope_key):
                continue
            _delete_edge_and_occurrences(conn, edge_id_)
            removed_edges += 1

        removed_nodes = 0
        for node_id_ in sorted(stale_nodes):
            if _has_other_owner(conn, "node", node_id_, scope_key):
                continue
            _delete_node_and_dependants(conn, node_id_)
            removed_nodes += 1

        removed_nodes += _delete_orphan_canonical_nodes(conn)
        removed_nodes += _delete_orphan_placeholders(conn)
        _mark_search_projection_dirty(conn)

        conn.execute(
            """
            UPDATE ingestion_run_scope
            SET status='succeeded',snapshot_id=?,node_count=?,edge_count=?,error='',completed_at=?
            WHERE run_id=? AND scope_key=?
            """,
            (snapshot, len(live_node_ids), len(current_edges), now, run_id, scope_key),
        )
    return {
        "scope_key": scope_key,
        "snapshot_id": snapshot,
        "nodes": len(live_node_ids),
        "edges": len(current_edges),
        "removed_nodes": removed_nodes,
        "removed_edges": removed_edges,
        "invalidated_nodes": len(changed_nodes),
    }


def _record_ingestion_scope_started(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    scope_key: str,
    source_url: str,
    source_type: str,
) -> None:
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO ingestion_run_scope(
          run_id,scope_key,source_url,source_type,status,started_at
        ) VALUES (?,?,?,?,?,?)
        ON CONFLICT(run_id,scope_key) DO UPDATE SET
          source_url=excluded.source_url,source_type=excluded.source_type,
          status='running',error='',started_at=excluded.started_at,completed_at=NULL
        """,
        (run_id, scope_key, source_url, source_type, "running", now),
    )


def _scope_membership(conn: sqlite3.Connection, scope_key: str) -> list[tuple[str, str, str]]:
    return [
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT object_type,object_id,payload_hash FROM ingestion_output WHERE scope_key=?",
            (scope_key,),
        )
    ]


def _scope_has_success(conn: sqlite3.Connection, scope_key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM ingestion_run_scope WHERE scope_key=? AND status='succeeded' LIMIT 1",
        (scope_key,),
    ).fetchone() is not None


def _bootstrap_legacy_membership(
    conn: sqlite3.Connection,
    *,
    scope_key: str,
    source_url: str,
    source_type: str,
    legacy_source_methods: tuple[str, ...],
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    if source_url:
        page_key = source_page_key(source_url)
        page_ids = {
            row[0]
            for row in conn.execute(
                """
                SELECT id FROM node
                WHERE url=?
                   OR json_extract(metadata_json,'$.source_page_key')=?
                """,
                (source_url, page_key),
            )
        }
        node_rows = conn.execute(
            """
            SELECT id,metadata_json FROM node
            WHERE url=? OR url LIKE ?
               OR json_extract(metadata_json,'$.source_page_key')=?
               OR json_extract(metadata_json,'$.source_page_id') IN ({})
            """.format(",".join("?" for _ in page_ids) or "NULL"),
            (source_url, source_url + "#%", page_key, *sorted(page_ids)),
        ).fetchall()
        node_ids = {row[0] for row in node_rows}
        edge_rows = conn.execute(
            """
            SELECT id,metadata_json FROM edge
            WHERE source_url=? OR from_node_id IN ({})
            """.format(",".join("?" for _ in node_ids) or "NULL"),
            (source_url, *sorted(node_ids)),
        ).fetchall()
        if source_type == "index":
            # Legacy index parses emitted the listed Part nodes, but those
            # nodes' URLs are not the index URL itself. Recover that ownership
            # from the index's structural edges for the first post-v9 refresh.
            node_ids.update(
                row[0]
                for row in conn.execute(
                    """
                    SELECT to_node_id FROM edge
                    WHERE source_url=? AND edge_type='contains'
                    """,
                    (source_url,),
                )
            )
            edge_rows = conn.execute(
                """
                SELECT id,metadata_json FROM edge
                WHERE source_url=? OR from_node_id IN ({})
                """.format(",".join("?" for _ in node_ids) or "NULL"),
                (source_url, *sorted(node_ids)),
            ).fetchall()
            node_rows = [
                row
                for row in conn.execute(
                    "SELECT id,metadata_json FROM node WHERE id IN ({})".format(",".join("?" for _ in node_ids) or "NULL"),
                    tuple(sorted(node_ids)),
                ).fetchall()
            ]
        candidates.extend(("node", row[0], _existing_node_hash(conn, row[0])) for row in node_rows)
        candidates.extend(("edge", row[0], _existing_edge_hash(conn, row[0])) for row in edge_rows)
    elif legacy_source_methods:
        placeholders = ",".join("?" for _ in legacy_source_methods)
        rows = conn.execute(
            f"SELECT id FROM edge WHERE source_method IN ({placeholders})",
            legacy_source_methods,
        ).fetchall()
        candidates.extend(("edge", row[0], _existing_edge_hash(conn, row[0])) for row in rows)

    if candidates:
        conn.executemany(
            """
            INSERT OR IGNORE INTO ingestion_output(scope_key,object_type,object_id,payload_hash,run_id,updated_at)
            VALUES (?,?,?,?,?,?)
            """,
            [(scope_key, object_type, object_id, payload_hash, "legacy-bootstrap", _utc_now()) for object_type, object_id, payload_hash in candidates],
        )
    return _scope_membership(conn, scope_key)


def _resolve_live_node_ids(
    conn: sqlite3.Connection,
    nodes: list[Node],
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    live_ids: set[str] = set()
    hashes: dict[str, str] = {}
    parsed_to_live: dict[str, str] = {}
    for node in nodes:
        row = conn.execute("SELECT id FROM node WHERE stable_key=?", (node.stable_key,)).fetchone()
        if row is None:
            continue
        live_id = row[0]
        live_ids.add(live_id)
        hashes[live_id] = _node_payload_hash(node)
        parsed_to_live[node.id] = live_id
    return live_ids, hashes, parsed_to_live


def _remap_edge(edge: Edge, node_id_map: dict[str, str]) -> Edge:
    from_id = node_id_map.get(edge.from_node_id, edge.from_node_id)
    to_id = node_id_map.get(edge.to_node_id, edge.to_node_id)
    if from_id == edge.from_node_id and to_id == edge.to_node_id:
        return edge
    return Edge(
        edge.id,
        from_id,
        to_id,
        edge.edge_type,
        edge.source_method,
        edge.confidence,
        edge.evidence_text,
        edge.source_url,
        dict(edge.metadata or {}),
    )


def _node_payload_hash(node: Node) -> str:
    return sha1(json.dumps(node.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _edge_payload_hash(edge: Edge) -> str:
    return sha1(json.dumps(edge.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _existing_node_hash(conn: sqlite3.Connection, node_id: str) -> str:
    row = conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        return ""
    metadata = json.loads(row[6] or "{}")
    payload = {"id": row[0], "node_type": row[1], "stable_key": row[2], "title": row[3], "text": row[4], "url": row[5], "metadata": metadata}
    return sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _existing_edge_hash(conn: sqlite3.Connection, edge_id: str) -> str:
    row = conn.execute(
        "SELECT id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json FROM edge WHERE id=?",
        (edge_id,),
    ).fetchone()
    if row is None:
        return ""
    payload = {
        "id": row[0], "from_node_id": row[1], "to_node_id": row[2], "edge_type": row[3],
        "source_method": row[4], "confidence": row[5], "evidence_text": row[6], "source_url": row[7],
        "metadata": json.loads(row[8] or "{}"),
    }
    return sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _has_other_owner(conn: sqlite3.Connection, object_type: str, object_id: str, scope_key: str) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM ingestion_output
        WHERE object_type=? AND object_id=? AND scope_key<>?
        LIMIT 1
        """,
        (object_type, object_id, scope_key),
    ).fetchone() is not None


def _delete_occurrences_for_source_nodes(conn: sqlite3.Connection, node_ids: set[str]) -> None:
    if not node_ids or not _has_table(conn, "reference_occurrence"):
        return
    placeholders = ",".join("?" for _ in node_ids)
    conn.execute(f"DELETE FROM reference_occurrence WHERE source_node_id IN ({placeholders})", tuple(sorted(node_ids)))


def _delete_edge_and_occurrences(conn: sqlite3.Connection, edge_id_: str) -> None:
    if _has_table(conn, "reference_occurrence"):
        conn.execute("DELETE FROM reference_occurrence WHERE edge_id=?", (edge_id_,))
    conn.execute("DELETE FROM ingestion_output WHERE object_type='edge' AND object_id=?", (edge_id_,))
    conn.execute("DELETE FROM edge WHERE id=?", (edge_id_,))


def _delete_node_and_dependants(conn: sqlite3.Connection, node_id_: str) -> None:
    edge_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM edge WHERE from_node_id=? OR to_node_id=?",
            (node_id_, node_id_),
        )
    ]
    for edge_id_ in edge_ids:
        _delete_edge_and_occurrences(conn, edge_id_)
    if _has_table(conn, "reference_occurrence"):
        conn.execute(
            "DELETE FROM reference_occurrence WHERE source_node_id=? OR target_node_id=?",
            (node_id_, node_id_),
        )
    for table, column in (
        ("embedding", "node_id"),
        ("node_fts", "id"),
        ("canonical_node", "id"),
        ("canonical_guidance_document", "id"),
        ("canonical_guidance_paragraph", "id"),
        ("canonical_guidance_section", "id"),
    ):
        if _has_table(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE {column}=?", (node_id_,))
    if _has_table(conn, "node_alias"):
        conn.execute("DELETE FROM node_alias WHERE node_id=?", (node_id_,))
    conn.execute("DELETE FROM ingestion_output WHERE object_type='node' AND object_id=?", (node_id_,))
    conn.execute("DELETE FROM node WHERE id=?", (node_id_,))


def _delete_orphan_canonical_nodes(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT n.id FROM node n
        WHERE n.node_type='provision'
          AND json_extract(n.metadata_json,'$.identity_type')='canonical_provision'
          AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.from_node_id=n.id OR e.to_node_id=n.id)
          AND NOT EXISTS (SELECT 1 FROM ingestion_output o WHERE o.object_type='node' AND o.object_id=n.id)
        """
    ).fetchall()
    for row in rows:
        _delete_node_and_dependants(conn, row[0])
    return len(rows)


def _delete_orphan_placeholders(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT n.id FROM node n
        WHERE json_extract(n.metadata_json,'$.placeholder')=1
          AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.from_node_id=n.id OR e.to_node_id=n.id)
          AND NOT EXISTS (SELECT 1 FROM ingestion_output o WHERE o.object_type='node' AND o.object_id=n.id)
        """
    ).fetchall()
    for row in rows:
        _delete_node_and_dependants(conn, row[0])
    return len(rows)


def _mark_search_projection_dirty(conn: sqlite3.Connection) -> None:
    if _has_table(conn, "search_projection_state"):
        conn.execute("UPDATE search_projection_state SET dirty=1 WHERE singleton=1")


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_nodes(conn: sqlite3.Connection, nodes: Iterable[Node], *, replace_metadata: bool = False) -> None:
    metadata_update = "excluded.metadata_json" if replace_metadata else "json_patch(COALESCE(node.metadata_json,'{}'), excluded.metadata_json)"
    conn.executemany(
        f"""
        INSERT INTO node (id, node_type, stable_key, title, text, url, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stable_key) DO UPDATE SET node_type=excluded.node_type,
          title=excluded.title, text=excluded.text, url=excluded.url,
          metadata_json={metadata_update}
        """,
        [(n.id, n.node_type, n.stable_key, n.title, n.text, n.url, json.dumps(n.metadata, ensure_ascii=False, sort_keys=True)) for n in nodes],
    )


def upsert_edges(conn: sqlite3.Connection, edges: Iterable[Edge]) -> None:
    normalised_edges = [_canonicalise_semantic_edge(conn, edge) for edge in edges]
    conn.executemany(
        """
        INSERT INTO edge (id, from_node_id, to_node_id, edge_type, source_method, confidence, evidence_text, source_url, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET from_node_id=excluded.from_node_id, to_node_id=excluded.to_node_id,
          edge_type=excluded.edge_type, source_method=excluded.source_method, confidence=excluded.confidence,
          evidence_text=excluded.evidence_text, source_url=excluded.source_url, metadata_json=excluded.metadata_json
        """,
        [(e.id, e.from_node_id, e.to_node_id, e.edge_type, e.source_method, e.confidence, e.evidence_text, e.source_url, json.dumps(e.metadata, ensure_ascii=False)) for e in normalised_edges],
    )


def _canonicalise_semantic_edge(conn: sqlite3.Connection, edge: Edge) -> Edge:
    """Keep semantic targets on canonical provisions as new versions arrive."""

    if edge.edge_type not in {"references", "amends"}:
        return edge
    row = conn.execute("SELECT metadata_json FROM node WHERE id=?", (edge.to_node_id,)).fetchone()
    if not row:
        return edge
    metadata = json.loads(row[0] or "{}")
    canonical_id = metadata.get("canonical_provision_id")
    if not canonical_id or canonical_id == edge.to_node_id:
        return edge
    edge_metadata = {**(edge.metadata or {}), "canonical_target_id": canonical_id}
    return Edge(
        edge.id,
        edge.from_node_id,
        canonical_id,
        edge.edge_type,
        edge.source_method,
        edge.confidence,
        edge.evidence_text,
        edge.source_url,
        edge_metadata,
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
        url = _placeholder_url(metadata)
        conn.execute(
            """
            INSERT INTO node (id, node_type, stable_key, title, text, url, metadata_json)
            VALUES (?, ?, ?, ?, '', ?, ?)
            ON CONFLICT(stable_key) DO NOTHING
            """,
            (to_node_id, node_type, stable_key, title, url, json.dumps({"placeholder": True, **metadata}, ensure_ascii=False)),
        )


def _placeholder_url(metadata: dict) -> str:
    href = metadata.get("href", "") or ""
    if href.startswith("#glossary-term-"):
        return urljoin(BASE_URL, "/glossary") + href
    if href.startswith("#"):
        return urljoin(BASE_URL, "/glossary") + href
    if href.startswith("/"):
        return urljoin(BASE_URL, href)
    return href


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
