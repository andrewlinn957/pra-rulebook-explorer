import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "review_reference_recall_corpus.py"
spec = importlib.util.spec_from_file_location("review_reference_recall_corpus", SCRIPT_PATH)
reviewer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reviewer)


def test_exact_quote_rejects_stale_candidate_text():
    quote, reasons = reviewer.exact_quote("See Article 26(6).", 4, 17, "Article 26(5)")
    assert quote == "Article 26(6)"
    assert reasons == ["candidate_text_differs_from_live_span"]


def test_legal_citation_without_local_target_is_retained_as_reference():
    decision, target_status, target, confidence, reasons = reviewer.classify_candidate(
        {
            "candidate_kind": "legal_citation",
            "status": "needs_review",
            "candidate_text": "Article 26(6) of the Statutory Audit Regulation",
        },
        {"id": "source", "title": "Audit committee"},
        "See Article 26(6) of the Statutory Audit Regulation.",
        [],
        "",
        0.0,
        set(),
        {},
    )
    assert (decision, target_status, target) == ("REFERENCE", "external_or_unresolved", None)
    assert confidence == 0.9
    assert "legal_or_article_detector_without_unique_local_target" in reasons


def test_unique_local_target_is_eligible_for_materialisation():
    target = {"id": "target", "title": "Paragraph 2.4", "node_type": "rule"}
    decision, target_status, resolved, confidence, reasons = reviewer.classify_candidate(
        {
            "candidate_kind": "structure_reference",
            "status": "needs_review",
            "candidate_text": "paragraph 2.4",
        },
        {"id": "source", "title": "Audit committee"},
        "See paragraph 2.4.",
        [target],
        "exact_structural_title",
        0.93,
        set(),
        {},
    )
    assert decision == "REFERENCE"
    assert target_status == "unique_local_target"
    assert resolved == target
    assert confidence == 0.93
    assert reasons == ["unique_exact_structural_title"]


def make_review_db(path: Path) -> tuple[str, str, int, int]:
    source_path = path / "source.sqlite3"
    ledger_path = path / "ledger.sqlite3"
    text = "The committee must follow paragraph 2.4."
    start = text.index("paragraph 2.4")
    end = start + len("paragraph 2.4")

    source = sqlite3.connect(source_path)
    source.executescript(
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
    source.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        ("source", "rule", "source", "Audit committee", text, "", json.dumps({"part_title": "Test"})),
    )
    source.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        ("target", "rule", "target", "Paragraph 2.4", "", "", json.dumps({"part_title": "Test"})),
    )
    source.commit()
    source.close()

    ledger = sqlite3.connect(ledger_path)
    ledger.executescript(
        """
        CREATE TABLE reference_gap (
          candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scanner_version TEXT NOT NULL,
          source_node_id TEXT NOT NULL, source_node_type TEXT NOT NULL, source_title TEXT NOT NULL,
          source_url TEXT NOT NULL, source_text_hash TEXT NOT NULL, span_start INTEGER, span_end INTEGER,
          candidate_text TEXT NOT NULL, candidate_kind TEXT NOT NULL, detector_json TEXT NOT NULL,
          reason_json TEXT NOT NULL, context_text TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL,
          evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    ledger.execute(
        "INSERT INTO reference_gap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "candidate-1", "ledger-run", "reference-recall-audit-v2", "source", "rule", "Audit committee", "",
            reviewer.digest(text), start, end, "paragraph 2.4", "structure_reference", "[]", "[]", "",
            "needs_review", 90, "{}", "now",
        ),
    )
    ledger.commit()
    ledger.close()
    return str(source_path), str(ledger_path), start, end


def test_run_records_every_candidate_and_stages_unique_target(tmp_path):
    source_path, ledger_path, start, end = make_review_db(tmp_path)
    review_path = tmp_path / "review.sqlite3"
    stage_path = tmp_path / "stage.sqlite3"
    args = reviewer.build_parser().parse_args(
        [
            "--db", source_path,
            "--ledger", ledger_path,
            "--review-db", str(review_path),
            "--stage", str(stage_path),
        ]
    )
    summary = reviewer.run(args)
    assert summary["processed"] == 1
    assert summary["decision_counts"] == {"REFERENCE": 1}
    conn = sqlite3.connect(review_path)
    row = conn.execute(
        "SELECT decision,target_status,target_node_id,span_start,span_end FROM corpus_review"
    ).fetchone()
    conn.close()
    assert row == ("REFERENCE", "unique_local_target", "target", start, end)

    staged = sqlite3.connect(stage_path)
    proposal = staged.execute(
        "SELECT source_node_id,target_node_id,quoted_text,status FROM staged_repair"
    ).fetchone()
    staged.close()
    assert proposal == ("source", "target", "paragraph 2.4", "eligible")


def test_run_holds_candidate_when_source_hash_is_stale(tmp_path):
    source_path, ledger_path, _start, _end = make_review_db(tmp_path)
    ledger = sqlite3.connect(ledger_path)
    ledger.execute("UPDATE reference_gap SET source_text_hash='stale'")
    ledger.commit()
    ledger.close()
    review_path = tmp_path / "review.sqlite3"
    args = reviewer.build_parser().parse_args(
        ["--db", source_path, "--ledger", ledger_path, "--review-db", str(review_path)]
    )
    summary = reviewer.run(args)
    assert summary["decision_counts"] == {"INVALID": 1}
    conn = sqlite3.connect(review_path)
    row = conn.execute("SELECT decision,target_status,reason_json FROM corpus_review").fetchone()
    conn.close()
    assert row[0:2] == ("INVALID", "stale_source_hash")
    assert "source_text_hash_mismatch" in row[2]
