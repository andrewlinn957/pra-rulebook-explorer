from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

import numpy as np
from sklearn.cluster import MiniBatchKMeans
from sklearn.feature_extraction.text import TfidfVectorizer

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
    nodes: list[Node] = []
    edges: list[Edge] = []
    topic_nodes, topic_edges = _embedding_topic_clusters(conn, n_topics=n_topics, min_cluster_size=min_cluster_size, max_nodes=max_nodes)
    obligation_nodes, obligation_edges = _structured_obligations(conn)
    nodes.extend(topic_nodes); nodes.extend(obligation_nodes)
    edges.extend(topic_edges); edges.extend(obligation_edges)
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


def _embedding_topic_clusters(conn: sqlite3.Connection, *, n_topics: int, min_cluster_size: int, max_nodes: int) -> tuple[list[Node], list[Edge]]:
    rows = conn.execute(
        """
        SELECT n.id,n.node_type,n.title,n.text,n.url,n.metadata_json,emb.vector_json
        FROM embedding emb JOIN node n ON n.id=emb.node_id
        WHERE n.node_type IN ('rule','guidance_paragraph') AND LENGTH(COALESCE(n.text,'')) > 50
        LIMIT ?
        """,
        (max_nodes,),
    ).fetchall()
    if len(rows) < n_topics * min_cluster_size:
        return [], []
    vectors = np.array([json.loads(r["vector_json"]) for r in rows], dtype="float32")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vectors = vectors / norms
    kmeans = MiniBatchKMeans(n_clusters=n_topics, random_state=42, batch_size=1024, n_init="auto")
    labels = kmeans.fit_predict(vectors)
    by_label: dict[int, list[int]] = defaultdict(list)
    for i, label in enumerate(labels):
        by_label[int(label)].append(i)

    texts = [f"{r['title']} {r['text']}"[:4000] for r in rows]
    tfidf = TfidfVectorizer(max_features=20000, min_df=2, max_df=0.75, stop_words="english", ngram_range=(1, 2))
    matrix = tfidf.fit_transform(texts)
    terms = np.array(tfidf.get_feature_names_out())

    topic_nodes: list[Node] = []
    topic_edges: list[Edge] = []
    for label, idxs in sorted(by_label.items()):
        if len(idxs) < min_cluster_size:
            continue
        centroid = kmeans.cluster_centers_[label]
        cluster_vectors = vectors[idxs]
        sims = cluster_vectors @ centroid / (np.linalg.norm(centroid) or 1)
        top_local = np.argsort(-sims)[:8]
        representative_idxs = [idxs[i] for i in top_local]
        term_scores = np.asarray(matrix[idxs].mean(axis=0)).ravel()
        top_terms = [t for t in terms[np.argsort(-term_scores)[:8]] if len(t) > 2]
        label_text = _topic_label(top_terms, rows[representative_idxs[0]]["title"])
        node_id = edge_id("topic_cluster", str(label), label_text)[:16]
        topic_nodes.append(Node(
            id=node_id,
            node_type="topic_cluster",
            stable_key=f"topic_cluster:{label}:{label_text.lower()}",
            title=label_text,
            text="; ".join(top_terms),
            metadata={"cluster": label, "size": len(idxs), "top_terms": top_terms, "representative_titles": [rows[i]["title"] for i in representative_idxs[:5]]},
        ))
        for i in idxs:
            score = float(vectors[i] @ centroid / (np.linalg.norm(centroid) or 1))
            topic_edges.append(Edge(
                edge_id(rows[i]["id"], node_id, "has_topic_cluster"), rows[i]["id"], node_id,
                "has_topic_cluster", "embedding_topic_cluster", max(0.35, min(0.98, score)),
                "; ".join(top_terms[:5]), rows[i]["url"],
                {"cluster": label, "topic_label": label_text, "top_terms": top_terms, "node_type": rows[i]["node_type"]},
            ))
    return topic_nodes, topic_edges


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


def _topic_label(top_terms: list[str], fallback: str) -> str:
    if top_terms:
        return " / ".join(t.title() for t in top_terms[:3])[:90]
    return fallback[:90]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[,;:\-\s]+|[,;:\-\s]+$", "", value or "")).strip()
