#!/usr/bin/env python3
"""Rebuild one derived edge layer: remove previous output, regenerate, validate.

Usage:
    .venv/bin/python scripts/rebuild_derived_layer.py <layer_name> [--dry-run]

Layers are declared in backend/app/taxonomy.py DERIVED_LAYERS. A rebuild:
 1. backs up the SQLite database;
 2. deletes every edge whose source_method belongs to the layer;
 3. runs the layer-specific generator (if registered below);
 4. re-runs integrity checks before committing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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

# Generators are optional; a layer may only need cleanup (e.g. after an
# extractor is retired). Register new generators here as they are written.
GENERATORS: dict[str, list[str]] = {
    # "regex_reference": ["scripts/backfill_legal_references.py"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=sorted(DERIVED_LAYERS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    methods = sorted(DERIVED_LAYERS[args.layer])
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    conn = configure_connection(connect(DB, timeout=30))
    counts_before = {
        method: conn.execute(
            "SELECT COUNT(*) FROM edge WHERE source_method=?", (method,)
        ).fetchone()[0]
        for method in methods
    }
    total_before = sum(counts_before.values())
    print(f"layer={args.layer} methods={methods} edges_before={total_before}")
    if args.dry_run:
        return 0

    BACKUP_DIR.mkdir(exist_ok=True)
    backup_path = BACKUP_DIR / f"rulebook-layer-{args.layer}-pre-{now}.sqlite3"
    shutil.copy2(DB, backup_path)
    print(f"backup={backup_path}")

    try:
        for method in methods:
            cur = conn.execute("DELETE FROM edge WHERE source_method=?", (method,))
            print(f"removed {cur.rowcount} edges with source_method={method}")

        generator = GENERATORS.get(args.layer)
        if generator:
            result = subprocess.run(
                [str(ROOT / ".venv/bin/python"), *[str(ROOT / g) for g in generator]],
                cwd=ROOT,
            )
            if result.returncode != 0:
                raise RuntimeError(f"generator failed with exit code {result.returncode}")

        report = integrity_report(conn)
        conn.commit()
        if not report["ok"]:
            print(json.dumps(report["metrics"], indent=2))
            raise RuntimeError("integrity check failed after rebuild; restore from backup")
        print("integrity=ok")

        counts_after = {
            method: conn.execute(
                "SELECT COUNT(*) FROM edge WHERE source_method=?", (method,)
            ).fetchone()[0]
            for method in methods
        }
        print(f"edges_after={sum(counts_after.values())}")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"FAILED: {exc}")
        print(f"restore with: cp {backup_path} {DB}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())