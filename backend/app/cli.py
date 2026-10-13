from __future__ import annotations

import argparse
from pathlib import Path

from .db import DEFAULT_DB, connect, ensure_indexes
from .graph import stats
from .analysis_cache import precompute_all
from .integrity import integrity_report
from .migrations import LATEST_SCHEMA_VERSION, apply_migrations, schema_version


def cmd_build_indexes(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    apply_migrations(conn)
    ensure_indexes(conn)
    print("rebuilt FTS/search indexes")
    print(stats(conn))


def cmd_migrate(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    applied = apply_migrations(conn)
    print({"applied": applied, "schema_version": schema_version(conn), "latest": LATEST_SCHEMA_VERSION})


def cmd_check(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    apply_migrations(conn)
    report = integrity_report(conn)
    print(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_stabilize(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    applied = apply_migrations(conn)
    ensure_indexes(conn)
    analysis = precompute_all(conn)
    report = integrity_report(conn)
    print({"applied": applied, "schema_version": schema_version(conn), "analysis_cache": analysis, **report})
    if not report["ok"]:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Graph backend maintenance commands")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    idx = sub.add_parser("build-indexes")
    idx.set_defaults(func=cmd_build_indexes)
    migrate = sub.add_parser("migrate", help="Apply ordered database migrations")
    migrate.set_defaults(func=cmd_migrate)
    check = sub.add_parser("check-integrity", help="Fail if database invariants or projections have drifted")
    check.set_defaults(func=cmd_check)
    stabilize = sub.add_parser("stabilize", help="Migrate, rebuild search projections, and verify integrity")
    stabilize.set_defaults(func=cmd_stabilize)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
