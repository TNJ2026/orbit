"""What an Agent CLI printed while it wrote a workflow's DSL.

The same kind of store as ``attempt_output``, for the other place this Runtime
runs somebody else's process: an observation, not an event log, written outside
any kernel transaction so a slow or full disk delays nobody's authoring job.

Worth keeping for the same reason: a CLI that thinks for a minute is otherwise
a black box, and a job that ends ``unknown_external_result`` reports nothing at
all, so its console is the only account of what happened.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Any

from .database import connect_workflow_database


STREAMS = ("stdout", "stderr")
# Per job and per stream. A second, independent bound on what is stored, so a
# chatty CLI cannot grow the database without limit.
DEFAULT_MAX_BYTES = 262_144
# Pipe-draining callbacks must give up quickly when another Runtime component
# owns the write lock. The main workflow connection deliberately waits much
# longer, but diagnostic console output must never backpressure the Agent CLI.
OUTPUT_BUSY_TIMEOUT_MS = 10


class SQLiteAuthoringOutputStore:
    def __init__(self, path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)

    def append(self, *, job_id: str, stream: str, text: str, now: datetime) -> None:
        if stream not in STREAMS:
            raise ValueError(f"unknown output stream: {stream}")
        if not text:
            return
        # mode=rw, never rwc: a console write must attach to a database that
        # already exists. Creating one would resurrect a database another
        # component (or a finished test) has already taken away, and an
        # observation is never worth conjuring storage for.
        uri = self.path.expanduser().absolute().as_uri() + "?mode=rw"
        with sqlite3.connect(
            uri, uri=True, timeout=OUTPUT_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        ) as connection:
            connection.execute(f"PRAGMA busy_timeout = {OUTPUT_BUSY_TIMEOUT_MS}")
            stored = int(connection.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0)"
                " FROM authoring_job_output"
                " WHERE job_id = ? AND stream = ?",
                (str(job_id), stream),
            ).fetchone()[0])
            if stored >= self.max_bytes:
                return
            # Cut the chunk rather than the record: a truncated tail still
            # tells the operator what the CLI was doing when it hit the cap.
            room = self.max_bytes - stored
            tail = text.encode("utf-8")[:room].decode("utf-8", errors="ignore")
            if not tail:
                return
            connection.execute(
                "INSERT INTO authoring_job_output(job_id, stream, text, created_at)"
                " VALUES (?, ?, ?, ?)",
                (str(job_id), stream, tail, now.isoformat()),
            )
            connection.commit()

    def read(
        self, job_id: str, *, after_chunk_id: int = 0, limit: int = 500,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Chunks in the order they were printed, newest cursor last."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT chunk_id, stream, text, created_at FROM authoring_job_output"
                " WHERE job_id = ? AND chunk_id > ? ORDER BY chunk_id LIMIT ?",
                (str(job_id), int(after_chunk_id), int(limit) + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        chunks = [
            {
                "chunk_id": int(row["chunk_id"]),
                "stream": row["stream"],
                "text": row["text"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        next_cursor = chunks[-1]["chunk_id"] if has_more and chunks else None
        return chunks, next_cursor
