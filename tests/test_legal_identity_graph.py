import json
import sqlite3

from backend.app.graph import _load_nx_graph, contents_tree, reader_bundle
from backend.app.unified import unified_node


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE node(
          id TEXT PRIMARY KEY,node_type TEXT NOT NULL,stable_key TEXT NOT NULL,
          title TEXT NOT NULL,text TEXT DEFAULT '',url TEXT DEFAULT '',metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE edge(
          id TEXT PRIMARY KEY,from_node_id TEXT NOT NULL,to_node_id TEXT NOT NULL,
          edge_type TEXT NOT NULL,source_method TEXT NOT NULL,confidence REAL NOT NULL,
          evidence_text TEXT DEFAULT '',source_url TEXT DEFAULT '',metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE canonical_node(id TEXT PRIMARY KEY,is_canonical INTEGER,canonical_reason TEXT);
        """
    )
    return conn


def add_node(conn, node_id, node_type, stable_key, title, text="", metadata=None):
    conn.execute(
        "INSERT INTO node VALUES (?,?,?,?,?,?,?)",
        (node_id, node_type, stable_key, title, text, "", json.dumps(metadata or {})),
    )
    conn.execute("INSERT INTO canonical_node VALUES (?,?,?)", (node_id, 1, "canonical"))


def test_analysis_graph_collapses_versions_to_one_canonical_entity() -> None:
    conn = make_db()
    add_node(conn, "canonical", "provision", "provision:pra-rules/test:chapter:2:rule-1:1", "Article 2(1)", metadata={"identity_type": "canonical_provision"})
    add_node(conn, "version-june", "rule", "provision_version:pra-rules/test:chapter:2:rule-1:1:01-06-2026", "Article 2(1)", "June", {"identity_type": "provision_version", "canonical_provision_id": "canonical"})
    add_node(conn, "version-july", "rule", "provision_version:pra-rules/test:chapter:2:rule-1:1:01-07-2026", "Article 2(1)", "July", {"identity_type": "provision_version", "canonical_provision_id": "canonical"})
    add_node(conn, "other", "rule", "provision:pra-rules/other:chapter:1:rule-1:1", "Other")
    conn.executemany(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("has-june", "canonical", "version-june", "has_version", "legal_identity", 1, "", "", "{}"),
            ("has-july", "canonical", "version-july", "has_version", "legal_identity", 1, "", "", "{}"),
            ("ref-june", "version-june", "other", "references", "test", 1, "", "", "{}"),
            ("ref-july", "version-july", "other", "references", "test", 1, "", "", "{}"),
        ],
    )

    graph = _load_nx_graph(conn, analysis=True)

    assert "canonical" in graph.nodes
    assert "version-june" not in graph.nodes
    assert "version-july" not in graph.nodes
    assert graph.has_edge("canonical", "other")
    assert not graph.has_edge("canonical", "canonical")


def test_reader_remains_version_oriented_and_exposes_identity_metadata() -> None:
    conn = make_db()
    add_node(conn, "part", "part", "part:pra-rules/test/01-06-2026", "Test Part", metadata={"identity_type": "source_page", "snapshot_id": "snapshot:1"})
    add_node(conn, "chapter", "chapter", "chapter:part:test:2", "Article 2")
    add_node(
        conn,
        "version",
        "rule",
        "provision_version:test:chapter:2:rule-1:1:01-06-2026",
        "Article 2(1)",
        "Dated version text",
        {
            "identity_type": "provision_version",
            "canonical_provision_id": "canonical",
            "rulebook_date": "01-06-2026",
            "source_page_id": "part",
            "snapshot_id": "snapshot:1",
        },
    )
    add_node(conn, "canonical", "provision", "provision:test:chapter:2:rule-1:1", "Article 2(1)", metadata={"identity_type": "canonical_provision"})
    add_node(
        conn,
        "target-canonical",
        "provision",
        "provision:test:chapter:3:rule-2:2",
        "Article 3(2)",
        metadata={"identity_type": "canonical_provision"},
    )
    add_node(
        conn,
        "target-version",
        "rule",
        "provision_version:test:chapter:3:rule-2:2:01-06-2026",
        "Article 3(2)",
        "Target dated version text",
        metadata={
            "identity_type": "provision_version",
            "canonical_provision_id": "target-canonical",
            "rulebook_date": "01-06-2026",
            "source_page_id": "part",
            "snapshot_id": "snapshot:1",
        },
    )
    conn.executemany(
        "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("part-chapter", "part", "chapter", "contains", "site_structure", 1, "", "", "{}"),
            ("chapter-version", "chapter", "version", "contains", "site_structure", 1, "", "", "{}"),
            ("has-version", "canonical", "version", "has_version", "legal_identity", 1, "", "", "{}"),
            ("target-has-version", "target-canonical", "target-version", "has_version", "legal_identity", 1, "", "", "{}"),
            ("version-reference", "version", "target-canonical", "references", "test", 1, "Article 3(2)", "", "{}"),
        ],
    )

    tree = contents_tree(conn, "part")
    canonical_tree = contents_tree(conn, "canonical")
    bundle = reader_bundle(conn, "part")

    assert tree["children"][0]["children"][0]["id"] == "version"
    assert canonical_tree["children"][0]["id"] == "version"
    assert bundle["graph"]["nodes"]
    version = next(node for node in bundle["graph"]["nodes"] if node["id"] == "version")
    assert version["text"] == "Dated version text"
    assert version["identity"]["canonical_provision_id"] == "canonical"
    assert version["identity"]["version_date"] == "01-06-2026"
    target = next(node for node in bundle["graph"]["nodes"] if node["id"] == "target-canonical")
    assert target["text"] == "Target dated version text"
    assert target["metadata"]["resolved_version_id"] == "target-version"


def test_unified_node_returns_identity_projection() -> None:
    conn = make_db()
    add_node(conn, "version", "rule", "provision_version:test:1:01-06-2026", "1", metadata={"identity_type": "provision_version", "canonical_provision_id": "canonical", "rulebook_date": "01-06-2026", "source_page_id": "part", "snapshot_id": "snapshot:1"})
    row = conn.execute("SELECT id,node_type,stable_key,title,text,url,metadata_json FROM node WHERE id='version'").fetchone()

    result = unified_node(conn, "version", source="rulebook")

    assert result["identity"]["canonical_provision_id"] == "canonical"
    assert result["identity"]["source_page_id"] == "part"
    assert result["properties"]["snapshot_id"] == "snapshot:1"
