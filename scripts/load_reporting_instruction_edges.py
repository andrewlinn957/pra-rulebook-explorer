#!/usr/bin/env python3
"""Load accepted reporting instruction-set edges into the graph.

Some semantic extraction runs produce accepted `USES_INSTRUCTIONS` candidate
edges after the main reporting package graph has already been built. This
loader projects those accepted return/template → instruction-set links into the
canonical `graph_edge` table, without touching other candidate edge types.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend/data/rulebook.sqlite3"
DEFAULT_RAW_ROOT = ROOT / "backend/data/raw/reporting-sources"


@dataclass(frozen=True)
class LoadResult:
    files_seen: int = 0
    candidates_seen: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    skipped_missing_node: int = 0


def load_reporting_instruction_edges(conn: sqlite3.Connection, raw_root: Path = DEFAULT_RAW_ROOT) -> LoadResult:
    """Insert accepted `USES_INSTRUCTIONS` candidate edges from reporting extracts.

    Only accepted candidates are loaded. Both return-level and template-level
    links are included, provided source and target nodes already exist in the
    graph. Existing source/type/target triples are left untouched, making the
    operation safe to run repeatedly.
    """
    files_seen = candidates_seen = inserted = skipped_existing = skipped_missing_node = 0
    for csv_path in _candidate_files(raw_root):
        files_seen += 1
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("edge_type") != "USES_INSTRUCTIONS" or row.get("review_status") != "accepted_candidate":
                    continue
                candidates_seen += 1
                source = (row.get("source_node_id") or "").strip()
                target = (row.get("target_node_id") or "").strip()
                if not source or not target:
                    skipped_missing_node += 1
                    continue
                if not _node_exists(conn, source) or not _node_exists(conn, target):
                    skipped_missing_node += 1
                    continue
                if _edge_exists(conn, source, target):
                    skipped_existing += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO graph_edge(
                        edge_id,source_node_id,target_node_id,edge_type,properties_json,
                        evidence_span_id,confidence,extraction_method,review_status
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _edge_id(source, target, csv_path),
                        source,
                        target,
                        "USES_INSTRUCTIONS",
                        json.dumps(
                            {
                                "explanation": row.get("explanation") or "",
                                "source": str(csv_path.relative_to(raw_root)) if _is_relative_to(csv_path, raw_root) else str(csv_path),
                            },
                            ensure_ascii=False,
                        ),
                        row.get("evidence_span_id") or None,
                        _float_or_none(row.get("confidence")),
                        row.get("extraction_method") or "semantic_extraction",
                        "accepted_candidate",
                    ),
                )
                inserted += 1
    conn.commit()
    return LoadResult(files_seen, candidates_seen, inserted, skipped_existing, skipped_missing_node)


def _candidate_files(raw_root: Path) -> Iterable[Path]:
    if raw_root.is_file():
        yield raw_root
        return
    yield from sorted(raw_root.glob("*/semantic-extraction/graph_edges_candidate.csv"))


def _node_exists(conn: sqlite3.Connection, node_id: str) -> bool:
    return conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (node_id,)).fetchone() is not None


def _edge_exists(conn: sqlite3.Connection, source: str, target: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM graph_edge WHERE source_node_id=? AND target_node_id=? AND edge_type='USES_INSTRUCTIONS'",
        (source, target),
    ).fetchone() is not None


def _edge_id(source: str, target: str, csv_path: Path) -> str:
    return "edge:" + hashlib.sha256(f"{source}|USES_INSTRUCTIONS|{target}|{csv_path}".encode()).hexdigest()[:16]


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    result = load_reporting_instruction_edges(conn, args.raw_root)
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
