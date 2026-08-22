"""Tests for the materialised analysis cache."""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from backend.app.analysis_cache import (
    CACHE_KEYS,
    get_or_compute,
    graph_fingerprint,
    invalidate_all,
    precompute_all,
    read_cached,
    write_cached,
)
from backend.app.migrations import apply_migrations


def _setup_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
          from_node_id TEXT NOT NULL,
          to_node_id TEXT NOT NULL,
          edge_type TEXT NOT NULL,
          source_method TEXT NOT NULL,
          confidence REAL DEFAULT 1.0,
          evidence_text TEXT DEFAULT '',
          source_url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        """
    )
    # Small chain: a-b-c
    for i in range(3):
        conn.execute(
            "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
            (f"n{i}", "rule", f"k{i}", f"N{i}", "", "", "{}"),
        )
    conn.execute("INSERT INTO edge VALUES ('e1','n0','n1','references','html_link',1.0,'','','{}')")
    conn.execute("INSERT INTO edge VALUES ('e2','n1','n2','contains','site_structure',1.0,'','','{}')")
    return conn


class AnalysisCacheTests(unittest.TestCase):
    def setUp(self):
        self.conn = _setup_db()
        apply_migrations(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_migrate_v11_creates_analysis_cache(self):
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_cache'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_write_and_read_roundtrip(self):
        payload = {"degree": [{"node": {"id": "n1"}, "degree": 2}]}
        write_cached(self.conn, "centrality", payload, node_count=3, edge_count=2)
        result = read_cached(self.conn, "centrality")
        self.assertEqual(result, payload)

    def test_get_or_compute_returns_cached_without_recompute(self):
        payload = {"component_count": 1, "largest_size": 3, "components": []}
        write_cached(self.conn, "components", payload)
        with patch("backend.app.analysis_cache._components") as mock:
            result = get_or_compute(self.conn, "components")
            mock.assert_not_called()
        self.assertEqual(result, payload)

    def test_get_or_compute_computes_when_missing(self):
        with patch("backend.app.analysis_cache._components") as mock:
            mock.return_value = {"component_count": 1, "largest_size": 3, "components": [], "graph_nodes": 0, "graph_edges": 0}
            result = get_or_compute(self.conn, "components")
            mock.assert_called_once()
        self.assertEqual(result["component_count"], 1)
        cached_after = read_cached(self.conn, "components")
        self.assertIsNotNone(cached_after)

    def test_invalidate_clears_all(self):
        write_cached(self.conn, "centrality", {"degree": []})
        write_cached(self.conn, "components", {})
        invalidate_all(self.conn)
        self.assertIsNone(read_cached(self.conn, "centrality"))
        self.assertIsNone(read_cached(self.conn, "components"))

    def test_fingerprint_changes_when_edge_count_changes(self):
        fp1 = graph_fingerprint(self.conn)
        self.conn.execute("INSERT INTO edge VALUES ('e3','n2','n0','references','html_link',1.0,'','','{}')")
        fp2 = graph_fingerprint(self.conn)
        self.assertNotEqual(fp1, fp2)

    def test_precompute_all_populates_every_key(self):
        results = precompute_all(self.conn)
        for key in CACHE_KEYS:
            self.assertIn(key, results)
            self.assertIsNotNone(read_cached(self.conn, key))


if __name__ == "__main__":
    unittest.main()
