"""Tests for the bidirectional shortest-path implementation."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from backend.app.graph import shortest_path


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
        CREATE INDEX idx_edge_from ON edge(from_node_id);
        CREATE INDEX idx_edge_to ON edge(to_node_id);
        """
    )
    # Chain a-b-c-d plus an isolated node e
    for nid in ("a", "b", "c", "d", "e"):
        conn.execute("INSERT INTO node VALUES (?,?,?,?,?,?,?)", (nid, "rule", f"key_{nid}", nid.upper(), "", "", "{}"))
    for eid, f, t in [("e1","a","b"), ("e2","b","c"), ("e3","c","d")]:
        conn.execute(
            "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, f, t, "references", "html_link", 1.0, "", "", "{}"),
        )
    return conn


class ShortestPathTests(unittest.TestCase):
    def setUp(self):
        self.conn = _setup_db()

    def tearDown(self):
        self.conn.close()

    def test_same_source_and_target_returns_single_node(self):
        result = shortest_path(self.conn, "a", "a")
        self.assertEqual(result["node_ids"], ["a"])
        self.assertEqual(result["length"], 0)

    def test_direct_path_found_in_two_hops(self):
        result = shortest_path(self.conn, "a", "d")
        self.assertEqual(result["length"], 3)
        self.assertEqual(result["node_ids"][0], "a")
        self.assertEqual(result["node_ids"][-1], "d")
        self.assertEqual(len(result["nodes"]), 4)
        self.assertEqual(len(result["edges"]), 3)

    def test_missing_source_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            shortest_path(self.conn, "ghost", "d")
        self.assertIn("source", str(ctx.exception))

    def test_missing_target_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            shortest_path(self.conn, "a", "ghost")
        self.assertIn("target", str(ctx.exception))

    def test_unreachable_target_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            shortest_path(self.conn, "a", "e")
        self.assertIn("no path", str(ctx.exception))

    def test_edges_include_type_and_method(self):
        result = shortest_path(self.conn, "a", "c")
        self.assertGreater(len(result["edges"]), 0)
        for edge in result["edges"]:
            self.assertIn("edge_type", edge)
            self.assertIn("source_method", edge)


if __name__ == "__main__":
    unittest.main()
