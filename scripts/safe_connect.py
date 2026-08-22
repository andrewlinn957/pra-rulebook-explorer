"""Shared sqlite3 connection helper for standalone scripts.

Ensures foreign_keys=ON, WAL mode, busy timeout and row_factory on every
script connection, matching backend.app.db.configure_connection.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(path, timeout=timeout)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn
