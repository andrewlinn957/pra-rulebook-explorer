#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend/data/rulebook.sqlite3"
DEFAULT_MANIFEST = ROOT / "backend/data/raw/guidance-pdfs/manifest.json"
OUT_DIR = ROOT / "logs/pdf-text-extraction"

PARA_START_RE = re.compile(r"^(?P<num>\d{1,3}(?:\.\d{1,3}){0,4}[A-Z]?)\s+(?P<body>\S.*)$")
SECTION_RE = re.compile(r"^(chapter|section|part)\s+\d+|^\d+\s+[A-Z][A-Za-z ,/&()\-]{8,}$", re.I)


def sha1(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def clean_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line or "").strip()
    # Common PDF boilerplate and page furniture.
    if not line:
        return ""
    if re.fullmatch(r"\d+", line):
        return ""
    if re.search(r"^PRA\s+(Supervisory Statement|Statement of Policy)\b", line, re.I):
        return ""
    if re.search(r"^Bank of England\s+\|", line, re.I):
        return ""
    return line


def extract_pdf_text(pdf_path: Path) -> list[dict]:
    reader = PdfReader(str(pdf_path))
    pages=[]
    for i, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
        except TypeError:
            raw = page.extract_text() or ""
        except KeyError as exc:
            # Some PRA PDFs include blank/artefact pages with no /Contents stream.
            # Treat those as empty rather than failing the whole document.
            if str(exc) == "'/Contents'":
                raw = ""
            else:
                raise
        lines=[clean_line(x) for x in raw.splitlines()]
        lines=[x for x in lines if x]
        pages.append({"page": i, "text": "\n".join(lines)})
    return pages


def segment_pages(pages: list[dict], max_chars: int = 1800) -> list[dict]:
    segments=[]
    current=None

    def flush():
        nonlocal current
        if current and len(current["text"].strip()) >= 20:
            current["text"] = re.sub(r"\s+", " ", current["text"]).strip()
            segments.append(current)
        current=None

    for page in pages:
        page_no=page["page"]
        for raw in page["text"].splitlines():
            line=clean_line(raw)
            if not line:
                continue
            m=PARA_START_RE.match(line)
            # New numbered paragraphs are the primary structure we need.
            if m:
                flush()
                current={"paragraph_number": m.group("num"), "text": m.group("body"), "page_start": page_no, "page_end": page_no, "kind": "numbered"}
                continue
            # Treat obvious headings as unnumbered short segments, not body glue.
            if SECTION_RE.match(line) and len(line) < 140:
                flush()
                current={"paragraph_number": "", "text": line, "page_start": page_no, "page_end": page_no, "kind": "heading"}
                flush()
                continue
            if current is None:
                current={"paragraph_number": "", "text": line, "page_start": page_no, "page_end": page_no, "kind": "unnumbered"}
            else:
                current["text"] += " " + line
                current["page_end"] = page_no
            if len(current["text"]) > max_chars:
                flush()
    flush()

    # Merge very short unnumbered fragments into neighbours where possible.
    merged=[]
    for seg in segments:
        if merged and not seg["paragraph_number"] and seg["kind"] == "unnumbered" and len(seg["text"]) < 180:
            merged[-1]["text"] += " " + seg["text"]
            merged[-1]["page_end"] = max(merged[-1]["page_end"], seg["page_end"])
        else:
            merged.append(seg)
    return merged


def zero_text_downloaded_docs(conn: sqlite3.Connection, manifest: dict) -> list[sqlite3.Row]:
    conn.row_factory=sqlite3.Row
    q = """
    with gp as (
      select d.id doc_id, p.id para_id
      from node d join edge e on e.from_node_id=d.id and e.edge_type='contains'
      join node p on p.id=e.to_node_id and p.node_type='guidance_paragraph'
      where d.node_type='guidance_document'
      union
      select d.id doc_id, p.id para_id
      from node d join edge e on e.from_node_id=d.id and e.edge_type='contains'
      join node s on s.id=e.to_node_id and s.node_type='guidance_section'
      join edge e2 on e2.from_node_id=s.id and e2.edge_type='contains'
      join node p on p.id=e2.to_node_id and p.node_type='guidance_paragraph'
      where d.node_type='guidance_document'
    )
    select d.* from node d left join gp on gp.doc_id=d.id
    where d.node_type='guidance_document'
    group by d.id
    having count(gp.para_id)=0
    order by d.title
    """
    rows=[]
    for r in conn.execute(q):
        item=manifest.get(r["id"])
        if item and item.get("status") == "downloaded" and item.get("source_pdf"):
            rows.append(r)
    return rows


