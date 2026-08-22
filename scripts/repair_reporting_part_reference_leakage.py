#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
STANDARD_FORMULA_URL = "%/pra-rules/solvency-capital-requirement---standard-formula/%"
EXTERNAL_AUDIT_URL = "%/pra-rules/external-audit/%"


def count_reporting_part_standard_formula_leaks(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """
        SELECT COUNT(*)
        FROM reference_occurrence ro
        JOIN node source ON source.id=ro.source_node_id
        JOIN node target ON target.id=ro.target_node_id
        WHERE ro.status='materialized'
          AND lower(target.url) LIKE ?
          AND (
            lower(ro.context_text) LIKE '%reporting part%'
            OR (
              lower(source.url) LIKE ?
              AND source.title='2.2'
              AND ro.source_method='resolution_policy_v1'
            )
          )
        """,
        (STANDARD_FORMULA_URL, EXTERNAL_AUDIT_URL),
    ).fetchone()[0]


def repair_reporting_part_standard_formula_leaks(conn: sqlite3.Connection) -> dict[str, int]:
    bad_edge_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT ro.edge_id
            FROM reference_occurrence ro
            JOIN node source ON source.id=ro.source_node_id
            JOIN node target ON target.id=ro.target_node_id
            WHERE ro.status='materialized'
              AND ro.edge_id IS NOT NULL
              AND lower(target.url) LIKE ?
              AND (
                lower(ro.context_text) LIKE '%reporting part%'
                OR (
                  lower(source.url) LIKE ?
                  AND source.title='2.2'
                  AND ro.source_method='resolution_policy_v1'
                )
              )
            """,
            (STANDARD_FORMULA_URL, EXTERNAL_AUDIT_URL),
        )
    ]
    before = conn.total_changes
    conn.execute(
        """
        UPDATE reference_occurrence
        SET status='not_reference',
            target_node_id=NULL,
            edge_id=NULL,
            updated_at=CURRENT_TIMESTAMP
        WHERE occurrence_id IN (
          SELECT ro.occurrence_id
          FROM reference_occurrence ro
          JOIN node source ON source.id=ro.source_node_id
          JOIN node target ON target.id=ro.target_node_id
          WHERE ro.status='materialized'
            AND lower(target.url) LIKE ?
            AND (
              lower(ro.context_text) LIKE '%reporting part%'
              OR (
                lower(source.url) LIKE ?
                AND source.title='2.2'
                AND ro.source_method='resolution_policy_v1'
              )
            )
        )
        """,
        (STANDARD_FORMULA_URL, EXTERNAL_AUDIT_URL),
    )
    marked = conn.total_changes - before

    deleted = 0
    if bad_edge_ids:
        placeholders = ",".join("?" for _ in bad_edge_ids)
        before = conn.total_changes
        conn.execute(
            f"""
            DELETE FROM edge
            WHERE id IN ({placeholders})
              AND NOT EXISTS (
                SELECT 1
                FROM reference_occurrence ro
                WHERE ro.edge_id=edge.id
                  AND ro.status='materialized'
              )
            """
            ,
            bad_edge_ids,
        )
        deleted = conn.total_changes - before

    return {"bad_occurrences_marked": marked, "bad_edges_deleted": deleted}


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.bak-reporting-part-leakage-{stamp}")
    shutil.copy2(db_path, backup)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(backup) + suffix))
    return backup


def run(db_path: Path, apply: bool) -> dict[str, int | str]:
    conn = connect(db_path)
    before = count_reporting_part_standard_formula_leaks(conn)
    report: dict[str, int | str] = {
        "reporting_part_standard_formula_leaks_before": before,
    }
    if not apply:
        return report

    backup = backup_db(db_path)
    with conn:
        report.update(repair_reporting_part_standard_formula_leaks(conn))
    report["reporting_part_standard_formula_leaks_after"] = count_reporting_part_standard_formula_leaks(conn)
    report["backup"] = str(backup)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove false Standard Formula references created from Reporting Part citation contexts."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for key, value in run(args.db, args.apply).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()