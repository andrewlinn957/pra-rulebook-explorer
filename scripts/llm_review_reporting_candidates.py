#!/usr/bin/env python3
"""Cheap LLM-assisted review for ambiguous reporting legal-basis candidates.

This writes review artefacts only. It does not mutate graph tables.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INFILE = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages/audit_exports/domain_reviews/llm_legal_basis_review_candidates.jsonl"
OUTROOT = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages/audit_exports/domain_reviews"
DEFAULT_MODEL = os.getenv("REPORTING_REVIEW_MODEL", "gpt-4o-mini")

SYSTEM = """You are reviewing UK PRA/BoE regulatory reporting graph candidates.
Your task is narrow: classify whether a supplied provision excerpt is a plausible legal basis or downstream reporting-effect provision for the supplied reporting package.
Use only the supplied package title, domain, provision label, and excerpt. Do not infer applicability beyond the text. Do not treat generic mentions of reporting as specific legal basis unless they clearly establish the reporting package/return or its reporting obligation.
Return strict JSON only with key reviews, an array. Each review must include: id, decision, confidence, rationale, cited_excerpt.
Decision must be one of: keep, reject, uncertain.
Use keep only where the text explicitly supports a reporting obligation/effect for the package/domain. Use reject where the text is clearly irrelevant or too generic. Use uncertain where potentially relevant but the excerpt is insufficient.
"""


def slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "other"


def call_openai(batch: list[dict[str, Any]], model: str, max_retries: int = 4) -> dict[str, Any]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"candidates": batch}, ensure_ascii=False)},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            parsed["_usage"] = body.get("usage", {})
            parsed["_model"] = body.get("model", model)
            return parsed
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"OpenAI HTTP {e.code}: {msg}") from e
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=12)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    rows = [json.loads(line) for line in INFILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r.get("domain") == args.domain]
    rows.sort(key=lambda r: (r.get("data_item_code", ""), r.get("provision_node_id", "")))
    if args.limit:
        rows = rows[: args.limit]
    outdir = OUTROOT / slug(args.domain)
    outdir.mkdir(parents=True, exist_ok=True)

    candidates = []
    for i, r in enumerate(rows, 1):
        candidates.append({
            "id": f"{r['data_item_code']}::{r['provision_node_id']}",
            "data_item_code": r.get("data_item_code"),
            "domain": r.get("domain"),
            "package_title": r.get("package_title"),
            "provision_node_id": r.get("provision_node_id"),
            "provision_label": r.get("provision_label"),
            "text_excerpt": (r.get("text_excerpt") or "")[:1800],
        })

    raw_out = outdir / "llm_legal_basis_review_raw.jsonl"
    review_out = outdir / "llm_legal_basis_review.csv"
    all_reviews: list[dict[str, Any]] = []
    total_tokens = 0
    for start in range(0, len(candidates), args.batch_size):
        batch = candidates[start : start + args.batch_size]
        result = call_openai(batch, args.model)
        usage = result.get("_usage", {})
        total_tokens += usage.get("total_tokens", 0) or 0
        with raw_out.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"batch_start": start, "model": result.get("_model"), "usage": usage, "result": result}, ensure_ascii=False) + "\n")
        by_id = {c["id"]: c for c in batch}
        for rev in result.get("reviews", []):
            cid = rev.get("id")
            base = by_id.get(cid, {})
            all_reviews.append({**base, **rev, "model": result.get("_model"), "total_tokens_batch": usage.get("total_tokens", "")})
        time.sleep(0.25)

    fieldnames = ["id", "data_item_code", "domain", "package_title", "provision_node_id", "provision_label", "decision", "confidence", "rationale", "cited_excerpt", "model", "total_tokens_batch"]
    with review_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(all_reviews)

    summary = {
        "domain": args.domain,
        "model": args.model,
        "input_candidates": len(candidates),
        "reviews": len(all_reviews),
        "total_tokens_reported": total_tokens,
        "outputs": {"csv": str(review_out), "raw_jsonl": str(raw_out)},
    }
    (outdir / "llm_legal_basis_review_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
