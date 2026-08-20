import sqlite3

from backend.app.legal_identity_migration import migrate_legal_identity
from scripts.audit_legal_identity import audit_legal_identity

from test_legal_identity_migration import make_db, seed_db


def test_audit_passes_for_a_migrated_corpus() -> None:
    conn = make_db()
    seed_db(conn)
    migrate_legal_identity(conn)

    report = audit_legal_identity(conn)

    assert report["ok"] is True
    assert report["metrics"]["legacy_dated_rule_keys"] == 0
    assert report["metrics"]["canonical_provisions"] == 2
    assert report["metrics"]["provision_versions"] == 2
    assert report["metrics"]["source_pages"] == 1
    assert report["metrics"]["snapshots"] == 1
    assert all(value == 0 for key, value in report["failures"].items())


def test_audit_reports_unmigrated_dated_rule_and_missing_provenance() -> None:
    conn = make_db()
    seed_db(conn)
    conn.execute("DELETE FROM document_source")

    report = audit_legal_identity(conn)

    assert report["ok"] is False
    assert report["metrics"]["legacy_dated_rule_keys"] == 2
    assert report["failures"]["legacy_dated_rule_keys"] == 2
    assert report["failures"]["versions_missing_canonical"] == 0
    assert report["failures"]["versions_missing_source_page"] == 0
    assert report["failures"]["versions_missing_snapshot"] == 0
