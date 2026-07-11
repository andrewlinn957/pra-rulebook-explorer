#!/usr/bin/env python3
"""Classify and deduplicate reporting source documents deterministically.

This script deliberately separates source-document facts from graph node types.
It records source classification and duplicate decisions in
``source_document_cleanup`` and rewires graph edges only for deterministic
canonical duplicates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "rulebook.sqlite3"

HUMAN_FACING_TYPES = {"pdf", "xlsx", "xlsm", "xltx", "xls", "html", "txt"}
TEMPLATE_FILE_TYPES = {"xlsx", "xlsm", "xltx", "xls"}
TAXONOMY_FILE_TYPES = {"xml", "xsd", "xbrl", "zip"}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_document_cleanup (
          source_id TEXT PRIMARY KEY,
          source_kind TEXT NOT NULL,
          canonical_source_id TEXT NOT NULL,
          dedupe_key TEXT NOT NULL,
          decision TEXT NOT NULL,
          decision_reason TEXT,
          graph_edges_rewired INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def source_text(row: Mapping[str, Any]) -> str:
    return " ".join(str(row_value(row, k) or "") for k in ("title", "url", "local_path", "file_type")).lower()


def row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def classify_source_document(row: Mapping[str, Any]) -> tuple[str, str]:
    file_type = str(row_value(row, "file_type") or "").lower().strip()
    text = source_text(row)
    if file_type in {"xml"}:
        return "taxonomy_xml", "XML taxonomy artefact."
    if file_type == "xsd":
        return "taxonomy_schema", "XSD taxonomy schema."
    if file_type in {"xbrl", "zip"} or "taxonomy" in text or "xbrl" in text or "dpm" in text:
        return "taxonomy_package", "Taxonomy, XBRL or DPM source artefact."
    if file_type in TEMPLATE_FILE_TYPES:
        return "template_workbook", "Spreadsheet template or workbook source."
    if file_type == "pdf":
        has_instruction = bool(re.search(r"\b(instruction|instructions|guidance|guidelines|notes)\b", text))
        has_template = bool(re.search(r"\b(template|templates|workbook|data item)\b", text))
        if has_instruction:
            return "instruction_pdf", "PDF title or URL identifies instructions or guidance."
        if has_template:
            return "template_pdf", "PDF title or URL identifies a reporting template or data item."
        if re.search(r"\b(cp|ps|ss|sop)\d+/?\d+\b", text):
            return "policy_pdf", "PDF title or URL identifies a PRA policy or supervisory publication."
        return "pdf_document", "PDF source document."
    if file_type == "html":
        if "prarulebook.co.uk/pra-rules" in text:
            return "rulebook_html", "PRA Rulebook HTML source."
        if "/publication/" in text:
            return "policy_html", "Bank of England or PRA publication page."
        return "webpage_html", "HTML web source."
    return "other_source", "No more specific deterministic source class matched."


def normalise_url(url: str | None) -> str:
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = re.sub(r"/+$", "", parts.path)
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in {"download", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}]
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def title_key(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def checksum_key(row: Mapping[str, Any], source_kind: str) -> str:
    checksum = str(row_value(row, "checksum_sha256") or "").strip().lower()
    file_type = str(row_value(row, "file_type") or "").lower().strip()
    if not checksum or file_type not in HUMAN_FACING_TYPES or source_kind.startswith("taxonomy"):
        return ""
    return f"checksum:{file_type}:{source_kind}:{checksum}:{title_key(str(row_value(row, 'title') or ''))}"


def dedupe_key(row: Mapping[str, Any], source_kind: str) -> str:
    file_type = str(row_value(row, "file_type") or "").lower().strip()
    if source_kind.startswith("taxonomy") and file_type != "zip":
        return f"source:{row['source_id']}"
    url = normalise_url(str(row_value(row, "url") or ""))
    if url:
        return f"url:{url}"
    ck = checksum_key(row, source_kind)
    if ck:
        return ck
    return f"source:{row['source_id']}"


def canonical_source(rows: list[sqlite3.Row]) -> sqlite3.Row:
    def score(row: sqlite3.Row) -> tuple[int, int, str]:
        sid = row["source_id"] or ""
        version_penalty = 1 if "-v-" in sid else 0
        title_len = -len(row["title"] or "")
        return (version_penalty, title_len, sid)
    return sorted(rows, key=score)[0]


def edge_id(source: str, edge_type: str, target: str) -> str:
    return "edge:source-dedupe:" + hashlib.sha1(f"{source}|{edge_type}|{target}".encode()).hexdigest()[:16]


def upsert_cleanup(conn: sqlite3.Connection, *, source_id: str, source_kind: str, canonical_source_id: str, key: str, decision: str, reason: str, rewired: int) -> None:
    conn.execute(
        """
        INSERT INTO source_document_cleanup(source_id,source_kind,canonical_source_id,dedupe_key,decision,decision_reason,graph_edges_rewired,updated_at)
        VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(source_id) DO UPDATE SET
          source_kind=excluded.source_kind,
          canonical_source_id=excluded.canonical_source_id,
          dedupe_key=excluded.dedupe_key,
          decision=excluded.decision,
          decision_reason=excluded.decision_reason,
          graph_edges_rewired=CASE
            WHEN excluded.graph_edges_rewired > 0 THEN excluded.graph_edges_rewired
            ELSE source_document_cleanup.graph_edges_rewired
          END,
          updated_at=CURRENT_TIMESTAMP
        """,
        (source_id, source_kind, canonical_source_id, key, decision, reason, rewired),
    )


def rewire_graph_source(conn: sqlite3.Connection, duplicate_source_id: str, canonical_source_id: str) -> int:
    duplicate_node = f"source_document:{duplicate_source_id}"
    canonical_node = f"source_document:{canonical_source_id}"
    if not conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (duplicate_node,)).fetchone():
        return 0
    if not conn.execute("SELECT 1 FROM graph_node WHERE node_id=?", (canonical_node,)).fetchone():
        conn.execute(
            """
            INSERT INTO graph_node(node_id,node_type,label,source_table,source_pk,properties_json,review_status)
            SELECT ?,node_type,label,source_table,?,properties_json,review_status FROM graph_node WHERE node_id=?
            """,
            (canonical_node, canonical_source_id, duplicate_node),
        )
    changed = 0
    for row in conn.execute("SELECT * FROM graph_edge WHERE source_node_id=? OR target_node_id=?", (duplicate_node, duplicate_node)).fetchall():
        src = canonical_node if row["source_node_id"] == duplicate_node else row["source_node_id"]
        tgt = canonical_node if row["target_node_id"] == duplicate_node else row["target_node_id"]
        if conn.execute("SELECT 1 FROM graph_edge WHERE source_node_id=? AND target_node_id=? AND edge_type=?", (src, tgt, row["edge_type"])).fetchone():
            conn.execute("DELETE FROM graph_edge WHERE edge_id=?", (row["edge_id"],))
        else:
            conn.execute("UPDATE graph_edge SET source_node_id=?, target_node_id=? WHERE edge_id=?", (src, tgt, row["edge_id"]))
        changed += 1
    if not conn.execute("SELECT 1 FROM graph_edge WHERE source_node_id=? OR target_node_id=?", (duplicate_node, duplicate_node)).fetchone():
        conn.execute("DELETE FROM graph_node WHERE node_id=?", (duplicate_node,))
    return changed


def run_source_cleanup(db_path: Path = DB_PATH, *, apply: bool = False) -> dict[str, Any]:
    conn = connect(db_path)
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM source_document ORDER BY source_id").fetchall()
    classified: list[tuple[sqlite3.Row, str, str, str]] = []
    for row in rows:
        kind, reason = classify_source_document(row)
        classified.append((row, kind, reason, dedupe_key(row, kind)))
    groups: dict[str, list[tuple[sqlite3.Row, str, str, str]]] = {}
    for item in classified:
        groups.setdefault(item[3], []).append(item)

    duplicates = rewired_total = 0
    for key, items in groups.items():
        canonical = canonical_source([item[0] for item in items])
        for row, kind, classify_reason, _ in items:
            is_duplicate = row["source_id"] != canonical["source_id"] and (key.startswith("url:") or key.startswith("checksum:"))
            rewired = 0
            decision = "canonical"
            reason = classify_reason
            if is_duplicate:
                duplicates += 1
                decision = "duplicate_rewired" if apply else "duplicate_candidate"
                reason = f"Duplicate of {canonical['source_id']} by {key.split(':', 1)[0]} key."
                if apply:
                    rewired = rewire_graph_source(conn, row["source_id"], canonical["source_id"])
                    rewired_total += rewired
            upsert_cleanup(conn, source_id=row["source_id"], source_kind=kind, canonical_source_id=canonical["source_id"], key=key, decision=decision, reason=reason, rewired=rewired)
    conn.commit()
    by_kind = dict(conn.execute("SELECT source_kind,COUNT(*) FROM source_document_cleanup GROUP BY source_kind").fetchall())
    by_decision = dict(conn.execute("SELECT decision,COUNT(*) FROM source_document_cleanup GROUP BY decision").fetchall())
    conn.close()
    return {"status": "applied" if apply else "dry_run", "sources": len(rows), "duplicate_candidates": duplicates, "duplicates_rewired": rewired_total, "by_kind": by_kind, "by_decision": by_decision}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    print(json.dumps(run_source_cleanup(args.db, apply=args.apply), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
