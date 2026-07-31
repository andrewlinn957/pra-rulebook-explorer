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


stage_order = load_module("stage_recommended_reference_order_test", "stage_recommended_reference_order.py")


def make_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
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
          confidence REAL NOT NULL, context_text TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}',
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO node VALUES ('source','rule','source','Source','See SS31/15 and PS24/21 under the CRR.','','{}');
        INSERT INTO node VALUES ('ss','guidance_document','ss','SS31/15 – Internal Governance','','','{}');
        INSERT INTO node VALUES ('crr','defined_term','crr','CRR','means the Capital Requirements Regulation.','','{}');
        """
    )
    conn.commit()
    conn.close()


def make_review_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE corpus_review (
          candidate_id TEXT PRIMARY KEY, source_node_id TEXT, span_start INTEGER,
          span_end INTEGER, candidate_text TEXT, candidate_kind TEXT, source_title TEXT,
          decision TEXT, target_status TEXT, source_text_hash TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO corpus_review VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "source", 4, 23, "SS31/15 and PS24/21", "named_document", "Source", "REFERENCE", "external_or_unresolved", "hash"),
    )
    conn.execute(
        "INSERT INTO corpus_review VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c2", "source", 30, 37, "the CRR", "named_document", "Source", "REFERENCE", "external_or_unresolved", "hash"),
    )
    conn.commit()
    conn.close()


def test_code_and_generic_term_staging_is_exact_and_conservative(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    review_path = tmp_path / "review.sqlite3"
    stage_path = tmp_path / "stage.sqlite3"
    make_source_db(source_path)
    make_review_db(review_path)
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    review = sqlite3.connect(review_path)
    review.row_factory = sqlite3.Row
    staged = stage_order.connect_stage(stage_path)
    nodes = stage_order.row_nodes(source)
    edges = set()
    spans = {}
    code_counts = stage_order.stage_code_aliases(
        source_conn=source,
        review_conn=review,
        stage=staged,
        run_id="test",
        nodes=nodes,
        edges=edges,
        spans=spans,
    )
    term_counts = stage_order.stage_generic_terms(
        review_conn=review,
        stage=staged,
        run_id="test",
        nodes=nodes,
        edges=edges,
        spans=spans,
    )
    assert code_counts["eligible"] == 1
    assert code_counts["held"] == 1
    assert term_counts["eligible"] == 1
    rows = staged.execute(
        "SELECT proposal_method,status,quoted_text,target_node_id,relationship_type FROM staged_repair ORDER BY proposal_method"
    ).fetchall()
    assert [(r["proposal_method"], r["status"], r["quoted_text"], r["target_node_id"], r["relationship_type"]) for r in rows] == [
        ("corpus_defined_term_alias_v1", "eligible", "the CRR", "crr", "DEF"),
        ("corpus_guidance_code_alias_v1", "eligible", "SS31/15", "ss", "REF"),
        ("corpus_guidance_code_alias_v1", "held_unresolved", "PS24/21", "", "REF"),
    ]
    staged.close()
    review.close()
    source.close()
