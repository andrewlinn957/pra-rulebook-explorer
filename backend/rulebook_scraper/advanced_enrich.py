from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from .models import Edge, Node
from .parse import edge_id
from .store import upsert_edges, upsert_nodes

TEXT_NODE_TYPES = ("rule", "guidance_paragraph")
CONDITION_RE = re.compile(r"\b(if|where|when|unless|except where|provided that|subject to)\b(?P<condition>[^.;]{5,220})", re.I)
SENTENCE_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")
OBLIGATION_RE = re.compile(
    r"(?P<subject>\b(?:a|an|the)?\s*(?:firm|firms|insurer|insurers|society|third country branch|PRA|management body|governing body|auditor|actuary|person|group|CRR firm|UK bank|Solvency II firm)s?\b[^.;:]{0,90}?)\s+"
    r"(?P<modal>must|shall|should|may|is required to|are required to|must not|should not|may not)\s+"
    r"(?P<predicate>[^.;]{5,260})",
    re.I,
)
STOP_ACTIONS = {"be", "have", "ensure", "make", "take", "provide", "include", "apply", "notify", "submit", "maintain", "hold", "calculate", "comply", "consider", "assess", "identify", "establish", "document", "review", "report", "obtain", "meet"}


def derive_advanced_topics_and_obligations(conn: sqlite3.Connection, *, n_topics: int = 36, min_cluster_size: int = 8, max_nodes: int = 12000) -> dict[str, int]:
    # Topic matching has been retired because it produced low-value graph noise.
    # Keep clearing the old derived layer, but only generate structured obligations.
    conn.execute("DELETE FROM edge WHERE source_method='embedding_topic_cluster' OR edge_type='has_topic_cluster'")
    conn.execute("DELETE FROM node WHERE node_type='topic_cluster'")
    nodes: list[Node] = []
    edges: list[Edge] = []
    obligation_nodes, obligation_edges = _structured_obligations(conn)
    nodes.extend(obligation_nodes)
    edges.extend(obligation_edges)
    upsert_nodes(conn, nodes)
    upsert_edges(conn, edges)
    conn.commit()
    counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        counts[f"node:{n.node_type}"] += 1
    for e in edges:
        counts[e.edge_type] += 1
        counts[f"method:{e.source_method}"] += 1
    return dict(counts)


def _structured_obligations(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    nodes_by_key: dict[str, Node] = {}
    edges: list[Edge] = []
    rows = conn.execute(
        "SELECT id,node_type,title,text,url,metadata_json FROM node WHERE node_type IN ('rule','guidance_paragraph') AND LENGTH(COALESCE(text,'')) > 40"
    ).fetchall()
    for row in rows:
        text = row["text"] or ""
        for sentence in SENTENCE_RE.split(text)[:24]:
            for match in OBLIGATION_RE.finditer(sentence):
                parsed = _parse_obligation(match, sentence)
                if not parsed:
                    continue
                key = "obligation_statement:" + ":".join(parsed[k] for k in ["subject", "modal", "action", "object", "condition"])
                node_id = edge_id("obligation_statement", key)[:16]
                if key not in nodes_by_key:
                    nodes_by_key[key] = Node(
                        id=node_id,
                        node_type="obligation_statement",
                        stable_key=key,
                        title=f"{parsed['subject']} {parsed['modal']} {parsed['action']} {parsed['object']}"[:180],
                        text=parsed["sentence"],
                        metadata=parsed,
                    )
                edges.append(Edge(
                    edge_id(row["id"], node_id, "has_structured_obligation"), row["id"], node_id,
                    "has_structured_obligation", "structured_obligation_parser", 0.82,
                    parsed["sentence"], row["url"], {**parsed, "source_node_type": row["node_type"], "source_title": row["title"]},
                ))
    return list(nodes_by_key.values()), edges


def _parse_obligation(match: re.Match, sentence: str) -> dict[str, str] | None:
    subject = _clean(match.group("subject"))
    modal = _clean(match.group("modal").lower())
    predicate = _clean(match.group("predicate"))
    words = predicate.split()
    if not words:
        return None
    action_idx = 0
    for i, word in enumerate(words[:8]):
        token = re.sub(r"[^a-z-]", "", word.lower())
        if token in STOP_ACTIONS or token.endswith(("e", "y", "t", "d")):
            action_idx = i
            break
    action = re.sub(r"[^a-z-]", "", words[action_idx].lower())
    obj = _clean(" ".join(words[action_idx + 1:]))[:220]
    cond_match = CONDITION_RE.search(sentence)
    condition = _clean(cond_match.group(0))[:220] if cond_match else ""
    if len(subject) < 3 or len(action) < 2 or len(obj) < 3:
        return None
    return {"subject": subject[:120], "modal": modal, "action": action, "object": obj, "condition": condition, "sentence": _clean(sentence)[:500]}



def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[,;:\-\s]+|[,;:\-\s]+$", "", value or "")).strip()
