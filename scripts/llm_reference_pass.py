#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"
OUT = ROOT / "logs/llm-reference-pass-summary.json"
PROMPT_VERSION = "llm-reference-extract-v1-no-candidates"
DEFAULT_MODEL = os.environ.get("PRA_LLM_REFERENCE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"
EXTRACT_NODE_TYPES = (
    "rule",
    "chapter",
    "part",
    "guidance_document",
    "guidance_section",
    "guidance_paragraph",
    "defined_term",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_reference_extraction (
  node_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  response_json TEXT DEFAULT '{}',
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_reference_resolution (
  id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL,
  ref_index INTEGER NOT NULL,
  reference_text TEXT NOT NULL,
  target_kind TEXT DEFAULT '',
  target_title_or_identifier TEXT DEFAULT '',
  target_part_or_document TEXT DEFAULT '',
  evidence_quote TEXT DEFAULT '',
  extracted_confidence REAL DEFAULT 0,
  target_node_id TEXT DEFAULT '',
  target_node_type TEXT DEFAULT '',
  target_title TEXT DEFAULT '',
  resolver_method TEXT DEFAULT '',
  resolver_confidence REAL DEFAULT 0,
  already_had_edge INTEGER DEFAULT 0,
  added_edge_id TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_llm_ref_resolution_source ON llm_reference_resolution(source_node_id);
CREATE INDEX IF NOT EXISTS idx_llm_ref_resolution_target ON llm_reference_resolution(target_node_id);
"""

SYSTEM_PROMPT = """You extract legal/regulatory cross-references from PRA Rulebook graph nodes.
Return JSON only. Do not infer graph targets from outside knowledge. Do not invent references.
Extract references that a human reader would understand as pointing to another rulebook provision, Part, Article, Chapter, guidance document, supervisory statement, statement of policy, definition, form, annex, table, template, legal instrument, statute, regulation, directive, policy statement, consultation, or external regulatory source.
Include references even if they are oddly formatted, partial, non-linked, or embedded in prose.
Do not include a reference to the current node itself merely because its own title/number appears in the context.
Do not include generic mentions with no target, such as "this Part", "this rule", "the PRA", "the firm", unless they specify a distinct target.
For each reference, quote the exact supporting words from the node text.
If there are no references, return {"references": []}.
"""

USER_TEMPLATE = """Extract cross-references from this final graph node.

Important: you are NOT being given candidate nearby targets. Extract only what is explicitly present in the node text/title/context.

Return exactly this JSON shape:
{{
  "references": [
    {{
      "reference_text": "exact referenced phrase as written",
      "target_kind": "rule|part|chapter|article|section|guidance|definition|form|annex|table|template|legal_instrument|statute|regulation|directive|policy_statement|consultation|external|unknown",
      "target_title_or_identifier": "best target identifier/title from the text, e.g. 2.1, Article 435, Insurance General Application, SS1/21, Annex XXXI",
      "target_part_or_document": "explicit Part/document/source named in the text, or empty string",
      "jurisdiction_or_source": "PRA Rulebook|CRR|UK CRR|FSMA|Bank of England|EBA|EU|other|unknown",
      "evidence_quote": "short exact quote from the node text",
      "reason": "brief reason this is a cross-reference",
      "confidence": 0.0
    }}
  ]
}}

Node:
- id: {id}
- type: {node_type}
- title: {title}
- url: {url}
- context: {context}

Text:
{text}
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def norm(value: str) -> str:
    value = (value or "").lower().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def edge_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def connect(path: Path = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    return conn


def node_context(row: sqlite3.Row) -> str:
    meta = json.loads(row["metadata_json"] or "{}")
    fields = []
    for key in ("part_title", "chapter_title", "document_title", "source", "rule_number", "paragraph_number", "section_number"):
        if meta.get(key):
            fields.append(f"{key}: {meta[key]}")
    return "; ".join(fields)


def node_payload(row: sqlite3.Row, max_chars: int) -> str:
    title = row["title"] or ""
    text = row["text"] or ""
    if not text.strip():
        text = title
    return text[:max_chars]


def load_nodes(conn: sqlite3.Connection, *, node_types: list[str], limit: int | None, only_missing: bool, max_chars: int, metadata_source: str | None = None, needs_llm_cleanup: bool = False) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in node_types)
    where = [f"n.node_type IN ({placeholders})", "(COALESCE(n.title,'') <> '' OR COALESCE(n.text,'') <> '')"]
    params: list[Any] = list(node_types)
    if metadata_source:
        where.append("json_extract(n.metadata_json, '$.source') = ?")
        params.append(metadata_source)
    if needs_llm_cleanup:
        where.append("json_extract(n.metadata_json, '$.needs_llm_cleanup') = 1")
    if only_missing:
        where.append("NOT EXISTS (SELECT 1 FROM llm_reference_extraction x WHERE x.node_id=n.id AND x.prompt_version=? AND x.status='ok' AND x.text_hash=?)")
    # only_missing with text_hash is handled in Python because it is per-row.
    sql = f"""
    SELECT n.id,n.node_type,n.stable_key,n.title,n.text,n.url,n.metadata_json
    FROM node n
    WHERE {' AND '.join(where[:-1] if only_missing else where)}
    ORDER BY n.node_type,n.title,n.id
    """
    rows = []
    for r in conn.execute(sql, params):
        text = node_payload(r, max_chars)
        h = sha1("\n".join([r["node_type"] or "", r["title"] or "", node_context(r), text]))
        if only_missing:
            existing = conn.execute(
                "SELECT 1 FROM llm_reference_extraction WHERE node_id=? AND prompt_version=? AND text_hash=? AND status='ok'",
                (r["id"], PROMPT_VERSION, h),
            ).fetchone()
            if existing:
                continue
        rows.append({"row": r, "text": text, "text_hash": h})
        if limit and len(rows) >= limit:
            break
    return rows


def _parse_model_json_output(content: str) -> dict[str, Any]:
    content = (content or "").strip()
    try:
        outer = json.loads(content)
        if isinstance(outer, dict):
            for key in ("text", "content", "output", "message", "response"):
                if isinstance(outer.get(key), str):
                    content = outer[key].strip()
                    break
            else:
                if "references" in outer:
                    return outer
        elif isinstance(outer, str):
            content = outer.strip()
    except Exception:
        pass
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError(f"No JSON object found in model output: {content[:500]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict) or "references" not in parsed or not isinstance(parsed["references"], list):
        raise ValueError(f"Unexpected JSON shape: {content[:500]}")
    return parsed


def call_openai(model: str, user_prompt: str, timeout: int = 90) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:1000]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_model_json_output(content)


def call_openclaw(model: str, user_prompt: str, timeout: int = 180) -> dict[str, Any]:
    # The gateway route gives access to the same configured provider stack as the active agent.
    proc = subprocess.run(
        ["openclaw", "infer", "model", "run", "--gateway", "--model", model, "--json", "--prompt", user_prompt],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    output = (proc.stdout or "").strip()
    if proc.returncode != 0 or output.startswith(("Error:", "GatewayClientRequestError")):
        raise RuntimeError(output[:1200])
    return _parse_model_json_output(output)


def extract_one(item: dict[str, Any], model: str, max_chars: int, backend: str) -> tuple[str, str, str, dict[str, Any] | None, str]:
    r = item["row"]
    prompt = USER_TEMPLATE.format(
        id=r["id"],
        node_type=r["node_type"],
        title=r["title"] or "",
        url=r["url"] or "",
        context=node_context(r),
        text=item["text"],
    )
    for attempt in range(1, 21):
        try:
            parsed = call_openclaw(model, prompt) if backend == "openclaw" else call_openai(model, prompt)
            return r["id"], item["text_hash"], "ok", parsed, ""
        except Exception as exc:
            err = str(exc)
            lower = err.lower()
            if "try again in" in lower or "usage limit" in lower or "429" in err or "cooldown" in lower or "rate_limit" in lower:
                wait = min(900, 120 * attempt)
            else:
                wait = 2 * attempt
            if attempt == 20:
                return r["id"], item["text_hash"], "error", None, err
            time.sleep(wait)
    raise AssertionError("unreachable")


def command_extract(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    node_types = args.node_type or list(EXTRACT_NODE_TYPES)
    rows = load_nodes(
        conn,
        node_types=node_types,
        limit=args.limit,
        only_missing=not args.rerun,
        max_chars=args.max_chars,
        metadata_source=args.metadata_source,
        needs_llm_cleanup=args.needs_llm_cleanup,
    )
    print(f"extracting {len(rows)} nodes with {args.model}, backend={args.backend}, workers={args.workers}, max_chars={args.max_chars}")
    if args.dry_run:
        for item in rows[: args.limit or 5]:
            r = item["row"]
            print(r["id"], r["node_type"], r["title"], node_context(r), item["text"][:300].replace("\n", " "))
        return
    done = 0
    ok = 0
    errors = 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(extract_one, item, args.model, args.max_chars, args.backend) for item in rows]
        for fut in futures.as_completed(futs):
            node_id, text_hash, status, parsed, error = fut.result()
            conn.execute(
                """
                INSERT INTO llm_reference_extraction (node_id,model,prompt_version,text_hash,status,response_json,error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET model=excluded.model,prompt_version=excluded.prompt_version,
                  text_hash=excluded.text_hash,status=excluded.status,response_json=excluded.response_json,
                  error=excluded.error,updated_at=excluded.updated_at
                """,
                (node_id, args.model, PROMPT_VERSION, text_hash, status, json.dumps(parsed or {}, ensure_ascii=False), error, now(), now()),
            )
            conn.commit()
            done += 1
            ok += status == "ok"
            errors += status != "ok"
            if done % args.progress_every == 0 or done == len(rows):
                print(f"progress {done}/{len(rows)} ok={ok} errors={errors}", flush=True)
    print(f"extract complete ok={ok} errors={errors}")


class Resolver:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.nodes = [dict(r) for r in conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node")]
        for n in self.nodes:
            n["meta"] = json.loads(n.get("metadata_json") or "{}")
            n["norm_title"] = norm(n.get("title") or "")
        self.by_norm_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.defs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.parts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.guidance_docs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for n in self.nodes:
            self.by_norm_title[n["norm_title"]].append(n)
            if n["node_type"] == "defined_term":
                self.defs[n["norm_title"]].append(n)
            if n["node_type"] == "part":
                self.parts[n["norm_title"]].append(n)
            if n["node_type"] == "guidance_document":
                self.guidance_docs[n["norm_title"]].append(n)

    def source_context(self, source_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT title,node_type,metadata_json FROM node WHERE id=?", (source_id,)).fetchone()
        if not row:
            return {}
        meta = json.loads(row["metadata_json"] or "{}")
        return {"title": row["title"], "node_type": row["node_type"], **meta}

    def resolve(self, source_id: str, ref: dict[str, Any]) -> tuple[dict[str, Any] | None, str, float]:
        text = str(ref.get("reference_text") or "")
        ident = str(ref.get("target_title_or_identifier") or "")
        doc = str(ref.get("target_part_or_document") or "")
        kind = str(ref.get("target_kind") or "")
        ctx = self.source_context(source_id)
        candidates: list[tuple[dict[str, Any], str, float]] = []

        def add(n: dict[str, Any], method: str, score: float):
            if n["id"] != source_id:
                candidates.append((n, method, score))

        # Definitions like "X" has the meaning given in Foo 2.
        for key in {norm(ident), norm(text)}:
            for n in self.defs.get(key, []):
                add(n, "exact_defined_term_title", 0.985)

        target_kind_norm = norm(kind)
        wants_specific_provision = target_kind_norm in {"rule", "chapter", "section", "article", "annex", "table", "template"}

        # Exact Part / guidance document titles. Only use the containing document
        # as the target when the extracted reference is itself document/Part-level;
        # for rule/chapter references it is context, not the target.
        part_level_kinds = {"part", "guidance", "legal instrument", "statute", "regulation", "directive", "policy statement", "consultation", "external", "unknown"}
        for key in {norm(ident), norm(text)} | ({norm(doc)} if not wants_specific_provision else set()):
            if not key:
                continue
            if target_kind_norm in part_level_kinds or not wants_specific_provision:
                for n in self.parts.get(key, []):
                    add(n, "exact_part_title", 0.90)
                for n in self.guidance_docs.get(key, []):
                    add(n, "exact_guidance_document_title", 0.90)
            if target_kind_norm != "definition":
                wanted_part_for_exact = norm(doc) or norm(ctx.get("part_title", "")) or norm(ctx.get("document_title", ""))
                for n in self.by_norm_title.get(key, []):
                    if n["node_type"] not in {"chapter", "rule", "guidance_section", "guidance_paragraph"}:
                        continue
                    meta = n["meta"]
                    part_doc_norms = {norm(meta.get("part_title", "")), norm(meta.get("document_title", ""))}
                    if wants_specific_provision and wanted_part_for_exact and not any(p and (wanted_part_for_exact == p or wanted_part_for_exact in p or p in wanted_part_for_exact) for p in part_doc_norms):
                        continue
                    if target_kind_norm in {"chapter", "section", "annex", "table", "template"} and n["node_type"] == "rule" and (n.get("title") or "").strip().lower() == key:
                        # Bare chapter/section numbers should not fall onto generic rule rows.
                        continue
                    add(n, "exact_node_title", 0.945)

        # Provision identifiers, preferably within an explicitly named Part, else same Part/source context.
        # Covers ordinary rule numbers (14.12A), SCR headings (3E3, 3D2.3), and ranges (14.4A to 14.10 => first target).
        identifier_match = re.search(r"\b(\d+[A-Z]?(?:\d+)?(?:\.\d+[A-Z]?)*|\d+[A-Z]\d+(?:\.\d+[A-Z]?)?)\b", ident or text, re.I)
        if identifier_match and target_kind_norm != "article":
            num = identifier_match.group(1).rstrip(".")
            wanted_part = norm(doc) or norm(ctx.get("part_title", ""))
            for n in self.nodes:
                if n["node_type"] not in {"rule", "chapter", "guidance_section", "guidance_paragraph"}:
                    continue
                meta = n["meta"]
                title = n.get("title") or ""
                node_num = (meta.get("rule_number") or meta.get("display_number") or meta.get("section_number") or meta.get("paragraph_number") or "").rstrip(".")
                title_first = title.split(" ", 1)[0].rstrip(".") if title else ""
                if node_num != num and title_first.lower() != num.lower():
                    continue
                part_doc_norms = {norm(meta.get("part_title", "")), norm(meta.get("document_title", ""))}
                part_ok = not wanted_part or any(p and (wanted_part == p or wanted_part in p or p in wanted_part) for p in part_doc_norms)
                if part_ok:
                    base = 0.975 if wanted_part else 0.74
                    method = "provision_identifier_with_context" if wanted_part else "provision_identifier_no_context"
                    # Honour the extracted kind where possible.
                    if target_kind_norm == "rule" and n["node_type"] == "rule":
                        add(n, method, base)
                    elif target_kind_norm in {"chapter", "section", "annex", "table", "template"} and n["node_type"] == "chapter":
                        add(n, method, base)
                    elif target_kind_norm not in {"rule", "chapter", "section", "annex", "table", "template"}:
                        add(n, method, base - 0.02)

        # CRR/Article references, optionally constrained by same Part/title document context.
        art_match = re.search(r"\bArticle\s+(\d+[A-Z]?)\b", ident or text, re.I)
        if art_match:
            art = art_match.group(1)
            wanted = f"article {art}"
            source_part = norm(doc) or norm(ctx.get("part_title", ""))
            for n in self.nodes:
                if n["node_type"] != "chapter":
                    continue
                title_norm = n["norm_title"]
                if not re.match(rf"^article\s+{re.escape(art.lower())}(?:\b|[^0-9])", title_norm):
                    continue
                if source_part:
                    part_norm = norm(n["meta"].get("part_title", ""))
                    if source_part and (source_part == part_norm or source_part in part_norm or part_norm in source_part):
                        add(n, "article_with_part_context", 0.9)
                else:
                    add(n, "article_title_no_part_context", 0.72)

        # SS / SoP guidance identifiers in title.
        ss_match = re.search(r"\b((?:SS|SoP|CP|PS)\s*\d+\/\d{2})\b", ident or text, re.I)
        if ss_match:
            code = norm(ss_match.group(1))
            for n in self.nodes:
                if n["node_type"] == "guidance_document" and n["norm_title"].startswith(code):
                    add(n, "guidance_code_title", 0.91)

        if not candidates:
            return None, "unresolved", 0.0
        # Prefer highest score, then more specific node types.
        priority = {"rule": 5, "chapter": 4, "guidance_paragraph": 4, "guidance_section": 3, "defined_term": 3, "part": 2, "guidance_document": 2}
        candidates.sort(key=lambda x: (x[2], priority.get(x[0]["node_type"], 0)), reverse=True)
        return candidates[0]


def existing_reference(conn: sqlite3.Connection, source_id: str, target_id: str) -> bool:
    # The comparison stage asks whether the relationship is already represented
    # anywhere in the graph, not only as edge_type='references'. Defined-term
    # links, for example, are usually stored as uses_defined_term.
    return bool(conn.execute(
        "SELECT 1 FROM edge WHERE from_node_id=? AND to_node_id=? LIMIT 1",
        (source_id, target_id),
    ).fetchone())


def command_resolve(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    resolver = Resolver(conn)
    filters = ["x.status='ok'", "x.prompt_version=?"]
    params: list[Any] = [PROMPT_VERSION]
    node_ids: list[str] = []
    if getattr(args, "node_id_file", None):
        path = Path(args.node_id_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value:
                node_ids.append(value)
        if node_ids:
            filters.append(f"x.node_id IN ({','.join('?' for _ in node_ids)})")
            params.extend(node_ids)
    if args.metadata_source or args.needs_llm_cleanup:
        filters.append("EXISTS (SELECT 1 FROM node n WHERE n.id=x.node_id" + (" AND json_extract(n.metadata_json, '$.source')=?" if args.metadata_source else "") + (" AND json_extract(n.metadata_json, '$.needs_llm_cleanup')=1" if args.needs_llm_cleanup else "") + ")")
        if args.metadata_source:
            params.append(args.metadata_source)
    if args.limit:
        limit_clause = " LIMIT ?"
        params.append(args.limit)
    else:
        limit_clause = ""

    target_rows = conn.execute(
        f"SELECT x.node_id,x.response_json FROM llm_reference_extraction x WHERE {' AND '.join(filters)} ORDER BY x.node_id{limit_clause}",
        params,
    ).fetchall()
    if args.metadata_source or args.needs_llm_cleanup or args.limit or node_ids:
        conn.executemany("DELETE FROM llm_reference_resolution WHERE source_node_id=?", [(r["node_id"],) for r in target_rows])
    else:
        conn.execute("DELETE FROM llm_reference_resolution")
    rows = target_rows
    total_refs = resolved = already = added = 0
    edge_rows_by_key = {}
    staged_pairs = set()
    for row in rows:
        data = json.loads(row["response_json"] or "{}")
        refs = data.get("references") or []
        if not isinstance(refs, list):
            continue
        for idx, ref in enumerate(refs):
            if not isinstance(ref, dict):
                continue
            total_refs += 1
            target, method, score = resolver.resolve(row["node_id"], ref)
            target_id = target["id"] if target else ""
            had = bool(target_id and existing_reference(conn, row["node_id"], target_id))
            if target_id:
                resolved += 1
                already += int(had)
            edge = ""
            extracted_conf = float(ref.get("confidence") or 0)
            if target_id and not had and score >= args.min_resolver_confidence and extracted_conf >= args.min_extracted_confidence:
                edge_key = (row["node_id"], target_id, "references")
                edge = edge_id(row["node_id"], target_id, "references", "llm_extracted_reference")
                evidence = ref.get("evidence_quote") or ref.get("reference_text") or ""
                confidence = min(0.92, score * extracted_conf)
                metadata = {"llm_refs": [ref], "resolver_methods": [method], "prompt_version": PROMPT_VERSION}
                if edge_key in edge_rows_by_key:
                    old = edge_rows_by_key[edge_key]
                    old_meta = json.loads(old[8] or "{}")
                    old_meta.setdefault("llm_refs", []).append(ref)
                    if method not in old_meta.setdefault("resolver_methods", []):
                        old_meta["resolver_methods"].append(method)
                    edge_rows_by_key[edge_key] = (
                        old[0], old[1], old[2], old[3], old[4], max(old[5], confidence),
                        old[6] if evidence in old[6] else (old[6] + "\n---\n" + evidence if old[6] and evidence else old[6] or evidence),
                        old[7], json.dumps(old_meta, ensure_ascii=False),
                    )
                else:
                    edge_rows_by_key[edge_key] = (
                        edge, row["node_id"], target_id, "references", "llm_extracted_reference", confidence,
                        evidence, "", json.dumps(metadata, ensure_ascii=False),
                    )
                    staged_pairs.add(edge_key)
                    added += 1
            rid = edge_id(row["node_id"], str(idx), ref.get("reference_text", ""), ref.get("target_title_or_identifier", ""))
            conn.execute(
                """
                INSERT INTO llm_reference_resolution
                (id,source_node_id,ref_index,reference_text,target_kind,target_title_or_identifier,target_part_or_document,evidence_quote,extracted_confidence,target_node_id,target_node_type,target_title,resolver_method,resolver_confidence,already_had_edge,added_edge_id,metadata_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rid, row["node_id"], idx, ref.get("reference_text", ""), ref.get("target_kind", ""), ref.get("target_title_or_identifier", ""), ref.get("target_part_or_document", ""), ref.get("evidence_quote", ""), extracted_conf,
                    target_id, target["node_type"] if target else "", target["title"] if target else "", method, score, int(had), edge, json.dumps(ref, ensure_ascii=False), now(),
                ),
            )
    edge_rows = list(edge_rows_by_key.values())
    if args.add_edges and edge_rows:
        conn.executemany(
            """
            INSERT INTO edge (id,from_node_id,to_node_id,edge_type,source_method,confidence,evidence_text,source_url,metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET confidence=excluded.confidence,evidence_text=excluded.evidence_text,metadata_json=excluded.metadata_json
            """,
            edge_rows,
        )
    conn.commit()
    summary = {
        "prompt_version": PROMPT_VERSION,
        "model": rows[0]["response_json"] if False and rows else "",
        "extracted_nodes": len(rows),
        "total_refs": total_refs,
        "resolved_refs": resolved,
        "already_had_edges": already,
        "new_edges_added": added if args.add_edges else 0,
        "new_edges_available": added,
    }
    OUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def command_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    data = {
        "extractions": {r["status"]: r["c"] for r in conn.execute("SELECT status,COUNT(*) c FROM llm_reference_extraction GROUP BY status")},
        "refs_by_method": {r["resolver_method"]: r["c"] for r in conn.execute("SELECT resolver_method,COUNT(*) c FROM llm_reference_resolution GROUP BY resolver_method")},
        "new_edges_added": conn.execute("SELECT COUNT(*) FROM edge WHERE source_method='llm_extracted_reference'").fetchone()[0],
    }
    print(json.dumps(data, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LLM pass for hard-to-parse PRA Rulebook cross-references.")
    p.add_argument("--db", type=Path, default=DB)
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("extract", help="Run LLM reference extraction over nodes, resumably.")
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.add_argument("--backend", choices=["openai", "openclaw"], default=os.environ.get("PRA_LLM_REFERENCE_BACKEND", "openai"))
    e.add_argument("--node-type", action="append", choices=EXTRACT_NODE_TYPES)
    e.add_argument("--limit", type=int)
    e.add_argument("--workers", type=int, default=2)
    e.add_argument("--max-chars", type=int, default=6000)
    e.add_argument("--progress-every", type=int, default=25)
    e.add_argument("--metadata-source", help="Restrict to nodes whose metadata_json.source matches this value")
    e.add_argument("--needs-llm-cleanup", action="store_true", help="Restrict to nodes marked metadata_json.needs_llm_cleanup=true")
    e.add_argument("--rerun", action="store_true")
    e.add_argument("--dry-run", action="store_true")
    e.set_defaults(func=command_extract)

    r = sub.add_parser("resolve", help="Resolve extracted references against corpus and optionally add edges.")
    r.add_argument("--add-edges", action="store_true")
    r.add_argument("--metadata-source", help="Restrict to extracted nodes whose metadata_json.source matches this value")
    r.add_argument("--needs-llm-cleanup", action="store_true", help="Restrict to extracted nodes marked metadata_json.needs_llm_cleanup=true")
    r.add_argument("--limit", type=int)
    r.add_argument("--node-id-file", help="Restrict to newline-delimited node ids")
    r.add_argument("--min-resolver-confidence", type=float, default=0.88)
    r.add_argument("--min-extracted-confidence", type=float, default=0.70)
    r.set_defaults(func=command_resolve)

    s = sub.add_parser("stats", help="Print LLM extraction/resolution stats.")
    s.set_defaults(func=command_stats)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
