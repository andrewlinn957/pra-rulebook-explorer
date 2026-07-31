import importlib.util
import json
import sqlite3
from pathlib import Path


def load_module(name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


importer = load_module("import_reference_recall_reviews_test", "import_reference_recall_reviews.py")


def test_importer_records_exact_review_and_local_definition_target(tmp_path):
    db = tmp_path / "rulebook.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE node (
          id TEXT PRIMARY KEY, node_type TEXT NOT NULL, stable_key TEXT NOT NULL,
          title TEXT NOT NULL, text TEXT DEFAULT '', url TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE edge (
          id TEXT PRIMARY KEY, from_node_id TEXT NOT NULL, to_node_id TEXT NOT NULL,
          edge_type TEXT NOT NULL, source_method TEXT NOT NULL, confidence REAL NOT NULL,
          evidence_text TEXT DEFAULT '', source_url TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE reference_occurrence (
          occurrence_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, source_node_id TEXT NOT NULL,
          target_node_id TEXT, edge_id TEXT, relationship_type TEXT NOT NULL DEFAULT 'REF',
          citation_kind TEXT NOT NULL, citation_text TEXT NOT NULL, group_text TEXT NOT NULL,
          instrument_id TEXT, provision_path TEXT, qualifier TEXT DEFAULT '', span_start INTEGER NOT NULL,
          span_end INTEGER NOT NULL, status TEXT NOT NULL, source_method TEXT NOT NULL,
          confidence REAL NOT NULL, context_text TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}'
        );
        """
    )
    text = "The Investment firm definition applies."
    conn.execute("INSERT INTO node VALUES (?,?,?,?,?,?,?)", ("source", "rule", "source", "Source", text, "", "{}"))
    conn.execute("INSERT INTO node VALUES (?,?,?,?,?,?,?)", ("term", "defined_term", "term", "Investment firm", "", "", "{}"))
    conn.commit()
    conn.close()

    source_hash = importer.digest(text)
    pilot = tmp_path / "pilot.jsonl"
    pilot_row = {
        "source_node_id": "source",
        "source_node_type": "rule",
        "source_title": "Source",
        "source_text_hash": source_hash,
        "chunk_start": 0,
        "chunk_end": len(text),
        "text": text,
        "candidates": [],
    }
    pilot.write_text(json.dumps(pilot_row) + "\n", encoding="utf-8")
    custom_id = importer.request_id("source", source_hash, 0, len(text))
    quote = "Investment firm"
    start = text.index(quote)
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "custom_id": custom_id,
                "findings": [
                    {
                        "span_start": start,
                        "span_end": start + len(quote),
                        "quoted_text": quote,
                        "target_hint": "Investment firm",
                        "target_kind": "definition",
                        "decision": "REFERENCE",
                        "confidence": 0.98,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    review_db = tmp_path / "reviews.sqlite3"
    args = importer.build_parser().parse_args([
        "--pilot", str(pilot), "--responses", str(responses), "--db", str(db), "--review-db", str(review_db)
    ])
    summary = importer.import_reviews(args)
    assert summary["status_counts"] == {"eligible_reviewed": 1}
    review = sqlite3.connect(review_db)
    row = review.execute("SELECT target_node_id,target_node_type,status FROM review_finding").fetchone()
    assert row == ("term", "defined_term", "eligible_reviewed")
    review.close()
