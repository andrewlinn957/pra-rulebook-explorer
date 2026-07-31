import importlib.util
import sqlite3
from pathlib import Path


def load_module(name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


stage = load_module("reference_recall_stage_test", "reference_recall_stage.py")
materializer = load_module("apply_reference_recall_stage_test", "apply_reference_recall_stage.py")


def make_db(path: Path, text: str = "See Investment firm.") -> None:
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
        """
    )
    conn.execute("INSERT INTO node VALUES (?,?,?,?,?,?,?)", ("source", "rule", "source", "Source", text, "", "{}"))
    conn.execute("INSERT INTO node VALUES (?,?,?,?,?,?,?)", ("term", "defined_term", "term", "Investment firm", "", "", "{}"))
    conn.commit()
    conn.close()


def test_llm_span_prefers_identifier_inside_broad_evidence():
    quote, spans, evidence = stage.llm_quote_and_spans(
        "Securities firms use the Glossary: ‘Investment firm means any legal person.’",
        "the Glossary: ‘Investment firm means any legal person.’",
        "Investment firm",
        "the Glossary: ‘Investment firm means any legal person.’",
    )
    assert quote == "Investment firm"
    assert spans == [(36, 51)]
    assert evidence.startswith("the Glossary")


def test_defined_term_relationship_is_def_and_uses_defined_term():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE node (id TEXT,node_type TEXT)")
    conn.execute("INSERT INTO node VALUES ('term','defined_term')")
    target = conn.execute("SELECT * FROM node").fetchone()
    assert stage.relationship_for_target(target) == ("DEF", "uses_defined_term")
    assert stage.relationship_for_target(None) == ("REF", "references")


def test_materializer_dry_run_and_apply_write_exact_occurrence(tmp_path):
    db = tmp_path / "rulebook.sqlite3"
    stage_db = tmp_path / "stage.sqlite3"
    make_db(db)
    source_conn = sqlite3.connect(db)
    source_conn.row_factory = sqlite3.Row
    source = source_conn.execute("SELECT * FROM node WHERE id='source'").fetchone()
    target = source_conn.execute("SELECT * FROM node WHERE id='term'").fetchone()
    staged = stage.connect_stage(stage_db)
    stage.insert_stage(
        staged,
        run_id="run-1",
        source=source,
        target=target,
        candidate_id="candidate-1",
        start=4,
        end=19,
        quote="Investment firm",
        candidate_text="Investment firm",
        citation_kind="definition",
        method="llm_resolved_proposed",
        confidence=0.98,
        status="eligible",
        reasons=["test"],
        evidence={},
        relationship_type="DEF",
    )
    staged.commit()
    staged.close()
    source_conn.close()

    args = materializer.build_parser().parse_args(["--db", str(db), "--stage", str(stage_db), "--audit", str(tmp_path / "dry.json")])
    dry = materializer.materialize(args)
    assert dry["applied"] is False
    assert dry["edge_rows_ready"] == 1
    assert dry["occurrence_rows_ready"] == 1

    apply_args = materializer.build_parser().parse_args(["--db", str(db), "--stage", str(stage_db), "--apply", "--audit", str(tmp_path / "apply.json")])
    applied = materializer.materialize(apply_args)
    assert applied["applied"] is True
    conn = sqlite3.connect(db)
    edge = conn.execute("SELECT edge_type,source_method FROM edge WHERE from_node_id='source'").fetchone()
    occurrence = conn.execute("SELECT relationship_type,status,span_start,span_end FROM reference_occurrence").fetchone()
    assert edge == ("uses_defined_term", "reference_recall_stage_v1")
    assert occurrence == ("DEF", "materialized", 4, 19)
    conn.close()


def test_materializer_can_reuse_existing_edge_for_new_occurrence(tmp_path):
    db = tmp_path / "rulebook.sqlite3"
    stage_db = tmp_path / "stage.sqlite3"
    make_db(db, text="See Investment firm and Investment firm.")
    source_conn = sqlite3.connect(db)
    source_conn.row_factory = sqlite3.Row
    source = source_conn.execute("SELECT * FROM node WHERE id='source'").fetchone()
    target = source_conn.execute("SELECT * FROM node WHERE id='term'").fetchone()
    source_conn.execute(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        ("existing-edge", "source", "term", "uses_defined_term", "prior", 0.9, "", "", "{}"),
    )
    source_conn.commit()
    staged = stage.connect_stage(stage_db)
    stage.insert_stage(
        staged,
        run_id="run-1",
        source=source,
        target=target,
        candidate_id="candidate-1",
        start=24,
        end=39,
        quote="Investment firm",
        candidate_text="Investment firm",
        citation_kind="definition",
        method="corpus_defined_term_alias_v1",
        confidence=0.94,
        status="eligible",
        reasons=["test"],
        evidence={},
        relationship_type="DEF",
    )
    staged.commit()
    staged.close()
    source_conn.close()

    args = materializer.build_parser().parse_args(
        [
            "--db",
            str(db),
            "--stage",
            str(stage_db),
            "--allow-existing-edge",
            "--apply",
            "--audit",
            str(tmp_path / "apply.json"),
        ]
    )
    result = materializer.materialize(args)
    assert result["applied"] is True
    assert result["applied_edges"] == 0
    assert result["applied_occurrences"] == 1
    conn = sqlite3.connect(db)
    occurrence = conn.execute(
        "SELECT edge_id,relationship_type,span_start,span_end FROM reference_occurrence"
    ).fetchone()
    assert occurrence == ("existing-edge", "DEF", 24, 39)
    assert conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0] == 1
    conn.close()
