#!/usr/bin/env python3
"""Re-anchor occurrence spans only when one exact lexical anchor is provable.

Preview is read-only. Apply creates a consistent SQLite backup, updates offsets
for unique exact/whitespace-equivalent group or citation text, and records when
the span is relative to a structural node title rather than body text.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend/data/rulebook.sqlite3"


def _boundary_ok(text: str, start: int, end: int) -> bool:
    first, last = text[start], text[end - 1]
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return not (first.isalnum() and before.isalnum()) and not (last.isalnum() and after.isalnum())


def _matches(text: str, phrase: str, *, require_boundaries: bool = True) -> list[tuple[int, int]]:
    words = str(phrase or "").split()
    if not words:
        return []
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)
    return [
        (match.start(), match.end())
        for match in pattern.finditer(text)
        if not require_boundaries or _boundary_ok(text, match.start(), match.end())
    ]


def _unique_anchor(
    text: str,
    group_text: str,
    citation_text: str,
    context_text: str = "",
    previous_span: tuple[int, int] | None = None,
) -> tuple[int, int, str] | None:
    # group_text is the complete lexical citation shared by expanded members;
    # prefer it when unique even if a shorter member citation also appears
    # elsewhere in the same source.
    for field, phrase in (("group_text", group_text), ("citation_text", citation_text)):
        if not phrase:
            continue
        found = _matches(text, phrase)
        if len(found) == 1:
            return found[0][0], found[0][1], field
    if previous_span:
        old_start, old_end = previous_span
        if 0 <= old_start < old_end <= len(text):
            old_text = " ".join(text[old_start:old_end].split()).casefold()
            for field, phrase in (("group_text", group_text), ("citation_text", citation_text)):
                if not phrase:
                    continue
                candidates = []
                for start, end in _matches(text, phrase):
                    if start <= old_start and old_end <= end:
                        old_part = " ".join(text[start:end][old_start-start:old_end-start].split()).casefold()
                        if old_part == old_text:
                            candidates.append((start, end))
                if len(candidates) == 1:
                    start, end = candidates[0]
                    return start, end, f"span_local_{field}"
    # Repeated citations can be disambiguated by the occurrence's saved local
    # context, but only when the complete exact phrase lies inside that exact
    # context in one unique source position.
    if context_text:
        context_ranges = _matches(text, context_text, require_boundaries=False)
        for field, phrase in (("group_text", group_text), ("citation_text", citation_text)):
            if not phrase:
                continue
            candidates = [
                match for match in _matches(text, phrase)
                if any(start <= match[0] and match[1] <= end for start, end in context_ranges)
            ]
            if len(candidates) == 1:
                return candidates[0][0], candidates[0][1], f"context_{field}"
    return None


def plan_repairs(conn: sqlite3.Connection) -> tuple[list[dict], Counter[str]]:
    rows = conn.execute(
        """
        SELECT o.occurrence_id,o.source_node_id,o.span_start,o.span_end,
               o.citation_text,o.group_text,o.context_text,o.metadata_json,
               n.node_type,n.title,n.text
        FROM reference_occurrence o
        LEFT JOIN node n ON n.id=o.source_node_id
        WHERE o.status IN ('materialized','unresolved')
        ORDER BY o.occurrence_id
        """
    ).fetchall()
    plan: list[dict] = []
    counts: Counter[str] = Counter()
    anchor_cache: dict[tuple[str, str, str, str, int, int], tuple[int, int, str] | None] = {}
    for row in rows:
        text = str(row["text"] or "")
        title = str(row["title"] or "")
        old_start, old_end = int(row["span_start"]), int(row["span_end"])
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        group_text = str(row["group_text"] or "").strip()
        citation_text = str(row["citation_text"] or "").strip()
        context_text = str(row["context_text"] or "").strip()
        basis = "title" if metadata.get("span_basis") == "title" else "text"
        anchor_text = title if basis == "title" else text
        in_bounds = 0 <= old_start < old_end <= len(anchor_text)
        current = anchor_text[old_start:old_end] if in_bounds else ""
        if in_bounds and " ".join(current.split()) in {
            " ".join(group_text.split()), " ".join(citation_text.split()),
        }:
            continue

        cache_key = (str(row["source_node_id"]), "text", group_text, citation_text + "\0" + context_text, old_start, old_end)
        if cache_key not in anchor_cache and text.strip():
            anchor_cache[cache_key] = _unique_anchor(
                text, group_text, citation_text, context_text, (old_start, old_end),
            )
        anchor = anchor_cache.get(cache_key)
        next_basis = "text"
        if anchor is None and not text.strip():
            title_previous_span = (old_start, old_end) if basis == "title" else None
            cache_start, cache_end = title_previous_span or (-1, -1)
            cache_key = (str(row["source_node_id"]), "title", group_text, citation_text + "\0" + context_text, cache_start, cache_end)
            if cache_key not in anchor_cache:
                anchor_cache[cache_key] = _unique_anchor(
                    title, group_text, citation_text, context_text,
                    title_previous_span,
                )
            anchor = anchor_cache[cache_key]
            next_basis = "title"
        if anchor is None:
            counts["ambiguous_or_missing_exact_anchor"] += 1
            continue

        start, end, anchor_field = anchor
        if next_basis == "title" and not title:
            counts["empty_source_title"] += 1
            continue
        if next_basis == "text" and not in_bounds:
            counts["invalid_span_unique_text_anchor"] += 1
        elif next_basis == "title":
            counts["title_span"] += 1
        else:
            counts["text_span"] += 1
        metadata["span_basis"] = next_basis
        metadata["span_repair"] = {
            "method": "unique_exact_lexical_anchor_v1",
            "anchor_field": anchor_field,
            "previous_span": {"start": old_start, "end": old_end},
        }
        if "group_span" in metadata:
            metadata["group_span"] = {"start": start, "end": end}
        plan.append({
            "occurrence_id": str(row["occurrence_id"]),
            "source_node_id": str(row["source_node_id"]),
            "start": start,
            "end": end,
            "basis": next_basis,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            "anchor_field": anchor_field,
        })
    return plan, counts


def consistent_backup(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path), timeout=60)
    target = sqlite3.connect(str(backup_path), timeout=60)
    try:
        source.backup(target, pages=2048, sleep=0.05)
    finally:
        target.close()
        source.close()


def _apply(conn: sqlite3.Connection, plan: list[dict]) -> None:
    for item in plan:
        conn.execute(
            "UPDATE reference_occurrence SET span_start=?,span_end=?,metadata_json=? WHERE occurrence_id=?",
            (item["start"], item["end"], item["metadata_json"], item["occurrence_id"]),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="Back up the database and apply unique-anchor repairs")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()
    db_path = args.db.resolve()
    if not db_path.is_file():
        parser.error(f"database does not exist: {db_path}")
    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        plan, counts = plan_repairs(conn)
        print(json.dumps({"mode": "apply" if args.apply else "preview", "planned": dict(counts), "updates": len(plan)}, indent=2))
        if not args.apply or not plan:
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = (args.backup_dir or db_path.parent / "backups").resolve()
        backup_path = backup_dir / f"{db_path.stem}.pre-span-repair-{stamp}{db_path.suffix}"
        conn.close()
        consistent_backup(db_path, backup_path)

        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN IMMEDIATE")
        plan, counts = plan_repairs(conn)
        _apply(conn, plan)
        for item in plan:
            source = conn.execute(
                "SELECT text,title FROM node WHERE id=?", (item["source_node_id"],),
            ).fetchone()
            content = str(source["title"] if item["basis"] == "title" else source["text"] or "")
            current = content[item["start"]:item["end"]]
            if not current.strip():
                raise RuntimeError(f"empty repaired span for {item['occurrence_id']}")
        conn.commit()
        receipt = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": str(db_path),
            "backup": str(backup_path),
            "repaired_occurrences": len(plan),
            "planned_by_type": dict(counts),
            "remaining_unmatched_or_ambiguous": counts["ambiguous_or_missing_exact_anchor"],
        }
        receipt_path = backup_path.with_suffix(backup_path.suffix + ".repair.json")
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt, indent=2))
        return 0
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
