#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"


def count_exact_duplicates(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """
        SELECT COALESCE(SUM(c-1), 0)
        FROM (
          SELECT COUNT(*) c
          FROM edge
          GROUP BY edge_type, from_node_id, to_node_id, source_method, evidence_text, source_url, metadata_json
          HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]


def count_html_regex_reference_duplicates(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """
        WITH html AS (
          SELECT from_node_id, to_node_id, evidence_text
          FROM edge
          WHERE edge_type='references' AND source_method='html_anchor_resolved'
        )
        SELECT COUNT(*)
        FROM edge regex
        WHERE regex.edge_type='references'
          AND regex.source_method='regex_reference'
          AND EXISTS (
            SELECT 1 FROM html
            WHERE html.from_node_id=regex.from_node_id
              AND html.to_node_id=regex.to_node_id
              AND html.evidence_text=COALESCE(json_extract(regex.metadata_json,'$.reference'), regex.evidence_text)
          )
        """
    ).fetchone()[0]


def delete_exact_duplicates(conn: sqlite3.Connection) -> int:
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM edge
        WHERE rowid NOT IN (
          SELECT MIN(rowid)
          FROM edge
          GROUP BY edge_type, from_node_id, to_node_id, source_method, evidence_text, source_url, metadata_json
        )
        """
    )
    return conn.total_changes - before


def delete_html_regex_reference_duplicates(conn: sqlite3.Connection) -> int:
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM edge AS regex
        WHERE regex.edge_type='references'
          AND regex.source_method='regex_reference'
          AND EXISTS (
            SELECT 1
            FROM edge AS html
            WHERE html.edge_type='references'
              AND html.source_method='html_anchor_resolved'
              AND html.from_node_id=regex.from_node_id
              AND html.to_node_id=regex.to_node_id
              AND html.evidence_text=COALESCE(json_extract(regex.metadata_json,'$.reference'), regex.evidence_text)
          )
        """
    )
    return conn.total_changes - before


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, backup)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(backup) + suffix))
    return backup


def run(db_path: Path, apply: bool) -> dict[str, int | str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    before_edges = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    exact_before = count_exact_duplicates(conn)
    html_regex_before = count_html_regex_reference_duplicates(conn)
    report: dict[str, int | str] = {
        "edges_before": before_edges,
        "exact_duplicates_before": exact_before,
        "html_regex_reference_duplicates_before": html_regex_before,
    }
    if not apply:
        return report

    backup = backup_db(db_path)
    with conn:
        report["exact_duplicates_deleted"] = delete_exact_duplicates(conn)
        report["html_regex_reference_duplicates_deleted"] = delete_html_regex_reference_duplicates(conn)
    report["edges_after"] = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    report["exact_duplicates_after"] = count_exact_duplicates(conn)
    report["html_regex_reference_duplicates_after"] = count_html_regex_reference_duplicates(conn)
    report["backup"] = str(backup)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate deterministic PRA Rulebook graph edges.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for k, v in run(args.db, args.apply).items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
