#!/usr/bin/env python3
"""Incrementally load a reporting source manifest into source_document/source_span.

This is the non-destructive counterpart to load_reporting_sources.py. It does not
apply schema, clear tables, infer COR011 relationships, or overwrite an existing
source_document with different bytes. If a URL-stable source_id already exists
with a different checksum, it creates a versioned source_id instead.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import load_reporting_sources as cor_loader

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend/data/rulebook.sqlite3"


class IncrementalLoader(cor_loader.Loader):
    def __init__(self, manifest: Path, output_dir: Path, extract_dir: Path) -> None:
        super().__init__()
        self.manifest = manifest
        self.output_dir = output_dir
        self.extract_dir = extract_dir

    def apply_schema_and_clear(self) -> None:  # intentionally disabled
        self.conn.executescript(cor_loader.SCHEMA_PATH.read_text())
        self.conn.commit()

    def safe_source_id(self, r: dict[str, str]) -> str:
        sid = r["source_id"]
        checksum = r.get("checksum_sha256") or ""
        existing = self.conn.execute("SELECT checksum_sha256 FROM source_document WHERE source_id=?", (sid,)).fetchone()
        if existing and (existing["checksum_sha256"] or "") != checksum:
            return f"{sid}-v-{checksum[:10]}"
        return sid

    def load_manifest_docs(self) -> list[dict[str, str]]:
        rows = list(csv.DictReader(self.manifest.open(newline="", encoding="utf-8")))
        loaded = []
        for r in rows:
            source_id = self.safe_source_id(r)
            r = dict(r)
            r["source_id"] = source_id
            exists = self.conn.execute("SELECT 1 FROM source_document WHERE source_id=?", (source_id,)).fetchone()
            self.conn.execute(
                """
                INSERT OR IGNORE INTO source_document
                (source_id,title,url,local_path,file_type,checksum_sha256,downloaded_at,publication_date,
                 effective_from,effective_to,parent_url,source_status,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_id, r.get("title"), r.get("url"), r.get("local_path"), r.get("file_type"),
                    r.get("checksum_sha256"), r.get("downloaded_at"), r.get("publication_date") or None,
                    r.get("effective_date") or None, None, r.get("parent_url"), "downloaded", r.get("notes"),
                ),
            )
            if not exists:
                self.c.source_documents += 1
            self.inserted_docs.add(source_id)
            loaded.append(r)
        self.conn.commit()
        return loaded

    def add_extracted_doc(self, parent: dict[str, str], entry_name: str, out_path: Path, data: bytes) -> str:
        checksum = hashlib.sha256(data).hexdigest()
        sid = self.stable("source", parent["source_id"], entry_name, checksum)
        existing = self.conn.execute("SELECT checksum_sha256 FROM source_document WHERE source_id=?", (sid,)).fetchone()
        if existing and (existing["checksum_sha256"] or "") != checksum:
            sid = f"{sid}-v-{checksum[:10]}"
        rel_path = out_path.relative_to(ROOT).as_posix()
        ext = Path(entry_name).suffix.lower().lstrip(".") or "binary"
        title = Path(entry_name).name
        self.conn.execute(
            """
            INSERT OR IGNORE INTO source_document
            (source_id,title,url,local_path,file_type,checksum_sha256,downloaded_at,parent_url,source_status,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (sid, title, parent["url"] + "#" + entry_name, rel_path, ext, checksum,
             datetime.now(timezone.utc).isoformat(), parent["url"], "extracted", f"Extracted from ZIP source_id={parent['source_id']}"),
        )
        self.inserted_docs.add(sid)
        self.span(parent["source_id"], "zip_entry", entry_name, anchor=entry_name)
        return sid

    def parse_zip(self, r: dict[str, str], path: Path) -> None:
        # Redirect extraction dir for this incremental run.
        old = cor_loader.EXTRACT_DIR
        cor_loader.EXTRACT_DIR = self.extract_dir
        try:
            super().parse_zip(r, path)
        finally:
            cor_loader.EXTRACT_DIR = old

    def write_outputs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with (self.output_dir / "incremental_load_summary.json").open("w", encoding="utf-8") as f:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "counts": self.c.__dict__, "errors": self.errors[:500], "unresolved": self.unresolved[:500]}, f, indent=2, ensure_ascii=False)
        with (self.output_dir / "parsing_errors.json").open("w", encoding="utf-8") as f:
            json.dump(self.errors, f, indent=2, ensure_ascii=False)

    def run_incremental(self) -> None:
        self.apply_schema_and_clear()
        rows = self.load_manifest_docs()
        # Parse only newly loaded/downloaded files with bounded ZIP text extraction inherited from COR011 loader.
        self.parse_sources(rows)
        self.conn.commit()
        self.write_outputs()
        print(json.dumps(self.c.__dict__, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--output-dir", type=Path, default=ROOT / "backend/data/raw/reporting-sources/banking-reporting-all/parsed-load")
    ap.add_argument("--extract-dir", type=Path, default=ROOT / "backend/data/raw/reporting-sources/banking-reporting-all/extracted")
    args = ap.parse_args()
    IncrementalLoader(args.manifest, args.output_dir, args.extract_dir).run_incremental()


if __name__ == "__main__":
    main()
