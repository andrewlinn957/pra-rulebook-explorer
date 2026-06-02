from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .fetch import fetch_url, normalise_url
from .enrich import derive_phase4_edges_and_nodes, derive_richer_edges, derive_rollup_and_shared_analysis_edges
from .advanced_enrich import derive_advanced_topics_and_obligations
from .parse import (
    extract_crr_terms,
    extract_glossary,
    extract_guidance_detail,
    extract_guidance_index,
    extract_legal_instruments_index,
    extract_part,
    extract_rulebook_index,
)
from .store import backfill_placeholder_targets, connect, export_json, upsert_edges, upsert_nodes, upsert_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "backend" / "data" / "rulebook.sqlite3"
DEFAULT_RAW = PROJECT_ROOT / "backend" / "data" / "raw"
DEFAULT_OUT = PROJECT_ROOT / "backend" / "data" / "processed" / "graph.json"


def command_scrape(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    total_nodes = 0
    total_edges = 0
    targets = _targets(args)
    for i, (url, kind) in enumerate(targets, start=1):
        full_url, html, fetched_at = fetch_url(url, args.raw_dir, refresh=args.refresh)
        upsert_source(conn, source_type=kind, url=full_url, fetched_at=_normalise_time(fetched_at), raw_html=html, raw_text=BeautifulSoup(html, "lxml").get_text(" "))
        if kind == "index":
            nodes, edges = extract_rulebook_index(html, full_url)
        elif kind == "glossary":
            nodes, edges = extract_glossary(html, full_url)
        elif kind == "crr_terms":
            nodes, edges = extract_crr_terms(html, full_url)
        elif kind == "guidance_index":
            nodes, edges = extract_guidance_index(html, full_url)
        elif kind == "guidance_detail":
            nodes, edges = extract_guidance_detail(html, full_url)
        elif kind == "legal_instruments":
            nodes, edges = extract_legal_instruments_index(html, full_url)
        else:
            nodes, edges = extract_part(html, full_url)
        upsert_nodes(conn, nodes)
        upsert_edges(conn, edges)
        backfill_placeholder_targets(conn)
        conn.commit()
        total_nodes += len(nodes)
        total_edges += len(edges)
        print(f"[{i:03d}/{len(targets):03d}] {kind:8} {full_url} -> {len(nodes)} nodes, {len(edges)} edges")
        if args.sleep and i < len(targets):
            time.sleep(args.sleep)
    if args.derive:
        counts = derive_richer_edges(conn)
        print(f"derived richer edges: {counts}")
    backfill_placeholder_targets(conn)
    conn.commit()
    export_json(conn, args.out)
    print(f"wrote {args.db}")
    print(f"wrote {args.out}")
    print(f"scraped totals this run: {total_nodes} nodes, {total_edges} edges")
    print_stats(conn)


def command_index(args: argparse.Namespace) -> None:
    full_url, html, _ = fetch_url("/pra-rules", args.raw_dir, refresh=args.refresh)
    nodes, _ = extract_rulebook_index(html, full_url)
    parts = [n for n in nodes if n.node_type == "part"]
    print(json.dumps([{"title": n.title, "url": n.url, "firm_categories": n.metadata.get("firm_categories", [])} for n in parts], indent=2, ensure_ascii=False))


def command_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    print_stats(conn)


def command_enrich(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    counts = derive_phase4_edges_and_nodes(conn)
    extra_counts = derive_rollup_and_shared_analysis_edges(conn)
    advanced_counts = derive_advanced_topics_and_obligations(conn)
    counts = {**counts, **{f"analysis:{k}": v for k, v in extra_counts.items()}, **{f"advanced:{k}": v for k, v in advanced_counts.items()}}
    backfill_placeholder_targets(conn)
    conn.commit()
    if args.out:
        export_json(conn, args.out)
    print(f"phase4 enrichment: {counts}")
    print_stats(conn)


def print_stats(conn) -> None:
    print("nodes by type:")
    for node_type, count in conn.execute("SELECT node_type, COUNT(*) FROM node GROUP BY node_type ORDER BY node_type"):
        print(f"  {node_type}: {count}")
    print("edges by type/method:")
    for edge_type, method, count in conn.execute("SELECT edge_type, source_method, COUNT(*) FROM edge GROUP BY edge_type, source_method ORDER BY edge_type, source_method"):
        print(f"  {edge_type}/{method}: {count}")


def _targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    if args.include_index or args.all_parts:
        targets.append(("/pra-rules", "index"))
    if args.all_parts:
        full_url, html, _ = fetch_url("/pra-rules", args.raw_dir, refresh=args.refresh)
        nodes, _ = extract_rulebook_index(html, full_url)
        part_urls = sorted({n.url for n in nodes if n.node_type == "part"})
        targets.extend((url, "part") for url in part_urls)
    for url in args.part:
        targets.append((normalise_url(url), "part"))
    if args.include_glossary:
        glossary_url = "/glossary?Date=01-06-2026&AZ=All&NoPage=1&p=1" if args.full_glossary else "/glossary"
        targets.append((glossary_url, "glossary"))
    if args.include_crr_terms:
        crr_url = "/crr-terms-list?Date=01-01-2027&AZ=All&NoPage=1&p=1" if args.full_crr_terms else "/crr-terms-list"
        targets.append((crr_url, "crr_terms"))
    if args.include_guidance or args.all_guidance:
        targets.append(("/guidance", "guidance_index"))
    if args.all_guidance:
        full_url, html, _ = fetch_url("/guidance", args.raw_dir, refresh=args.refresh)
        nodes, _ = extract_guidance_index(html, full_url)
        targets.extend((n.url, "guidance_detail") for n in nodes if n.node_type == "guidance_document")
    for url in args.guidance:
        targets.append((normalise_url(url), "guidance_detail"))
    if args.include_legal_instruments:
        targets.append(("/legal-instruments", "legal_instruments"))
    return _dedupe_targets(targets)


def _dedupe_targets(targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for url, kind in targets:
        key = (normalise_url(url), kind)
        if key not in seen:
            out.append((url, kind))
            seen.add(key)
    return out


def _normalise_time(value: str) -> str:
    if "T" in value:
        return value
    return datetime.now(timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape PRA Rulebook pages into nodes/edges.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    sub = parser.add_subparsers(dest="command", required=True)

    scrape = sub.add_parser("scrape", help="Fetch and parse selected pages")
    scrape.add_argument("--refresh", action="store_true", help="Re-fetch pages even if cached")
    scrape.add_argument("--include-index", action="store_true", help="Parse /pra-rules listing")
    scrape.add_argument("--all-parts", action="store_true", help="Parse every Part linked from /pra-rules")
    scrape.add_argument("--include-glossary", action="store_true", help="Parse glossary")
    scrape.add_argument("--full-glossary", action="store_true", help="Use glossary export URL for all terms")
    scrape.add_argument("--include-crr-terms", action="store_true", help="Parse CRR Terms List")
    scrape.add_argument("--full-crr-terms", action="store_true", help="Use CRR Terms export URL; currently effective from 01-01-2027")
    scrape.add_argument("--include-guidance", action="store_true", help="Parse guidance listing")
    scrape.add_argument("--all-guidance", action="store_true", help="Parse every guidance document linked from /guidance")
    scrape.add_argument("--guidance", action="append", default=[], help="Guidance document URL/path to scrape; can be repeated")
    scrape.add_argument("--include-legal-instruments", action="store_true", help="Parse legal instrument listing")
    scrape.add_argument("--derive", action="store_true", help="Derive richer cross-rule/guidance edges after scraping")
    scrape.add_argument("--part", action="append", default=[], help="Part URL/path to scrape; can be repeated")
    scrape.add_argument("--sleep", type=float, default=0.15, help="Polite delay between fetch/parse targets")
    scrape.set_defaults(func=command_scrape)

    index = sub.add_parser("index", help="List Rulebook part URLs from /pra-rules")
    index.add_argument("--refresh", action="store_true")
    index.set_defaults(func=command_index)

    stats = sub.add_parser("stats", help="Print database counts")
    stats.set_defaults(func=command_stats)

    enrich = sub.add_parser("enrich", help="Add regex references, topic nodes and obligation-pattern nodes/edges")
    enrich.add_argument("--out", type=Path, default=DEFAULT_OUT)
    enrich.set_defaults(func=command_enrich)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
