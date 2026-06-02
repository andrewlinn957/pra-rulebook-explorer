from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from backend.rulebook_scraper.models import Edge
from backend.rulebook_scraper.store import sha1, upsert_edges

DEFAULT_NODE_TYPES = ("rule", "guidance_paragraph", "defined_term")


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _edge_id(*parts: str) -> str:
    return sha1(":".join(parts))[:20]


def build_embeddings(conn: sqlite3.Connection, *, node_types: tuple[str, ...] = DEFAULT_NODE_TYPES, model_name: str = "tfidf-svd-256", limit: int | None = None) -> dict[str, Any]:
    rows = _text_rows(conn, node_types=node_types, limit=limit)
    ids = [r["id"] for r in rows]
    texts = [_node_text(r) for r in rows]
    if not rows:
        return {"model": model_name, "embedded": 0}

    vectors, actual_model = _embed_texts(texts, model_name=model_name)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO embedding (node_id, model_name, text_hash, vector_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET model_name=excluded.model_name,
          text_hash=excluded.text_hash, vector_json=excluded.vector_json, created_at=excluded.created_at
        """,
        [(node_id, actual_model, text_hash(text), json.dumps(vec), now) for node_id, text, vec in zip(ids, texts, vectors)],
    )
    conn.commit()
    return {"model": actual_model, "embedded": len(ids)}


def derive_similar_edges(conn: sqlite3.Connection, *, top_k: int = 5, threshold: float = 0.62, node_types: tuple[str, ...] = DEFAULT_NODE_TYPES, max_nodes: int | None = None) -> dict[str, Any]:
    import numpy as np
    from sklearn.neighbors import NearestNeighbors

    # Rebuild semantic edges as a coherent layer rather than accumulating stale
    # results from an earlier model/threshold.
    conn.execute("DELETE FROM edge WHERE edge_type='similar_to' AND source_method='embedding'")

    rows = conn.execute(
        """
        SELECT n.id, n.node_type, n.title, n.metadata_json, emb.vector_json
        FROM embedding emb JOIN node n ON n.id=emb.node_id
        WHERE n.node_type IN (%s)
        ORDER BY n.node_type, n.title
        %s
        """ % (",".join("?" for _ in node_types), f"LIMIT {int(max_nodes)}" if max_nodes else ""),
        list(node_types),
    ).fetchall()
    if len(rows) < 2:
        return {"similar_edges": 0, "reason": "not enough embeddings"}
    vectors = np.array([json.loads(r["vector_json"]) for r in rows], dtype="float32")
    # Normalise for cosine; guard zero rows.
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vectors = vectors / norms

    nn = NearestNeighbors(n_neighbors=min(top_k + 1, len(rows)), metric="cosine", algorithm="brute")
    nn.fit(vectors)
    distances, indices = nn.kneighbors(vectors)
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for i, (dists, neighs) in enumerate(zip(distances, indices)):
        src = rows[i]
        for dist, j in zip(dists, neighs):
            if i == j:
                continue
            score = 1.0 - float(dist)
            if score < threshold:
                continue
            dst = rows[j]
            a, b = sorted([src["id"], dst["id"]])
            if (a, b) in seen:
                continue
            seen.add((a, b))
            src_part = _part_title(src)
            dst_part = _part_title(dst)
            evidence = f"cosine={score:.3f}"
            edges.append(Edge(
                _edge_id(a, b, "similar_to"), a, b, "similar_to", "embedding", score, evidence, "",
                {"model": src_model(conn, a) or "embedding", "from_title": src["title"], "to_title": dst["title"], "from_part": src_part, "to_part": dst_part},
            ))
    upsert_edges(conn, edges)
    conn.commit()
    return {"similar_edges": len(edges), "threshold": threshold, "top_k": top_k}


def src_model(conn: sqlite3.Connection, node_id: str) -> str | None:
    row = conn.execute("SELECT model_name FROM embedding WHERE node_id=?", (node_id,)).fetchone()
    return row[0] if row else None


def _text_rows(conn: sqlite3.Connection, *, node_types: tuple[str, ...], limit: int | None) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id,node_type,title,text,metadata_json
        FROM node
        WHERE node_type IN (%s)
          AND LENGTH(COALESCE(text,'')) > 20
        ORDER BY node_type,title
        %s
        """ % (",".join("?" for _ in node_types), f"LIMIT {int(limit)}" if limit else ""),
        list(node_types),
    ).fetchall()


def _node_text(row: sqlite3.Row) -> str:
    return f"{row['title']}\n{row['text']}"[:6000]


def _embed_texts(texts: list[str], *, model_name: str) -> tuple[list[list[float]], str]:
    if model_name.startswith("sentence-transformers:"):
        try:
            from sentence_transformers import SentenceTransformer
            model_id = model_name.split(":", 1)[1]
            model = SentenceTransformer(model_id)
            arr = model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=True)
            return arr.astype("float32").tolist(), model_name
        except Exception:
            # Fall back to deterministic local vectorisation below.
            pass

    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import Normalizer

    n_components = min(256, max(2, len(texts) - 1))
    vectorizer = TfidfVectorizer(max_features=30000, min_df=2, max_df=0.9, ngram_range=(1, 2), stop_words="english")
    svd = TruncatedSVD(n_components=n_components, random_state=42)
    pipe = make_pipeline(vectorizer, svd, Normalizer(copy=False))
    arr = pipe.fit_transform(texts)
    return arr.astype("float32").tolist(), f"tfidf-svd-{n_components}"


def _part_title(row: sqlite3.Row) -> str | None:
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except Exception:
        return None
    return meta.get("part_title") or meta.get("document_title")
