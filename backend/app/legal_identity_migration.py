"""Transactional migration from dated Rule nodes to canonical/version identity."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from ..rulebook_scraper.legal_identity import (
    canonical_part_key,
    canonical_provision_key,
    normalise_rulebook_date,
    provision_version_key,
    rulebook_date_from_url,
    snapshot_id,
    source_page_key,
)
from ..rulebook_scraper.parse import edge_id, node_id


SEMANTIC_TARGET_EDGE_TYPES = {"references", "amends"}


def migrate_legal_identity(conn: sqlite3.Connection) -> dict[str, int]:
    """Migrate legacy dated Rule nodes inside one rollback-safe savepoint.

    The function deliberately does not change ``PRAGMA user_version``.  The
    ordered application migration owns that version marker, while this helper
    is independently testable and safe for a staged copy of the corpus.
    """

    savepoint = "legal_identity_v8"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _create_support_tables(conn)
        source_rows = _source_rows(conn)
        _materialise_source_snapshots(conn, source_rows)
        old_alias_rows = _legacy_alias_rows(conn)
        plans = _plan_versions(conn, source_rows)
        if not plans:
            _mark_projections_dirty(conn)
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return {"migrated_versions": 0, "canonical_provisions": 0, "snapshots": _count(conn, "document_snapshot")}

        id_map = {plan["old_id"]: plan["version_id"] for plan in plans}
        canonical_map = {plan["old_id"]: plan["canonical_id"] for plan in plans}
        _insert_canonical_nodes(conn, plans)
        _ensure_source_page_nodes(conn, plans, source_rows)
        _rewrite_rule_nodes(conn, plans)
        _rewrite_node_metadata(conn, id_map)
        edge_id_map = _rewrite_edges(conn, id_map, canonical_map)
        _rewrite_occurrences(conn, id_map, canonical_map, edge_id_map)
        _rewrite_embeddings(conn, id_map)
        _rewrite_known_node_references(conn, id_map)
        _rewrite_aliases(conn, old_alias_rows, id_map)
        _insert_aliases(conn, plans)
        _insert_identity_edges(conn, plans)
        _mark_projections_dirty(conn)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {
            "migrated_versions": len(plans),
            "canonical_provisions": len({plan["canonical_id"] for plan in plans}),
            "snapshots": _count(conn, "document_snapshot"),
        }
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _create_support_tables(conn: sqlite3.Connection) -> None:
    # sqlite3.executescript issues an implicit COMMIT before running its
    # script, which would destroy the savepoint guarding this migration.
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_document_snapshot_source ON document_snapshot(source_id, fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_document_snapshot_url ON document_snapshot(url, fetched_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_alias (
          node_id TEXT NOT NULL,
          alias_type TEXT NOT NULL,
          alias_value TEXT NOT NULL,
          PRIMARY KEY(node_id, alias_type, alias_value)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_node_alias_node ON node_alias(node_id)")


def _source_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    if not _has_table(conn, "document_source"):
        return {}
    return {row["url"]: row for row in conn.execute("SELECT * FROM document_source")}


def _materialise_source_snapshots(conn: sqlite3.Connection, source_rows: dict[str, sqlite3.Row]) -> None:
    for row in source_rows.values():
        raw_html = row["raw_html"] or ""
        conn.execute(
            """
            INSERT OR IGNORE INTO document_snapshot
              (snapshot_id,source_id,url,fetched_at,content_hash,raw_html,raw_text)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                snapshot_id(row["url"], raw_html),
                row["id"],
                row["url"],
                row["fetched_at"],
                row["content_hash"],
                raw_html,
                row["raw_text"] or "",
            ),
        )


