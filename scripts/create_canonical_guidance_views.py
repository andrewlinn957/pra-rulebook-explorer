#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.canonical import rebuild_canonical_guidance  # noqa: E402

DB = ROOT / "backend/data/rulebook.sqlite3"


def main() -> None:
    conn = sqlite3.connect(DB)
    rebuild_canonical_guidance(conn)
    conn.commit()
    checks = {
        "noncanonical_guidance_documents": conn.execute("SELECT COUNT(*) FROM canonical_guidance_document WHERE is_canonical=0").fetchone()[0],
        "pdf_paragraphs_suppressed": conn.execute("SELECT COUNT(*) FROM canonical_guidance_paragraph WHERE is_canonical=0").fetchone()[0],
        "pdf_sections_suppressed": conn.execute("SELECT COUNT(*) FROM canonical_guidance_section WHERE is_canonical=0").fetchone()[0],
        "canonical_nodes": conn.execute("SELECT COUNT(*) FROM canonical_node WHERE is_canonical=1").fetchone()[0],
        "noncanonical_nodes": conn.execute("SELECT COUNT(*) FROM canonical_node WHERE is_canonical=0").fetchone()[0],
    }
    for key, value in checks.items():
        print(f"{key}|{value}")


if __name__ == "__main__":
    main()
