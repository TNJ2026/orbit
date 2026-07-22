"""What a Handler's process printed, kept for the operator to read.

This is the one store in the Runtime that is not an event log. A subprocess's
console is an observation: non-deterministic, possibly truncated, and never
something a replay or a reducer may read. It is written outside the kernel
transaction on purpose — a full disk or a slow write must delay nobody's run
and must never fail an attempt that actually succeeded.

Two things make it worth keeping anyway: an Agent that runs for minutes is
otherwise a black box, and an attempt that ends `unknown_external_result`
reports nothing at all, so its console is the only account of what happened.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ..domain.ids import EntityId
from .database import connect_workflow_database


STREAMS = ("stdout", "stderr")
# Per attempt and per stream. The Agent clients already bound what they read
# from the pipes; this is the second, independent bound on what is stored, so
# a chatty CLI cannot grow the database without limit.
DEFAULT_MAX_BYTES = 262_144


class SQLiteAttemptOutputStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        run_id: EntityId,
        node_run_id: EntityId,
        attempt_id: EntityId,
        stream: str,
        text: str,
        now: datetime,
    ) -> None:
        if stream not in STREAMS:
            raise ValueError(f"unknown output stream: {stream}")
        if not text:
            return
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                "INSERT INTO attempt_output(run_id, node_run_id, attempt_id,"
                " stream, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(run_id), str(node_run_id), str(attempt_id), stream,
                    text, now.isoformat(),
                ),
            )
            connection.commit()

    def read(
        self, run_id: EntityId | str, *, after_chunk_id: int = 0, limit: int = 500,
        node_run_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Chunks in the order they were printed, newest cursor last."""

        clauses = ["run_id = ?", "chunk_id > ?"]
        parameters: list[Any] = [str(run_id), int(after_chunk_id)]
        if node_run_id:
            clauses.append("node_run_id = ?")
            parameters.append(node_run_id)
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT chunk_id, node_run_id, attempt_id, stream, text, created_at"
                f" FROM attempt_output WHERE {' AND '.join(clauses)}"
                " ORDER BY chunk_id LIMIT ?",
                (*parameters, int(limit) + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        chunks = [
            {
                "chunk_id": int(row["chunk_id"]),
                "node_run_id": row["node_run_id"],
                "attempt_id": row["attempt_id"],
                "stream": row["stream"],
                "text": row["text"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        next_cursor = chunks[-1]["chunk_id"] if has_more and chunks else None
        return chunks, next_cursor

    def stored_bytes(self, attempt_id: EntityId | str, stream: str) -> int:
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(LENGTH(text)), 0) AS total FROM attempt_output"
                " WHERE attempt_id = ? AND stream = ?",
                (str(attempt_id), stream),
            ).fetchone()
        return int(row["total"])


class AttemptOutputSink:
    """The port a Handler sees: one attempt, bounded, never raising.

    A Handler that cannot print is still a Handler that ran. Every failure
    here is swallowed deliberately — the alternative is failing a real attempt
    because its console could not be saved.
    """

    def __init__(
        self, store: SQLiteAttemptOutputStore, request, *, clock,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.store = store
        self.request = request
        self.clock = clock
        self.max_bytes = max_bytes
        self._written = {stream: 0 for stream in STREAMS}

    def emit(self, stream: str, text: str) -> None:
        if stream not in STREAMS or not text:
            return
        remaining = self.max_bytes - self._written[stream]
        if remaining <= 0:
            return
        encoded = text.encode("utf-8")
        truncated = False
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            truncated = True
        self._written[stream] += len(text.encode("utf-8"))
        try:
            self.store.append(
                run_id=self.request.run_id,
                node_run_id=self.request.node_run_id,
                attempt_id=self.request.attempt_id,
                stream=stream, text=text, now=self.clock(),
            )
            if truncated:
                self._written[stream] = self.max_bytes
                self.store.append(
                    run_id=self.request.run_id,
                    node_run_id=self.request.node_run_id,
                    attempt_id=self.request.attempt_id,
                    stream=stream,
                    text=f"\n… output truncated at {self.max_bytes} bytes\n",
                    now=self.clock(),
                )
        except Exception:  # noqa: BLE001 - see the class docstring
            return


def attempt_output_sink_factory(
    path: Path | str, *, clock, max_bytes: int = DEFAULT_MAX_BYTES
):
    """Bind a store to the executor's per-request context construction."""

    store = SQLiteAttemptOutputStore(path)

    def build(request) -> AttemptOutputSink:
        return AttemptOutputSink(store, request, clock=clock, max_bytes=max_bytes)

    return build


def stream_names() -> Sequence[str]:
    return STREAMS
