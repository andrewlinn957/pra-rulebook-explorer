import json
import sqlite3

import pytest

from backend.app.legal_identity_migration import migrate_legal_identity
from backend.rulebook_scraper.parse import node_id
from backend.rulebook_scraper.legal_identity import canonical_provision_key, provision_version_key, snapshot_id


PART_URL = "https://www.prarulebook.co.uk/pra-rules/test-part/01-06-2026"
JULY_PART_URL = "https://www.prarulebook.co.uk/pra-rules/test-part/01-07-2026"


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE document_source (
          id TEXT PRIMARY KEY, source_type TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
          fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL, raw_html TEXT NOT NULL,
          raw_text TEXT DEFAULT ''
        );
        CREATE TABLE node (
          id TEXT PRIMARY KEY, node_type TEXT NOT NULL, stable_key TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, text TEXT DEFAULT '', url TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE edge (
          id TEXT PRIMARY KEY, from_node_id TEXT NOT NULL, to_node_id TEXT NOT NULL,
          edge_type TEXT NOT NULL, source_method TEXT NOT NULL, confidence REAL NOT NULL,
          evidence_text TEXT DEFAULT '', source_url TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE reference_occurrence (
          occurrence_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, source_node_id TEXT NOT NULL,
          target_node_id TEXT, edge_id TEXT, relationship_type TEXT NOT NULL DEFAULT 'REF',
          citation_kind TEXT NOT NULL, citation_text TEXT NOT NULL, group_text TEXT NOT NULL,
          instrument_id TEXT, provision_path TEXT, qualifier TEXT DEFAULT '', span_start INTEGER NOT NULL,
          span_end INTEGER NOT NULL, status TEXT NOT NULL, source_method TEXT NOT NULL,
          confidence REAL NOT NULL, context_text TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE embedding (
          node_id TEXT PRIMARY KEY, model_name TEXT NOT NULL, text_hash TEXT NOT NULL,
          vector_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE node_alias (
          alias_type TEXT NOT NULL, alias_value TEXT NOT NULL, node_id TEXT NOT NULL,
          PRIMARY KEY(alias_type, alias_value)
        );
        """
    )
    return conn


def seed_db(conn: sqlite3.Connection) -> tuple[str, str, str, str]:
    part_id = node_id("part:pra-rules/test-part/01-06-2026")
    chapter_id = node_id("chapter:part:pra-rules/test-part/01-06-2026:2")
    source_id = node_id("rule:part:pra-rules/test-part/01-06-2026:2.1")
    target_id = node_id("rule:part:pra-rules/test-part/01-06-2026:2.2")
    conn.execute(
        "INSERT INTO document_source VALUES (?,?,?,?,?,?,?)",
        ("source", "part", PART_URL, "2026-06-01T00:00:00Z", "hash", "<html>old</html>", "old"),
    )
    conn.executemany(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        [
            (part_id, "part", "part:pra-rules/test-part/01-06-2026", "Test Part", "", PART_URL, "{}"),
            (chapter_id, "chapter", "chapter:part:pra-rules/test-part/01-06-2026:2", "Article 2", "", PART_URL + "#chapter-2", json.dumps({"chapter_number": "2", "html_id": "chapter-2"})),
            (source_id, "rule", "rule:part:pra-rules/test-part/01-06-2026:2.1", "2.1", "Source text", PART_URL + "#rule-21", json.dumps({"rule_number": "2.1", "html_id": "rule-21", "nested": {"node_id": target_id}})),
            (target_id, "rule", "rule:part:pra-rules/test-part/01-06-2026:2.2", "2.2", "Target text", PART_URL + "#rule-22", json.dumps({"rule_number": "2.2", "html_id": "rule-22"})),
        ],
    )
    contains_part = "contains-part"
    contains_source = "contains-source"
    contains_target = "contains-target"
    reference = "reference"
    conn.executemany(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (contains_part, part_id, chapter_id, "contains", "site_structure", 1.0, "", PART_URL, "{}"),
            (contains_source, chapter_id, source_id, "contains", "site_structure", 1.0, "", PART_URL, "{}"),
            (contains_target, chapter_id, target_id, "contains", "site_structure", 1.0, "", PART_URL, "{}"),
            (reference, source_id, target_id, "references", "regex_reference", 0.9, "Article 2.2", PART_URL, json.dumps({"nested_node": target_id})),
        ],
    )
    conn.execute(
        """INSERT INTO reference_occurrence
          (occurrence_id,group_id,source_node_id,target_node_id,edge_id,citation_kind,citation_text,group_text,
           span_start,span_end,status,source_method,confidence,metadata_json)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("occ", "group", source_id, target_id, reference, "article", "Article 2.2", "Article 2.2", 0, 10, "materialized", "test", 1.0, json.dumps({"source": source_id, "target": target_id})),
    )
    conn.execute("INSERT INTO embedding VALUES (?,?,?,?,?)", (source_id, "test", "hash", "[1,2]", "2026-06-01"))
    return part_id, chapter_id, source_id, target_id


def test_migration_creates_canonical_versions_and_remaps_dependants() -> None:
    conn = make_db()
    part_id, chapter_id, old_source_id, old_target_id = seed_db(conn)
    conn.execute(
        "INSERT INTO node_alias(alias_type,alias_value,node_id) VALUES(?,?,?)",
        ("legacy_id", "preexisting-source-alias", old_source_id),
    )

    result = migrate_legal_identity(conn)

    assert result["migrated_versions"] == 2
    versions = conn.execute("SELECT id,stable_key,metadata_json FROM node WHERE node_type='rule'").fetchall()
    assert len(versions) == 2
    assert all(row["stable_key"].startswith("provision_version:") for row in versions)
    canonical = conn.execute("SELECT id,stable_key FROM node WHERE node_type='provision'").fetchall()
    assert len(canonical) == 2
    canonical_ids = {row["id"] for row in canonical}
    version_ids = {row["id"] for row in versions}
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE edge_type='has_version'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE edge_type='sourced_from'").fetchone()[0] == 2

    reference = conn.execute("SELECT * FROM edge WHERE edge_type='references'").fetchone()
    assert reference["from_node_id"] in version_ids
    assert reference["to_node_id"] in canonical_ids
    assert old_source_id not in {reference["from_node_id"], reference["to_node_id"]}
    assert old_target_id not in {reference["from_node_id"], reference["to_node_id"]}

    occurrence = conn.execute("SELECT * FROM reference_occurrence").fetchone()
    assert occurrence["source_node_id"] in version_ids
    assert occurrence["target_node_id"] in canonical_ids
    assert occurrence["edge_id"] == reference["id"]
    assert old_target_id not in occurrence["metadata_json"]
    assert conn.execute("SELECT COUNT(*) FROM embedding WHERE node_id=?", (old_source_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0] == 1
    aliases = conn.execute("SELECT alias_type,alias_value,node_id FROM node_alias").fetchall()
    assert {(row["alias_type"], row["alias_value"]) for row in aliases} >= {
        ("legacy_id", old_source_id),
        ("legacy_stable_key", "rule:part:pra-rules/test-part/01-06-2026:2.1"),
        ("legacy_id", "preexisting-source-alias"),
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM node_alias WHERE node_id=?",
        (old_source_id,),
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM document_snapshot").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM node WHERE id=?", (part_id,)).fetchone()[0] == 1


def test_migration_is_idempotent() -> None:
    conn = make_db()
    seed_db(conn)
    first = migrate_legal_identity(conn)
    counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("node", "edge", "document_snapshot", "node_alias")}
    second = migrate_legal_identity(conn)
    assert first["migrated_versions"] == 2
    assert second["migrated_versions"] == 0
    assert counts == {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in counts}


def test_migration_groups_same_provision_across_dated_pages() -> None:
    conn = make_db()
    seed_db(conn)

    july_part_id = node_id("part:pra-rules/test-part/01-07-2026")
    july_chapter_id = node_id("chapter:part:pra-rules/test-part/01-07-2026:2")
    july_source_id = node_id("rule:part:pra-rules/test-part/01-07-2026:2.1")
    july_target_id = node_id("rule:part:pra-rules/test-part/01-07-2026:2.2")
    conn.execute(
        "INSERT INTO document_source VALUES (?,?,?,?,?,?,?)",
        ("source-july", "part", JULY_PART_URL, "2026-07-01T00:00:00Z", "hash-july", "<html>july</html>", "july"),
    )
    conn.executemany(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        [
            (july_part_id, "part", "part:pra-rules/test-part/01-07-2026", "Test Part", "", JULY_PART_URL, "{}"),
            (july_chapter_id, "chapter", "chapter:part:pra-rules/test-part/01-07-2026:2", "Article 2", "", JULY_PART_URL + "#chapter-2", json.dumps({"chapter_number": "2", "html_id": "chapter-2"})),
            (july_source_id, "rule", "rule:part:pra-rules/test-part/01-07-2026:2.1", "2.1", "July source text", JULY_PART_URL + "#rule-21", json.dumps({"rule_number": "2.1", "html_id": "rule-21"})),
            (july_target_id, "rule", "rule:part:pra-rules/test-part/01-07-2026:2.2", "2.2", "July target text", JULY_PART_URL + "#rule-22", json.dumps({"rule_number": "2.2", "html_id": "rule-22"})),
        ],
    )
    conn.executemany(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("contains-part-july", july_part_id, july_chapter_id, "contains", "site_structure", 1.0, "", JULY_PART_URL, "{}"),
            ("contains-source-july", july_chapter_id, july_source_id, "contains", "site_structure", 1.0, "", JULY_PART_URL, "{}"),
            ("contains-target-july", july_chapter_id, july_target_id, "contains", "site_structure", 1.0, "", JULY_PART_URL, "{}"),
        ],
    )

    result = migrate_legal_identity(conn)

    assert result["migrated_versions"] == 4
    assert conn.execute("SELECT COUNT(*) FROM node WHERE node_type='provision'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM node WHERE node_type='rule'").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE edge_type='has_version'").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE edge_type='sourced_from'").fetchone()[0] == 4
    assert all(
        count == 2
        for (count,) in conn.execute(
            "SELECT COUNT(*) FROM edge WHERE edge_type='has_version' GROUP BY from_node_id"
        ).fetchall()
    )


def test_conflicting_identity_rolls_back_every_change() -> None:
    conn = make_db()
    part_id, chapter_id, source_id, _ = seed_db(conn)
    conflicting_id = node_id("rule:part:pra-rules/test-part/01-06-2026:2.1:duplicate")
    conn.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        (conflicting_id, "rule", "rule:part:pra-rules/test-part/01-06-2026:2.1:duplicate", "2.1 duplicate", "conflict", PART_URL + "#rule-21", json.dumps({"rule_number": "2.1", "html_id": "rule-21"})),
    )
    conn.execute("INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)", ("contains-conflict", chapter_id, conflicting_id, "contains", "site_structure", 1.0, "", PART_URL, "{}"))
    before = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    with pytest.raises(ValueError, match="conflicting version identity"):
        migrate_legal_identity(conn)
    assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == before
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='document_snapshot'").fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM node WHERE id=?", (source_id,)).fetchone()[0] == 1
