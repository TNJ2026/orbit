"""Refuse to serve a database that mixes legacy and new Runtime tables.

M1A renamed the project database to `runtime.db` while the legacy engine was
still the thing running, so a development-era file can hold both schemas. The
plan is explicit that such a file must not be carried forward: it is deleted
and the Migration Ledger re-runs from empty. This guard is what makes that
non-optional at startup instead of a thing someone remembers to do.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3


# Tables owned by the legacy engine. Their presence proves the file predates
# the cutover, whatever else it also contains.
LEGACY_TABLES = frozenset({
    "agents",
    "messages",
    "tasks",
    "task_transitions",
    "task_runs",
    "workflow_actions",
    "run_jobs",
})


class MixedSchemaError(RuntimeError):
    """The database contains legacy tables and cannot be served."""


def table_names(path: Path | str) -> frozenset[str]:
    """Tables in a SQLite file; empty for a file that does not exist yet."""

    database = Path(path)
    if not database.exists():
        return frozenset()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    return frozenset(name for (name,) in rows if not name.startswith("sqlite_"))


def assert_runtime_schema(path: Path | str) -> frozenset[str]:
    """Return the table set, or raise when legacy tables are present."""

    found = table_names(path)
    legacy = sorted(found & LEGACY_TABLES)
    if legacy:
        raise MixedSchemaError(
            f"{path} contains legacy engine tables ({', '.join(legacy)}). "
            "This file predates the runtime cutover and is not migrated. "
            "Delete it and start again — the Runtime will create a fresh "
            "database and run its migrations from empty."
        )
    return found
