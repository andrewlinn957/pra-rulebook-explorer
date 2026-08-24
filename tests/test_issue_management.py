import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app import main
from backend.app.reported_issues import create_issue, list_issues


class IssueMaintenanceApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def _client(self):
        transport = httpx.ASGITransport(app=main.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    async def test_post_report_endpoint_still_creates_issue(self):
        payload = {
            "node": {"id": "node-1", "title": "Rule one", "node_type": "rule"},
            "description": "Missing reference",
            "page_url": "https://example.test/rule-one",
            "context": "graph_view",
        }
        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                response = await client.post("/issues/node", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["description"], "Missing reference")
        self.assertEqual(len(list_issues(self.root)["items"]), 1)

    async def test_patch_updates_description_and_status(self):
        item = create_issue(self.root, node={"id": "node-1", "title": "Rule one"}, description="Old")

        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                response = await client.patch(
                    f"/issues/{item['id']}",
                    json={"description": "New", "status": "in_progress"},
                )

        self.assertEqual(response.status_code, 200)
        updated = response.json()["item"]
        self.assertEqual(updated["description"], "New")
        self.assertEqual(updated["status"], "in_progress")
        self.assertEqual(updated["node"], item["node"])

    async def test_patch_rejects_bad_status_and_oversized_description(self):
        item = create_issue(self.root, node={"id": "node-1"}, description="Original")

        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                bad_status = await client.patch(f"/issues/{item['id']}", json={"status": "unknown"})
                oversized = await client.patch(
                    f"/issues/{item['id']}",
                    json={"description": "x" * 2001, "status": "open"},
                )

        self.assertEqual(bad_status.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        stored = list_issues(self.root)["items"][0]
        self.assertEqual(stored["description"], "Original")

    async def test_patch_malformed_json_returns_400(self):
        item = create_issue(self.root, node={"id": "node-1"})

        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                response = await client.patch(
                    f"/issues/{item['id']}",
                    content="{not-json",
                    headers={"Content-Type": "application/json"},
                )

        self.assertEqual(response.status_code, 400)

    async def test_patch_unknown_issue_returns_404(self):
        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                response = await client.patch("/issues/ghost", json={"status": "resolved"})

        self.assertEqual(response.status_code, 404)

    async def test_delete_removes_issue(self):
        item = create_issue(self.root, node={"id": "node-1"})

        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                response = await client.delete(f"/issues/{item['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["id"], item["id"])
        self.assertEqual(list_issues(self.root)["items"], [])

    async def test_delete_unknown_issue_returns_404(self):
        with patch.object(main, "PROJECT_ROOT", self.root):
            async with await self._client() as client:
                response = await client.delete("/issues/ghost")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
