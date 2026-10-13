#!/usr/bin/env python3
"""Rebuild one derived edge layer: remove previous output, regenerate, validate.

Usage:
    .venv/bin/python scripts/rebuild_derived_layer.py <layer_name> [--apply]

Layers are declared in backend/app/taxonomy.py DERIVED_LAYERS. A rebuild:
 1. makes a consistent SQLite backup;
 2. deletes old layer edges only when a replacement generator is registered;
 3. runs the layer-specific generator (if registered below);
 4. re-runs integrity checks before committing. Without --apply it only previews.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.taxonomy import DERIVED_LAYERS
from backend.app.db import configure_connection
from backend.app.integrity import integrity_report
try:
    from safe_connect import connect
except ImportError:
    from scripts.safe_connect import connect

DB = ROOT / "backend/data/rulebook.sqlite3"
BACKUP_DIR = ROOT / "backups"

# Generators run against the same transaction as cleanup. This prevents a
# second process from observing stale edges or committing around validation.
GENERATORS: dict[str, Callable[[sqlite3.Connection], None]] = {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=sorted(DERIVED_LAYERS))
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    parser.add_argument("--apply", action="store_true", help="Back up and apply the layer cleanup")
    args = parser.parse_args()

    methods = sorted(DERIVED_LAYERS[args.layer])
    generator = GENERATORS.get(args.layer)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    conn = configure_connection(connect(args.db, timeout=30))
    counts_before = {
        method: conn.execute(
            "SELECT COUNT(*) FROM edge WHERE source_method=?", (method,)
        ).fetchone()[0]
        for method in methods
    }
    total_before = sum(counts_before.values())
    print(f"layer={args.layer} methods={methods} edges_before={total_before}")
    try:
        if args.apply and generator is None:
            print("FAILED: no generator is registered; refusing to delete the existing layer")
            return 2
        if not args.apply:
            print("mode=preview; pass --apply to make changes")
            return 0

        args.backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = args.backup_dir / f"{args.db.stem}-layer-{args.layer}-pre-{now}{args.db.suffix}"
        backup_conn = sqlite3.connect(backup_path)
        try:
            conn.backup(backup_conn, pages=2048, sleep=0.05)
        finally:
            backup_conn.close()
        print(f"backup={backup_path}")

        conn.execute("BEGIN IMMEDIATE")
        for method in methods:
            cur = conn.execute("DELETE FROM edge WHERE source_method=?", (method,))
            print(f"removed {cur.rowcount} edges with source_method={method}")

        generator(conn)

        report = integrity_report(conn)
        if not report["ok"]:
            print(json.dumps(report["metrics"], indent=2))
            raise RuntimeError("integrity check failed before commit; cleanup was rolled back")
        print("integrity=ok")

        counts_after = {
            method: conn.execute(
                "SELECT COUNT(*) FROM edge WHERE source_method=?", (method,)
            ).fetchone()[0]
            for method in methods
        }
        print(f"edges_after={sum(counts_after.values())}")
        conn.commit()
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"FAILED: {exc}")
        print("database transaction rolled back; backup retained")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
