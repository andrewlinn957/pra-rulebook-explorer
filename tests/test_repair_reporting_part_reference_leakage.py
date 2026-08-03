import sqlite3
import unittest

from scripts.repair_reporting_part_reference_leakage import (
    count_reporting_part_standard_formula_leaks,
    repair_reporting_part_standard_formula_leaks,
)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE node (
          id TEXT PRIMARY KEY,
          node_type TEXT,
          stable_key TEXT,
          title TEXT,
          text TEXT,
          url TEXT,
          metadata_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edge (
          id TEXT PRIMARY KEY,
          from_node_id TEXT,
          to_node_id TEXT,
          edge_type TEXT,
          source_method TEXT,
          confidence REAL,
          evidence_text TEXT,
          source_url TEXT,
          metadata_json TEXT
        )
        """
    )
    conn.execute(
        """
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
          metadata_json TEXT DEFAULT '{}',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def add_node(conn, node_id, title, url):
    conn.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        (node_id, "rule", f"stable:{node_id}", title, "", url, "{}"),
    )


def add_edge_with_occurrence(conn, edge_id, source_id, target_id, context):
    conn.execute(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        (
            edge_id,
            source_id,
            target_id,
            "references",
            "resolution_policy_v1",
            0.93,
            context,
            "",
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO reference_occurrence(
          occurrence_id, group_id, source_node_id, target_node_id, edge_id,
          citation_kind, citation_text, group_text, span_start, span_end,
          status, source_method, confidence, context_text
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"occ-{edge_id}",
            f"group-{edge_id}",
            source_id,
            target_id,
            edge_id,
            "rule",
            "3.4",
            "3.4",
            10,
            13,
            "materialized",
            "resolution_policy_v1",
            0.93,
            context,
        ),
    )


def test_repairs_only_reporting_part_references_that_leak_to_standard_formula():
    conn = make_conn()
    add_node(conn, "source", "2.2", "https://www.prarulebook.co.uk/pra-rules/external-audit/01-06-2026#rule-2-2")
    add_node(conn, "other-source", "5.1", "https://www.prarulebook.co.uk/pra-rules/other/01-06-2026#rule-5-1")
    add_node(conn, "reporting", "3.4", "https://www.prarulebook.co.uk/pra-rules/reporting/01-06-2026#rule-3-4")
    add_node(
        conn,
        "standard-formula",
        "3.4",
        "https://www.prarulebook.co.uk/pra-rules/solvency-capital-requirement---standard-formula/01-06-2026#rule-3-4",
    )
    add_node(
        conn,
        "real-standard-formula",
        "3.4",
        "https://www.prarulebook.co.uk/pra-rules/solvency-capital-requirement---standard-formula/01-06-2026#real",
    )
    add_edge_with_occurrence(
        conn,
        "bad",
        "source",
        "standard-formula",
        "The firm discloses pursuant to Reporting 3.4 of the Reporting Part of the PRA Rulebook.",
    )
    add_edge_with_occurrence(
        conn,
        "bad-self-reference",
        "source",
        "standard-formula",
        "Where the information in 2.2(1) and 2.2(2) derives from the SCR.",
    )
    add_edge_with_occurrence(
        conn,
        "good-reporting",
        "source",
        "reporting",
        "The firm discloses pursuant to Reporting 3.4 of the Reporting Part of the PRA Rulebook.",
    )
    add_edge_with_occurrence(
        conn,
        "good-standard-formula",
        "other-source",
        "real-standard-formula",
        "The firm calculates the SCR under the standard formula.",
    )

    assert count_reporting_part_standard_formula_leaks(conn) == 2

    result = repair_reporting_part_standard_formula_leaks(conn)

    assert result == {"bad_occurrences_marked": 2, "bad_edges_deleted": 2}
    assert count_reporting_part_standard_formula_leaks(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='bad'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='bad-self-reference'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='good-reporting'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM edge WHERE id='good-standard-formula'").fetchone()[0] == 1
    bad_occurrence = conn.execute(
        "SELECT status,target_node_id,edge_id FROM reference_occurrence WHERE occurrence_id='occ-bad'"
    ).fetchone()
    assert bad_occurrence == ("not_reference", None, None)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
