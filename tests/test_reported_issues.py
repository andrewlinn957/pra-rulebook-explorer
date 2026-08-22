"""Tests for the reported-issues queue."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from backend.app.reported_issues import (
    create_issue,
    list_issues,
    update_issue_status,
)


class ReportedIssuesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_issue_with_description(self):
        item = create_issue(
            self.root,
            node={"id": "n1", "title": "Test Node", "node_type": "rule"},
            description="Reference looks wrong",
            page_url="https://x/pra-rulebook",
            context="reading_mode",
        )
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["description"], "Reference looks wrong")
        self.assertEqual(item["context"], "reading_mode")
        stored = json.loads((self.root / "outputs/reported-issues/reported-issues.jsonl").read_text().strip())
        self.assertEqual(stored["id"], item["id"])

    def test_create_issue_without_description(self):
        item = create_issue(
            self.root,
            node={"id": "n2", "title": "Another"},
        )
        self.assertEqual(item["description"], "")
        self.assertEqual(item["status"], "open")

    def test_create_issue_requires_node_id(self):
        with self.assertRaises(ValueError):
            create_issue(self.root, node={})

    def test_create_issue_rejects_oversized_description(self):
        with self.assertRaises(ValueError):
            create_issue(self.root, node={"id": "n"}, description="x" * 2001)

    def test_node_payload_is_sanitised(self):
        item = create_issue(
            self.root,
            node={"id": "n", "title": "T", "text": "y" * 5000, "metadata": {"secret": True}},
        )
        stored = json.loads((self.root / "outputs/reported-issues/reported-issues.jsonl").read_text())
        self.assertNotIn("metadata", stored["node"])
        self.assertLessEqual(len(stored["node"]["text"]), 501)

    def test_list_and_filter_by_status(self):
        a = create_issue(self.root, node={"id": "a"})
        b = create_issue(self.root, node={"id": "b"})
        update_issue_status(self.root, issue_id=a["id"], status="resolved")
        result = list_issues(self.root)
        self.assertEqual(result["counts"]["open"], 1)
        self.assertEqual(result["counts"]["resolved"], 1)
        only_open = list_issues(self.root, status="open")
        self.assertEqual([i["id"] for i in only_open["items"]], [b["id"]])

    def test_update_status_appends_note(self):
        item = create_issue(self.root, node={"id": "a"})
        update_issue_status(self.root, issue_id=item["id"], status="in_progress", note="looking into it")
        result = list_issues(self.root)
        self.assertEqual(result["items"][0]["status"], "in_progress")
        self.assertEqual(result["items"][0]["notes"][0]["text"], "looking into it")

    def test_update_rejects_bad_status(self):
        item = create_issue(self.root, node={"id": "a"})
        with self.assertRaises(ValueError):
            update_issue_status(self.root, issue_id=item["id"], status="bogus")

    def test_update_missing_issue_raises(self):
        with self.assertRaises(ValueError):
            update_issue_status(self.root, issue_id="ghost", status="resolved")

    def test_concurrent_writes_do_not_lose_entries(self):
        def worker(n):
            for _ in range(20):
                create_issue(self.root, node={"id": f"n{n}"})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result = list_issues(self.root)
        self.assertEqual(len(result["items"]), 80)


if __name__ == "__main__":
    unittest.main()
