from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUEUE_DIR = Path("outputs/node-feedback")
QUEUE_FILE = "feedback-queue.jsonl"
RUNS_FILE = "feedback-runs.jsonl"
DEFAULT_MODEL = "openai-codex/gpt-5.5"
DEFAULT_SESSION_ID = "pra-rulebook-feedback"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(root: Path) -> Path:
    return root / QUEUE_DIR / QUEUE_FILE


def _runs_path(root: Path) -> Path:
    return root / QUEUE_DIR / RUNS_FILE


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


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item, sort_keys=True) for item in items) + ("\n" if items else ""), encoding="utf-8")


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, sort_keys=True) + "\n")


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
    items = [_normalise_item_for_display(item) for item in _read_jsonl(_queue_path(root))]
    runs = _read_jsonl(_runs_path(root))
    counts: dict[str, int] = {}
    for item in items:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"items": items, "runs": runs[-20:], "counts": counts}


def _normalise_item_for_display(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item.get("last_result"), str):
        return item
    display = _extract_text_result(item["last_result"])
    if display == item["last_result"]:
        return item
    return {**item, "last_result": display}


def _prompt_for(item: dict[str, Any]) -> str:
    node = item.get("node") or {}
    return f"""You are Declan working in /root/.openclaw/workspace/projects/pra-rulebook-explorer.

Andrew submitted feedback from the PRA Rulebook Explorer UI. Handle it as a normal implementation/repair task.

Rules:
- Inspect the code/data first. Do not guess.
- If the feedback is clear and safe, make the smallest useful fix.
- Run the relevant tests or verification commands.
- If the feedback is ambiguous or unsafe, do not make speculative changes; write down what is blocked.
- Do not send external messages. Return a concise summary of what you did, tests run, and any blocker.

Feedback item: {item.get('id')}
Status: {item.get('status')}
Page URL: {item.get('page_url') or ''}

Node:
{json.dumps(node, indent=2, ensure_ascii=False)}

User feedback:
{item.get('feedback')}
"""


def _extract_result(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stdout or "").strip()
    if text:
        return _extract_text_result(text)
    return (completed.stderr or "").strip()[-4000:]


def _extract_text_result(text: str) -> str:
    text = text.strip()
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        extracted = _assistant_text_from_payload(payload)
        if extracted:
            return extracted[-4000:]
    fragment = _assistant_text_from_json_fragment(text)
    if fragment:
        return fragment[-4000:]
    return text[-4000:]


def _assistant_text_from_json_fragment(text: str) -> str:
    for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
        match = re.search(r'"' + key + r'"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.S)
        if not match:
            continue
        try:
            return json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            return match.group(1)
    return ""


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    candidates.extend(line.strip() for line in text.splitlines() if line.strip().startswith("{") and line.strip().endswith("}"))
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first:last + 1])
    return candidates


def _assistant_text_from_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        for item in reversed(payload):
            extracted = _assistant_text_from_payload(item)
            if extracted:
                return extracted
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in (
        "finalAssistantVisibleText",
        "finalAssistantRawText",
        "reply",
        "message",
        "content",
        "output",
        "result",
        "text",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            if key == "result" and value.strip().startswith("{"):
                nested = _extract_text_result(value)
                if nested:
                    return nested
            return value.strip()
        if isinstance(value, (dict, list)):
            extracted = _assistant_text_from_payload(value)
            if extracted:
                return extracted
    for value in payload.values():
        if isinstance(value, (dict, list)):
            extracted = _assistant_text_from_payload(value)
            if extracted:
                return extracted
    return ""


def process_feedback_queue(
    root: Path = PROJECT_ROOT,
    *,
    runner: Runner = subprocess.run,
    limit: int = 3,
    model: str = DEFAULT_MODEL,
    session_id: str = DEFAULT_SESSION_ID,
    timeout: int = 1800,
) -> dict[str, Any]:
    items = _read_jsonl(_queue_path(root))
    pending = [item for item in items if item.get("status") in {"pending", "failed"}][: max(1, min(limit, 10))]
    runs: list[dict[str, Any]] = []
    by_id = {item.get("id"): item for item in items}

    for item in pending:
        started = _now()
        item["status"] = "running"
        item["updated_at"] = started
        _write_jsonl(_queue_path(root), items)
        cmd = [
            "openclaw",
            "agent",
            "--session-id",
            session_id,
            "--model",
            model,
            "--thinking",
            "medium",
            "--json",
            "--timeout",
            str(timeout),
            "--message",
            _prompt_for(item),
        ]
        completed = runner(cmd, cwd=str(root), text=True, capture_output=True, timeout=timeout + 30)
        result = _extract_result(completed)
        status = "completed" if completed.returncode == 0 else "failed"
        run = {
            "id": uuid.uuid4().hex[:12],
            "feedback_id": item.get("id"),
            "started_at": started,
            "finished_at": _now(),
            "status": status,
            "returncode": completed.returncode,
            "result": result,
        }
        current = by_id.get(item.get("id"), item)
        current["status"] = status
        current["updated_at"] = run["finished_at"]
        current["last_run_id"] = run["id"]
        current["last_result"] = result
        runs.append(run)
        _append_jsonl(_runs_path(root), run)
        _write_jsonl(_queue_path(root), items)

    remaining = len([i for i in items if i.get("status") in {"pending", "failed"}])
    return {"processed": len(runs), "runs": runs, "remaining_pending": remaining}
