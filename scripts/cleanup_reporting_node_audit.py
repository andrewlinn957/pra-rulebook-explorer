#!/usr/bin/env python3
"""Resolve reporting node audit findings into deterministic graph decisions.

The audit used user-facing categories (for example ``instructions_guidance_pdf``)
while the graph uses structural node types (for example ``SourceDocument`` for a
file and ``InstructionSet`` for the semantic instructions object). This pass
therefore does not blindly retype nodes. It either:

* applies graph-safe corrections, such as legal-reference promotion or missing
  instruction provenance edges;
* records that the recommendation is already represented by existing graph
  edges; or
* discards the finding with a concrete reason when the proposed structural
  rewrite is wrong for the graph model.

Audit metadata is kept only in ``reporting_node_cleanup`` and is stripped from
``graph_node.properties_json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"

CATEGORY_TO_NODE_TYPE = {
    "concept": "Concept",
    "data_item": "DataItem",
    "external_reference": "ExternalReference",
    "firm_type": "FirmType",
    "instructions_guidance_pdf": "InstructionSet",
    "instruction_set": "InstructionSet",
    "legal_instrument": "LegalInstrument",
    "legal_reference": "LegalInstrument",
    "permission": "Permission",
    "policy_statement": "PolicyStatement",
    "provision": "Provision",
    "reporting_obligation": "ReportingObligation",
    "return": "ReportingObligation",
    "scope_rule": "ScopeRule",
    "source_document": "SourceDocument",
    "template": "Template",
    "template_set": "TemplateSet",
    "template_workbook": "TemplateSet",
    "validation_rule": "ValidationRule",
}

RECLASSIFY_ISSUES = {"wrong_node_type", "wrong_category"}
REVIEW_ONLY_ISSUES = {"duplicate_source", "missing_source"}
ALLOWED_RECLASSIFICATION_PAIRS = {
    # Materialised IFRS/IAS/etc references are sometimes emitted as generic
    # ExternalReference nodes. Promoting these to LegalInstrument keeps them in
    # the legal/reference family without disturbing reporting navigation.
    ("ExternalReference", "LegalInstrument"),
}

INSTRUCTION_TEXT_RE = re.compile(r"\b(instruction|instructions|guidance|notes)\b", re.I)
TEMPLATE_TEXT_RE = re.compile(r"\b(template|templates|workbook|data item)\b", re.I)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reporting_node_cleanup (
          node_id TEXT PRIMARY KEY,
          issue_type TEXT NOT NULL,
          severity TEXT,
          confidence TEXT,
          expected_category TEXT,
          source_category TEXT,
          current_node_type TEXT,
          proposed_node_type TEXT,
          proposed_action TEXT NOT NULL,
          safety TEXT NOT NULL,
          finding TEXT,
          recommended_action TEXT,
          duplicate_of TEXT,
          decision TEXT NOT NULL DEFAULT 'proposed',
          decision_reason TEXT,
          applied INTEGER NOT NULL DEFAULT 0,
          applied_reclassification INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reporting_node_cleanup)").fetchall()}
    if "decision" not in existing:
        conn.execute("ALTER TABLE reporting_node_cleanup ADD COLUMN decision TEXT NOT NULL DEFAULT 'proposed'")
    if "decision_reason" not in existing:
        conn.execute("ALTER TABLE reporting_node_cleanup ADD COLUMN decision_reason TEXT")


def normalise_confidence(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if text in {"very high", "high"}:
        return 0.9
    if text == "medium":
        return 0.65
    if text == "low":
        return 0.35
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_props(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_previous_properties_json": raw}
    return parsed if isinstance(parsed, dict) else {"_previous_properties_json": parsed}


def slug_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def annex_roman(value: str | None) -> str | None:
    match = re.search(r"\bannex\s*([ivxlcdm]+)\b", value or "", re.I)
    return match.group(1).upper() if match else None


def reporting_code(value: str | None) -> str | None:
    match = re.search(r"\b(PRA\d{3}|FSA\d{3}|RFB\d{3}|REP\d{3}A?|MLAR)\b", value or "", re.I)
    return match.group(1).upper() if match else None


def source_document_for_node(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT sd.*, n.label AS node_label, n.node_id
        FROM graph_node n
        JOIN source_document sd ON sd.source_id=n.source_pk
        WHERE n.node_id=?
        """,
        (node_id,),
    ).fetchone()


def source_document_text(row: sqlite3.Row | None) -> str:
    if not row:
        return ""
    return " ".join(str(row[key] or "") for key in row.keys()).lower()


def looks_like_instruction_source(row: sqlite3.Row | None) -> bool:
    if not row:
        return False
    text = source_document_text(row)
    return bool(INSTRUCTION_TEXT_RE.search(text)) and not bool(TEMPLATE_TEXT_RE.search(text) and not INSTRUCTION_TEXT_RE.search(text))


def has_edge(conn: sqlite3.Connection, source: str, edge_type: str, target: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM graph_edge WHERE source_node_id=? AND edge_type=? AND target_node_id=? LIMIT 1",
            (source, edge_type, target),
        ).fetchone()
    )


def has_out_edge_to_type(conn: sqlite3.Connection, node_id: str, edge_type: str, target_type: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM graph_edge e
            JOIN graph_node t ON t.node_id=e.target_node_id
            WHERE e.source_node_id=? AND e.edge_type=? AND t.node_type=?
            LIMIT 1
            """,
            (node_id, edge_type, target_type),
        ).fetchone()
    )


def has_in_edge_from_type(conn: sqlite3.Connection, node_id: str, edge_type: str, source_type: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM graph_edge e
            JOIN graph_node s ON s.node_id=e.source_node_id
            WHERE e.target_node_id=? AND e.edge_type=? AND s.node_type=?
            LIMIT 1
            """,
            (node_id, edge_type, source_type),
        ).fetchone()
    )


def instruction_candidate_for_source(conn: sqlite3.Connection, source_node_id: str) -> str | None:
    """Find an existing InstructionSet that this source document evidences.

    This is intentionally conservative: it only matches when the source title or
    URL identifies the same annex/code as an existing instruction set.
    """
    sd = source_document_for_node(conn, source_node_id)
    if not sd or not looks_like_instruction_source(sd):
        return None
    haystack = source_document_text(sd)
    source_annex = annex_roman(haystack)
    source_code = reporting_code(haystack)
    candidates = conn.execute(
        "SELECT node_id,label FROM graph_node WHERE node_type='InstructionSet' ORDER BY length(node_id) DESC"
    ).fetchall()
    for candidate in candidates:
        cid = candidate["node_id"].split(":", 1)[-1]
        label_text = f"{cid} {candidate['label'] or ''}"
        candidate_annex = annex_roman(label_text)
        if source_annex and candidate_annex and source_annex == candidate_annex:
            return candidate["node_id"]
        candidate_code = reporting_code(label_text)
        if source_code and candidate_code and source_code == candidate_code:
            return candidate["node_id"]
    return None


def deterministic_edge_id(source: str, edge_type: str, target: str) -> str:
    digest = hashlib.sha1(f"{source}|{edge_type}|{target}".encode("utf-8")).hexdigest()[:16]
    return f"edge:audit-cleanup:{digest}"


def add_instruction_evidence_edge(conn: sqlite3.Connection, instruction_id: str, source_node_id: str) -> bool:
    if has_edge(conn, instruction_id, "EVIDENCED_BY", source_node_id):
        return False
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_edge(edge_id,source_node_id,target_node_id,edge_type,properties_json,confidence,extraction_method,review_status)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            deterministic_edge_id(instruction_id, "EVIDENCED_BY", source_node_id),
            instruction_id,
            source_node_id,
            "EVIDENCED_BY",
            json.dumps({"source": "reporting_node_audit_cleanup", "reason": "instruction_source_repair"}, sort_keys=True),
            0.96,
            "deterministic_audit_cleanup",
            "accepted_candidate",
        ),
    )
    return True


def classify(row: sqlite3.Row) -> dict[str, Any]:
    issue_type = row["issue_type"] or "unknown"
    expected = (row["expected_category"] or "").strip().lower()
    current = row["node_type"] or ""
    proposed_node_type = CATEGORY_TO_NODE_TYPE.get(expected)
    conf = normalise_confidence(row["confidence"])

    if issue_type in REVIEW_ONLY_ISSUES:
        proposed_action = "review_duplicate_or_missing_source"
        safety = "review_only"
        proposed_node_type = None
    elif issue_type in RECLASSIFY_ISSUES and proposed_node_type and proposed_node_type != current and (current, proposed_node_type) in ALLOWED_RECLASSIFICATION_PAIRS:
        proposed_action = "reclassify_node_type"
        # LLM audit is useful signal, but changing graph semantics should remain opt-in.
        safety = "review_before_reclassify" if conf >= 0.65 else "low_confidence_review"
    elif issue_type in RECLASSIFY_ISSUES and proposed_node_type and proposed_node_type != current:
        proposed_action = "annotate_structural_reclassification"
        safety = "protected_reporting_structure"
    elif issue_type in RECLASSIFY_ISSUES:
        proposed_action = "annotate_category_mismatch"
        safety = "review_only"
    else:
        proposed_action = "review_audit_finding"
        safety = "review_only"

    return {
        "node_id": row["node_id"],
        "issue_type": issue_type,
        "severity": row["severity"],
        "confidence": str(row["confidence"] if row["confidence"] is not None else ""),
        "expected_category": row["expected_category"],
        "source_category": row["source_category"],
        "current_node_type": current,
        "proposed_node_type": proposed_node_type,
        "proposed_action": proposed_action,
        "safety": safety,
        "finding": row["finding"],
        "recommended_action": row["recommended_action"],
        "duplicate_of": row["duplicate_of"],
    }


