"""Connection policy for the new workflow persistence subsystem."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_workflow_database(
    path: Path | str,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    raw_path = str(path)
    if read_only:
        connection = sqlite3.connect(
            f"file:{Path(raw_path).resolve()}?mode=ro",
            uri=True,
            timeout=30.0,
            isolation_level=None,
        )
    else:
        connection = sqlite3.connect(raw_path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection
