"""Immutable SQLite WorkflowVersion repository and publication transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable

from ..domain.definitions import CompiledWorkflow, WorkflowIR
from ..domain.ir_schema import workflow_ir_from_primitive
from ..domain.serialization import canonical_json, definition_hash
from ..domain.versions import DefinitionHash, Revision
from .database import connect_workflow_database
from .migrations import migrate_workflow_database


class PublishConflictError(RuntimeError):
    def __init__(self, workflow_id: str, expected: int, actual: int) -> None:
        self.workflow_id = workflow_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"workflow {workflow_id} expected latest version {expected}, actual {actual}"
        )


@dataclass(frozen=True)
class WorkflowVersionRecord:
    workflow_id: str
    version: Revision
    definition_hash: DefinitionHash
    dsl_version: str
    ir_version: str
    compiler_version: str
    ir: WorkflowIR
    source_format: str
    source_text: str | None
    catalog_fingerprint: str
    created_at: str
    created_by: str


def merge_workflow_library(source_path: Path | str, library_path: Path | str) -> int:
    """Idempotently merge project definitions into the host-wide library.

    Equal definition hashes collapse. Divergent histories sharing a Workflow
    id are appended as new immutable public versions instead of overwriting
    either project's history.
    """

    source_path, library_path = Path(source_path), Path(library_path)
    if source_path.resolve() == library_path.resolve():
        return 0
    SQLiteWorkflowVersionStore(source_path)
    SQLiteWorkflowVersionStore(library_path)
    with connect_workflow_database(source_path, read_only=True) as source:
        definitions = {
            row["workflow_id"]: dict(row)
            for row in source.execute("SELECT * FROM workflow_definitions")
        }
        versions = [
            dict(row) for row in source.execute(
                "SELECT * FROM workflow_versions ORDER BY workflow_id,version"
            )
        ]
    columns = (
        "workflow_id", "version", "definition_hash", "dsl_version", "ir_version",
        "compiler_version", "canonical_ir_json", "source_format", "source_text",
        "catalog_fingerprint", "created_at", "created_by",
    )
    inserted = 0
    with connect_workflow_database(library_path) as library:
        library.execute("BEGIN IMMEDIATE")
        for row in versions:
            workflow_id = row["workflow_id"]
            if library.execute(
                "SELECT 1 FROM workflow_versions"
                " WHERE workflow_id=? AND definition_hash=?",
                (workflow_id, row["definition_hash"]),
            ).fetchone() is not None:
                continue
            definition = definitions[workflow_id]
            library.execute(
                "INSERT INTO workflow_definitions(workflow_id,name,created_at,created_by)"
                " VALUES (?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET name=excluded.name",
                tuple(definition[key] for key in (
                    "workflow_id", "name", "created_at", "created_by",
                )),
            )
            row["version"] = int(library.execute(
                "SELECT COALESCE(MAX(version),0) FROM workflow_versions"
                " WHERE workflow_id=?", (workflow_id,),
            ).fetchone()[0]) + 1
            library.execute(
                f"INSERT INTO workflow_versions({','.join(columns)})"
                f" VALUES ({','.join('?' for _ in columns)})",
                tuple(row[key] for key in columns),
            )
            inserted += 1
        library.commit()
    return inserted


def archived_in(connection, workflow_id: str) -> bool:
    """Whether this id is retired, asked on a connection the caller already has.

    The predicate belongs to this module — `delete` is what writes the row —
    and it had been hand-copied into the draft service and the authoring job
    service, each opening its own connection to ask. Three spellings of one
    question is three places to keep in step; a workflow that stops counting
    as deleted in only one of them is a Workflow that can be edited but not
    published, or published under an id that is gone.
    """

    return connection.execute(
        "SELECT 1 FROM archived_workflows WHERE workflow_id = ?", (workflow_id,),
    ).fetchone() is not None


def workflow_is_archived(library_path: Path | str, workflow_id: str) -> bool:
    """The same question from a caller that holds a path rather than a store.

    Constructing a `SQLiteWorkflowVersionStore` to ask would run the migration
    ledger, which is not what a read on a hot path should cost.
    """

    with connect_workflow_database(Path(library_path), read_only=True) as connection:
        return archived_in(connection, workflow_id)


class SQLiteWorkflowVersionStore:
    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            migrate_workflow_database(connection)

    def _connect(self) -> sqlite3.Connection:
        return connect_workflow_database(self.path)

    def publish(
        self,
        compiled: CompiledWorkflow,
        *,
        expected_latest_version: int,
        source_format: str,
        source_text: str | None,
        actor: str,
        dsl_version: str = "1.0",
    ) -> WorkflowVersionRecord:
        """Publish an immutable version.

        Content idempotency has precedence over optimistic concurrency: when
        the same workflow/hash already exists, that record is returned even if
        ``expected_latest_version`` is stale. A version conflict is evaluated
        only when publication would create new content.
        """
        if isinstance(expected_latest_version, bool) or expected_latest_version < 0:
            raise ValueError("expected_latest_version must be a non-negative integer")
        if source_format not in {"yaml", "json", "ui"}:
            raise ValueError("source_format must be yaml, json, or ui")
        if not actor.strip():
            raise ValueError("publication actor is required")
        workflow_id = compiled.ir.workflow_id
        now = self._clock().astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        ir_json = canonical_json(compiled.ir)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "SELECT 1 FROM archived_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if deleted is not None:
                raise ValueError(
                    f"workflow id was permanently deleted: {workflow_id}"
                )
            existing = connection.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND definition_hash = ?",
                (workflow_id, compiled.definition_hash.value),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._record(existing)
            latest = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM workflow_versions WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()[0]
            if latest != expected_latest_version:
                connection.rollback()
                raise PublishConflictError(workflow_id, expected_latest_version, latest)
            # The display name is de-duplicated against other workflows so two
            # generated flows never show the same title in the catalog. Only
            # the definitions row (what the catalog reads) is adjusted — the IR
            # name is part of the hashed definition and must stay untouched.
            display_name = self._unique_display_name(
                connection, workflow_id, compiled.ir.name,
            )
            connection.execute(
                """
                INSERT INTO workflow_definitions(
                    workflow_id, name, created_at, created_by
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET name = excluded.name
                """,
                (workflow_id, display_name, now, actor),
            )
            version = latest + 1
            connection.execute(
                """
                INSERT INTO workflow_versions(
                    workflow_id, version, definition_hash, dsl_version, ir_version,
                    compiler_version, canonical_ir_json, source_format, source_text,
                    catalog_fingerprint, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id, version, compiled.definition_hash.value, dsl_version,
                    compiled.ir.ir_version, compiled.compiler_version, ir_json,
                    source_format, source_text, compiled.catalog_fingerprint, now, actor,
                ),
            )
            row = connection.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                (workflow_id, version),
            ).fetchone()
            connection.commit()
            return self._record(row)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _unique_display_name(
        self, connection: sqlite3.Connection, workflow_id: str, name: str,
    ) -> str:
        """The name, or the first ``Name N`` free of other workflows' titles.

        Only names owned by *other* workflow ids count as taken, so a workflow
        keeping or reusing its own title never collides with itself.
        """
        taken = {
            row[0] for row in connection.execute(
                "SELECT name FROM workflow_definitions WHERE workflow_id != ?",
                (workflow_id,),
            )
        }
        if name not in taken:
            return name
        suffix = 2
        while f"{name} {suffix}" in taken:
            suffix += 1
        return f"{name} {suffix}"

    def get(self, workflow_id: str, version: int) -> WorkflowVersionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                (workflow_id, version),
            ).fetchone()
        return None if row is None else self._record(row)

    def latest_version(self, workflow_id: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM workflow_versions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()[0]
            )

    def is_archived(self, workflow_id: str) -> bool:
        """Has this id been retired?

        `get` deliberately still answers for an archived id: a run started
        before the deletion has to be able to read the definition it is
        executing. Starting a *new* run is the case that must be refused, and
        that caller asks this.
        """

        with self._connect() as connection:
            return archived_in(connection, workflow_id)

    def delete(self, workflow_id: str, *, expected_latest_version: int) -> None:
        """Permanently retire an id while retaining versions for old runs."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT x.workflow_id AS deleted_id,"
                " COALESCE(MAX(v.version), 0) AS latest"
                " FROM workflow_definitions d"
                " LEFT JOIN workflow_versions v ON v.workflow_id=d.workflow_id"
                " LEFT JOIN archived_workflows x ON x.workflow_id=d.workflow_id"
                " WHERE d.workflow_id=? GROUP BY d.workflow_id,x.workflow_id",
                (workflow_id,),
            ).fetchone()
            if row is None or row["deleted_id"] is not None:
                raise ValueError(f"workflow not found: {workflow_id}")
            actual = int(row["latest"])
            if actual != expected_latest_version:
                raise PublishConflictError(workflow_id, expected_latest_version, actual)
            connection.execute(
                "INSERT INTO archived_workflows(workflow_id,archived_at) VALUES (?,?)",
                (
                    workflow_id,
                    self._clock().astimezone(timezone.utc).isoformat(
                        timespec="microseconds"
                    ).replace("+00:00", "Z"),
                ),
            )
            connection.commit()

    @staticmethod
    def _record(row: sqlite3.Row) -> WorkflowVersionRecord:
        primitive = json.loads(row["canonical_ir_json"])
        ir = workflow_ir_from_primitive(primitive)
        stored_hash = DefinitionHash(row["definition_hash"])
        if definition_hash(ir) != stored_hash:
            raise ValueError(
                f"stored WorkflowVersion {row['workflow_id']}@{row['version']} has an invalid hash"
            )
        return WorkflowVersionRecord(
            workflow_id=row["workflow_id"],
            version=Revision(row["version"]),
            definition_hash=stored_hash,
            dsl_version=row["dsl_version"],
            ir_version=row["ir_version"],
            compiler_version=row["compiler_version"],
            ir=ir,
            source_format=row["source_format"],
            source_text=row["source_text"],
            catalog_fingerprint=row["catalog_fingerprint"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )
