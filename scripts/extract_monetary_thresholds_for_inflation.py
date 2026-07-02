#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT_DIR = ROOT / "outputs/quantitative-thresholds"
CANDIDATES_OUT = OUT_DIR / "monetary-threshold-candidates.csv"
REVIEWED_OUT = OUT_DIR / "monetary-thresholds-inflation-review.csv"
API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.4-mini"

CURRENCY_RE = re.compile(
    r"(?:£\s?\d[\d,]*(?:\.\d+)?\s?(?:m|mn|million|bn|billion|k|thousand)?|"
    r"\bGBP\s?\d[\d,]*(?:\.\d+)?\s?(?:m|mn|million|bn|billion|k|thousand)?|"
    r"\bEUR\s?\d[\d,]*(?:\.\d+)?\s?(?:m|mn|million|bn|billion|k|thousand)?|"
    r"€\s?\d[\d,]*(?:\.\d+)?\s?(?:m|mn|million|bn|billion|k|thousand)?|"
    r"\b\d[\d,]*(?:\.\d+)?\s?(?:pounds?|sterling|euro|euros)\b)",
    re.I,
)
THRESHOLD_CUE_RE = re.compile(
    r"\b(threshold|limit|minimum|maximum|floor|cap|exceed(?:s|ed|ing)?|greater than|more than|"
    r"less than|below|above|under|over|not exceeding|no more than|no less than|at least|at most|"
    r"equal to or greater than|equal to or less than|required|requirement|only .* with|applies? where|"
    r"eligible|qualif(?:y|ies|ying)|small company|size band|turnover|assets?|income|revenue|fees?)\b|[<>≤≥]",
    re.I,
)
HARD_THRESHOLD_CUE_RE = re.compile(
    r"\b(threshold|limit|minimum|maximum|floor|cap|exceed(?:s|ed|ing)?|greater than|more than|"
    r"less than|below|above|under|over|not exceeding|no more than|no less than|at least|at most|"
    r"equal to or greater than|equal to or less than|only .* with|applies? where|eligible|qualif(?:y|ies|ying))\b|[<>≤≥]",
    re.I,
)
EXCLUDE_CUE_RE = re.compile(
    r"\b(for example|example|illustration|carrying value|fair value|balance sheet|account balance|"
    r"reported amount|amount reported|book value|transactional account|profit or loss|dividend|paid|received|"
    r"fine imposed|penalty imposed|costs? of|fee paid|invoice|remuneration paid)\b",
    re.I,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9£€])|\n+")

NODE_QUERY = """
SELECT id, node_type, title, text, url, metadata_json
FROM node
WHERE node_type IN ('rule','guidance_paragraph','guidance_document')
  AND COALESCE(text,'') <> ''
"""

SYSTEM_PROMPT = """You are reviewing UK PRA Rulebook and PRA guidance text for monetary thresholds that might need inflation indexing.
Classify only monetary amounts. A threshold is a monetary amount that creates a boundary, trigger, eligibility test, reporting/scope condition, minimum/maximum requirement, fee band, compensation limit, capital/resource floor, or similar regulatory cut-off.
Exclude ordinary mentions of money: balances, examples, accounting values, worked examples, amounts payable/paid, references to transactions, historical facts, fines/penalties already imposed, or narrative context without a regulatory cut-off.
Return strict JSON only."""

USER_TEMPLATE = """Review these candidate excerpts. Return a JSON object with key "items", an array in the same order. Each item must have:
- id: copied from input
- is_threshold: boolean
- inflation_index_candidate: boolean, true only where this is a monetary threshold/cut-off plausibly suitable for an inflation-indexing index
- threshold_amounts: array of monetary amount strings from the text
- threshold_type: one of scope_trigger, reporting_trigger, capital_or_resource_minimum, compensation_or_protection_limit, fee_or_levy_band, permission_or_exemption_condition, other_threshold, not_a_threshold
- confidence: high, medium, or low
- rationale: concise reason

Candidates:
{items_json}
"""


def is_monetary_candidate(text: str) -> bool:
    return bool(CURRENCY_RE.search(text or ""))


def classify_candidate_without_llm(text: str) -> tuple[str, str]:
    t = re.sub(r"\s+", " ", text or "").strip()
    if not is_monetary_candidate(t):
        return "exclude", "No monetary amount found."
    if EXCLUDE_CUE_RE.search(t) and not HARD_THRESHOLD_CUE_RE.search(t):
        return "exclude", "Looks like an accounting/example/payment mention rather than a threshold."
    if re.search(r"\b(for example|example|illustration)\b", t, re.I):
        return "exclude", "Worked example rather than the operative threshold."
    if THRESHOLD_CUE_RE.search(t):
        return "review", "Monetary amount appears with threshold/trigger language."
    return "review", "Monetary amount needs LLM review to distinguish threshold from ordinary mention."


def parse_llm_decision(raw: str | dict[str, Any]) -> dict[str, Any]:
    try:
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception as exc:
        return {
            "is_threshold": False,
            "inflation_index_candidate": False,
            "threshold_amounts": [],
            "threshold_type": "not_a_threshold",
            "confidence": "low",
            "rationale": f"Could not parse LLM response: {type(exc).__name__}",
        }
    return {
        "is_threshold": bool(data.get("is_threshold")),
        "inflation_index_candidate": bool(data.get("inflation_index_candidate")),
        "threshold_amounts": data.get("threshold_amounts") if isinstance(data.get("threshold_amounts"), list) else [],
        "threshold_type": data.get("threshold_type") or ("other_threshold" if data.get("is_threshold") else "not_a_threshold"),
        "confidence": data.get("confidence") or "low",
        "rationale": data.get("rationale") or "",
    }


