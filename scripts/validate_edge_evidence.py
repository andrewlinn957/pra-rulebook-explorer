#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"

HARD_EVIDENCE_STATUSES = {"direct_text", "html_structure", "document_metadata"}


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    checks = {
        "missing_source_method": "coalesce(source_method,'')=''",
        "missing_confidence": "confidence is null",
        "missing_source_url": "coalesce(source_url,'')=''",
        "missing_evidence_text": "coalesce(evidence_text,'')=''",
        "missing_extraction_run_id": "json_extract(metadata_json,'$.extraction_run_id') is null",
        "missing_evidence_status": "json_extract(metadata_json,'$.evidence_status') is null",
    }
    failures: dict[str, int] = {}
    for name, predicate in checks.items():
        count = conn.execute(f"SELECT COUNT(*) FROM edge WHERE {predicate}").fetchone()[0]
        if count:
            failures[name] = count

    hard_missing = conn.execute(
        """
        SELECT COUNT(*)
        FROM edge
        WHERE json_extract(metadata_json,'$.evidence_status') IN ('direct_text','html_structure','document_metadata')
          AND (coalesce(evidence_text,'')='' OR coalesce(source_url,'')='')
        """
    ).fetchone()[0]
    if hard_missing:
        failures["hard_edges_missing_evidence_or_url"] = hard_missing

    status_counts = {
        str(status) if status is not None else "<missing>": count
        for status, count in conn.execute("SELECT json_extract(metadata_json,'$.evidence_status') status, COUNT(*) FROM edge GROUP BY status ORDER BY status")
    }
    result = {"status_counts": status_counts, "failures": failures}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
