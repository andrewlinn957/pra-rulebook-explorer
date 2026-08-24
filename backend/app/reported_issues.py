"""User-reported node issues queue.

Stores reports as append-only JSONL entries under outputs/reported-issues/.
Reports are collected for manual review and resolution by the operator.
"""
from __future__ import annotations

import fcntl
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUEUE_DIR = Path("outputs/reported-issues")
QUEUE_FILE = "reported-issues.jsonl"
MAX_DESCRIPTION_CHARS = 2_000
MAX_PAGE_URL_CHARS = 1_000
ALLOWED_STATUSES = frozenset({"open", "in_progress", "resolved", "wont_fix"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(root: Path) -> Path:
    return root / QUEUE_DIR / QUEUE_FILE


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


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
    with _lock_path(path).open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, sort_keys=True) + "\n")
                fh.flush()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_jsonl_unlocked(path: Path, items: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, sort_keys=True) + "\n")
        fh.flush()
    tmp.replace(path)


def _mutate_issue(path: Path, issue_id: str, mutation) -> dict[str, Any]:
    """Apply a mutation while holding the same lock used by appends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path(path).open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            items = _read_jsonl(path)
            found = next((item for item in items if item.get("id") == issue_id), None)
            if not found:
                raise ValueError(f"issue {issue_id!r} not found")
            mutation(found)
            found["updated_at"] = _now()
            _write_jsonl_unlocked(path, items)
            return found
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _clean_node(node: dict[str, Any]) -> dict[str, Any]:
    allowed = ["id", "node_type", "stable_key", "title", "text", "url"]
    clean = {k: (v[:500] + "…" if isinstance(v, str) and len(v) > 500 else v)
             for k, v in node.items() if k in allowed and v not in (None, "")}
    return clean


def create_issue(
    root: Path,
    *,
    node: dict[str, Any],
    description: str = "",
    page_url: str = "",
    context: str = "",
) -> dict[str, Any]:
    """Record a new issue report. Description is optional."""
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"description must be at most {MAX_DESCRIPTION_CHARS} characters")
    if len(page_url) > MAX_PAGE_URL_CHARS:
        raise ValueError(f"page_url must be at most {MAX_PAGE_URL_CHARS} characters")
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        raise ValueError("node.id is required")
    item = {
        "id": uuid.uuid4().hex[:12],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "open",
        "description": description,
        "page_url": page_url,
        "context": context,
        "node": _clean_node(node),
    }
    _append_jsonl(_queue_path(root), item)
    return item


def list_issues(root: Path, *, status: str | None = None) -> dict[str, Any]:
    items = list(reversed(_read_jsonl(_queue_path(root))))
    counts: dict[str, int] = {}
    filtered: list[dict[str, Any]] = []
    for item in items:
        s = item.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
        if status is None or s == status:
            filtered.append(item)
    return {"items": filtered, "counts": counts}


def update_issue_status(
    root: Path,
    *,
    issue_id: str,
    status: str,
    note: str = "",
) -> dict[str, Any]:
    path = _queue_path(root)
    _validate_status(status)

    def mutation(item: dict[str, Any]) -> None:
        item["status"] = status
        if note.strip():
            notes = item.setdefault("notes", [])
            notes.append({"text": note.strip(), "ts": _now()})

    return _mutate_issue(path, issue_id, mutation)


def _validate_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of: {sorted(ALLOWED_STATUSES)}")


def update_issue(
    root: Path,
    *,
    issue_id: str,
    description: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Amend an issue without changing its linked node or report context."""
    if description is None and status is None:
        raise ValueError("description or status is required")
    clean_description = None
    if description is not None:
        clean_description = description.strip()
        if len(clean_description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"description must be at most {MAX_DESCRIPTION_CHARS} characters")
    if status is not None:
        _validate_status(status)

    def mutation(item: dict[str, Any]) -> None:
        if clean_description is not None:
            item["description"] = clean_description
        if status is not None:
            item["status"] = status

    return _mutate_issue(_queue_path(root), issue_id, mutation)


def delete_issue(root: Path, *, issue_id: str) -> dict[str, Any]:
    """Permanently remove one issue and return the deleted item."""
    path = _queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path(path).open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            items = _read_jsonl(path)
            index = next((index for index, item in enumerate(items) if item.get("id") == issue_id), None)
            if index is None:
                raise ValueError(f"issue {issue_id!r} not found")
            deleted = items.pop(index)
            _write_jsonl_unlocked(path, items)
            return deleted
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
