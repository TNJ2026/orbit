"""SQLite repositories for immutable Values, Artifacts, and lineage."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from ..domain.data import (
    ArtifactLink, ArtifactLinkType, ArtifactMetadata, ArtifactStatus,
    ArtifactVisibility, DataOwnerKind, ValueLink, ValueLinkType, ValueRecord,
)
from ..domain.ids import EntityId
from ..domain.persistence import IntegrityViolationError, RepositoryAlreadyExistsError
from ..domain.serialization import canonical_json
from ..domain.versions import DefinitionHash


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def value_record_from_row(row) -> ValueRecord:
    return ValueRecord(
        EntityId.parse(row["value_id"]), EntityId.parse(row["run_id"]),
        DataOwnerKind(row["owner_kind"]), EntityId.parse(row["owner_id"]),
        row["port_id"], row["schema_id"], json.loads(row["data_json"]),
        DefinitionHash(row["checksum"]), row["size_bytes"],
        EntityId.parse(row["created_event_id"]), _datetime(row["created_at"]),
    )


def value_link_from_row(row) -> ValueLink:
    return ValueLink(
        EntityId.parse(row["link_id"]), EntityId.parse(row["run_id"]),
        EntityId.parse(row["source_value_id"]), EntityId.parse(row["target_value_id"]),
        ValueLinkType(row["link_type"]),
        None if row["mapping_hash"] is None else DefinitionHash(row["mapping_hash"]),
        EntityId.parse(row["created_event_id"]), _datetime(row["created_at"]),
    )


def artifact_from_row(row) -> ArtifactMetadata:
    return ArtifactMetadata(
        EntityId.parse(row["artifact_id"]), EntityId.parse(row["run_id"]),
        EntityId.parse(row["workflow_id"]), row["producer_type"],
        EntityId.parse(row["producer_id"]),
        None if row["producer_node_run_id"] is None else EntityId.parse(row["producer_node_run_id"]),
        row["output_port_id"], row["schema_id"], row["content_type"],
        DefinitionHash(row["checksum"]), row["size_bytes"], row["blob_key"],
        ArtifactVisibility(row["visibility"]), EntityId.parse(row["scope_id"]),
        ArtifactStatus(row["status"]), _datetime(row["created_at"]),
        _datetime(row["committed_at"]),
        None if row["created_event_id"] is None else EntityId.parse(row["created_event_id"]),
    )


def artifact_link_from_row(row) -> ArtifactLink:
    return ArtifactLink(
        EntityId.parse(row["link_id"]), EntityId.parse(row["workflow_id"]),
        EntityId.parse(row["run_id"]), EntityId.parse(row["artifact_id"]),
        ArtifactLinkType(row["link_type"]), EntityId.parse(row["target_id"]),
        EntityId.parse(row["created_event_id"]), _datetime(row["created_at"]),
    )


class _SQLiteDataRepository:
    def __init__(self, connection: sqlite3.Connection, *, fault_hook=None) -> None:
        self.connection = connection
        self.fault_hook = fault_hook

    def _write(self, point, sql, parameters):
        if not self.connection.in_transaction:
            raise RuntimeError("data write requires an active UnitOfWork")
        if self.fault_hook is not None:
            self.fault_hook(point)
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error) or "PRIMARY" in str(error):
                raise RepositoryAlreadyExistsError(str(error)) from None
            raise IntegrityViolationError(str(error)) from None


class SQLiteValueRepository(_SQLiteDataRepository):
    def insert(self, record: ValueRecord) -> None:
        self._write("before_value_insert", """
            INSERT INTO "values" (
                value_id, run_id, owner_kind, owner_id, port_id, schema_id,
                data_json, checksum, size_bytes, created_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(record.value_id), str(record.run_id), record.owner_kind.value,
            str(record.owner_id), record.port_id, record.schema_id,
            canonical_json(record.data), record.checksum.value, record.size_bytes,
            str(record.created_event_id), _time(record.created_at),
        ))

    def get(self, value_id):
        row = self.connection.execute(
            "SELECT * FROM \"values\" WHERE value_id = ?", (str(value_id),)
        ).fetchone()
        return None if row is None else value_record_from_row(row)

    def get_by_owner_port(self, owner_kind, owner_id, port_id):
        kind = getattr(owner_kind, "value", owner_kind)
        row = self.connection.execute(
            "SELECT * FROM \"values\" WHERE owner_kind = ? AND owner_id = ? AND port_id = ?",
            (kind, str(owner_id), port_id),
        ).fetchone()
        return None if row is None else value_record_from_row(row)

    def list_by_owner(self, owner_kind, owner_id):
        kind = getattr(owner_kind, "value", owner_kind)
        rows = self.connection.execute(
            "SELECT * FROM \"values\" WHERE owner_kind = ? AND owner_id = ? ORDER BY port_id",
            (kind, str(owner_id)),
        ).fetchall()
        return tuple(value_record_from_row(row) for row in rows)