def downloaded_docs(conn: sqlite3.Connection, manifest: dict) -> list[sqlite3.Row]:
    conn.row_factory=sqlite3.Row
    rows=[]
    for r in conn.execute("select * from node where node_type='guidance_document' order by title"):
        item=manifest.get(r["id"])
        if item and item.get("status") == "downloaded" and item.get("source_pdf"):
            rows.append(r)
    return rows


def delete_existing_pdf_extraction_children(conn: sqlite3.Connection, doc_id: str) -> None:
    section_ids=[
        r[0]
        for r in conn.execute(
            """
            select n.id
            from edge e join node n on n.id=e.to_node_id
            where e.from_node_id=?
              and e.edge_type='contains'
              and n.node_type='guidance_section'
              and json_extract(n.metadata_json,'$.source')='pdf_text_extraction'
            """,
            (doc_id,),
        )
    ]
    child_ids=[
        r[0]
        for r in conn.execute(
            """
            select n.id
            from edge e join node n on n.id=e.to_node_id
            where e.from_node_id=?
              and e.edge_type='contains'
              and n.node_type='guidance_paragraph'
              and json_extract(n.metadata_json,'$.source')='pdf_text_extraction'
            """,
            (doc_id,),
        )
    ]
    if section_ids:
        placeholders=",".join("?" for _ in section_ids)
        child_ids.extend(
            r[0]
            for r in conn.execute(
                f"""
                select n.id
                from edge e join node n on n.id=e.to_node_id
                where e.from_node_id in ({placeholders})
                  and e.edge_type='contains'
                  and n.node_type='guidance_paragraph'
                  and json_extract(n.metadata_json,'$.source')='pdf_text_extraction'
                """,
                section_ids,
            )
        )
    ids=list(dict.fromkeys(child_ids + section_ids))
    if not ids:
        return
    placeholders=",".join("?" for _ in ids)
    conn.execute(f"delete from edge where from_node_id in ({placeholders}) or to_node_id in ({placeholders})", (*ids, *ids))
    conn.execute(f"delete from node_fts where id in ({placeholders})", ids)
    conn.execute(f"delete from node where id in ({placeholders})", ids)


def upsert_node_fts(conn: sqlite3.Connection, node_id: str, title: str, text: str, node_type: str) -> None:
    conn.execute("delete from node_fts where id=?", (node_id,))
    conn.execute(
        "insert into node_fts (id,title,text,node_type) values (?,?,?,?)",
        (node_id, title or "", text or "", node_type),
    )


def upsert_source_document_and_spans(conn: sqlite3.Connection, doc: sqlite3.Row, item: dict, segments: list[dict]) -> str:
    pdf_url=item.get("pdf_url", "") or doc["url"]
    source_pdf=item["source_pdf"]
    checksum=item.get("sha256", "")
    existing=conn.execute(
        "select source_id from source_document where url=? and checksum_sha256=?",
        (pdf_url, checksum),
    ).fetchone()
    source_id=existing[0] if existing else sha1(f"source_document:pdf:{pdf_url}:{checksum}")[:24]
    now=datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        insert into source_document
          (source_id,title,url,local_path,file_type,checksum_sha256,downloaded_at,parent_url,source_status,notes)
        values (?,?,?,?,?,?,?,?,?,?)
        on conflict(source_id) do update set
          title=excluded.title,
          url=excluded.url,
          local_path=excluded.local_path,
          file_type=excluded.file_type,
          checksum_sha256=excluded.checksum_sha256,
          downloaded_at=coalesce(source_document.downloaded_at, excluded.downloaded_at),
          parent_url=excluded.parent_url,
          source_status=excluded.source_status,
          notes=excluded.notes
        """,
        (
            source_id,
            doc["title"],
            pdf_url,
            source_pdf,
            "pdf",
            checksum,
            now,
            doc["url"],
            "extracted",
            "Imported from validated guidance PDF manifest.",
        ),
    )
    conn.execute("delete from source_span where source_id=?", (source_id,))
    for idx, seg in enumerate(segments, start=1):
        span_id=sha1(f"source_span:{source_id}:{idx}:{seg.get('page_start')}:{seg.get('paragraph_number','')}")[:24]
        heading_path=seg.get("section_title") or "PDF extracted text"
        anchor=f"pdf-page-{seg.get('page_start')}-segment-{idx:04d}"
        conn.execute(
            """
            insert into source_span
              (span_id,source_id,span_type,page_number,heading_path,anchor,raw_text,normalised_text,start_offset,end_offset)
            values (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                span_id,
                source_id,
                seg.get("kind") or "paragraph",
                seg.get("page_start"),
                heading_path,
                anchor,
                seg["text"],
                re.sub(r"\s+", " ", seg["text"]).strip(),
                idx,
                idx,
            ),
        )
        seg["source_span_id"] = span_id
    return source_id