def load_metadata(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def estate_for(node_type: str, title: str, metadata: dict[str, Any]) -> str:
    if node_type == "rule":
        return "rule"
    t = (title or "").lower()
    dt = (metadata.get("document_type") or "").lower()
    if dt == "statement_of_policy" or re.search(r"\bsop\d*[/ -]", t):
        return "sop"
    if dt == "supervisory_statement" or re.search(r"\b(?:ss|lss)\d+[/ -]", t):
        return "ss"
    return "guidance"


def iter_snippets(text: str) -> Iterable[str]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(clean) if p.strip()]
    if len(parts) == 1 and len(clean) > 900:
        parts = [clean[i : i + 700] for i in range(0, len(clean), 600)]
    for part in parts:
        if is_monetary_candidate(part):
            yield part[:1200]


def collect_candidates(db: Path = DB) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in conn.execute(NODE_QUERY):
        md = load_metadata(r["metadata_json"])
        estate = estate_for(r["node_type"], r["title"] or "", md)
        document_or_part = md.get("document_title") or md.get("part_title") or ""
        paragraph_or_rule = md.get("paragraph_number") or md.get("rule_number") or md.get("display_number") or ""
        for snippet in iter_snippets(r["text"] or ""):
            decision, reason = classify_candidate_without_llm(snippet)
            if decision == "exclude":
                continue
            key = (r["id"], snippet.lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "candidate_id": f"cand-{len(rows)+1:05d}",
                    "estate": estate,
                    "source_id": r["id"],
                    "node_type": r["node_type"],
                    "title": r["title"] or "",
                    "document_or_part": document_or_part,
                    "paragraph_or_rule": paragraph_or_rule,
                    "url": r["url"] or "",
                    "matched_amounts": "; ".join(dict.fromkeys(m.group(0).strip() for m in CURRENCY_RE.finditer(snippet))),
                    "rule_based_reason": reason,
                    "snippet": snippet,
                }
            )
    rows.sort(key=lambda x: (x["estate"], x["document_or_part"], x["title"], x["paragraph_or_rule"], x["candidate_id"]))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)


def openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return key


def review_batch(items: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    compact = [
        {
            "id": item["candidate_id"],
            "estate": item["estate"],
            "title": item["title"],
            "document_or_part": item["document_or_part"],
            "paragraph_or_rule": item["paragraph_or_rule"],
            "amounts": item["matched_amounts"],
            "text": item["snippet"],
        }
        for item in items
    ]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(items_json=json.dumps(compact, ensure_ascii=False))},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        f"{API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {openai_key()}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:1000]}")
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    by_id = {entry.get("id"): parse_llm_decision(entry) for entry in data.get("items", []) if isinstance(entry, dict)}
    return [by_id.get(item["candidate_id"], parse_llm_decision("{}")) for item in items]


def run_review(candidates: list[dict[str, Any]], model: str, batch_size: int, limit: int | None, sleep_seconds: float) -> list[dict[str, Any]]:
    selected = candidates[:limit] if limit else candidates
    reviewed: list[dict[str, Any]] = []
    for i in range(0, len(selected), batch_size):
        chunk = selected[i : i + batch_size]
        decisions = review_batch(chunk, model)
        for item, decision in zip(chunk, decisions):
            out = dict(item)
            out.update(
                {
                    "is_threshold": decision["is_threshold"],
                    "inflation_index_candidate": decision["inflation_index_candidate"],
                    "threshold_amounts": "; ".join(decision["threshold_amounts"]),
                    "threshold_type": decision["threshold_type"],
                    "confidence": decision["confidence"],
                    "llm_rationale": decision["rationale"],
                    "model": model,
                }
            )
            reviewed.append(out)
        print(json.dumps({"reviewed": len(reviewed), "total": len(selected)}), flush=True)
        if sleep_seconds and i + batch_size < len(selected):
            time.sleep(sleep_seconds)
    return reviewed


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract monetary regulatory thresholds for possible inflation indexing review.")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--candidates-out", type=Path, default=CANDIDATES_OUT)
    ap.add_argument("--reviewed-out", type=Path, default=REVIEWED_OUT)
    ap.add_argument("--review", action="store_true", help="Call the OpenAI API to classify candidates.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="Optional candidate limit for testing/sampling.")
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    candidates = collect_candidates(args.db)
    candidate_fields = [
        "candidate_id", "estate", "source_id", "node_type", "title", "document_or_part", "paragraph_or_rule",
        "url", "matched_amounts", "rule_based_reason", "snippet",
    ]
    write_csv(args.candidates_out, candidates, candidate_fields)
    print(json.dumps({"candidates": len(candidates), "candidates_out": str(args.candidates_out)}, indent=2))

    if args.review:
        reviewed = run_review(candidates, args.model, args.batch_size, args.limit or None, args.sleep)
        reviewed_fields = candidate_fields + [
            "is_threshold", "inflation_index_candidate", "threshold_amounts", "threshold_type", "confidence", "llm_rationale", "model",
        ]
        write_csv(args.reviewed_out, reviewed, reviewed_fields)
        print(json.dumps({"reviewed": len(reviewed), "reviewed_out": str(args.reviewed_out)}, indent=2))


if __name__ == "__main__":
    main()
