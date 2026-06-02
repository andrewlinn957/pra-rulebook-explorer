from __future__ import annotations

import argparse
from pathlib import Path

from .db import DEFAULT_DB, connect, ensure_indexes
from .embeddings import build_embeddings, derive_similar_edges
from .graph import stats


def cmd_build_indexes(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    ensure_indexes(conn)
    print("rebuilt FTS/search indexes")
    if args.embeddings:
        result = build_embeddings(conn, model_name=args.model, limit=args.limit)
        print(f"embeddings: {result}")
    if args.similar:
        result = derive_similar_edges(conn, top_k=args.top_k, threshold=args.threshold, max_nodes=args.max_nodes)
        print(f"similar edges: {result}")
    print(stats(conn))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Graph backend maintenance commands")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    idx = sub.add_parser("build-indexes")
    idx.add_argument("--embeddings", action="store_true")
    idx.add_argument("--similar", action="store_true")
    idx.add_argument("--model", default="tfidf-svd-256", help="Use sentence-transformers:<model_id> if installed, otherwise tfidf-svd-256")
    idx.add_argument("--limit", type=int, default=None)
    idx.add_argument("--max-nodes", type=int, default=None)
    idx.add_argument("--top-k", type=int, default=5)
    idx.add_argument("--threshold", type=float, default=0.62)
    idx.set_defaults(func=cmd_build_indexes)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
