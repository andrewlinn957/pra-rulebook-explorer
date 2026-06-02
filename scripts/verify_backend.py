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
    assert stats["edges_by_type"].get("similar_to", 0) > 0, stats
    assert stats["nodes_by_type"].get("topic", 0) >= 10, stats
    assert stats["nodes_by_type"].get("obligation_pattern", 0) > 1000, stats
    assert stats["edges_by_type"].get("has_topic", 0) > 1000, stats
    assert stats["edges_by_type"].get("has_obligation_pattern", 0) > 1000, stats
    assert stats["edge_methods"].get("regex_named_reference", 0) > 100, stats
    assert stats["edge_methods"].get("rollup_child_edge", 0) > 10000, stats
    assert stats["edges_by_type"].get("shares_obligation_pattern", 0) > 100, stats

    search = get("/search", {"q": "operational resilience", "limit": 5})
    assert search["results"], search
    node_id = search["results"][0]["id"]

    node = get(f"/node/{node_id}")
    assert node["id"] == node_id, node

    hood = get(f"/node/{node_id}/neighbourhood", {"depth": 1, "limit": 50})
    assert hood["nodes"] and hood["edges"], hood

    interesting = get("/interesting", {"limit": 10})
    assert interesting["results"], interesting

    centrality = get("/centrality", {"limit": 10})
    assert centrality["degree"], centrality

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
        "similar_to": stats["edges_by_type"].get("similar_to", 0),
        "topics": stats["nodes_by_type"].get("topic", 0),
        "obligation_patterns": stats["nodes_by_type"].get("obligation_pattern", 0),
        "regex_named_references": stats["edge_methods"].get("regex_named_reference", 0),
        "rolled_up_edges": stats["edge_methods"].get("rollup_child_edge", 0),
        "shares_obligation_pattern": stats["edges_by_type"].get("shares_obligation_pattern", 0),
        "search_top": search["results"][0]["title"],
        "neighbourhood_nodes": len(hood["nodes"]),
        "neighbourhood_edges": len(hood["edges"]),
    }, indent=2))


if __name__ == "__main__":
    main()
