"""Connection policy for the new workflow persistence subsystem."""

from __future__ import annotations

import sqlite3
from pathlib import Path


# How long a writer waits for the lock before giving up. Every lease renewal
# competes for it against five workers, the timer loop and recovery, so this is
# also the longest a single renewal can stall — the job lease TTL is sized
# against it (see orbit.workflow.worker.runtime.JOB_LEASE_TTL).
BUSY_TIMEOUT_MS = 30_000


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
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection
