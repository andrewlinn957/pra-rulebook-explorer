#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "data" / "rulebook.sqlite3"
OUT = ROOT / "logs" / "node-quality-audit.json"


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("–", "-").strip().lower())


def q(conn: sqlite3.Connection, sql: str, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, object] = {}

    out["counts"] = {
        "nodes": conn.execute("select count(*) from node").fetchone()[0],
        "edges": conn.execute("select count(*) from edge").fetchone()[0],
        "document_sources": conn.execute("select count(*) from document_source").fetchone()[0],
        "source_documents": conn.execute("select count(*) from source_document").fetchone()[0],
        "source_spans": conn.execute("select count(*) from source_span").fetchone()[0],
    }
    out["node_type_counts"] = q(conn, "select node_type,count(*) count from node group by node_type order by count desc")
    out["edge_type_counts"] = q(conn, "select edge_type,source_method,count(*) count from edge group by edge_type,source_method order by count desc")

    out["orphan_edges"] = {
        "missing_from": conn.execute("select count(*) from edge e left join node n on n.id=e.from_node_id where n.id is null").fetchone()[0],
        "missing_to": conn.execute("select count(*) from edge e left join node n on n.id=e.to_node_id where n.id is null").fetchone()[0],
    }
    out["self_edges_by_type"] = q(conn, "select edge_type,source_method,count(*) count from edge where from_node_id=to_node_id group by edge_type,source_method order by count desc")
    out["duplicate_edge_keys"] = q(conn, """
        select from_node_id,to_node_id,edge_type,source_method,evidence_text,count(*) count
        from edge
        group by from_node_id,to_node_id,edge_type,source_method,evidence_text
        having count(*) > 1
        order by count desc
        limit 50
    """)
    out["edge_provenance_gaps"] = q(conn, """
        select edge_type,source_method,
               sum(case when coalesce(evidence_text,'')='' then 1 else 0 end) missing_evidence,
               sum(case when coalesce(source_url,'')='' then 1 else 0 end) missing_source_url,
               count(*) count
        from edge
        group by edge_type,source_method
        having missing_evidence > 0 or missing_source_url > 0
        order by missing_evidence desc, missing_source_url desc
    """)
    out["empty_text_by_type"] = q(conn, """
        select node_type,
               sum(case when coalesce(trim(text),'')='' then 1 else 0 end) empty_text,
               count(*) count
        from node group by node_type having empty_text > 0 order by empty_text desc
    """)
    out["empty_url_by_type"] = q(conn, """
        select node_type,
               sum(case when coalesce(trim(url),'')='' then 1 else 0 end) empty_url,
               count(*) count
        from node group by node_type having empty_url > 0 order by empty_url desc
    """)

    # Normalised duplicate identity checks done in Python because SQLite has no regexp replace.
    rows = q(conn, "select id,node_type,stable_key,title,text,url,metadata_json from node")
    buckets: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    title_buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    url_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["node_type"], norm(r["title"]), norm(r["text"]), norm(r["url"]))
        buckets[key].append(r)
        title_buckets[(r["node_type"], norm(r["title"]))].append(r)
        if norm(r["url"]):
            url_buckets[norm(r["url"])].append(r)

    def sample_bucket(items: list[dict]) -> dict:
        return {
            "count": len(items),
            "node_type": items[0]["node_type"],
            "title": items[0]["title"],
            "url": items[0]["url"],
            "ids": [x["id"] for x in items[:8]],
            "stable_keys": [x["stable_key"] for x in items[:8]],
        }

    exact_dupes = [sample_bucket(v) for v in buckets.values() if len(v) > 1]
    exact_dupes.sort(key=lambda x: x["count"], reverse=True)
    out["exact_node_duplicates_top"] = exact_dupes[:100]
    out["exact_node_duplicate_bucket_count"] = len(exact_dupes)
    out["exact_node_duplicate_node_count"] = sum(x["count"] for x in exact_dupes)

    title_dupes = []
    for (node_type, title), items in title_buckets.items():
        if len(items) > 1 and title:
            texts = {norm(x["text"]) for x in items}
            urls = {norm(x["url"]) for x in items}
            title_dupes.append({
                "count": len(items),
                "node_type": node_type,
                "title": items[0]["title"],
                "distinct_texts": len(texts),
                "distinct_urls": len(urls),
                "ids": [x["id"] for x in items[:8]],
                "stable_keys": [x["stable_key"] for x in items[:8]],
            })
    title_dupes.sort(key=lambda x: (x["count"], x["distinct_texts"] == 1), reverse=True)
    out["same_type_title_duplicates_top"] = title_dupes[:100]

    url_multi = []
    for url, items in url_buckets.items():
        types = sorted({x["node_type"] for x in items})
        if len(items) > 1 and len(types) > 1:
            url_multi.append({
                "count": len(items),
                "url": items[0]["url"],
                "node_types": types,
                "examples": [{"id": x["id"], "type": x["node_type"], "title": x["title"], "stable_key": x["stable_key"]} for x in items[:12]],
            })
    url_multi.sort(key=lambda x: x["count"], reverse=True)
    out["same_url_multiple_node_types_top"] = url_multi[:100]

    out["article_like_rules"] = q(conn, """
        select id,title,url,stable_key from node
        where node_type='rule' and title like 'Article %'
        order by title limit 200
    """)
    out["numbered_guidance_title_duplicates"] = q(conn, """
        select title,count(*) count
        from node where node_type in ('guidance_section','guidance_paragraph')
        group by title having count(*) > 1
        order by count desc, title limit 100
    """)
    out["canonical_guidance_summary"] = q(conn, """
        select is_canonical,count(*) count from canonical_node group by is_canonical order by is_canonical
    """) if conn.execute("select count(*) from sqlite_master where name='canonical_node'").fetchone()[0] else []

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps({
        "wrote": str(OUT),
        "counts": out["counts"],
        "orphan_edges": out["orphan_edges"],
        "exact_duplicate_buckets": out["exact_node_duplicate_bucket_count"],
        "exact_duplicate_nodes": out["exact_node_duplicate_node_count"],
        "same_url_multi_type_buckets": len(url_multi),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