def _plan_versions(conn: sqlite3.Connection, source_rows: dict[str, sqlite3.Row]) -> list[dict[str, object]]:
    if not _has_table(conn, "node"):
        return []
    node_columns = {row[1] for row in conn.execute("PRAGMA table_info(node)")}
    if not {"id", "node_type", "stable_key", "title", "metadata_json"}.issubset(node_columns):
        return []
    rows = conn.execute(
        "SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE node_type='rule' ORDER BY id"
    ).fetchall()
    plans: list[dict[str, object]] = []
    by_canonical: dict[str, str] = {}
    by_version: dict[str, str] = {}
    for row in rows:
        metadata = _json(row["metadata_json"])
        if metadata.get("identity_type") == "provision_version" or row["stable_key"].startswith("provision_version:"):
            continue
        source_url = _source_url(row["url"], row["stable_key"])
        rulebook_date = rulebook_date_from_url(source_url) or normalise_rulebook_date(metadata.get("rulebook_date"))
        snapshot = _snapshot_for_source(conn, source_rows, source_url)
        parent = _parent_node(conn, row["id"])
        structural_locator = _structural_locator(parent, metadata.get("html_id", ""))
        rule_number = str(metadata.get("rule_number") or _rule_number_from_stable(row["stable_key"]))
        canonical_key = canonical_provision_key(source_url, structural_locator, rule_number or "unnumbered")
        version_key = provision_version_key(canonical_key, rulebook_date, snapshot=snapshot["snapshot_id"])

        # The same legal provision is expected to occur on more than one
        # dated source page.  The version key below still rejects two rows
        # claiming the same provision on the same date, while this canonical
        # key is deliberately shared across dates.
        if version_key in by_version and by_version[version_key] != row["id"]:
            raise ValueError(f"conflicting version identity: {version_key}")
        by_canonical.setdefault(canonical_key, row["id"])
        by_version[version_key] = row["id"]

        existing_canonical = conn.execute(
            "SELECT id,node_type FROM node WHERE stable_key=?", (canonical_key,)
        ).fetchone()
        if existing_canonical and existing_canonical["node_type"] != "provision":
            raise ValueError(f"conflicting canonical identity: {canonical_key}")
        canonical_id = existing_canonical["id"] if existing_canonical else node_id(canonical_key)

        existing_version = conn.execute(
            "SELECT id FROM node WHERE stable_key=?", (version_key,)
        ).fetchone()
        if existing_version and existing_version["id"] != row["id"]:
            raise ValueError(f"conflicting version identity: {version_key}")
        version_id = existing_version["id"] if existing_version else node_id(version_key)
        collision = conn.execute("SELECT stable_key FROM node WHERE id=?", (version_id,)).fetchone()
        if collision and collision["stable_key"] not in {row["stable_key"], version_key}:
            raise ValueError(f"conflicting version node id: {version_id}")

        source_page = _source_page(conn, source_url)
        plans.append(
            {
                "old_id": row["id"],
                "old_stable_key": row["stable_key"],
                "title": row["title"],
                "text": row["text"] or "",
                "url": row["url"] or source_url,
                "metadata": metadata,
                "source_url": source_url,
                "rulebook_date": rulebook_date,
                "snapshot_id": snapshot["snapshot_id"],
                "canonical_key": canonical_key,
                "canonical_id": canonical_id,
                "version_key": version_key,
                "version_id": version_id,
                "source_page_id": source_page["id"] if source_page else _source_page_id(source_url),
                "source_page_key": source_page_key(source_url),
                "canonical_part_key": canonical_part_key(source_url),
                "structural_locator": structural_locator,
                "rule_number": rule_number,
            }
        )
    return plans


def _snapshot_for_source(conn: sqlite3.Connection, source_rows: dict[str, sqlite3.Row], source_url: str) -> dict[str, str]:
    row = source_rows.get(source_url)
    if row:
        raw_html = row["raw_html"] or ""
        sid = snapshot_id(source_url, raw_html)
        conn.execute(
            """
            INSERT OR IGNORE INTO document_snapshot
              (snapshot_id,source_id,url,fetched_at,content_hash,raw_html,raw_text)
            VALUES (?,?,?,?,?,?,?)
            """,
            (sid, row["id"], source_url, row["fetched_at"], row["content_hash"], raw_html, row["raw_text"] or ""),
        )
        return {"snapshot_id": sid}
    sid = snapshot_id(source_url, "")
    conn.execute(
        """
        INSERT OR IGNORE INTO document_snapshot
          (snapshot_id,source_id,url,fetched_at,content_hash,raw_html,raw_text)
        VALUES (?,?,?,?,?,?,?)
        """,
        (sid, node_id(source_url), source_url, "", "", "", ""),
    )
    return {"snapshot_id": sid}


