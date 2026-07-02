#!/usr/bin/env python3
"""Apply direct legal sanity review decisions to graph_edge review status.

This is intentionally conservative:
- accept_specific_reporting_basis -> accepted_candidate, confidence raised to 0.86
- reject_do_not_promote -> rejected_candidate, confidence capped at 0.20
- accept_specific_schedule_basis stays candidate, because the current ontology has no
  separate due-date/frequency edge type and LEGAL_BASIS would be too broad.
- keep_* decisions stay as they are.

The review artefact remains the source of detailed rationale.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
REVIEW = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages/audit_exports/domain_reviews/direct_legal_sanity_review.csv"


def main() -> None:
    rows = list(csv.DictReader(REVIEW.open(encoding="utf-8")))
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    promoted = rejected = schedule_kept = missing = 0
    for r in rows:
        code = r["data_item_code"]
        src = f"reporting_obligation:{code}"
        tgt = r["provision_node_id"]
        decision = r["direct_sanity_decision"]
        edges = con.execute(
            "SELECT edge_id,properties_json,confidence,review_status FROM graph_edge WHERE source_node_id=? AND target_node_id=? AND edge_type='LEGAL_BASIS'",
            (src, tgt),
        ).fetchall()
        if not edges:
            missing += 1
            continue
        for edge in edges:
            props = json.loads(edge["properties_json"] or "{}")
            props["direct_legal_sanity_review"] = {
                "decision": decision,
                "rationale": r["direct_sanity_rationale"],
                "source": str(REVIEW.relative_to(ROOT)),
            }
            if decision == "accept_specific_reporting_basis":
                con.execute(
                    "UPDATE graph_edge SET properties_json=?, confidence=max(coalesce(confidence,0),0.86), extraction_method=?, review_status=? WHERE edge_id=?",
                    (json.dumps(props, sort_keys=True), "manual_legal_sanity_review", "accepted_candidate", edge["edge_id"]),
                )
                promoted += 1
            elif decision == "reject_do_not_promote":
                con.execute(
                    "UPDATE graph_edge SET properties_json=?, confidence=min(coalesce(confidence,0.2),0.20), extraction_method=?, review_status=? WHERE edge_id=?",
                    (json.dumps(props, sort_keys=True), "manual_legal_sanity_review", "rejected_candidate", edge["edge_id"]),
                )
                rejected += 1
            elif decision == "accept_specific_schedule_basis":
                # Preserve review rationale but do not promote as LEGAL_BASIS.
                con.execute(
                    "UPDATE graph_edge SET properties_json=?, extraction_method=?, review_status=? WHERE edge_id=?",
                    (json.dumps(props, sort_keys=True), "manual_legal_sanity_review_schedule", "candidate", edge["edge_id"]),
                )
                schedule_kept += 1
            elif decision in {"keep_context_no_promotion", "keep_candidate_manual_review"}:
                con.execute(
                    "UPDATE graph_edge SET properties_json=?, extraction_method=?, review_status=? WHERE edge_id=?",
                    (json.dumps(props, sort_keys=True), "manual_legal_sanity_review_context", "candidate", edge["edge_id"]),
                )
    con.commit()
    print(json.dumps({"promoted": promoted, "rejected": rejected, "schedule_kept_candidate": schedule_kept, "missing_edges": missing}, indent=2))


if __name__ == "__main__":
    main()
