#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "backend/data/raw/guidance-pdfs/manifest.json"
DEFAULT_OUT = ROOT / "logs/guidance-pdf-audit.csv"
DOC_CODE_RE = re.compile(r"\b((?:L?SS|SoP)\s*\d+\s*/\s*\d+|PS\s*\d+\s*/\s*\d+|CP\s*\d+\s*/\s*\d+)\b", re.I)


def canonical_code(value: str) -> str:
    m = DOC_CODE_RE.search(value or "")
    return re.sub(r"\s+", "", m.group(1)).upper() if m else ""


def code_family(code: str) -> str:
    m = re.match(r"[A-Z]+", code or "")
    return m.group(0) if m else ""


def pdf_sample(path: Path, max_pages: int = 4, max_chars: int = 8000) -> str:
    reader = PdfReader(str(path))
    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        chunks.append(page.extract_text() or "")
        if sum(len(c) for c in chunks) >= max_chars:
            break
    return "\n".join(chunks)[:max_chars]


def audit_item(item: dict) -> dict:
    expected = canonical_code(item.get("title", ""))
    source_pdf = item.get("source_pdf", "")
    row = {
        "node_id": item.get("node_id", ""),
        "title": item.get("title", ""),
        "document_type": item.get("document_type", ""),
        "status": item.get("status", ""),
        "reason": item.get("reason", ""),
        "expected_code": expected,
        "pdf_url": item.get("pdf_url", ""),
        "source_pdf": source_pdf,
        "found_codes": "",
        "audit_status": "not_downloaded",
        "sample_start": "",
    }
    if item.get("status") != "downloaded" or not source_pdf:
        return row
    path = Path(source_pdf)
    if not path.is_absolute():
        path = ROOT / path
    try:
        sample = pdf_sample(path)
        codes = sorted({re.sub(r"\s+", "", m.group(1)).upper() for m in DOC_CODE_RE.finditer(sample)})
        row["found_codes"] = ";".join(codes)
        row["sample_start"] = re.sub(r"\s+", " ", sample[:500]).strip()
        if expected:
            row["audit_status"] = "ok" if expected in codes else "expected_code_absent"
        elif item.get("document_type") == "statement_of_policy" and any(code_family(c) in {"PS", "CP"} for c in codes):
            row["audit_status"] = "suspicious_ps_cp_for_uncoded_sop"
        else:
            row["audit_status"] = "ok_uncoded"
    except Exception as exc:  # pragma: no cover - operational audit script
        row["audit_status"] = f"error:{type(exc).__name__}"
        row["sample_start"] = str(exc)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    items = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = [audit_item(item) for item in items]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "audit_status",
        "node_id",
        "title",
        "document_type",
        "status",
        "reason",
        "expected_code",
        "found_codes",
        "pdf_url",
        "source_pdf",
        "sample_start",
    ]
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["audit_status"]] = counts.get(row["audit_status"], 0) + 1
    print(json.dumps({"manifest": str(args.manifest), "out": str(args.out), "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