def _source_page(conn: sqlite3.Connection, source_url: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT id FROM node WHERE node_type='part' AND url=?", (source_url,)).fetchone()
    if row:
        return row
    stable = _source_page_stable(source_url)
    return conn.execute("SELECT id FROM node WHERE stable_key=?", (stable,)).fetchone()


def _source_page_id(source_url: str) -> str:
    return node_id(_source_page_stable(source_url))


def _source_page_stable(source_url: str) -> str:
    path = urlsplit(source_url).path.strip("/")
    return f"part:{path}"


def _ensure_source_page_nodes(conn: sqlite3.Connection, plans: list[dict[str, object]], source_rows: dict[str, sqlite3.Row]) -> None:
    by_page: dict[str, dict[str, object]] = {}
    for plan in plans:
        by_page.setdefault(str(plan["source_url"]), plan)
    for source_url, plan in by_page.items():
        row = conn.execute("SELECT id,title,metadata_json FROM node WHERE id=?", (plan["source_page_id"],)).fetchone()
        metadata = _json(row["metadata_json"] if row else "{}")
        source = source_rows.get(source_url)
        metadata.update(
            {
                "identity_type": "source_page",
                "source_page_key": plan["source_page_key"],
                "canonical_part_key": plan["canonical_part_key"],
                "rulebook_date": plan["rulebook_date"],
                "snapshot_id": plan["snapshot_id"],
            }
        )
        if row:
            conn.execute("UPDATE node SET metadata_json=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]))
            continue
        title = str((source["url"] if source else source_url).rstrip("/").rsplit("/", 1)[-1])
        part_title = str(plan["metadata"].get("part_title") or title)
        conn.execute(
            "INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES(?,?,?,?,?,?,?)",
            (plan["source_page_id"], "part", _source_page_stable(source_url), part_title, "", source_url, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )


def _insert_canonical_nodes(conn: sqlite3.Connection, plans: list[dict[str, object]]) -> None:
    seen: set[str] = set()
    for plan in plans:
        key = str(plan["canonical_key"])
        if key in seen:
            continue
        seen.add(key)
        metadata = {
            "identity_type": "canonical_provision",
            "canonical_part_key": plan["canonical_part_key"],
            "structural_locator": plan["structural_locator"],
            "rule_number": plan["rule_number"],
            "display_number": plan["title"],
        }
        conn.execute(
            """
            INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(stable_key) DO UPDATE SET
              title=excluded.title, metadata_json=excluded.metadata_json
            """,
            (plan["canonical_id"], "provision", key, plan["title"], "", "", json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )


def _rewrite_rule_nodes(conn: sqlite3.Connection, plans: list[dict[str, object]]) -> None:
    for plan in plans:
        metadata = dict(plan["metadata"])
        metadata.update(
            {
                "identity_type": "provision_version",
                "canonical_provision_id": plan["canonical_id"],
                "canonical_provision_key": plan["canonical_key"],
                "version_key": plan["version_key"],
                "source_page_id": plan["source_page_id"],
                "source_page_key": plan["source_page_key"],
                "canonical_part_key": plan["canonical_part_key"],
                "snapshot_id": plan["snapshot_id"],
                "rulebook_date": plan["rulebook_date"],
                "structural_locator": plan["structural_locator"],
            }
        )
        conn.execute(
            "UPDATE node SET id=?,stable_key=?,metadata_json=? WHERE id=?",
            (plan["version_id"], plan["version_key"], json.dumps(metadata, ensure_ascii=False, sort_keys=True), plan["old_id"]),
        )


def _rewrite_node_metadata(conn: sqlite3.Connection, id_map: dict[str, str]) -> None:
    for row in conn.execute("SELECT id,metadata_json FROM node").fetchall():
        metadata = _replace_ids(_json(row["metadata_json"]), id_map)
        conn.execute("UPDATE node SET metadata_json=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]))


def _rewrite_edges(conn: sqlite3.Connection, id_map: dict[str, str], canonical_map: dict[str, str]) -> dict[str, str]:
    rows = [dict(row) for row in conn.execute("SELECT * FROM edge ORDER BY id").fetchall()]
    rewritten: list[tuple[object, ...]] = []
    edge_id_map: dict[str, str] = {}
    seen_ids: set[str] = set()
    for row in rows:
        old_id = str(row["id"])
        from_id = id_map.get(row["from_node_id"], row["from_node_id"])
        if row["edge_type"] in SEMANTIC_TARGET_EDGE_TYPES:
            to_id = canonical_map.get(row["to_node_id"], id_map.get(row["to_node_id"], row["to_node_id"]))
        else:
            to_id = id_map.get(row["to_node_id"], row["to_node_id"])
        metadata = _replace_ids(_json(row.get("metadata_json")), id_map)
        changed = from_id != row["from_node_id"] or to_id != row["to_node_id"]
        new_id = edge_id(from_id, to_id, row["edge_type"], old_id) if changed else old_id
        if new_id in seen_ids:
            raise ValueError(f"conflicting edge identity: {new_id}")
        seen_ids.add(new_id)
        edge_id_map[old_id] = new_id
        rewritten.append((new_id, from_id, to_id, row["edge_type"], row["source_method"], row["confidence"], row["evidence_text"], row["source_url"], json.dumps(metadata, ensure_ascii=False, sort_keys=True)))
    conn.execute("DELETE FROM edge")
    conn.executemany("INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", rewritten)
    return edge_id_map


def _rewrite_occurrences(conn: sqlite3.Connection, id_map: dict[str, str], canonical_map: dict[str, str], edge_id_map: dict[str, str]) -> None:
    if not _has_table(conn, "reference_occurrence"):
        return
    rows = [dict(row) for row in conn.execute("SELECT * FROM reference_occurrence").fetchall()]
    for row in rows:
        source_id = id_map.get(row["source_node_id"], row["source_node_id"])
        target_id = canonical_map.get(row["target_node_id"], id_map.get(row["target_node_id"], row["target_node_id"])) if row["target_node_id"] else None
        edge_value = edge_id_map.get(row["edge_id"], row["edge_id"]) if row["edge_id"] else None
        metadata = _replace_ids(_json(row.get("metadata_json")), id_map)
        conn.execute(
            "UPDATE reference_occurrence SET source_node_id=?,target_node_id=?,edge_id=?,metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE occurrence_id=?",
            (source_id, target_id, edge_value, json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["occurrence_id"]),
        )


def _rewrite_embeddings(conn: sqlite3.Connection, id_map: dict[str, str]) -> None:
    if not _has_table(conn, "embedding"):
        return
    for old_id, new_id in id_map.items():
        if old_id == new_id:
            continue
        row = conn.execute("SELECT * FROM embedding WHERE node_id=?", (old_id,)).fetchone()
        if not row:
            continue
        if conn.execute("SELECT 1 FROM embedding WHERE node_id=?", (new_id,)).fetchone():
            conn.execute("DELETE FROM embedding WHERE node_id=?", (old_id,))
        else:
            conn.execute("UPDATE embedding SET node_id=? WHERE node_id=?", (new_id, old_id))


def _rewrite_known_node_references(conn: sqlite3.Connection, id_map: dict[str, str]) -> None:
    for table, columns in {
        "llm_reference_extraction": ("node_id",),
        "llm_reference_resolution": ("source_node_id", "target_node_id"),
    }.items():
        if not _has_table(conn, table):
            continue
        available = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in available:
                continue
            for old_id, new_id in id_map.items():
                conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (new_id, old_id))


def _rewrite_aliases(conn: sqlite3.Connection, old_alias_rows: list[sqlite3.Row], id_map: dict[str, str]) -> None:
    if _has_table(conn, "node_aliases"):
        conn.execute("DELETE FROM node_aliases")
        conn.executemany(
            "INSERT INTO node_aliases(node_id,alias_type,alias_value) VALUES(?,?,?)",
            [(id_map.get(row["node_id"], row["node_id"]), row["alias_type"], row["alias_value"]) for row in old_alias_rows],
        )
    if _has_table(conn, "node_alias"):
        # A partially applied deployment may already have the new alias table
        # populated with old node IDs. Remove only those rows, preserving
        # unrelated aliases, then reinsert them against the live IDs.
        for old_id, new_id in id_map.items():
            if old_id == new_id:
                continue
            conn.execute("DELETE FROM node_alias WHERE node_id=?", (old_id,))
    for row in old_alias_rows:
        _insert_alias(conn, row["alias_type"], row["alias_value"], id_map.get(row["node_id"], row["node_id"]))


def _insert_aliases(conn: sqlite3.Connection, plans: list[dict[str, object]]) -> None:
    for plan in plans:
        _insert_alias(conn, "legacy_id", str(plan["old_id"]), str(plan["version_id"]))
        _insert_alias(conn, "legacy_stable_key", str(plan["old_stable_key"]), str(plan["version_id"]))


def _insert_alias(conn: sqlite3.Connection, alias_type: str, alias_value: str, node_id_value: str) -> None:
    if alias_type in {"legacy_id", "legacy_stable_key"}:
        existing = conn.execute(
            "SELECT node_id FROM node_alias WHERE alias_type=? AND alias_value=? AND node_id<>? LIMIT 1",
            (alias_type, alias_value, node_id_value),
        ).fetchone()
        if existing:
            raise ValueError(f"conflicting alias identity: {alias_type}:{alias_value}")
    conn.execute(
        "INSERT OR IGNORE INTO node_alias(node_id,alias_type,alias_value) VALUES(?,?,?)",
        (node_id_value, alias_type, alias_value),
    )


def _insert_identity_edges(conn: sqlite3.Connection, plans: list[dict[str, object]]) -> None:
    for plan in plans:
        edges = [
            (
                edge_id(str(plan["canonical_id"]), str(plan["version_id"]), "has_version"),
                plan["canonical_id"], plan["version_id"], "has_version", "legal_identity", 1.0, "", plan["source_url"],
                json.dumps({"canonical_key": plan["canonical_key"], "version_key": plan["version_key"]}, ensure_ascii=False, sort_keys=True),
            ),
            (
                edge_id(str(plan["version_id"]), str(plan["source_page_id"]), "sourced_from"),
                plan["version_id"], plan["source_page_id"], "sourced_from", "legal_identity", 1.0, "", plan["source_url"],
                json.dumps({"source_page_key": plan["source_page_key"], "snapshot_id": plan["snapshot_id"]}, ensure_ascii=False, sort_keys=True),
            ),
        ]
        conn.executemany(
            """
            INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              from_node_id=excluded.from_node_id,to_node_id=excluded.to_node_id,
              edge_type=excluded.edge_type,source_method=excluded.source_method,
              confidence=excluded.confidence,source_url=excluded.source_url,
              metadata_json=excluded.metadata_json
            """,
            edges,
        )


def _parent_node(conn: sqlite3.Connection, node_id_value: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.id,p.node_type,p.stable_key,p.metadata_json
        FROM edge e JOIN node p ON p.id=e.from_node_id
        WHERE e.to_node_id=? AND e.edge_type='contains'
        ORDER BY CASE p.node_type WHEN 'chapter' THEN 0 WHEN 'part' THEN 1 ELSE 2 END,p.id
        LIMIT 1
        """,
        (node_id_value,),
    ).fetchone()


def _structural_locator(parent: sqlite3.Row | None, html_id: str) -> str:
    metadata = _json(parent["metadata_json"] if parent else "{}")
    if parent and parent["node_type"] == "part":
        prefix = "part"
    elif metadata.get("chapter_number"):
        prefix = f"chapter:{metadata['chapter_number']}"
    else:
        prefix = f"container:{metadata.get('html_id') or (parent['stable_key'].rsplit(':', 1)[-1] if parent else 'root')}"
    return f"{prefix}:{html_id}" if html_id else prefix


def _source_url(value: str, stable_key: str) -> str:
    if value:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    marker = stable_key.split("rule:part:", 1)[-1]
    pieces = marker.split(":")
    path = pieces[0]
    if len(pieces) > 1 and rulebook_date_from_url(path) is None:
        for i in range(1, min(4, len(pieces))):
            candidate = ":".join(pieces[: i + 1])
            if rulebook_date_from_url(candidate):
                path = candidate
                break
    return f"https://www.prarulebook.co.uk/{path.strip('/')}"


def _rule_number_from_stable(stable_key: str) -> str:
    return stable_key.rsplit(":", 1)[-1] if stable_key else ""


def _legacy_alias_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    seen: set[tuple[str, str, str]] = set()
    for table in ("node_aliases", "node_alias"):
        if not _has_table(conn, table):
            continue
        for row in conn.execute(f"SELECT node_id,alias_type,alias_value FROM {table}"):
            key = (str(row["node_id"]), str(row["alias_type"]), str(row["alias_value"]))
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def _replace_ids(value: object, id_map: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {key: _replace_ids(item, id_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ids(item, id_map) for item in value]
    if isinstance(value, str):
        return id_map.get(value, value)
    return value


def _json(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _mark_projections_dirty(conn: sqlite3.Connection) -> None:
    if _has_table(conn, "search_projection_state"):
        conn.execute("UPDATE search_projection_state SET dirty=1 WHERE singleton=1")
