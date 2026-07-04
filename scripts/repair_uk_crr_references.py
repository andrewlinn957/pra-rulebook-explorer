#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
GENERATED_METHODS = {
    "llm_extracted_reference",
    "regex_article_reference",
    "regex_uk_crr_article_reference",
    "regex_reference",
    "rollup_child_edge",
}
LIQUIDITY_PARTS = {"liquidity coverage ratio (crr)", "liquidity (crr)"}
BARE_CRR_ARTICLE_RE = re.compile(
    r"\b(?:CRR\s+Article\s+|Article\s+)(?P<article>\d+[A-Za-z]?)(?:\s*\([^)]*\))*[^.;\n]{0,100}\bCRR\b|"
    r"\bCRR\s+Article\s+(?P<article2>\d+[A-Za-z]?)|"
    r"\b(?P<article3>\d+[A-Za-z]?)(?:\s*\([^)]*\))*\)?\s+of\s+(?:the\s+)?CRR\b",
    re.I,
)
ANNEX_CRR_RE = re.compile(r"\bAnnex\s+(?P<annex>[IVXLCDM]+|\d+)\s+of\s+(?:the\s+)?CRR\b", re.I)
PART_TITLE_CRR_RE = re.compile(r"\bPart\s+(?P<part>\d+|[IVXLCDM]+)\s*,?\s*Title\s+(?P<title>\d+|[IVXLCDM]+)[^.;\n]{0,80}\bof\s+(?:the\s+)?CRR\b", re.I)
EXPLICIT_RULEBOOK_PART_RE = re.compile(r"\b[A-Z][A-Za-z& ]+\s*\(CRR\)\s+Part\b|\b\(CRR\)\s+Part\b", re.I)
NUM_TO_WORD = {
    "1": "ONE",
    "2": "TWO",
    "3": "THREE",
    "4": "FOUR",
    "5": "FIVE",
    "6": "SIX",
    "7": "SEVEN",
    "8": "EIGHT",
    "9": "NINE",
    "10": "TEN",
}


def edge_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def external_article(article: str) -> tuple[str, str, str]:
    article = article.lower()
    node_id = f"external:uk-crr:article:{article}"
    title = f"UK CRR Article {article.upper() if article.isalpha() else article}"
    url = f"https://www.legislation.gov.uk/eur/2013/575/article/{article}"
    return node_id, title, url


def external_annex(annex: str) -> tuple[str, str, str]:
    annex = annex.upper()
    node_id = f"external:uk-crr:annex:{annex.lower()}"
    title = f"UK CRR Annex {annex}"
    url = f"https://www.legislation.gov.uk/eur/2013/575/annex/{annex}"
    return node_id, title, url


def external_part_title(part: str, title_number: str) -> tuple[str, str, str]:
    part_label = NUM_TO_WORD.get(part.upper(), part.upper())
    title_label = title_number.upper()
    node_id = f"external:uk-crr:part:{part_label.lower()}:title:{title_label.lower()}"
    title = f"UK CRR Part {part_label.title()} Title {title_label}"
    url = f"https://www.legislation.gov.uk/eur/2013/575/part/{part_label}/title/{title_label}"
    return node_id, title, url


def external_target_from_evidence(evidence: str, source_title: str) -> tuple[str, str, str, str] | None:
    if EXPLICIT_RULEBOOK_PART_RE.search(evidence) and not re.search(r"\bof\s+(?:the\s+)?CRR\b", evidence or "", re.I):
        return None
    match = BARE_CRR_ARTICLE_RE.search(evidence)
    if match:
        article = (match.group("article") or match.group("article2") or match.group("article3") or "").lower()
        if match.group("article3") and article in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            source_article = re.search(r"\bArticle\s+(\d+[A-Za-z]?)", source_title or "", re.I)
            if source_article:
                article = source_article.group(1).lower()
        if article:
            return (*external_article(article), "article")
    match = ANNEX_CRR_RE.search(evidence)
    if match:
        return (*external_annex(match.group("annex")), "annex")
    match = PART_TITLE_CRR_RE.search(evidence)
    if match:
        return (*external_part_title(match.group("part"), match.group("title")), "part_title")
    return None


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        """
        SELECT e.*, s.title AS source_title, s.url AS source_node_url, coalesce(json_extract(s.metadata_json,'$.part_title'),'') AS source_part,
               t.title AS target_title, t.url AS target_url
        FROM edge e
        JOIN node s ON s.id=e.from_node_id
        JOIN node t ON t.id=e.to_node_id
        WHERE e.edge_type='references'
          AND e.source_method IN ({})
          AND e.evidence_text LIKE '%CRR%'
          AND t.url LIKE 'https://www.prarulebook.co.uk/pra-rules/%'
        """.format(",".join("?" for _ in GENERATED_METHODS)),
        sorted(GENERATED_METHODS),
    ).fetchall()

    added_edges = {}
    stale_edge_ids = []
    touched_articles = set()
    for row in candidates:
        evidence = row["evidence_text"] or ""
        external_target = external_target_from_evidence(evidence, row["source_title"] or "")
        if not external_target:
            continue
        target_id, target_title, target_url, target_kind = external_target
        touched_articles.add(target_id)
        metadata = {"source": "UK CRR", "external_reference": True, "target_kind": target_kind}
        conn.execute(
            """
            INSERT INTO node(id,node_type,stable_key,title,text,url,metadata_json)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,url=excluded.url,metadata_json=excluded.metadata_json
            """,
            (target_id, "external_reference", target_id, target_title, "", target_url, json.dumps(metadata, ensure_ascii=False)),
        )
        new_edge_id = edge_id(row["from_node_id"], target_id, "references", "uk_crr_reference_repair")
        edge_key = (row["from_node_id"], target_id)
        added_edges[edge_key] = (
            new_edge_id,
            row["from_node_id"],
            target_id,
            "references",
            "uk_crr_reference_repair",
            max(float(row["confidence"] or 0), 0.90),
            evidence,
            row["source_url"] or row["source_node_url"] or "",
            json.dumps({
                "repair": "bare_crr_article_to_uk_crr",
                "original_edge_id": row["id"],
                "original_source_method": row["source_method"],
                "original_target_id": row["to_node_id"],
                "target_url": target_url,
            }, ensure_ascii=False),
        )
        stale_edge_ids.append(row["id"])

    if added_edges:
        conn.executemany(
            """
            INSERT INTO edge(id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET confidence=excluded.confidence,evidence_text=excluded.evidence_text,source_url=excluded.source_url,metadata_json=excluded.metadata_json
            """,
            list(added_edges.values()),
        )
    if stale_edge_ids:
        conn.executemany("DELETE FROM edge WHERE id=?", [(edge_id_value,) for edge_id_value in stale_edge_ids])
    conn.commit()
    print(json.dumps({
        "candidate_generated_edges": len(candidates),
        "stale_internal_edges_removed": len(stale_edge_ids),
        "external_edges_added_or_updated": len(added_edges),
        "articles_touched": len(touched_articles),
    }, indent=2))


if __name__ == "__main__":
    main()
