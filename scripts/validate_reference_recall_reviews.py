#!/usr/bin/env python3
"""Validate read-only reviewer JSONL against a reference-recall pilot.

Validation is intentionally strict.  It checks source identity, text hashes,
absolute offsets and exact quoted substrings, but it does not resolve targets or
write occurrences/edges.  Invalid or ambiguous findings remain in the report
for a later adjudication step.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from reference_recall_batches import load_pilot, request_id


DECISIONS = {"REFERENCE", "NOT_REFERENCE", "AMBIGUOUS"}
TARGET_KINDS = {
    "rule",
    "part",
    "chapter",
    "article",
    "paragraph",
    "annex",
    "table",
    "template",
    "form",
    "definition",
    "guidance",
    "statute",
    "regulation",
    "directive",
    "external",
    "unknown",
}


def parse_content(record: dict[str, Any]) -> dict[str, Any]:
    """Extract the reviewer object from direct or OpenAI Batch JSONL."""
    if isinstance(record.get("findings"), list):
        return record
    response = record.get("response") or {}
    body = response.get("body") or response
    choices = body.get("choices") if isinstance(body, dict) else None
    if choices:
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, str):
            return json.loads(content)
    raise ValueError("record has no direct findings or chat-completion JSON content")


def all_occurrences(value: str, quote: str) -> list[int]:
    if not quote:
        return []
    starts: list[int] = []
    cursor = 0
    while True:
        start = value.find(quote, cursor)
        if start < 0:
            return starts
        starts.append(start)
        cursor = start + 1


def validate_finding(finding: dict[str, Any], row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = finding.get("decision")
    if decision not in DECISIONS:
        errors.append("invalid_decision")
    target_kind = finding.get("target_kind")
    if not isinstance(target_kind, str) or target_kind not in TARGET_KINDS:
        errors.append("invalid_target_kind")
    confidence = finding.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append("invalid_confidence")
    quote = finding.get("quoted_text")
    if not isinstance(quote, str) or not quote:
        errors.append("missing_quoted_text")
        return errors
    text = row.get("text") or ""
    relative_starts = all_occurrences(text, quote)
    if not relative_starts:
        errors.append("quoted_text_not_exact_substring")
        return errors
    absolute_starts = [int(row.get("chunk_start") or 0) + start for start in relative_starts]
    span_start, span_end = finding.get("span_start"), finding.get("span_end")
    if not isinstance(span_start, int) or not isinstance(span_end, int):
        errors.append("missing_integer_span")
    elif span_end <= span_start:
        errors.append("non_positive_span")
    elif span_start not in absolute_starts or span_end != span_start + len(quote):
        errors.append("span_does_not_match_quote")
    elif span_start < int(row.get("chunk_start") or 0) or span_end > int(row.get("chunk_end") or 0):
        errors.append("span_outside_chunk")
    return errors


def validate(pilot: Path, responses: Path, *, allow_partial: bool = False) -> dict[str, Any]:
    pilot_rows = load_pilot(pilot)
    expected: dict[str, dict[str, Any]] = {}
    for row in pilot_rows:
        custom_id = request_id(
            str(row.get("source_node_id") or ""),
            str(row.get("source_text_hash") or ""),
            int(row.get("chunk_start") or 0),
            int(row.get("chunk_end") or 0),
        )
        expected[custom_id] = row

    report: dict[str, Any] = {
        "pilot": str(pilot),
        "responses": str(responses),
        "expected_requests": len(expected),
        "received_requests": 0,
        "missing_custom_ids": [],
        "unexpected_custom_ids": [],
        "invalid_records": [],
        "findings": 0,
        "valid_findings": 0,
        "decisions": {},
        "allow_partial": allow_partial,
    }
    seen: set[str] = set()
    decision_counts: Counter[str] = Counter()
    with responses.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                custom_id = str(record.get("custom_id") or "")
                if custom_id not in expected:
                    report["unexpected_custom_ids"].append(custom_id)
                    continue
                seen.add(custom_id)
                parsed = parse_content(record)
                row = expected[custom_id]
                if parsed.get("source_node_id") not in (None, row.get("source_node_id")):
                    report["invalid_records"].append({"line": line_number, "custom_id": custom_id, "error": "source_node_id_mismatch"})
                if parsed.get("source_text_hash") not in (None, row.get("source_text_hash")):
                    report["invalid_records"].append({"line": line_number, "custom_id": custom_id, "error": "source_text_hash_mismatch"})
                findings = parsed.get("findings")
                if not isinstance(findings, list):
                    report["invalid_records"].append({"line": line_number, "custom_id": custom_id, "error": "findings_not_array"})
                    continue
                for finding in findings:
                    report["findings"] += 1
                    if not isinstance(finding, dict):
                        report["invalid_records"].append({"line": line_number, "custom_id": custom_id, "error": "finding_not_object"})
                        continue
                    decision = str(finding.get("decision") or "")
                    decision_counts[decision] += 1
                    errors = validate_finding(finding, row)
                    if errors:
                        report["invalid_records"].append({"line": line_number, "custom_id": custom_id, "errors": errors, "finding": finding})
                    else:
                        report["valid_findings"] += 1
            except Exception as exc:  # malformed reviewer lines are reportable data
                report["invalid_records"].append({"line": line_number, "error": str(exc)})
    report["received_requests"] = len(seen)
    report["missing_custom_ids"] = sorted(set(expected) - seen)
    report["decisions"] = dict(decision_counts)
    report["valid"] = (allow_partial or not report["missing_custom_ids"]) and not report["unexpected_custom_ids"] and not report["invalid_records"]
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="accept a subset of pilot requests as long as every received record and finding is valid",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = validate(args.pilot, args.responses, allow_partial=args.allow_partial)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