class SQLiteValueLinkRepository(_SQLiteDataRepository):
    def insert(self, record: ValueLink) -> None:
        self._write("before_value_link_insert", """
            INSERT INTO value_links (
                link_id, run_id, source_value_id, target_value_id, link_type,
                mapping_hash, created_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(record.link_id), str(record.run_id), str(record.source_value_id),
            str(record.target_value_id), record.link_type.value,
            None if record.mapping_hash is None else record.mapping_hash.value,
            str(record.created_event_id), _time(record.created_at),
        ))

    def list_for_value(self, value_id, *, direction="both"):
        if direction not in {"upstream", "downstream", "both"}:
            raise ValueError("invalid lineage direction")
        clauses, parameters = [], []
        if direction in {"upstream", "both"}:
            clauses.append("target_value_id = ?"); parameters.append(str(value_id))
        if direction in {"downstream", "both"}:
            clauses.append("source_value_id = ?"); parameters.append(str(value_id))
        rows = self.connection.execute(
            "SELECT * FROM value_links WHERE " + " OR ".join(clauses) + " ORDER BY link_id",
            tuple(parameters),
        ).fetchall()
        return tuple(value_link_from_row(row) for row in rows)


class SQLiteArtifactRepository(_SQLiteDataRepository):
    def stage(self, record: ArtifactMetadata) -> None:
        if record.status is not ArtifactStatus.STAGED:
            raise ValueError("stage requires staged Artifact metadata")
        self._write("before_artifact_stage", """
            INSERT INTO artifacts (
                artifact_id, run_id, workflow_id, producer_type, producer_id,
                producer_node_run_id, output_port_id, schema_id, content_type,
                checksum, size_bytes, blob_key, visibility, scope_id, status,
                created_at, committed_at, created_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(record.artifact_id), str(record.run_id), str(record.workflow_id),
            record.producer_type, str(record.producer_id),
            None if record.producer_node_run_id is None else str(record.producer_node_run_id),
            record.output_port_id, record.schema_id, record.content_type,
            record.checksum.value, record.size_bytes, record.blob_key,
            record.visibility.value, str(record.scope_id), record.status.value,
            _time(record.created_at), None, None,
        ))

    def get(self, artifact_id, *, committed_only=False):
        sql = "SELECT * FROM artifacts WHERE artifact_id = ?"
        parameters = [str(artifact_id)]
        if committed_only:
            sql += " AND status = 'committed'"
        row = self.connection.execute(sql, tuple(parameters)).fetchone()
        return None if row is None else artifact_from_row(row)

    def commit(self, record: ArtifactMetadata) -> None:
        if record.status is not ArtifactStatus.COMMITTED:
            raise ValueError("commit requires committed Artifact metadata")
        prior = self.get(record.artifact_id)
        if prior is None or prior.status is not ArtifactStatus.STAGED:
            raise IntegrityViolationError("Artifact is not staged")
        immutable = (
            "run_id", "workflow_id", "producer_type", "producer_id",
            "producer_node_run_id", "output_port_id", "schema_id", "content_type",
            "checksum", "size_bytes", "blob_key", "visibility", "scope_id", "created_at",
        )
        if any(getattr(prior, field) != getattr(record, field) for field in immutable):
            raise IntegrityViolationError("committed Artifact metadata differs from staged metadata")
        cursor = self._write("before_artifact_commit", """
            UPDATE artifacts SET status = 'committed', committed_at = ?, created_event_id = ?
            WHERE artifact_id = ? AND status = 'staged'
        """, (_time(record.committed_at), str(record.created_event_id), str(record.artifact_id)))
        if cursor.rowcount != 1:
            raise IntegrityViolationError("Artifact stage was concurrently changed")

    def abandon(self, artifact_id) -> None:
        cursor = self._write(
            "before_artifact_abandon",
            "UPDATE artifacts SET status = 'abandoned' WHERE artifact_id = ? AND status = 'staged'",
            (str(artifact_id),),
        )
        if cursor.rowcount != 1:
            raise IntegrityViolationError("only staged Artifact can be abandoned")

    def list_by_run(self, run_id, *, status=None):
        sql, parameters = "SELECT * FROM artifacts WHERE run_id = ?", [str(run_id)]
        if status is not None:
            sql += " AND status = ?"; parameters.append(getattr(status, "value", status))
        sql += " ORDER BY artifact_id"
        return tuple(artifact_from_row(row) for row in self.connection.execute(sql, tuple(parameters)))

    def list_staged_before(self, before, *, limit=100):
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE status = 'staged' AND created_at < ? ORDER BY created_at, artifact_id LIMIT ?",
            (_time(before), limit),
        ).fetchall()
        return tuple(artifact_from_row(row) for row in rows)

    def committed_blob_keys(self):
        return frozenset(
            row[0] for row in self.connection.execute(
                "SELECT DISTINCT blob_key FROM artifacts WHERE status = 'committed'"
            )
        )

    def retained_blob_keys(self):
        return frozenset(
            row[0] for row in self.connection.execute(
                "SELECT DISTINCT blob_key FROM artifacts WHERE status IN ('staged', 'committed')"
            )
        )

    def list_all(self, *, limit=1000, after_artifact_id=""):
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id > ? ORDER BY artifact_id LIMIT ?",
            (after_artifact_id, limit),
        ).fetchall()
        return tuple(artifact_from_row(row) for row in rows)


