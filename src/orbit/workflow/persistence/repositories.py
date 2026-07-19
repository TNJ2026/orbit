"""SQLite projection repositories; all writes require an active UnitOfWork."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from ..domain.ids import EntityId
from ..domain.persistence import (
    AttemptRecord,
    BranchTokenRecord,
    ConcurrencyConflictError,
    ExecutionPlanRecord,
    IntegrityViolationError,
    NodeRunRecord,
    RepositoryAlreadyExistsError,
    WorkflowRunRecord,
)
from ..domain.serialization import canonical_json, definition_hash, to_primitive
from ..domain.states import AttemptStatus, BranchTokenStatus, NodeRunStatus, WorkflowRunStatus
from ..domain.versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _time(value: datetime) -> str:
    return to_primitive(value)


class _Repository:
    def __init__(self, connection: sqlite3.Connection, events, *, fault_hook=None) -> None:
        self.connection = connection
        self.events = events
        self.fault_hook = fault_hook

    def _write(self, point: str) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("projection write requires an active UnitOfWork")
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _verify_head(self, aggregate_id: EntityId, version: AggregateVersion) -> None:
        actual = self.events.stream_head(aggregate_id)
        if actual != version:
            raise IntegrityViolationError(
                f"projection {aggregate_id} version {version.value} does not match stream head {actual.value}"
            )

    def _ensure_absent(self, table: str, column: str, identifier: EntityId) -> None:
        if self.connection.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ?", (str(identifier),)
        ).fetchone() is not None:
            raise RepositoryAlreadyExistsError(str(identifier))

    def _conflict(
        self,
        aggregate_id: EntityId,
        expected: AggregateVersion,
        cursor,
        table: str,
        id_column: str,
    ) -> None:
        if cursor.rowcount != 1:
            row = self.connection.execute(
                f"SELECT aggregate_version FROM {table} WHERE {id_column} = ?",
                (str(aggregate_id),),
            ).fetchone()
            actual = -1 if row is None else int(row[0])
            raise ConcurrencyConflictError(aggregate_id, expected.value, actual)


class SQLiteWorkflowRunRepository(_Repository):
    def create(self, record: WorkflowRunRecord) -> None:
        self._write("before_run_create")
        self._ensure_absent("workflow_runs", "run_id", record.run_id)
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new WorkflowRun projection must start at version 0")
        self.connection.execute(
            """
            INSERT INTO workflow_runs(
                run_id, workflow_id, workflow_version, definition_hash, status,
                aggregate_version, correlation_id, created_at, updated_at, goal,
                display_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.run_id), str(record.workflow_id), record.workflow_version.value,
                record.definition_hash.value, record.status.value,
                record.aggregate_version.value, str(record.correlation_id),
                _time(record.created_at), _time(record.updated_at),
                record.goal, record.display_name or str(record.run_id),
            ),
        )
        self._write("after_run_create")

    def get(self, run_id: EntityId) -> WorkflowRunRecord | None:
        row = self.connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (str(run_id),)
        ).fetchone()
        if row is None:
            return None
        return WorkflowRunRecord(
            EntityId.parse(row["run_id"]), EntityId.parse(row["workflow_id"]),
            Revision(row["workflow_version"]), DefinitionHash(row["definition_hash"]),
            WorkflowRunStatus(row["status"]), AggregateVersion(row["aggregate_version"]),
            EntityId.parse(row["correlation_id"]), _datetime(row["created_at"]),
            _datetime(row["updated_at"]), row["goal"], row["display_name"],
        )

    def update(self, record: WorkflowRunRecord, expected: AggregateVersion) -> None:
        self._write("before_run_update")
        self._verify_head(record.run_id, record.aggregate_version)
        cursor = self.connection.execute(
            """
            UPDATE workflow_runs SET status = ?, aggregate_version = ?, updated_at = ?
            WHERE run_id = ? AND aggregate_version = ?
            """,
            (
                record.status.value, record.aggregate_version.value,
                _time(record.updated_at), str(record.run_id), expected.value,
            ),
        )
        self._conflict(record.run_id, expected, cursor, "workflow_runs", "run_id")
        self._write("after_run_update")

    def list_non_terminal(
        self, *, after_run_id: str = "", limit: int = 100
    ) -> tuple[WorkflowRunRecord, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        terminal = (
            WorkflowRunStatus.SUCCEEDED.value,
            WorkflowRunStatus.FAILED.value,
            WorkflowRunStatus.CANCELLED.value,
        )
        return tuple(
            self.get(EntityId.parse(row["run_id"]))
            for row in self.connection.execute(
                """
                SELECT run_id FROM workflow_runs
                WHERE run_id > ? AND status NOT IN (?, ?, ?)
                ORDER BY run_id LIMIT ?
                """,
                (after_run_id, *terminal, limit),
            ).fetchall()
        )


class SQLiteExecutionPlanRepository(_Repository):
    def append(self, record: ExecutionPlanRecord) -> None:
        self._write("before_plan_append")
        self._ensure_absent("execution_plans", "plan_id", record.plan_id)
        if definition_hash(record.plan) != record.definition_hash:
            raise IntegrityViolationError("ExecutionPlan definition hash mismatch")
        event = self.connection.execute(
            "SELECT run_id FROM run_events WHERE event_id = ?",
            (str(record.created_event_id),),
        ).fetchone()
        if event is None or event["run_id"] != str(record.run_id):
            raise IntegrityViolationError("ExecutionPlan creation event is missing")
        self.connection.execute(
            """
            INSERT INTO execution_plans(
                plan_id, run_id, plan_version, workflow_id, workflow_version,
                plan_schema_version, canonical_plan_json, definition_hash,
                created_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.plan_id), str(record.run_id), record.plan_version.value,
                str(record.workflow_id), record.workflow_version.value,
                record.plan_schema_version.value, canonical_json(record.plan),
                record.definition_hash.value, str(record.created_event_id),
                _time(record.created_at),
            ),
        )
        self._write("after_plan_append")

    def get(self, run_id: EntityId, version: Revision) -> ExecutionPlanRecord | None:
        row = self.connection.execute(
            "SELECT * FROM execution_plans WHERE run_id = ? AND plan_version = ?",
            (str(run_id), version.value),
        ).fetchone()
        return None if row is None else self._record(row)

    def list_versions(self, run_id: EntityId) -> tuple[ExecutionPlanRecord, ...]:
        return tuple(
            self._record(row)
            for row in self.connection.execute(
                "SELECT * FROM execution_plans WHERE run_id = ? ORDER BY plan_version",
                (str(run_id),),
            ).fetchall()
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> ExecutionPlanRecord:
        return ExecutionPlanRecord(
            EntityId.parse(row["plan_id"]), EntityId.parse(row["run_id"]),
            Revision(row["plan_version"]), EntityId.parse(row["workflow_id"]),
            Revision(row["workflow_version"]), SchemaVersion(row["plan_schema_version"]),
            json.loads(row["canonical_plan_json"]), DefinitionHash(row["definition_hash"]),
            EntityId.parse(row["created_event_id"]), _datetime(row["created_at"]),
        )


class SQLiteNodeRunRepository(_Repository):
    def create(self, record: NodeRunRecord) -> None:
        self._write("before_node_run_create")
        self._ensure_absent("node_runs", "node_run_id", record.node_run_id)
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new NodeRun projection must start at version 0")
        self.connection.execute(
            """
            INSERT INTO node_runs(
                node_run_id, run_id, node_id, source_plan_version, status,
                aggregate_version, created_at, updated_at, generation, activation_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.node_run_id), str(record.run_id), record.node_id,
                record.source_plan_version.value, record.status.value,
                record.aggregate_version.value, _time(record.created_at),
                _time(record.updated_at), record.generation, record.activation_key,
            ),
        )
        self._write("after_node_run_create")

    def get(self, node_run_id: EntityId) -> NodeRunRecord | None:
        row = self.connection.execute(
            "SELECT * FROM node_runs WHERE node_run_id = ?", (str(node_run_id),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_run(self, run_id: EntityId) -> tuple[NodeRunRecord, ...]:
        return tuple(
            self._record(row)
            for row in self.connection.execute(
                "SELECT * FROM node_runs WHERE run_id = ? ORDER BY created_at, node_run_id",
                (str(run_id),),
            ).fetchall()
        )

    def update(self, record: NodeRunRecord, expected: AggregateVersion) -> None:
        self._write("before_node_run_update")
        self._verify_head(record.node_run_id, record.aggregate_version)
        cursor = self.connection.execute(
            """
            UPDATE node_runs SET status = ?, aggregate_version = ?, updated_at = ?
            WHERE node_run_id = ? AND aggregate_version = ?
            """,
            (
                record.status.value, record.aggregate_version.value,
                _time(record.updated_at), str(record.node_run_id), expected.value,
            ),
        )
        self._conflict(
            record.node_run_id, expected, cursor, "node_runs", "node_run_id"
        )
        self._write("after_node_run_update")

    @staticmethod
    def _record(row: sqlite3.Row) -> NodeRunRecord:
        return NodeRunRecord(
            EntityId.parse(row["node_run_id"]), EntityId.parse(row["run_id"]),
            row["node_id"], Revision(row["source_plan_version"]),
            NodeRunStatus(row["status"]), AggregateVersion(row["aggregate_version"]),
            _datetime(row["created_at"]), _datetime(row["updated_at"]),
            row["generation"], row["activation_key"],
        )


class SQLiteAttemptRepository(_Repository):
    def create(self, record: AttemptRecord) -> None:
        self._write("before_attempt_create")
        self._ensure_absent("node_attempts", "attempt_id", record.attempt_id)
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new Attempt projection must start at version 0")
        self.connection.execute(
            """
            INSERT INTO node_attempts(
                attempt_id, node_run_id, attempt_number, status,
                aggregate_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.attempt_id), str(record.node_run_id),
                record.attempt_number.value, record.status.value,
                record.aggregate_version.value, _time(record.created_at),
                _time(record.updated_at),
            ),
        )
        self._write("after_attempt_create")

    def get(self, attempt_id: EntityId) -> AttemptRecord | None:
        row = self.connection.execute(
            "SELECT * FROM node_attempts WHERE attempt_id = ?", (str(attempt_id),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_node_run(self, node_run_id: EntityId) -> tuple[AttemptRecord, ...]:
        return tuple(
            self._record(row)
            for row in self.connection.execute(
                "SELECT * FROM node_attempts WHERE node_run_id = ? ORDER BY attempt_number",
                (str(node_run_id),),
            ).fetchall()
        )

    def update(self, record: AttemptRecord, expected: AggregateVersion) -> None:
        self._write("before_attempt_update")
        self._verify_head(record.attempt_id, record.aggregate_version)
        cursor = self.connection.execute(
            """
            UPDATE node_attempts SET status = ?, aggregate_version = ?, updated_at = ?
            WHERE attempt_id = ? AND aggregate_version = ?
            """,
            (
                record.status.value, record.aggregate_version.value,
                _time(record.updated_at), str(record.attempt_id), expected.value,
            ),
        )
        self._conflict(
            record.attempt_id, expected, cursor, "node_attempts", "attempt_id"
        )
        self._write("after_attempt_update")

    @staticmethod
    def _record(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            EntityId.parse(row["attempt_id"]), EntityId.parse(row["node_run_id"]),
            Revision(row["attempt_number"]), AttemptStatus(row["status"]),
            AggregateVersion(row["aggregate_version"]), _datetime(row["created_at"]),
            _datetime(row["updated_at"]),
        )


class SQLiteBranchTokenRepository(_Repository):
    def create(self, record: BranchTokenRecord) -> None:
        self._write("before_token_create")
        self._ensure_absent("branch_tokens", "token_id", record.token_id)
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new BranchToken projection must start at version 0")
        self.connection.execute(
            """
            INSERT INTO branch_tokens(
                token_id, run_id, source_node_run_id, status, aggregate_version,
                scope_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.token_id), str(record.run_id),
                None if record.source_node_run_id is None else str(record.source_node_run_id),
                record.status.value, record.aggregate_version.value,
                canonical_json(record.scope), _time(record.created_at),
                _time(record.updated_at),
            ),
        )
        self._write("after_token_create")

    def get(self, token_id: EntityId) -> BranchTokenRecord | None:
        row = self.connection.execute(
            "SELECT * FROM branch_tokens WHERE token_id = ?", (str(token_id),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_run(self, run_id: EntityId, *, active_only: bool = False) -> tuple[BranchTokenRecord, ...]:
        sql = "SELECT * FROM branch_tokens WHERE run_id = ?"
        parameters: list[object] = [str(run_id)]
        if active_only:
            sql += " AND status = ?"
            parameters.append(BranchTokenStatus.ACTIVE.value)
        sql += " ORDER BY created_at, token_id"
        return tuple(
            self._record(row)
            for row in self.connection.execute(sql, parameters).fetchall()
        )

    def update(self, record: BranchTokenRecord, expected: AggregateVersion) -> None:
        self._write("before_token_update")
        self._verify_head(record.token_id, record.aggregate_version)
        cursor = self.connection.execute(
            """
            UPDATE branch_tokens SET status = ?, aggregate_version = ?,
                scope_json = ?, updated_at = ?
            WHERE token_id = ? AND aggregate_version = ?
            """,
            (
                record.status.value, record.aggregate_version.value,
                canonical_json(record.scope), _time(record.updated_at),
                str(record.token_id), expected.value,
            ),
        )
        self._conflict(
            record.token_id, expected, cursor, "branch_tokens", "token_id"
        )
        self._write("after_token_update")

    @staticmethod
    def _record(row: sqlite3.Row) -> BranchTokenRecord:
        source = row["source_node_run_id"]
        return BranchTokenRecord(
            EntityId.parse(row["token_id"]), EntityId.parse(row["run_id"]),
            None if source is None else EntityId.parse(source),
            BranchTokenStatus(row["status"]), AggregateVersion(row["aggregate_version"]),
            json.loads(row["scope_json"]), _datetime(row["created_at"]),
            _datetime(row["updated_at"]),
        )
