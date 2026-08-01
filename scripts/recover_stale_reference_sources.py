#!/usr/bin/env python3
"""Recover stale source-node IDs in the historical LLM reference ledger.

The LLM resolution table predates one or more node rebuilds, so some rows
retain a source ID that is no longer present in ``node``.  This pass recovers
provenance from current Rulebook text and the duplicate resolution evidence
that still has a live source:

* exact resolution fingerprints are intersected at the old-source group
  level;
* the proposed source must contain the citation/evidence text; and
* unresolved groups may be recovered only when text/context scoring produces
  one clearly dominant current node.

The default mode is a read-only audit.  ``--apply`` updates only rows in the
audit's accepted set, preserving the old ID and match evidence in
``metadata_json``.  The live graph edges and occurrences are not changed by
this script.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_AUDIT = ROOT / "logs" / "stale-reference-source-recovery-20260801.json"
RECOVERY_VERSION = "stale-reference-source-recovery-v1"

# These fields identify one extracted reference independently of its source
# node.  Volatile resolver fields are intentionally excluded.
FINGERPRINT_FIELDS = (
    "reference_text",
    "target_kind",
    "target_title_or_identifier",
    "target_part_or_document",
    "evidence_quote",
    "target_node_id",
    "target_node_type",
    "target_title",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise(value: str) -> str:
    value = (value or "").casefold().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", value).strip()


def source_text(row: sqlite3.Row | dict[str, Any]) -> str:
    return str(row["text"] or row["title"] or "")


def parse_metadata(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def fingerprint(row: sqlite3.Row | dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[field] or "") for field in FINGERPRINT_FIELDS)


def text_contains(text: str, phrase: str) -> bool:
    phrase = (phrase or "").strip()
    if not phrase:
        return False
    return phrase in text or normalise(phrase) in normalise(text)


def node_contains(node: "NodeInfo", phrase: str) -> bool:
    phrase = (phrase or "").strip()
    return bool(phrase) and (phrase in node.text or normalise(phrase) in node.normalised_text)


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    node_type: str
    title: str
    text: str
    normalised_text: str
    title_norm: str
    context_norm: str


def node_info(row: sqlite3.Row) -> NodeInfo:
    metadata = parse_metadata(row["metadata_json"])
    context = " ".join(
        str(metadata.get(key) or "")
        for key in ("document_title", "part_title", "chapter_title", "source")
    )
    text = source_text(row)
    return NodeInfo(
        node_id=row["id"],
        node_type=row["node_type"] or "",
        title=row["title"] or "",
        text=text,
        normalised_text=normalise(text),
        title_norm=normalise(row["title"] or ""),
        context_norm=normalise(context),
    )


@dataclass
class CandidateScore:
    score: int = 0
    rows_hit: set[str] = field(default_factory=set)
    evidence: list[dict[str, Any]] = field(default_factory=list)


def context_hit(node: NodeInfo, context: str) -> bool:
    context = normalise(context)
    if not context:
        return False
    return context in node.title_norm or context in node.context_norm


def score_row_against_node(row: sqlite3.Row, node: NodeInfo, exact_candidates: set[str], exact_bonus: int = 80) -> tuple[int, dict[str, Any] | None]:
    phrases = (
        (str(row["evidence_quote"] or "").strip(), 100, "evidence_quote"),
        (str(row["reference_text"] or "").strip(), 60, "reference_text"),
        (str(row["target_title_or_identifier"] or "").strip(), 25, "target_title_or_identifier"),
    )
    hits = [
        (weight, len(phrase), label, phrase)
        for phrase, weight, label in phrases
        if len(phrase) >= 5 and (phrase in node.text or normalise(phrase) in node.normalised_text)
    ]
    if not hits:
        return 0, None
    weight, phrase_len, label, phrase = max(hits)
    score = weight
    if node.node_id in exact_candidates:
        score += exact_bonus
    if context_hit(node, str(row["target_part_or_document"] or "")):
        score += 20
    if node.node_type in {"guidance_paragraph", "guidance_section", "rule", "chapter", "defined_term"}:
        score += 5
    return score, {
        "row_id": row["id"],
        "matched_field": label,
        "matched_phrase": phrase,
        "score": score,
    }


def score_group(
    rows: list[sqlite3.Row],
    candidates: Iterable[str],
    nodes: dict[str, NodeInfo],
    current_by_key: dict[tuple[str, ...], set[str]],
    exact_bonus: int = 80,
) -> dict[str, CandidateScore]:
    scores: dict[str, CandidateScore] = {candidate: CandidateScore() for candidate in candidates}
    for row in rows:
        exact = current_by_key.get(fingerprint(row), set())
        for candidate in scores:
            score, evidence = score_row_against_node(row, nodes[candidate], exact, exact_bonus=exact_bonus)
            if score:
                scores[candidate].score += score
                scores[candidate].rows_hit.add(row["id"])
                if evidence:
                    scores[candidate].evidence.append(evidence)
    return scores


def ranked(scores: dict[str, CandidateScore]) -> list[tuple[str, CandidateScore]]:
    return sorted(scores.items(), key=lambda item: (item[1].score, len(item[1].rows_hit), item[0]), reverse=True)


def fts_candidates(conn: sqlite3.Connection, phrase: str, nodes: dict[str, NodeInfo], cache: dict[str, set[str]]) -> set[str]:
    """Find current nodes containing a citation phrase via the node FTS index."""
    phrase = (phrase or "").strip()
    cache_key = normalise(phrase)
    if len(cache_key) < 5:
        return set()
    if cache_key in cache:
        return cache[cache_key]
    tokens = re.findall(r"[A-Za-z0-9]+", phrase)
    if not tokens:
        cache[cache_key] = set()
        return set()
    # AND all lexical tokens, then validate the original phrase against the
    # stored text to handle punctuation and whitespace exactly.
    expression = " AND ".join(f'"{token.replace(chr(34), "")}"' for token in dict.fromkeys(tokens))
    try:
        candidate_ids = {
            row["id"]
            for row in conn.execute("SELECT id FROM node_fts WHERE node_fts MATCH ? LIMIT 2000", (expression,))
            if row["id"] in nodes and node_contains(nodes[row["id"]], phrase)
        }
    except sqlite3.OperationalError:
        candidate_ids = set()
    cache[cache_key] = candidate_ids
    return candidate_ids


def unique_group_candidate(
    rows: list[sqlite3.Row],
    current_by_key: dict[tuple[str, ...], set[str]],
    current_group_counts: dict[str, collections.Counter[tuple[str, ...]]],
) -> str | None:
    """Return the one current source containing every fingerprint occurrence."""
    counts = collections.Counter(fingerprint(row) for row in rows)
    candidate_sets = [current_by_key.get(key, set()) for key in counts]
    if not candidate_sets or any(not values for values in candidate_sets):
        return None
    intersection = set.intersection(*candidate_sets)
    valid = [
        candidate
        for candidate in intersection
        if all(current_group_counts[candidate][key] >= count for key, count in counts.items())
    ]
    return valid[0] if len(valid) == 1 else None


def build_recovery(args: argparse.Namespace) -> dict[str, Any]:
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    nodes = {
        row["id"]: node_info(row)
        for row in conn.execute("SELECT id,node_type,title,text,metadata_json FROM node")
    }
    resolutions = list(conn.execute("SELECT * FROM llm_reference_resolution ORDER BY source_node_id,id"))
    valid_source_ids = set(nodes)
    stale_groups: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    current_by_key: dict[tuple[str, ...], set[str]] = collections.defaultdict(set)
    current_group_counts: dict[str, collections.Counter[tuple[str, ...]]] = collections.defaultdict(collections.Counter)
    alias_targets: dict[str, set[str]] = collections.defaultdict(set)
    try:
        for alias in conn.execute("SELECT alias_value,node_id FROM node_aliases WHERE alias_type='legacy_id'"):
            if alias["node_id"] in nodes:
                alias_targets[alias["alias_value"]].add(alias["node_id"])
    except sqlite3.OperationalError:
        pass
    for row in resolutions:
        key = fingerprint(row)
        if row["source_node_id"] in valid_source_ids:
            current_by_key[key].add(row["source_node_id"])
            current_group_counts[row["source_node_id"]][key] += 1
        else:
            stale_groups[row["source_node_id"]].append(row)

    recoveries: list[dict[str, Any]] = []
    group_audit: list[dict[str, Any]] = []
    counts = collections.Counter()
    fts_cache: dict[str, set[str]] = {}

    def add_recovery(row: sqlite3.Row, new_source: str, method: str, confidence: float, evidence: dict[str, Any]) -> bool:
        if row["id"] in assigned_ids:
            return False
        assigned_ids.add(row["id"])
        recoveries.append(
            {
                "resolution_id": row["id"],
                "old_source_node_id": row["source_node_id"],
                "new_source_node_id": new_source,
                "method": method,
                "confidence": confidence,
                "evidence": evidence,
            }
        )
        return True

    for old_source, rows in stale_groups.items():
        assigned_ids: set[str] = set()
        alias_candidates = alias_targets.get(old_source, set())
        if len(alias_candidates) == 1:
            alias_target = next(iter(alias_candidates))
            for row in rows:
                add_recovery(
                    row,
                    alias_target,
                    "legacy_id_alias_exact",
                    1.0,
                    {"alias_type": "legacy_id", "alias_value": old_source},
                )
            counts["legacy_id_alias_exact"] += len(rows)
            group_audit.append({"old_source_node_id": old_source, "status": "recovered", "method": "legacy_id_alias_exact", "new_source_node_id": alias_target, "rows": len(rows)})
            continue
        exact_candidate = unique_group_candidate(rows, current_by_key, current_group_counts)
        if exact_candidate:
            scores = score_group(rows, [exact_candidate], nodes, current_by_key)[exact_candidate]
            if len(scores.rows_hit) == len(rows):
                for row, evidence in zip(rows, [item for item in scores.evidence]):
                    add_recovery(row, exact_candidate, "exact_fingerprint_group_and_text", 0.995, evidence)
                counts["exact_fingerprint_group_and_text"] += len(rows)
                group_audit.append({"old_source_node_id": old_source, "status": "recovered", "method": "exact_fingerprint_group_and_text", "new_source_node_id": exact_candidate, "rows": len(rows)})
                continue

        # Recover row-level exact matches when they all point consistently to
        # one source, even if the group has a duplicate fingerprint elsewhere.
        row_candidates = [current_by_key.get(fingerprint(row), set()) for row in rows]
        unique_row_sources = {next(iter(values)) for values in row_candidates if len(values) == 1}
        if unique_row_sources and len(unique_row_sources) == 1:
            candidate = next(iter(unique_row_sources))
            recovered = 0
            for row, values in zip(rows, row_candidates):
                if values != {candidate}:
                    continue
                score, evidence = score_row_against_node(row, nodes[candidate], values)
                if score:
                    recovered += add_recovery(row, candidate, "unique_fingerprint_row_and_text", 0.98, evidence or {})
            if recovered:
                counts["unique_fingerprint_row_and_text"] += recovered
            if recovered == len(rows):
                group_audit.append({"old_source_node_id": old_source, "status": "recovered", "method": "unique_fingerprint_row_and_text", "new_source_node_id": candidate, "rows": recovered})
                continue

        # Some groups contain a mixture of duplicate and unique fingerprints.
        # Resolve the remaining rows independently when the citation text
        # clearly singles out one of the row's current source candidates.
        row_disambiguated = 0
        for row in rows:
            if row["id"] in assigned_ids:
                continue
            initial_row_candidates = set(current_by_key.get(fingerprint(row), set()))
            row_candidates = set(initial_row_candidates)
            if not row_candidates:
                for phrase in (row["evidence_quote"], row["reference_text"], row["target_title_or_identifier"]):
                    row_candidates.update(fts_candidates(conn, phrase, nodes, fts_cache))
            if not row_candidates:
                continue
            row_scores = ranked(score_group([row], row_candidates, nodes, current_by_key, exact_bonus=80 if initial_row_candidates else 0))
            if not row_scores or row_scores[0][1].score < args.min_score:
                continue
            top_candidate, top_score = row_scores[0]
            runner_score = row_scores[1][1].score if len(row_scores) > 1 else 0
            if len(row_scores) > 1 and top_score.score - runner_score < args.min_margin:
                continue
            evidence = top_score.evidence[0] if top_score.evidence else {}
            if add_recovery(row, top_candidate, "citation_text_context_row_disambiguation", min(0.96, 0.86 + (top_score.score - runner_score) / 1000), evidence):
                row_disambiguated += 1
                counts["citation_text_context_row_disambiguation"] += 1
        if row_disambiguated == len(rows):
            group_audit.append({"old_source_node_id": old_source, "status": "recovered", "method": "citation_text_context_row_disambiguation", "rows": row_disambiguated})
            continue

        # Text/context scoring for duplicate fingerprints.  Restrict the
        # search to current source candidates already associated with the same
        # extracted reference; this prevents generic phrases from matching an
        # unrelated provision.
        initial_union = set().union(*(current_by_key.get(fingerprint(row), set()) for row in rows))
        candidate_union = set(initial_union)
        # Existing resolution matches are usually the strongest source of
        # provenance.  Expand to the full text index only when no current
        # resolution row provides a candidate; otherwise generic phrases such
        # as ``Chapter 2`` create thousands of distracting matches.
        if not initial_union:
            for row in rows:
                for phrase in (row["evidence_quote"], row["reference_text"], row["target_title_or_identifier"]):
                    candidate_union.update(fts_candidates(conn, phrase, nodes, fts_cache))
        if not candidate_union:
            candidate_union = set(nodes)
        # Once free-text search adds candidates outside the duplicate
        # resolution ledger, reduce the exact-candidate bonus.  Otherwise a
        # matching child node can beat the aggregate provision even when the
        # aggregate contains more of the stale source's citations.
        exact_bonus = 0 if not initial_union else 80
        scored = ranked(score_group(rows, candidate_union, nodes, current_by_key, exact_bonus=exact_bonus))
        top = scored[0] if scored else None
        second = scored[1][1].score if len(scored) > 1 else 0
        if top:
            candidate, score = top
            coverage = len(score.rows_hit)
            runner_node = nodes[scored[1][0]] if len(scored) > 1 else None
            atomic_over_aggregate = (
                runner_node is not None
                and coverage == len(rows)
                and nodes[candidate].node_type in {"guidance_paragraph", "guidance_section", "rule", "chapter"}
                and runner_node.node_type == "guidance_document"
                and score.score - second >= 10
            )
            clear_single_text_match = (
                not initial_union
                and score.score >= 100
                and second < 60
                and coverage == len(rows)
            )
            if (score.score >= args.min_score or clear_single_text_match) and (score.score - second >= args.min_margin or atomic_over_aggregate or clear_single_text_match) and coverage >= max(1, int(len(rows) * args.min_coverage)):
                for row in rows:
                    if row["id"] in assigned_ids:
                        continue
                    row_score, evidence = score_row_against_node(row, nodes[candidate], current_by_key.get(fingerprint(row), set()), exact_bonus=exact_bonus)
                    if row_score:
                        if add_recovery(row, candidate, "citation_text_context_dominant", min(0.97, 0.80 + (score.score - second) / 1000), evidence or {}):
                            counts["citation_text_context_dominant"] += 1
                group_audit.append({"old_source_node_id": old_source, "status": "recovered", "method": "citation_text_context_dominant", "new_source_node_id": candidate, "rows": len(assigned_ids), "score": score.score, "runner_up_score": second})
                continue
        remaining = len(rows) - len(assigned_ids)
        counts["unrecovered"] += remaining
        group_audit.append({"old_source_node_id": old_source, "status": "unrecovered", "rows": remaining, "candidate_count": len(candidate_union), "top_score": top[1].score if top else 0, "runner_up_score": second})

    summary = {
        "run_id": hashlib.sha1(f"{RECOVERY_VERSION}|{now()}".encode()).hexdigest()[:20],
        "version": RECOVERY_VERSION,
        "db": str(args.db),
        "generated_at": now(),
        "stale_source_rows": sum(len(rows) for rows in stale_groups.values()),
        "stale_source_groups": len(stale_groups),
        "recoveries": len(recoveries),
        "counts": dict(sorted(counts.items())),
        "unrecovered_rows": sum(1 for row in resolutions if row["source_node_id"] not in valid_source_ids) - len(recoveries),
        "recovered_source_groups": len({row["old_source_node_id"] for row in recoveries}),
        "rows": recoveries,
        "groups": group_audit,
    }
    conn.close()
    return summary


def apply_recovery(args: argparse.Namespace, audit: dict[str, Any]) -> dict[str, Any]:
    if not args.apply:
        return {"applied": False}
    conn = sqlite3.connect(args.db, timeout=120)
    conn.execute("PRAGMA busy_timeout=30000")
    applied = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in audit["rows"]:
            row = conn.execute("SELECT source_node_id,metadata_json FROM llm_reference_resolution WHERE id=?", (item["resolution_id"],)).fetchone()
            if row is None or row[0] != item["old_source_node_id"]:
                continue
            metadata = parse_metadata(row[1])
            metadata["source_recovery"] = {
                "version": RECOVERY_VERSION,
                "run_id": audit["run_id"],
                "old_source_node_id": item["old_source_node_id"],
                "new_source_node_id": item["new_source_node_id"],
                "method": item["method"],
                "confidence": item["confidence"],
                "evidence": item["evidence"],
            }
            conn.execute(
                "UPDATE llm_reference_resolution SET source_node_id=?,metadata_json=? WHERE id=? AND source_node_id=?",
                (item["new_source_node_id"], json.dumps(metadata, ensure_ascii=False, sort_keys=True), item["resolution_id"], item["old_source_node_id"]),
            )
            applied += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"applied": True, "applied_rows": applied}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--apply", action="store_true", help="Update stale source IDs after generating the audit.")
    parser.add_argument("--min-score", type=int, default=160)
    parser.add_argument("--min-margin", type=int, default=60)
    parser.add_argument("--min-coverage", type=float, default=0.5)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    audit = build_recovery(args)
    apply_result = apply_recovery(args, audit)
    audit["apply"] = apply_result
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in audit.items() if key not in {"rows", "groups"}}, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