def finding_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, COALESCE(c.current_node_type, n.node_type) AS node_type, n.properties_json
        FROM reporting_node_audit a
        JOIN graph_node n ON n.node_id = a.node_id
        LEFT JOIN reporting_node_cleanup c ON c.node_id = a.node_id
        WHERE a.status='ok'
          AND COALESCE(a.issue_type, '') NOT IN ('', 'no_issue', 'none', 'ok')
        ORDER BY
          CASE a.severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 9 END,
          a.node_id
        """
    ).fetchall()


def structural_decision(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    *,
    apply: bool,
    repaired: bool,
) -> tuple[str, str]:
    node_id = proposal["node_id"]
    current = proposal["current_node_type"]
    proposed = proposal["proposed_node_type"]

    if proposed == "InstructionSet":
        if current == "SourceDocument":
            candidate = instruction_candidate_for_source(conn, node_id)
            if candidate and repaired:
                return "implemented", f"Implemented: added missing EVIDENCED_BY edge from {candidate} to this instruction source document."
            if has_in_edge_from_type(conn, node_id, "EVIDENCED_BY", "InstructionSet"):
                return "implemented", "Implemented: source document is represented as instruction provenance via InstructionSet -> EVIDENCED_BY -> SourceDocument."
            if candidate and not apply:
                return "pending_apply", f"Safe source-edge repair identified: add EVIDENCED_BY edge from {candidate} to this instruction source document."
            return "discarded", "Discarded: source document is not deterministically identifiable as an instruction source needing a semantic InstructionSet edge."

        if current in {"Template", "DataItem", "ReportingObligation"} and has_out_edge_to_type(conn, node_id, "USES_INSTRUCTIONS", "InstructionSet"):
            return "implemented", "Implemented: the node keeps its structural type and is already linked to the relevant InstructionSet via USES_INSTRUCTIONS."

        return "discarded", "Discarded: the audit proposed changing a structural/domain node into InstructionSet, but no deterministic instruction-source correction applies."

    if proposed == "TemplateSet" and current == "Template":
        if has_in_edge_from_type(conn, node_id, "CONTAINS", "TemplateSet"):
            return "implemented", "Implemented: the node remains a Template and is already represented as a member of a TemplateSet via CONTAINS."
        return "discarded", "Discarded: no containing TemplateSet edge supports changing or remodelling this Template."

    if proposed == "SourceDocument":
        if has_out_edge_to_type(conn, node_id, "EVIDENCED_BY", "SourceDocument"):
            return "implemented", "Implemented: the node keeps its semantic type and is already linked to source evidence via EVIDENCED_BY."
        return "discarded", "Discarded: source-document category describes evidence/provenance, not this node's structural type."

    if proposed == "ReportingObligation":
        if current == "DataItem" and has_in_edge_from_type(conn, node_id, "CONTAINS", "ReportingObligation"):
            return "implemented", "Implemented: the DataItem remains distinct and is already contained by its ReportingObligation."
        return "discarded", "Discarded: the proposed ReportingObligation rewrite is not supported by deterministic obligation structure."

    if proposed == "LegalInstrument":
        return "discarded", "Discarded: articles/provisions and policy statements remain separate from LegalInstrument nodes unless covered by an allowed reference-family reclassification."

    return "discarded", "Discarded: no deterministic graph correction is available for this structural recommendation."


def decision_for(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    *,
    apply: bool,
    apply_reclassifications: bool,
    reclassified: bool,
    repaired: bool,
) -> tuple[str, str]:
    if proposal["proposed_action"] == "reclassify_node_type":
        if reclassified:
            return "implemented", "Applied allowed ExternalReference to LegalInstrument reclassification."
        if apply_reclassifications:
            return "discarded", "Allowed reclassification did not apply because the node already had the proposed type."
        return "pending_apply", "Safe reclassification identified, but --apply-reclassifications was not used."
    if proposal["proposed_action"] == "annotate_structural_reclassification":
        return structural_decision(conn, proposal, apply=apply, repaired=repaired)
    if proposal["proposed_action"] == "review_duplicate_or_missing_source":
        return "discarded", "Discarded: duplicate or missing-source claims need deterministic source identity rules, not direct LLM graph edits."
    if proposal["proposed_action"] == "annotate_category_mismatch":
        return "discarded", "Discarded: category mismatch does not imply a safe graph mutation."
    return "discarded", "Discarded: no deterministic implementation rule is available for this audit finding."


def upsert_proposal(conn: sqlite3.Connection, proposal: dict[str, Any], *, applied: bool, reclassified: bool, repaired: bool, apply_reclassifications: bool) -> None:
    decision, decision_reason = decision_for(
        conn,
        proposal,
        apply=applied,
        apply_reclassifications=apply_reclassifications,
        reclassified=reclassified,
        repaired=repaired,
    )
    conn.execute(
        """
        INSERT INTO reporting_node_cleanup(
          node_id,issue_type,severity,confidence,expected_category,source_category,current_node_type,proposed_node_type,
          proposed_action,safety,finding,recommended_action,duplicate_of,decision,decision_reason,applied,applied_reclassification,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(node_id) DO UPDATE SET
          issue_type=excluded.issue_type,
          severity=excluded.severity,
          confidence=excluded.confidence,
          expected_category=excluded.expected_category,
          source_category=excluded.source_category,
          current_node_type=excluded.current_node_type,
          proposed_node_type=excluded.proposed_node_type,
          proposed_action=excluded.proposed_action,
          safety=excluded.safety,
          finding=excluded.finding,
          recommended_action=excluded.recommended_action,
          duplicate_of=excluded.duplicate_of,
          decision=excluded.decision,
          decision_reason=excluded.decision_reason,
          applied=excluded.applied,
          applied_reclassification=excluded.applied_reclassification,
          updated_at=CURRENT_TIMESTAMP
        """,
        (
            proposal["node_id"],
            proposal["issue_type"],
            proposal["severity"],
            proposal["confidence"],
            proposal["expected_category"],
            proposal["source_category"],
            proposal["current_node_type"],
            proposal["proposed_node_type"],
            proposal["proposed_action"],
            proposal["safety"],
            proposal["finding"],
            proposal["recommended_action"],
            proposal["duplicate_of"],
            decision,
            decision_reason,
            1 if applied else 0,
            1 if reclassified else 0,
        ),
    )


def apply_decision(conn: sqlite3.Connection, row: sqlite3.Row, proposal: dict[str, Any], *, apply_reclassifications: bool) -> tuple[bool, bool]:
    props = parse_props(row["properties_json"])
    props.pop("audit_cleanup", None)
    reclassify = bool(
        apply_reclassifications
        and proposal["proposed_action"] == "reclassify_node_type"
        and proposal["proposed_node_type"]
        and proposal["proposed_node_type"] != row["node_type"]
    )
    if reclassify:
        conn.execute(
            "UPDATE graph_node SET node_type=?, properties_json=? WHERE node_id=?",
            (proposal["proposed_node_type"], json.dumps(props, ensure_ascii=False, sort_keys=True), row["node_id"]),
        )
    else:
        conn.execute(
            "UPDATE graph_node SET properties_json=? WHERE node_id=?",
            (json.dumps(props, ensure_ascii=False, sort_keys=True), row["node_id"]),
        )
    repaired = False
    if proposal["proposed_action"] == "annotate_structural_reclassification" and proposal["proposed_node_type"] == "InstructionSet" and proposal["current_node_type"] == "SourceDocument":
        candidate = instruction_candidate_for_source(conn, proposal["node_id"])
        if candidate:
            repaired = add_instruction_evidence_edge(conn, candidate, proposal["node_id"])
    return reclassify, repaired


def run_cleanup(db_path: Path = DB_PATH, *, apply: bool = False, apply_reclassifications: bool = False) -> dict[str, Any]:
    conn = connect(db_path)
    ensure_schema(conn)
    rows = finding_rows(conn)
    proposals = [classify(row) for row in rows]

    would_reclassify = sum(1 for p in proposals if p["proposed_action"] == "reclassify_node_type")
    decided = reclassified = repaired = 0
    for row, proposal in zip(rows, proposals):
        did_reclassify = False
        did_repair = False
        if apply:
            did_reclassify, did_repair = apply_decision(conn, row, proposal, apply_reclassifications=apply_reclassifications)
            decided += 1
            reclassified += 1 if did_reclassify else 0
            repaired += 1 if did_repair else 0
        upsert_proposal(conn, proposal, applied=apply, reclassified=did_reclassify, repaired=did_repair, apply_reclassifications=apply_reclassifications)
    conn.commit()

    by_action = dict(conn.execute("SELECT proposed_action, COUNT(*) FROM reporting_node_cleanup GROUP BY proposed_action").fetchall())
    by_safety = dict(conn.execute("SELECT safety, COUNT(*) FROM reporting_node_cleanup GROUP BY safety").fetchall())
    by_decision = dict(conn.execute("SELECT decision, COUNT(*) FROM reporting_node_cleanup GROUP BY decision").fetchall())
    implemented = by_decision.get("implemented", 0)
    conn.close()
    return {
        "status": "applied" if apply else "dry_run",
        "findings": len(rows),
        "would_mark_nodes": len(rows),
        "would_reclassify": would_reclassify,
        "marked_nodes": decided,
        "decided": decided,
        "implemented": implemented,
        "reclassified": reclassified,
        "source_edges_repaired": repaired,
        "by_action": by_action,
        "by_safety": by_safety,
        "by_decision": by_decision,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true", help="Annotate graph nodes and mark them needs_cleanup")
    ap.add_argument("--apply-reclassifications", action="store_true", help="Also change graph_node.node_type for proposed reclassifications")
    args = ap.parse_args()
    if args.apply_reclassifications and not args.apply:
        ap.error("--apply-reclassifications requires --apply")
    print(json.dumps(run_cleanup(args.db, apply=args.apply, apply_reclassifications=args.apply_reclassifications), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
