import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.feedback import create_feedback, list_feedback, process_feedback_queue
from backend.app.main import app


class NodeFeedbackApiTests(unittest.TestCase):
    def test_malformed_feedback_request_returns_400_not_500(self):
        client = TestClient(app)
        response = client.post("/feedback/node", content="", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON", response.text)


class NodeFeedbackTests(unittest.TestCase):
    def test_create_feedback_persists_pending_item_with_node_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = create_feedback(
                root,
                node={"id": "node-1", "node_type": "rule", "title": "Liquidity 1.1", "url": "https://example/rule"},
                feedback="This node is missing the relevant SS reference.",
            )

            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["node"]["id"], "node-1")
            self.assertIn("This node is missing", item["feedback"])

            queued = list_feedback(root)
            self.assertEqual(len(queued["items"]), 1)
            self.assertEqual(queued["items"][0]["id"], item["id"])

    def test_process_feedback_queue_runs_openclaw_for_pending_items_and_records_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_feedback(
                root,
                node={"id": "node-1", "node_type": "rule", "title": "Liquidity 1.1", "text": "A firm must..."},
                feedback="Please fix the missing reference edge.",
            )
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"reply": "Fixed and tested."}), stderr="")

            result = process_feedback_queue(root, runner=fake_run, limit=5)

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["runs"][0]["status"], "completed")
            self.assertEqual(len(calls), 1)
            self.assertIn("openclaw", calls[0][0][0])
            self.assertIn("Please fix the missing reference edge", " ".join(calls[0][0]))

            queued = list_feedback(root)
            self.assertEqual(queued["items"][0]["status"], "completed")
            self.assertEqual(queued["items"][0]["last_result"], "Fixed and tested.")

    def test_process_feedback_queue_reports_failed_openclaw_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_feedback(root, node={"id": "node-1", "title": "Node"}, feedback="Fix this")

            def fake_run(cmd, **kwargs):
                return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")

            result = process_feedback_queue(root, runner=fake_run)

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["runs"][0]["status"], "failed")
            self.assertIn("boom", result["runs"][0]["result"])
            self.assertEqual(list_feedback(root)["items"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
