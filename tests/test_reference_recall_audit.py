import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reference_recall_audit.py"
spec = importlib.util.spec_from_file_location("reference_recall_audit", SCRIPT_PATH)
reference_recall_audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reference_recall_audit)


def make_source_db(path: Path, *, text: str, title: str = "2.4", node_type: str = "rule") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE node (
          id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          stable_key TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          text TEXT DEFAULT '',
          url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE edge (
          id TEXT PRIMARY KEY,
          from_node_id TEXT NOT NULL,
          to_node_id TEXT NOT NULL,
          edge_type TEXT NOT NULL,
          source_method TEXT NOT NULL,
          confidence REAL NOT NULL,
          evidence_text TEXT DEFAULT '',
          source_url TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE reference_occurrence (
          occurrence_id TEXT PRIMARY KEY,
          group_id TEXT NOT NULL,
          source_node_id TEXT NOT NULL,
          target_node_id TEXT,
          edge_id TEXT,
          relationship_type TEXT NOT NULL DEFAULT 'REF',
          citation_kind TEXT NOT NULL,
          citation_text TEXT NOT NULL,
          group_text TEXT NOT NULL,
          instrument_id TEXT,
          provision_path TEXT,
          qualifier TEXT DEFAULT '',
          span_start INTEGER NOT NULL,
          span_end INTEGER NOT NULL,
          status TEXT NOT NULL,
          source_method TEXT NOT NULL,
          confidence REAL NOT NULL,
          context_text TEXT DEFAULT '',
          metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE llm_reference_extraction (
          node_id TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          text_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          response_json TEXT DEFAULT '{}',
          error TEXT DEFAULT ''
        );
        CREATE TABLE llm_reference_resolution (
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
          metadata_json TEXT DEFAULT '{}'
        );
        """
    )
    conn.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        (
            "source",
            node_type,
            "source",
            title,
            text,
            "https://www.prarulebook.co.uk/pra-rules/test#source",
            json.dumps({"part_title": "Test Part", "html_id": "source"}),
        ),
    )
    conn.commit()
    conn.close()


def test_deterministic_candidates_keep_adjacent_named_citations_separate():
    text = (
        "The committee must comply with Article 26(6) of the Statutory Audit Regulation "
        "and paragraph 2.4 of Part 2. See Annexes I and II."
    )
    candidates = reference_recall_audit.build_deterministic_candidates(text)
    values = [candidate["text"] for candidate in candidates]
    assert "Article 26(6) of the Statutory Audit Regulation" in values
    assert "paragraph 2.4 of Part 2" in values
    assert "Annexes I and II" in values


def test_generic_instrument_fragments_are_not_sent_to_review():
    assert reference_recall_audit.generic_named_instrument_label("Rules")
    assert reference_recall_audit.generic_named_instrument_label("Regulations Regulations")
    assert reference_recall_audit.generic_named_instrument_label("This Code")
    assert not reference_recall_audit.generic_named_instrument_label("Bank of England Act 1998")


def test_generic_instrument_candidate_is_excluded_context():
    status, priority, reasons = reference_recall_audit.classify_candidate(
        {"kind": "named_instrument", "start": 0, "end": 5, "text": "Rules"},
        "Rules apply.",
        {"occurrences": [], "edges": [], "llm": []},
        "1.1",
        "guidance_paragraph",
        6000,
    )
    assert status == "excluded_context"
    assert priority == 10
    assert "generic_or_table_instrument_label" in reasons


def test_scan_node_marks_existing_occurrence_as_covered(tmp_path):
    text = "The committee must comply with Article 26(6) of the Statutory Audit Regulation."
    source_path = tmp_path / "source.sqlite3"
    make_source_db(source_path, text=text)
    conn = sqlite3.connect(source_path)
    start = text.index("Article")
    end = start + len("Article 26(6)")
    conn.execute(
        """
        INSERT INTO reference_occurrence(
          occurrence_id,group_id,source_node_id,target_node_id,edge_id,relationship_type,
          citation_kind,citation_text,group_text,span_start,span_end,status,source_method,confidence
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "occ-1",
            "group-1",
            "source",
            "target",
            "edge-1",
            "REF",
            "article",
            "Article 26(6)",
            "Article 26(6)",
            start,
            end,
            "materialized",
            "legal_reference_occurrence_v1",
            0.99,
        ),
    )
    conn.commit()
    conn.close()

    source_conn = reference_recall_audit.connect_source(source_path)
    source = source_conn.execute("SELECT * FROM node").fetchone()
    node, candidates, chunks = reference_recall_audit.scan_node(
        source, source_conn, "run-1", 6000, 800
    )
    assert not chunks
    article = next(candidate for candidate in candidates if "Article 26" in candidate["text"])
    assert article["status"] == "covered_occurrence"
    assert article["evidence"]["occurrences"][0]["id"] == "occ-1"
    assert node["uncovered_candidate_count"] == 0
    source_conn.close()


def test_long_source_creates_overlapping_tail_chunks_and_tail_priority(tmp_path):
    tail = " A reviewer should inspect Article 435 of the UK CRR."
    text = "x" * 6050 + tail
    source_path = tmp_path / "source.sqlite3"
    make_source_db(source_path, text=text, title="Long rule")
    source_conn = reference_recall_audit.connect_source(source_path)
    source = source_conn.execute("SELECT * FROM node").fetchone()
    _, candidates, chunks = reference_recall_audit.scan_node(
        source, source_conn, "run-1", 6000, 800
    )
    article = next(candidate for candidate in candidates if "Article 435" in candidate["text"])
    assert article["status"] == "tail_unreviewed"
    assert article["priority"] == 100
    assert any(chunk["chunk_end"] > 6000 and chunk["reason"].startswith("tail") for chunk in chunks)
    assert any(article["candidate_id"] in chunk["candidate_ids"] for chunk in chunks)
    source_conn.close()


def test_run_is_resumable_by_source_hash(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    ledger_path = tmp_path / "ledger.sqlite3"
    make_source_db(source_path, text="See Article 435 of the UK CRR.")
    args = reference_recall_audit.build_parser().parse_args(
        ["--db", str(source_path), "--ledger", str(ledger_path)]
    )
    first = reference_recall_audit.run(args)
    assert first["scanned"] == 1
    args.resume = True
    second = reference_recall_audit.run(args)
    assert second["skipped_unchanged"] == 1
    assert second.get("scanned", 0) == 0
