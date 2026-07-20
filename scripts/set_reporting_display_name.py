#!/usr/bin/env python3
"""Set or remove a durable human-readable reporting ontology name."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.db import connect
from backend.app.migrations import apply_migrations
from scripts.project_reporting_ontology import apply_display_name_overrides, project_graph


DB_PATH = ROOT / "backend/data/rulebook.sqlite3"
ENTITY_TYPES = ("regime", "collection", "requirement", "edition", "resource", "component", "taxonomy_release")
ENTITY_TABLES = {
    "regime": ("reporting_regime", "regime_id"),
    "collection": ("reporting_collection", "collection_id"),
    "requirement": ("reporting_requirement", "requirement_id"),
    "edition": ("reporting_requirement_edition", "edition_id"),
    "resource": ("reporting_resource", "resource_id"),
    "component": ("reporting_resource_component", "component_id"),
    "taxonomy_release": ("reporting_taxonomy_release", "release_id"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("entity_type", choices=ENTITY_TYPES)
    parser.add_argument("entity_id")
    parser.add_argument("display_name", nargs="?", help="New display name; omit with --clear")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    if not args.clear and not str(args.display_name or "").strip():
        parser.error("display_name is required unless --clear is used")

    conn = connect(args.db)
    try:
        apply_migrations(conn)
        if args.clear:
            changed = conn.execute(
                "DELETE FROM reporting_display_name_override WHERE entity_type=? AND entity_id=?",
                (args.entity_type, args.entity_id),
            ).rowcount
            table, key = ENTITY_TABLES[args.entity_type]
            conn.execute(f"UPDATE {table} SET display_name=NULL WHERE {key}=?", (args.entity_id,))
            graph = project_graph(conn)
            result = {"cleared": bool(changed), **graph}
        else:
            conn.execute(
                """INSERT INTO reporting_display_name_override(entity_type,entity_id,display_name,updated_at)
                   VALUES (?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(entity_type,entity_id) DO UPDATE SET
                     display_name=excluded.display_name,updated_at=CURRENT_TIMESTAMP""",
                (args.entity_type, args.entity_id, args.display_name.strip()),
            )
            applied = apply_display_name_overrides(conn)
            graph = project_graph(conn)
            result = {"saved": True, "applied_overrides": applied, **graph}
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
