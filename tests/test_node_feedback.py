import json
import tempfile
import unittest
from pathlib import Path

import httpx

from backend.app.feedback import create_feedback, list_feedback
from backend.app.main import app


class NodeFeedbackApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_feedback_request_returns_400_not_500(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/feedback/node", content="", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON", response.text)

    async def test_feedback_processing_is_not_exposed_over_http(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/feedback/process", json={"limit": 3})
        self.assertEqual(response.status_code, 404)


class NodeFeedbackTests(unittest.TestCase):
    def test_create_feedback_persists_pending_item_with_node_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = create_feedback(
                root,
                node={"id": "node:1", "title": "Test Node"},
                feedback="Something looks wrong here.",
                page_url="https://example.com/page",
            )
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["feedback"], "Something looks wrong here.")
            stored = json.loads((root / "outputs/node-feedback/feedback-queue.jsonl").read_text().strip())
            self.assertEqual(stored["id"], item["id"])
            self.assertEqual(stored["node"]["title"], "Test Node")

    def test_create_feedback_rejects_empty_and_oversized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                create_feedback(root, node={"id": "n"}, feedback="   ")
            with self.assertRaises(ValueError):
                create_feedback(root, node={"id": "n"}, feedback="x" * 2001)

    def test_list_feedback_returns_items_and_counts_without_runs_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_feedback(root, node={"id": "a"}, feedback="one")
            create_feedback(root, node={"id": "b"}, feedback="two")
            result = list_feedback(root)
            self.assertNotIn("runs", result)
            self.assertEqual(len(result["items"]), 2)
            self.assertEqual(result["counts"], {"pending": 2})

    def test_list_feedback_handles_corrupt_lines_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "outputs/node-feedback/feedback-queue.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("{\"id\": \"ok\", \"status\": \"pending\"}\nnot-json\n")
            result = list_feedback(root)
            self.assertEqual(len(result["items"]), 2)
            self.assertEqual(result["items"][1]["status"], "corrupt")


if __name__ == "__main__":
    unittest.main()
