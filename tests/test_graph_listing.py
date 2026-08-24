from __future__ import annotations

import json
import sqlite3
import unittest

from backend.app.db import configure_connection
from backend.app.graph import list_nodes


class GraphListingTests(unittest.TestCase):
    def setUp(self):
        self.conn = configure_connection(sqlite3.connect(":memory:"))
        self.conn.executescript(
            """
            CREATE TABLE node (
              id TEXT PRIMARY KEY,
              node_type TEXT NOT NULL,
              stable_key TEXT NOT NULL,
              title TEXT DEFAULT '',
              text TEXT DEFAULT '',
              url TEXT DEFAULT '',
              metadata_json TEXT DEFAULT '{}'
            );
            CREATE TABLE canonical_node (
              id TEXT PRIMARY KEY,
              is_canonical INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                "part-1",
                "part",
                "part-1",
                "Capital",
                "Long provision text that should not be sent to the initial catalogue.",
                "https://example.test/capital",
                json.dumps({
                    "firm_categories": ["CRR Firms"],
                    "large_metadata_field": "not needed in the catalogue",
                }),
            ),
        )
        self.conn.execute("INSERT INTO canonical_node(id) VALUES (?)", ("part-1",))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_summary_listing_keeps_navigation_metadata_but_omits_text(self):
        result = list_nodes(self.conn, node_types=["part"], limit=10, summary=True)

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0],
            {
                "id": "part-1",
                "node_type": "part",
                "stable_key": "part-1",
                "title": "Capital",
                "url": "https://example.test/capital",
                "metadata": {"firm_categories": ["CRR Firms"]},
            },
        )

    def test_full_listing_still_includes_text(self):
        result = list_nodes(self.conn, node_types=["part"], limit=10)

        self.assertEqual(result[0]["text"], "Long provision text that should not be sent to the initial catalogue.")


if __name__ == "__main__":
    unittest.main()
