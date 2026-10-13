#!/usr/bin/env python3
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

BASE = "http://127.0.0.1:8100"


def get(path: str, params: dict | None = None) -> dict:
    url = BASE + path
    if params:
        url += "?" + urlencode(params, doseq=True)
    with urlopen(url, timeout=60) as resp:
        return json.load(resp)


def main() -> None:
    health = get("/health")
    assert health["ok"] and health["exists"], health

    stats = get("/stats")
    assert stats["nodes"] > 17000, stats
    assert stats["edges"] > 100000, stats
    assert stats["missing_edge_targets"] == 0, stats
    assert stats["edge_methods"].get("regex_named_reference", 0) > 100, stats
    assert stats["edge_methods"].get("rollup_child_edge", 0) > 10000, stats

    search = get("/search", {"q": "operational resilience", "limit": 5})
    assert search["results"], search
    node_id = search["results"][0]["id"]

    node = get(f"/node/{node_id}")
    assert node["id"] == node_id, node

    hood = get(f"/node/{node_id}/neighbourhood", {"depth": 1, "limit": 50})
    assert hood["nodes"] and hood["edges"], hood
    retired_types = {
        "has_obligation_pattern", "has_structured_obligation",
        "shares_defined_term", "shares_defined_term_with_guidance",
        "shares_obligation_pattern",
    }
    assert not any(edge["edge_type"] in retired_types for edge in hood["edges"]), hood

    centrality = get("/centrality", {"limit": 10})
    assert centrality["degree"], centrality

    # Regression checks for public prototype endpoints and parameter clamping.
    # SQLite treats LIMIT -1 as unlimited, so API routes must not pass negative
    # values through to query helpers.
    negative_limit = get("/nodes", {"limit": -1})
    assert 1 <= len(negative_limit["results"]) <= 1000, len(negative_limit["results"])

    bet = get("/analysis/betweenness", {"limit": 5, "k": 40, "max_nodes": 600})
    assert bet["results"], bet
    comps = get("/analysis/components", {"limit": 3})
    assert comps["component_count"] > 0, comps
    comms = get("/analysis/communities", {"limit": 3, "max_nodes": 1200})
    assert comms["community_count"] > 0, comms

    # Smoke related-node analysis. The shortest-path endpoint intentionally samples
    # the graph for speed, so a direct neighbourhood node may occasionally sit
    # outside that sample; treat that as non-fatal for this corpus-level smoke test.
    other = next((n["id"] for n in hood["nodes"] if n["id"] != node_id), None)
    if other:
        try:
            path = get("/path", {"from_id": node_id, "to_id": other})
            assert path["length"] >= 1, path
        except HTTPError as exc:
            if exc.code != 404:
                raise
        common = get("/analysis/common-neighbours", {"from_id": node_id, "to_id": other, "limit": 5})
        assert "count" in common, common

    print(json.dumps({
        "ok": True,
        "nodes": stats["nodes"],
        "edges": stats["edges"],
        "regex_named_references": stats["edge_methods"].get("regex_named_reference", 0),
        "rolled_up_edges": stats["edge_methods"].get("rollup_child_edge", 0),
        "retired_analysis_edges_stored": sum(stats["edges_by_type"].get(edge_type, 0) for edge_type in retired_types),
        "search_top": search["results"][0]["title"],
        "neighbourhood_nodes": len(hood["nodes"]),
        "neighbourhood_edges": len(hood["edges"]),
    }, indent=2))


if __name__ == "__main__":
    main()