class SQLiteArtifactLinkRepository(_SQLiteDataRepository):
    def insert(self, record: ArtifactLink) -> None:
        artifact = self.connection.execute(
            "SELECT workflow_id FROM artifacts WHERE artifact_id = ?",
            (str(record.artifact_id),),
        ).fetchone()
        if artifact is None or artifact[0] != str(record.workflow_id):
            raise IntegrityViolationError("Artifact Link crosses Workflow boundary")
        self._write("before_artifact_link_insert", """
            INSERT INTO artifact_links (
                link_id, workflow_id, run_id, artifact_id, link_type,
                target_id, created_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(record.link_id), str(record.workflow_id), str(record.run_id),
            str(record.artifact_id), record.link_type.value, str(record.target_id),
            str(record.created_event_id), _time(record.created_at),
        ))

    def list_for_artifact(self, artifact_id, *, link_type=None):
        sql, parameters = "SELECT * FROM artifact_links WHERE artifact_id = ?", [str(artifact_id)]
        if link_type is not None:
            sql += " AND link_type = ?"; parameters.append(getattr(link_type, "value", link_type))
        sql += " ORDER BY link_id"
        return tuple(artifact_link_from_row(row) for row in self.connection.execute(sql, tuple(parameters)))

    def list_for_target(self, target_id):
        rows = self.connection.execute(
            "SELECT * FROM artifact_links WHERE target_id = ? ORDER BY link_id",
            (str(target_id),),
        ).fetchall()
        return tuple(artifact_link_from_row(row) for row in rows)