def upsert_pdf_segments(conn: sqlite3.Connection, doc: sqlite3.Row, item: dict, pages: list[dict], segments: list[dict]) -> None:
    doc_meta=json.loads(doc["metadata_json"] or "{}")
    full_text="\n\n".join(p["text"] for p in pages if p["text"]).strip()
    source_pdf=item["source_pdf"]
    pdf_url=item.get("pdf_url", "")
    extraction={
        "source": "pdf_text_extraction",
        "source_pdf": source_pdf,
        "pdf_url": pdf_url,
        "pdf_sha256": item.get("sha256", ""),
        "pdf_bytes": item.get("bytes", 0),
        "pages": len(pages),
        "segments": len(segments),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "parser": "pypdf-6.13.2",
        "needs_llm_cleanup": True,
    }
    doc_meta["pdf_extraction"] = extraction
    source_id=upsert_source_document_and_spans(conn, doc, item, segments)
    extraction["source_id"] = source_id
    doc_meta["pdf_extraction"] = extraction
    conn.execute("update node set text=?, metadata_json=? where id=?", (full_text[:200000], json.dumps(doc_meta, ensure_ascii=False), doc["id"]))
    upsert_node_fts(conn, doc["id"], doc["title"], full_text[:200000], "guidance_document")

    current_section_id=""
    current_section_title="PDF extracted text"
    current_section_index=0

    def ensure_section(title: str, page_no: int | None, section_number: str = "") -> str:
        nonlocal current_section_index
        current_section_index += 1
        stable=f"guidance_section:{doc['stable_key']}:pdf-section:{current_section_index:04d}"
        node_id=sha1(stable)[:16]
        meta={
            "section_number": section_number,
            "document_title": doc["title"],
            "source": "pdf_text_extraction",
            "source_pdf": source_pdf,
            "pdf_url": pdf_url,
            "page_start": page_no,
            "source_id": source_id,
            "needs_llm_cleanup": True,
        }
        conn.execute(
            """
            insert into node (id,node_type,stable_key,title,text,url,metadata_json)
            values (?,?,?,?,?,?,?)
            on conflict(stable_key) do update set node_type=excluded.node_type,title=excluded.title,text=excluded.text,url=excluded.url,metadata_json=excluded.metadata_json
            """,
            (node_id, "guidance_section", stable, title, "", f"{doc['url']}#pdf-page-{page_no or 1}", json.dumps(meta, ensure_ascii=False)),
        )
        upsert_node_fts(conn, node_id, title, "", "guidance_section")
        eid=sha1(f"{doc['id']}|{node_id}|contains|pdf_text_extraction")[:20]
        conn.execute(
            """
            insert into edge (id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json)
            values (?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set from_node_id=excluded.from_node_id,to_node_id=excluded.to_node_id,edge_type=excluded.edge_type,source_method=excluded.source_method,confidence=excluded.confidence,evidence_text=excluded.evidence_text,source_url=excluded.source_url,metadata_json=excluded.metadata_json
            """,
            (eid, doc["id"], node_id, "contains", "pdf_text_extraction", 0.88, title[:300], pdf_url or doc["url"], json.dumps({"source_pdf": source_pdf, "page_start": page_no}, ensure_ascii=False)),
        )
        return node_id

    for idx, seg in enumerate(segments, start=1):
        if seg.get("kind") == "heading":
            current_section_title=seg["text"]
            current_section_id=ensure_section(current_section_title, seg.get("page_start"))
            continue
        if not current_section_id:
            current_section_id=ensure_section(current_section_title, seg.get("page_start"))
        para = seg.get("paragraph_number") or f"pdf-{idx:04d}"
        stable=f"guidance_paragraph:{doc['stable_key']}:pdf:{idx:04d}:{para}"
        node_id=sha1(stable)[:16]
        title=f"{doc['title']} {seg.get('paragraph_number') or f'PDF paragraph {idx}'}"
        meta={
            "paragraph_number": seg.get("paragraph_number", ""),
            "document_title": doc["title"],
            "source": "pdf_text_extraction",
            "source_pdf": source_pdf,
            "pdf_url": pdf_url,
            "page_start": seg.get("page_start"),
            "page_end": seg.get("page_end"),
            "segment_index": idx,
            "segment_kind": seg.get("kind"),
            "section_title": current_section_title,
            "source_id": source_id,
            "source_span_id": seg.get("source_span_id"),
            "needs_llm_cleanup": True,
        }
        conn.execute(
            """
            insert into node (id,node_type,stable_key,title,text,url,metadata_json)
            values (?,?,?,?,?,?,?)
            on conflict(stable_key) do update set node_type=excluded.node_type,title=excluded.title,text=excluded.text,url=excluded.url,metadata_json=excluded.metadata_json
            """,
            (node_id, "guidance_paragraph", stable, title, seg["text"], f"{doc['url']}#pdf-page-{seg.get('page_start')}", json.dumps(meta, ensure_ascii=False)),
        )
        upsert_node_fts(conn, node_id, title, seg["text"], "guidance_paragraph")
        eid=sha1(f"{current_section_id}|{node_id}|contains|pdf_text_extraction")[:20]
        conn.execute(
            """
            insert into edge (id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json)
            values (?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set from_node_id=excluded.from_node_id,to_node_id=excluded.to_node_id,edge_type=excluded.edge_type,source_method=excluded.source_method,confidence=excluded.confidence,evidence_text=excluded.evidence_text,source_url=excluded.source_url,metadata_json=excluded.metadata_json
            """,
            (eid, current_section_id, node_id, "contains", "pdf_text_extraction", 0.88, seg["text"][:300], pdf_url or doc["url"], json.dumps({"source_pdf": source_pdf, "page_start": seg.get("page_start"), "page_end": seg.get("page_end"), "source_span_id": seg.get("source_span_id")}, ensure_ascii=False)),
        )


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--node-id", action="append", help="Import only the specified guidance_document node id. May be repeated.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all-downloaded", action="store_true", help="Import every validated downloaded PDF, not only guidance documents with no paragraph text.")
    ap.add_argument("--replace-existing-pdf-extraction", action="store_true", help="Delete existing pdf_text_extraction paragraph nodes for targeted documents before importing.")
    args=ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_items=json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest={i["node_id"]: i for i in manifest_items}
    conn=sqlite3.connect(args.db); conn.row_factory=sqlite3.Row
    if args.node_id:
        wanted=set(args.node_id)
        docs=[]
        for r in conn.execute("select * from node where node_type='guidance_document' order by title"):
            item=manifest.get(r["id"])
            if r["id"] in wanted and item and item.get("status") == "downloaded" and item.get("source_pdf"):
                docs.append(r)
    else:
        docs=downloaded_docs(conn, manifest) if args.all_downloaded else zero_text_downloaded_docs(conn, manifest)
    if args.limit:
        docs=docs[:args.limit]

    backup=""
    if not args.dry_run and docs:
        backup=args.db.with_name(f"{args.db.name}.pre-pdf-import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak")
        shutil.copy2(args.db, backup)

    summary={"db": str(args.db), "backup": str(backup), "docs_targeted": len(docs), "docs": [], "dry_run": args.dry_run}
    for doc in docs:
        item=manifest[doc["id"]]
        pdf_path=ROOT / item["source_pdf"] if not Path(item["source_pdf"]).is_absolute() else Path(item["source_pdf"])
        try:
            pages=extract_pdf_text(pdf_path)
            segments=segment_pages(pages)
            text_chars=sum(len(p["text"]) for p in pages)
            if not args.dry_run:
                if args.replace_existing_pdf_extraction:
                    delete_existing_pdf_extraction_children(conn, doc["id"])
                upsert_pdf_segments(conn, doc, item, pages, segments)
            (OUT_DIR / f"{doc['id']}.txt").write_text("\n\n--- PAGE ---\n\n".join(p["text"] for p in pages), encoding="utf-8")
            summary["docs"].append({"node_id": doc["id"], "title": doc["title"], "pdf": str(pdf_path), "pages": len(pages), "chars": text_chars, "segments": len(segments), "status": "ok"})
        except Exception as exc:
            summary["docs"].append({"node_id": doc["id"], "title": doc["title"], "pdf": str(pdf_path), "status": "error", "error": f"{type(exc).__name__}: {exc}"})
    if not args.dry_run:
        conn.commit()
    ok=sum(1 for d in summary["docs"] if d["status"]=="ok")
    summary["ok"] = ok
    summary["errors"] = [d for d in summary["docs"] if d["status"]!="ok"]
    out=OUT_DIR / "summary.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"targeted": len(docs), "ok": ok, "errors": len(summary["errors"]), "summary": str(out), "backup": str(backup)}, indent=2))

if __name__ == "__main__":
    main()
