import sqlite3

import pytest

from scripts.safe_connect import connect


def test_readonly_connection_accepts_a_path_and_rejects_writes(tmp_path):
    db = tmp_path / "source.sqlite3"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE sample(value TEXT NOT NULL)")
    raw.execute("INSERT INTO sample VALUES ('ok')")
    raw.commit()
    raw.close()

    conn = connect(db, readonly=True, timeout=60)
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("SELECT value FROM sample").fetchone()["value"] == "ok"
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO sample VALUES ('blocked')")
    conn.close()
