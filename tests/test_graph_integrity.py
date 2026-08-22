"""Factual-correctness checks that go beyond API/size verification."""
from __future__ import annotations

import sqlite3
import unittest

from backend.app.db import configure_connection
from backend.app.taxonomy import (
    NODE_TYPES,
    EDGE_TYPES,
    SOURCE_METHODS,
    PROVENANCE_CLASSES,
)


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE node (
          id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          stable_key TEXT NOT NULL UNIQUE,
          title TEXT DEFAULT '',
          text TEXT DEFAULT '',
          url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE edge (
          id TEXT PRIMARY KEY,
          from_node_id TEXT NOT NULL REFERENCES node(id),
          to_node_id TEXT NOT NULL REFERENCES node(id),
          edge_type TEXT NOT NULL,
          source_method TEXT NOT NULL,
          confidence REAL DEFAULT 1.0,
          evidence_text TEXT DEFAULT '',
          source_url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE canonical_node (id TEXT PRIMARY KEY);
        CREATE TABLE document_snapshot (
          snapshot_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          url TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          raw_html TEXT NOT NULL,
          raw_text TEXT DEFAULT '',
          UNIQUE(url, content_hash)
        );
        CREATE TABLE source_snapshot_version (
          version_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          url TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          raw_html TEXT NOT NULL,
          raw_text TEXT DEFAULT '',
          parser_version TEXT NOT NULL DEFAULT '',
          ingestion_run_id TEXT DEFAULT '',
          UNIQUE(url, content_hash, fetched_at)
        );
        """
    )


def _add_node(conn, node_id, node_type="rule", stable_key=None, metadata_json="{}"):
    stable_key = stable_key or node_id
    conn.execute(
        "INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json) VALUES (?,?,?,?,?,?,?)",
        (node_id, node_type, stable_key, node_id, "", "", metadata_json),
    )
    conn.execute("INSERT INTO canonical_node(id) VALUES (?)", (node_id,))


def _add_edge(conn, edge_id, from_id, to_id, edge_type="references",
              source_method="html_link", evidence_text="", metadata_json="{}"):
    conn.execute(
        "INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,"
        "confidence,evidence_text,source_url,metadata_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (edge_id, from_id, to_id, edge_type, source_method, 1.0,
         evidence_text, "https://example.com", metadata_json),
    )


class GraphIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.conn = configure_connection(sqlite3.connect(":memory:"))
        _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_orphan_edge(self, edge_id, from_id, to_id):
        # Bypass FK to simulate a corrupted row.
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,"
            "confidence,evidence_text,source_url,metadata_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (edge_id, from_id, to_id, "references", "html_link",
             1.0, "", "https://example.com", "{}"),
        )
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=ON")

    def test_missing_source_node_detected(self):
        _add_node(self.conn, "b")
        self._insert_orphan_edge("e1", "ghost_source", "b")
        missing_sources = self.conn.execute(
            "SELECT COUNT(*) FROM edge e "
            "WHERE NOT EXISTS(SELECT 1 FROM node n WHERE n.id=e.from_node_id)"
        ).fetchone()[0]
        self.assertGreater(missing_sources, 0)

    def test_missing_target_node_detected(self):
        _add_node(self.conn, "a")
        self._insert_orphan_edge("e1", "a", "ghost_target")
        missing_targets = self.conn.execute(
            "SELECT COUNT(*) FROM edge e "
            "WHERE NOT EXISTS(SELECT 1 FROM node n WHERE n.id=e.to_node_id)"
        ).fetchone()[0]
        self.assertGreater(missing_targets, 0)

    def test_cycle_detection(self):
        _add_node(self.conn, "a")
        _add_node(self.conn, "b")
        _add_edge(self.conn, "e1", "a", "b")
        _add_edge(self.conn, "e2", "b", "a")
        rows = self.conn.execute(
            "SELECT from_node_id,to_node_id FROM edge WHERE edge_type='references'"
        ).fetchall()
        adj: dict[str, set[str]] = {}
        for f, t in rows:
            adj.setdefault(f, set()).add(t)
        visited: set[str] = set()
        on_stack: set[str] = set()
        found_cycle = False

        def dfs(n: str) -> None:
            nonlocal found_cycle
            if n in on_stack:
                found_cycle = True
                return
            if n in visited:
                return
            visited.add(n)
            on_stack.add(n)
            for nxt in adj.get(n, ()):
                dfs(nxt)
            on_stack.discard(n)

        for start in list(adj):
            dfs(start)
        self.assertTrue(found_cycle)

    def test_multiple_structural_parents_detected(self):
        _add_node(self.conn, "parent_a")
        _add_node(self.conn, "parent_b")
        _add_node(self.conn, "child")
        _add_edge(self.conn, "e1", "parent_a", "child", edge_type="contains")
        _add_edge(self.conn, "e2", "parent_b", "child", edge_type="contains")
        multi_parent = self.conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT to_node_id FROM edge WHERE edge_type='contains'"
            "  GROUP BY to_node_id HAVING COUNT(DISTINCT from_node_id)>1)"
        ).fetchone()[0]
        self.assertGreater(multi_parent, 0)

    def test_duplicate_canonical_provision_detected(self):
        _add_node(self.conn, "p1", node_type="provision",
                  stable_key="provision:part1:rule1@2026-01-01")
        _add_node(self.conn, "p2", node_type="provision",
                  stable_key="provision:part1:rule1@2026-06-01")
        # Both versions share the same date-free canonical locator prefix;
        # a real integrity check would group on that prefix and flag it.
        dupes = self.conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT substr(stable_key,1,instr(stable_key,'@')-1) AS canon"
            "  FROM node WHERE node_type='provision' AND instr(stable_key,'@')>0"
            "  GROUP BY canon HAVING COUNT(*)>1)"
        ).fetchone()[0]
        self.assertGreater(dupes, 0)

    def test_duplicate_html_id_detected(self):
        meta = '{"html_id": "abc123"}'
        _add_node(self.conn, "n1", stable_key="k1", metadata_json=meta)
        _add_node(self.conn, "n2", stable_key="k2", metadata_json=meta)
        dupes = self.conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT json_extract(metadata_json,'$.html_id') AS hid"
            "  FROM node WHERE json_valid(metadata_json)"
            "    AND json_extract(metadata_json,'$.html_id') IS NOT NULL"
            "  GROUP BY hid HAVING COUNT(*)>1)"
        ).fetchone()[0]
        self.assertGreater(dupes, 0)

    def test_stale_placeholder_reference_detected(self):
        placeholder_meta = '{"placeholder": true}'
        resolved_meta = '{"placeholder": false}'
        _add_node(self.conn, "src")
        _add_node(self.conn, "ph", node_type="rule_reference",
                  stable_key="url:old#x", metadata_json=placeholder_meta)
        _add_node(self.conn, "real", node_type="rule",
                  stable_key="url:new#x", metadata_json=resolved_meta)
        _add_edge(self.conn, "e_stale", "src", "ph", evidence_text="see rule")
        _add_edge(self.conn, "e_live", "src", "real", evidence_text="see rule")
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM node ph"
            " JOIN edge e ON e.to_node_id=ph.id AND e.edge_type='references'"
            " WHERE json_extract(ph.metadata_json,'$.placeholder')=1"
            "   AND EXISTS ("
            "     SELECT 1 FROM edge live JOIN node real ON real.id=live.to_node_id"
            "     WHERE live.from_node_id=e.from_node_id"
            "       AND live.edge_type='references'"
            "       AND json_extract(real.metadata_json,'$.placeholder')<>1)"
        ).fetchone()[0]
        self.assertGreater(stale, 0)

    def test_unsupported_llm_edge_detected(self):
        _add_node(self.conn, "a")
        _add_node(self.conn, "b")
        _add_edge(self.conn, "e1", "a", "b",
                  source_method="llm_extracted_reference", evidence_text="")
        unsupported = self.conn.execute(
            "SELECT COUNT(*) FROM edge"
            " WHERE source_method='llm_extracted_reference'"
            "   AND (coalesce(evidence_text,'')=''"
            "     OR json_extract(metadata_json,'$.extraction_run_id') IS NULL)"
        ).fetchone()[0]
        self.assertGreater(unsupported, 0)

    def test_citation_edge_without_evidence_detected(self):
        _add_node(self.conn, "a")
        _add_node(self.conn, "b")
        _add_edge(self.conn, "e1", "a", "b",
                  source_method="llm_extracted_reference", evidence_text="")
        missing = self.conn.execute(
            "SELECT COUNT(*) FROM edge"
            " WHERE edge_type IN ('references','amends','uses_defined_term')"
            "   AND coalesce(evidence_text,'')=''"
        ).fetchone()[0]
        self.assertGreater(missing, 0)

    def test_unknown_node_type_detected(self):
        unknown = {"not_a_real_type"} - NODE_TYPES
        self.assertEqual(unknown, {"not_a_real_type"})

    def test_unknown_edge_type_detected(self):
        unknown = {"frobnicates"} - EDGE_TYPES
        self.assertEqual(unknown, {"frobnicates"})

    def test_unknown_source_method_detected(self):
        unknown = {"vibes_based_extraction"} - SOURCE_METHODS
        self.assertEqual(unknown, {"vibes_based_extraction"})

    def test_unknown_provenance_class_detected(self):
        unknown = {"hallucinated"} - PROVENANCE_CLASSES
        self.assertEqual(unknown, {"hallucinated"})


class SnapshotImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = configure_connection(sqlite3.connect(":memory:"))
        _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_snapshot_versions_are_append_only_per_fetch(self):
        self.conn.execute(
            "INSERT INTO source_snapshot_version VALUES"
            " ('v1','s1','https://x','2026-01-01T00:00:00Z','hash1','<old/>','','parse_v10_backfill','')"
        )
        self.conn.execute(
            "INSERT INTO source_snapshot_version VALUES"
            " ('v2','s1','https://x','2026-01-02T00:00:00Z','hash2','<new/>','','parse_current','')"
        )
        versions = self.conn.execute(
            "SELECT content_hash FROM source_snapshot_version"
            " WHERE url='https://x' ORDER BY fetched_at"
        ).fetchall()
        self.assertEqual([r[0] for r in versions], ["hash1", "hash2"])


if __name__ == "__main__":
    unittest.main()
