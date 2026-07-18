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
            connection.execute(
                """
                INSERT INTO workflow_definitions(
                    workflow_id, name, created_at, created_by
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET name = excluded.name
                """,
                (workflow_id, compiled.ir.name, now, actor),
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
