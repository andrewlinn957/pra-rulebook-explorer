"""Tests for M1 (FTS sanitisation) and M5 (blob table guard)."""
from __future__ import annotations

import sqlite3
import unittest

from backend.app.graph import _fts_safe_query
from backend.app.unified import BLOCKED_BLOB_TABLES


class FtsSanitisationTests(unittest.TestCase):
    def test_plain_query_is_wrapped(self):
        self.assertEqual(_fts_safe_query("capital"), '"capital"')

    def test_near_operator_neutralised(self):
        result = _fts_safe_query("a NEAR b")
        self.assertEqual(result, '"a NEAR b"')

    def test_column_filter_neutralised(self):
        result = _fts_safe_query("title: rule")
        self.assertEqual(result, '"title: rule"')

    def test_star_wildcard_neutralised(self):
        result = _fts_safe_query("rule*")
        self.assertEqual(result, '"rule*"')

    def test_internal_quotes_doubled(self):
        result = _fts_safe_query('say "hello"')
        self.assertEqual(result, '"say ""hello"""')


class BlobTableGuardTests(unittest.TestCase):
    def test_document_snapshot_is_blocked(self):
        self.assertIn("document_snapshot", BLOCKED_BLOB_TABLES)

    def test_source_snapshot_version_is_blocked(self):
        self.assertIn("source_snapshot_version", BLOCKED_BLOB_TABLES)


if __name__ == "__main__":
    unittest.main()
