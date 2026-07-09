#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.db import connect
from backend.app.reporting import reporting_overview_graph


def assert_all_nodes_have_urls(selected_return: str | None, *, include_datapoints: bool = False) -> None:
    conn = connect()
    graph = reporting_overview_graph(
        conn,
        selected_return=selected_return,
        q=None if selected_return else "",
        limit=30,
        child_limit=1200,
        include_datapoints=include_datapoints,
    )
    missing = [node for node in graph["nodes"] if not str(node.get("url") or "").startswith(("http://", "https://"))]
    if missing:
        sample = "; ".join(f"{node.get('id')} ({node.get('node_type')})" for node in missing[:20])
        raise AssertionError(f"{len(missing)} reporting graph nodes are missing source URLs: {sample}")


def main() -> None:
    assert_all_nodes_have_urls(None)
    assert_all_nodes_have_urls("PRA110", include_datapoints=True)
    print("reporting graph URL invariant passed")


if __name__ == "__main__":
    main()
