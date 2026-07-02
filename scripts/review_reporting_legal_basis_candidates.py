#!/usr/bin/env python3
"""Create review packs for ReportingObligation -> Provision legal-basis candidates.

Read-only by default. This does not invent ontology terms and does not promote
legal conclusions. It triages existing LEGAL_BASIS candidates into deterministic
review buckets and writes LLM/manual review inputs for ambiguous cases.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
PKG_DIR = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages/packages"
OUT = ROOT / "backend/data/raw/reporting-sources/all-reporting-packages/audit_exports/domain_reviews"

OBSOLETE_PREFIXES = ("FSA", "REP")
OBSOLETE_EXACT = {"MLAR"}

DOMAIN_TERMS = {
    "capital / COREP own funds": ["own funds", "capital", "capital adequacy", "output floor", "sddt", "corep", "reporting"],
    "credit risk": ["credit risk", "standardised approach", "irb", "specialised lending", "securitisation", "pillar 3", "disclosure", "reporting"],
    "counterparty credit risk": ["counterparty credit risk", "ccr", "exposure value", "pillar 3", "disclosure", "reporting"],
    "market risk": ["market risk", "ima", "trading book", "pillar 3", "disclosure", "reporting"],
    "leverage": ["leverage", "leverage ratio", "reporting"],
    "large exposures": ["large exposures", "concentration risk", "reporting"],
    "FINREP": ["financial reporting", "finrep", "reduced finrep", "financial information", "reporting"],
    "liquidity": ["liquidity", "lcr", "stable funding", "nsfr", "pra110", "intraday", "reporting"],
    "operational resilience / other": ["operational", "resolution", "close links", "controllers", "ring-fenced", "reporting"],
    "other PRA/BoE reporting": ["reporting", "regulatory reporting"],
}

IRRELEVANT_BY_DOMAIN = {
    "capital / COREP own funds": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
    "credit risk": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
    "counterparty credit risk": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
    "market risk": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
    "leverage": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
    "large exposures": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
    "FINREP": ["liquidity coverage", "liquidity crr", "lcr", "stable funding", "net stable funding"],
}


def is_obsolete(code: str) -> bool:
    return code.startswith(OBSOLETE_PREFIXES) or code in OBSOLETE_EXACT


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "other"


def rows_to_csv(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def haystack(row: sqlite3.Row | None) -> str:
    if not row:
        return ""
    return " ".join(str(row[k] or "") for k in ["part_id", "provision_label", "provision_type", "heading_path", "text"]).lower()


def classify(domain: str, provision: sqlite3.Row | None) -> tuple[str, str]:
    if provision is None:
        return "unresolved_provision", "Provision graph node has no matching provision row."
    hay = haystack(provision)
    for term in IRRELEVANT_BY_DOMAIN.get(domain, []):
        if term in hay:
            return "reject_candidate_irrelevant_domain", f"Provision text/part matches irrelevant term `{term}` for {domain}."
    terms = DOMAIN_TERMS.get(domain, ["reporting"])
    matched = [t for t in terms if t in hay]
    if matched:
        if "reporting" in matched and len(matched) == 1:
            return "generic_reporting_candidate_review", "Only generic reporting term matched; requires legal/manual review."
        return "candidate_relevant_review", "Matched domain terms: " + ", ".join(matched[:6])
    if "reporting" in hay or "regulatory reporting" in hay:
        return "generic_reporting_candidate_review", "Generic reporting provision; requires manual review."
    return "weak_candidate_needs_review", "No domain-specific term matched; keep as weak candidate pending review."


def edge_for(con: sqlite3.Connection, obligation_node: str, provision_node: str) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT * FROM graph_edge
        WHERE source_node_id=? AND target_node_id=? AND edge_type='LEGAL_BASIS'
        ORDER BY confidence DESC LIMIT 1
        """,
        (obligation_node, provision_node),
    ).fetchone()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    all_rows: list[dict[str, Any]] = []
    llm_rows: list[dict[str, Any]] = []
    packages_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    llm_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for pkg_path in sorted(PKG_DIR.glob("*_package.json")):
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        code = pkg["data_item_code"]
        if is_obsolete(code):
            continue
        domain = pkg.get("reporting_domain") or "other PRA/BoE reporting"
        packages_by_domain[domain].append(pkg)
        out_dir = OUT / slug(domain)
        out_dir.mkdir(parents=True, exist_ok=True)
        obligation_node = f"reporting_obligation:{code}"
        package_rows = []
        for cand in pkg.get("legal_basis", {}).get("candidate_provisions", []):
            provision_node = cand.get("node_id")
            provision = con.execute(
                "SELECT provision_id,part_id,provision_label,provision_type,heading_path,text,effective_from,effective_to,source_span_id FROM provision WHERE provision_id=?",
                (provision_node,),
            ).fetchone()
            edge = edge_for(con, obligation_node, provision_node)
            triage, reason = classify(domain, provision)
            row = {
                "data_item_code": code,
                "package_title": pkg.get("title", ""),
                "domain": domain,
                "provision_node_id": provision_node,
                "part_id": provision["part_id"] if provision else "",
                "provision_label": provision["provision_label"] if provision else cand.get("label", ""),
                "heading_path": provision["heading_path"] if provision else "",
                "effective_from": provision["effective_from"] if provision else "",
                "effective_to": provision["effective_to"] if provision else "",
                "provision_source_span_id": provision["source_span_id"] if provision else "",
                "edge_id": edge["edge_id"] if edge else "",
                "edge_evidence_span_id": edge["evidence_span_id"] if edge else "",
                "confidence": edge["confidence"] if edge else "",
                "review_status": edge["review_status"] if edge else "",
                "triage": triage,
                "triage_reason": reason,
                "text_excerpt": (provision["text"] or "")[:1500] if provision else "",
            }
            package_rows.append(row)
            all_rows.append(row)
            rows_by_domain[domain].append(row)
            if triage in {"generic_reporting_candidate_review", "weak_candidate_needs_review"}:
                llm_row = {
                    "data_item_code": code,
                    "domain": domain,
                    "package_title": pkg.get("title", ""),
                    "provision_node_id": provision_node,
                    "provision_label": row["provision_label"],
                    "text_excerpt": row["text_excerpt"],
                    "question": "Assess whether this provision is a plausible legal basis or downstream reporting-effect provision for the reporting package. Return keep/reject/uncertain with a short rationale and cite the supplied text only.",
                }
                llm_rows.append(llm_row)
                llm_by_domain[domain].append(llm_row)
        # per-package file
        if package_rows:
            rows_to_csv(out_dir / f"{slug(code)}_legal_basis_review.csv", list(package_rows[0].keys()), package_rows)

    headers = list(all_rows[0].keys()) if all_rows else ["data_item_code"]
    rows_to_csv(OUT / "all_legal_basis_candidate_review.csv", headers, all_rows)
    with (OUT / "llm_legal_basis_review_candidates.jsonl").open("w", encoding="utf-8") as f:
        for r in llm_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    package_headers = ["data_item_code", "title", "domain", "source_documents", "template_documents", "instruction_documents", "taxonomy_documents", "validation_documents", "legal_basis_candidates", "package_file", "source_titles"]
    for domain, pkgs in sorted(packages_by_domain.items()):
        out_dir = OUT / slug(domain)
        out_dir.mkdir(parents=True, exist_ok=True)
        package_summary = []
        for pkg in sorted(pkgs, key=lambda p: p.get("data_item_code", "")):
            s = pkg.get("summary") or {}
            srcs = pkg.get("source_provenance", []) or []
            package_summary.append({
                "data_item_code": pkg.get("data_item_code", ""),
                "title": pkg.get("title", ""),
                "domain": domain,
                "source_documents": s.get("source_documents", 0),
                "template_documents": s.get("template_documents", 0),
                "instruction_documents": s.get("instruction_documents", 0),
                "taxonomy_documents": s.get("taxonomy_documents", 0),
                "validation_documents": s.get("validation_documents", 0),
                "legal_basis_candidates": s.get("legal_basis_candidates", 0),
                "package_file": pkg.get("package_file", ""),
                "source_titles": " | ".join((d.get("title") or d.get("source_id") or "") for d in srcs[:12]),
            })
        rows_to_csv(out_dir / "package_source_summary.csv", package_headers, package_summary)
        domain_rows = sorted(rows_by_domain.get(domain, []), key=lambda r: (r.get("data_item_code", ""), r.get("triage", ""), r.get("provision_label", "")))
        rows_to_csv(out_dir / "legal_basis_candidate_triage.csv", headers, domain_rows)
        rows_to_csv(out_dir / "rejected_irrelevant_provision_candidates.csv", headers, [r for r in domain_rows if r.get("triage") == "reject_candidate_irrelevant_domain"])
        unresolved = [r for r in domain_rows if r.get("triage") == "unresolved_provision" or not r.get("edge_id") or not r.get("edge_evidence_span_id")]
        missing_pkg_rows = [{
            "data_item_code": p.get("data_item_code", ""),
            "package_title": p.get("title", ""),
            "domain": domain,
            "provision_node_id": "",
            "part_id": "",
            "provision_label": "",
            "heading_path": "",
            "effective_from": "",
            "effective_to": "",
            "provision_source_span_id": "",
            "edge_id": "",
            "edge_evidence_span_id": "",
            "confidence": "",
            "review_status": "",
            "triage": "missing_legal_basis_candidates",
            "triage_reason": "Package has no legal-basis candidates in the current deterministic graph build.",
            "text_excerpt": "",
        } for p in pkgs if not (p.get("legal_basis", {}) or {}).get("candidate_provisions")]
        rows_to_csv(out_dir / "unresolved_missing_source_or_provision_candidates.csv", headers, unresolved + missing_pkg_rows)
        rec_rows = [r for r in domain_rows if r.get("triage") in {"candidate_relevant_review", "generic_reporting_candidate_review", "weak_candidate_needs_review"}]
        rows_to_csv(out_dir / "recommended_manual_llm_review_items.csv", headers, rec_rows)
        with (out_dir / "recommended_manual_llm_review_items.jsonl").open("w", encoding="utf-8") as f:
            for r in llm_by_domain.get(domain, []):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        triage_counts = Counter(r["triage"] for r in domain_rows)
        md = [f"# Domain review pack: {domain}", "", "Deterministic review pack only. No candidate has been promoted to accepted legal conclusion.", "", f"- Packages: {len(pkgs)}", f"- Legal-basis candidate rows: {len(domain_rows)}", f"- Recommended manual/LLM review rows: {len(rec_rows)}", f"- Rejected irrelevant provision candidates: {triage_counts.get('reject_candidate_irrelevant_domain', 0)}", f"- Unresolved/missing source/provision rows: {len(unresolved) + len(missing_pkg_rows)}", "", "## Triage counts"]
        md.extend(f"- {k}: {v}" for k, v in triage_counts.most_common())
        (out_dir / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    counts = Counter(r["triage"] for r in all_rows)
    by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    for r in all_rows:
        by_domain[r["domain"]][r["triage"]] += 1
    md = ["# Reporting legal-basis candidate review", "", "Read-only deterministic triage. No legal conclusion is promoted by this report.", "", "## Overall triage"]
    for k, v in counts.most_common():
        md.append(f"- {k}: {v}")
    md.append("\n## By domain")
    for domain in sorted(by_domain):
        md.append(f"\n### {domain}")
        for k, v in by_domain[domain].most_common():
            md.append(f"- {k}: {v}")
    md.append("\n## LLM/manual review input")
    md.append(f"- Ambiguous candidates written to `llm_legal_basis_review_candidates.jsonl`: {len(llm_rows)}")
    (OUT / "legal_basis_review_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(all_rows), "llm_candidates": len(llm_rows), "triage": counts}, indent=2))


if __name__ == "__main__":
    main()
