"""User-submitted feedback queue.

Stores feedback as append-only JSONL entries under outputs/node-feedback/.
There is no automated processing pipeline: feedback items are collected for
manual review only.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUEUE_DIR = Path("outputs/node-feedback")
QUEUE_FILE = "feedback-queue.jsonl"
MAX_FEEDBACK_CHARS = 2_000
MAX_PAGE_URL_CHARS = 1_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(root: Path) -> Path:
    return root / QUEUE_DIR / QUEUE_FILE


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append({"status": "corrupt", "raw": line})
    return items


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(item, sort_keys=True) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _clean_node(node: dict[str, Any]) -> dict[str, Any]:
    allowed = ["id", "node_type", "stable_key", "title", "text", "url", "metadata"]
    clean = {k: node.get(k) for k in allowed if node.get(k) not in (None, "")}
    if "text" in clean and isinstance(clean["text"], str) and len(clean["text"]) > 3000:
        clean["text"] = clean["text"][:3000] + "…"
    return clean


def create_feedback(root: Path, *, node: dict[str, Any], feedback: str, page_url: str = "") -> dict[str, Any]:
    feedback = feedback.strip()
    if not feedback:
        raise ValueError("feedback is required")
    if len(feedback) > MAX_FEEDBACK_CHARS:
        raise ValueError(f"feedback must be at most {MAX_FEEDBACK_CHARS} characters")
    if len(page_url) > MAX_PAGE_URL_CHARS:
        raise ValueError(f"page_url must be at most {MAX_PAGE_URL_CHARS} characters")
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        raise ValueError("node.id is required")
    item = {
        "id": uuid.uuid4().hex[:12],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "pending",
        "feedback": feedback,
        "page_url": page_url,
        "node": _clean_node(node),
    }
    _append_jsonl(_queue_path(root), item)
    return item


def list_feedback(root: Path) -> dict[str, Any]:
    """Return all queued feedback items with status counts."""
    items = _read_jsonl(_queue_path(root))
    counts: dict[str, int] = {}
    for item in items:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"items": items, "counts": counts}
