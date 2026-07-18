"""SQLite adapters for durable jobs, leases, and timers."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from ..domain.durable_execution import (
    DurableTimerRecord, ExecutionSafety, JobRecord, JobScanCursor, LeaseRecord,
    LeaseScanCursor, TimerPurpose, TimerScanCursor,
)
from ..domain.ids import EntityId
from ..domain.persistence import (
    ConcurrencyConflictError, IntegrityViolationError, RepositoryAlreadyExistsError,
)
from ..domain.serialization import canonical_json
from ..domain.states import JobStatus, LeaseStatus, TimerStatus
from ..domain.versions import AggregateVersion, Revision, SchemaVersion


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def job_record_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        EntityId.parse(row["job_id"]), EntityId.parse(row["run_id"]),
        EntityId.parse(row["node_run_id"]),
        None if row["current_attempt_id"] is None else EntityId.parse(row["current_attempt_id"]),
        row["job_kind"], ExecutionSafety(row["execution_safety"]),
        JobStatus(row["status"]), row["priority"], _datetime(row["available_at"]),
        row["delivery_count"], row["max_delivery_attempts"],
        AggregateVersion(row["aggregate_version"]), _datetime(row["created_at"]),
        _datetime(row["updated_at"]),
    )


def lease_record_from_row(row: sqlite3.Row) -> LeaseRecord:
    return LeaseRecord(
        EntityId.parse(row["lease_id"]), EntityId.parse(row["job_id"]),
        EntityId.parse(row["attempt_id"]), row["worker_id"], row["token_hash"],
        SchemaVersion(row["token_hash_version"]), Revision(row["fencing_token"]),
        LeaseStatus(row["status"]), _datetime(row["acquired_at"]),
        _datetime(row["expires_at"]), _datetime(row["released_at"]),
        AggregateVersion(row["aggregate_version"]), row["renewal_revision"],
    )


def timer_record_from_row(row: sqlite3.Row) -> DurableTimerRecord:
    return DurableTimerRecord(
        EntityId.parse(row["timer_id"]), EntityId.parse(row["run_id"]),
        TimerPurpose(row["purpose"]), row["dedupe_key"], row["target_type"],
        EntityId.parse(row["target_id"]), SchemaVersion(row["payload_schema_version"]),
        json.loads(row["payload_json"]), TimerStatus(row["status"]),
        _datetime(row["due_at"]), _datetime(row["fired_at"]), row["lease_owner"],
        row["lease_token_hash"], row["lease_fencing_token"],
        _datetime(row["lease_expires_at"]), AggregateVersion(row["aggregate_version"]),
        _datetime(row["created_at"]), _datetime(row["updated_at"]),
    )


class _Repository:
    def __init__(self, connection: sqlite3.Connection, events, *, fault_hook=None) -> None:
        self.connection = connection
        self.events = events
        self.fault_hook = fault_hook

    def _write(self, point: str) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("durable projection write requires an active UnitOfWork")
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _verify_head(self, identifier: EntityId, version: AggregateVersion) -> None:
        actual = self.events.stream_head(identifier)
        if actual != version:
            raise IntegrityViolationError(
                f"projection {identifier} version {version.value} does not match stream head {actual.value}"
            )

    @staticmethod
    def _limit(limit: int) -> None:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")


class SQLiteJobRepository(_Repository):
    def create(self, record: JobRecord) -> None:
        self._write("before_job_create")
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new Job projection must start at version 0")
        try:
            self.connection.execute(
                """INSERT INTO jobs(
                    job_id, run_id, node_run_id, current_attempt_id, job_kind,
                    execution_safety, status, priority, available_at, delivery_count,
                    max_delivery_attempts, aggregate_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record.job_id), str(record.run_id), str(record.node_run_id),
                    None if record.current_attempt_id is None else str(record.current_attempt_id),
                    record.job_kind, record.execution_safety.value, record.status.value,
                    record.priority, _time(record.available_at), record.delivery_count,
                    record.max_delivery_attempts, record.aggregate_version.value,
                    _time(record.created_at), _time(record.updated_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryAlreadyExistsError(str(record.job_id)) from exc
        self._write("after_job_create")

    def get(self, job_id: EntityId) -> JobRecord | None:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)).fetchone()
        return None if row is None else job_record_from_row(row)

    def update(self, record: JobRecord, expected: AggregateVersion) -> None:
        self._write("before_job_update")
        self._verify_head(record.job_id, record.aggregate_version)
        cursor = self.connection.execute(
            """UPDATE jobs SET current_attempt_id=?, status=?, priority=?,
                available_at=?, delivery_count=?, max_delivery_attempts=?,
                aggregate_version=?, updated_at=?
                WHERE job_id=? AND aggregate_version=?""",
            (
                None if record.current_attempt_id is None else str(record.current_attempt_id),
                record.status.value, record.priority, _time(record.available_at),
                record.delivery_count, record.max_delivery_attempts,
                record.aggregate_version.value, _time(record.updated_at),
                str(record.job_id), expected.value,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(record.job_id)
            raise ConcurrencyConflictError(
                record.job_id, expected.value,
                -1 if current is None else current.aggregate_version.value,
            )
        self._write("after_job_update")

    def list_by_run(self, run_id: EntityId) -> tuple[JobRecord, ...]:
        return tuple(
            job_record_from_row(row) for row in self.connection.execute(
                "SELECT * FROM jobs WHERE run_id=? ORDER BY created_at, job_id",
                (str(run_id),),
            ).fetchall()
        )

    def list_claimable(self, now: datetime, *, after: JobScanCursor | None = None, limit: int = 100):
        self._limit(limit)
        sql = "SELECT * FROM jobs WHERE status='ready' AND available_at <= ?"
        params: list[object] = [_time(now)]
        if after is not None:
            sql += """ AND (
                priority < ? OR
                (priority = ? AND available_at > ?) OR
                (priority = ? AND available_at = ? AND created_at > ?) OR
                (priority = ? AND available_at = ? AND created_at = ? AND job_id > ?)
            )"""
            params.extend([
                after.priority, after.priority, _time(after.available_at),
                after.priority, _time(after.available_at), _time(after.created_at),
                after.priority, _time(after.available_at), _time(after.created_at),
                str(after.job_id),
            ])
        sql += " ORDER BY priority DESC, available_at, created_at, job_id LIMIT ?"
        params.append(limit)
        return tuple(job_record_from_row(row) for row in self.connection.execute(sql, params).fetchall())


class SQLiteLeaseRepository(_Repository):
    def create(self, record: LeaseRecord) -> None:
        self._write("before_lease_create")
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new Lease projection must start at version 0")
        try:
            self.connection.execute(
                """INSERT INTO job_leases(
                    lease_id, job_id, attempt_id, worker_id, token_hash,
                    token_hash_version, fencing_token, status, acquired_at,
                    expires_at, released_at, aggregate_version, renewal_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record.lease_id), str(record.job_id), str(record.attempt_id),
                    record.worker_id, record.token_hash, record.token_hash_version.value,
                    record.fencing_token.value, record.status.value,
                    _time(record.acquired_at), _time(record.expires_at),
                    _time(record.released_at), record.aggregate_version.value,
                    record.renewal_revision,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryAlreadyExistsError(str(record.lease_id)) from exc
        self._write("after_lease_create")

    def get(self, lease_id: EntityId) -> LeaseRecord | None:
        row = self.connection.execute(
            "SELECT * FROM job_leases WHERE lease_id=?", (str(lease_id),)
        ).fetchone()
        return None if row is None else lease_record_from_row(row)

    def get_active_for_job(self, job_id: EntityId) -> LeaseRecord | None:
        row = self.connection.execute(
            "SELECT * FROM job_leases WHERE job_id=? AND status='active'", (str(job_id),)
        ).fetchone()
        return None if row is None else lease_record_from_row(row)

    def list_by_job(self, job_id: EntityId):
        return tuple(
            lease_record_from_row(row) for row in self.connection.execute(
                "SELECT * FROM job_leases WHERE job_id=? ORDER BY fencing_token",
                (str(job_id),),
            ).fetchall()
        )

    def update(self, record: LeaseRecord, expected: AggregateVersion) -> None:
        self._write("before_lease_update")
        self._verify_head(record.lease_id, record.aggregate_version)
        cursor = self.connection.execute(
            """UPDATE job_leases SET status=?, expires_at=?, released_at=?,
                aggregate_version=?, renewal_revision=?
                WHERE lease_id=? AND aggregate_version=?""",
            (
                record.status.value, _time(record.expires_at), _time(record.released_at),
                record.aggregate_version.value, record.renewal_revision,
                str(record.lease_id), expected.value,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(record.lease_id)
            raise ConcurrencyConflictError(
                record.lease_id, expected.value,
                -1 if current is None else current.aggregate_version.value,
            )
        self._write("after_lease_update")

    def renew(self, lease_id, *, token_hash, fencing_token, expected_revision, expires_at):
        self._write("before_lease_renew")
        cursor = self.connection.execute(
            """UPDATE job_leases SET expires_at=?, renewal_revision=renewal_revision+1
                WHERE lease_id=? AND status='active' AND token_hash=?
                  AND fencing_token=? AND renewal_revision=? AND expires_at < ?""",
            (
                _time(expires_at), str(lease_id), token_hash, fencing_token,
                expected_revision, _time(expires_at),
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(lease_id)
            raise ConcurrencyConflictError(
                lease_id, expected_revision,
                -1 if current is None else current.renewal_revision,
            )
        self._write("after_lease_renew")
        return self.get(lease_id)

    def list_expired(self, now, *, after: LeaseScanCursor | None = None, limit=100):
        self._limit(limit)
        sql = "SELECT * FROM job_leases WHERE status='active' AND expires_at <= ?"
        params: list[object] = [_time(now)]
        if after is not None:
            sql += " AND (expires_at > ? OR (expires_at = ? AND lease_id > ?))"
            params.extend([_time(after.expires_at), _time(after.expires_at), str(after.lease_id)])
        sql += " ORDER BY expires_at, lease_id LIMIT ?"
        params.append(limit)
        return tuple(lease_record_from_row(row) for row in self.connection.execute(sql, params).fetchall())


class SQLiteTimerRepository(_Repository):
    def create(self, record: DurableTimerRecord) -> None:
        self._write("before_timer_create")
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new Timer projection must start at version 0")
        try:
            self.connection.execute(
                """INSERT INTO durable_timers(
                    timer_id, run_id, purpose, dedupe_key, target_type, target_id,
                    payload_schema_version, payload_json, status, due_at, fired_at,
                    lease_owner, lease_token_hash, lease_fencing_token,
                    lease_expires_at, aggregate_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record.timer_id), str(record.run_id), record.purpose.value,
                    record.dedupe_key, record.target_type, str(record.target_id),
                    record.payload_schema_version.value, canonical_json(record.payload),
                    record.status.value, _time(record.due_at), _time(record.fired_at),
                    record.lease_owner, record.lease_token_hash,
                    record.lease_fencing_token, _time(record.lease_expires_at),
                    record.aggregate_version.value, _time(record.created_at),
                    _time(record.updated_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryAlreadyExistsError(str(record.timer_id)) from exc
        self._write("after_timer_create")

    def get(self, timer_id):
        row = self.connection.execute(
            "SELECT * FROM durable_timers WHERE timer_id=?", (str(timer_id),)
        ).fetchone()
        return None if row is None else timer_record_from_row(row)

    def get_by_dedupe(self, run_id, purpose, dedupe_key):
        row = self.connection.execute(
            "SELECT * FROM durable_timers WHERE run_id=? AND purpose=? AND dedupe_key=?",
            (str(run_id), str(purpose), dedupe_key),
        ).fetchone()
        return None if row is None else timer_record_from_row(row)

    def update(self, record, expected):
        self._write("before_timer_update")
        self._verify_head(record.timer_id, record.aggregate_version)
        cursor = self.connection.execute(
            """UPDATE durable_timers SET status=?, due_at=?, fired_at=?,
                lease_owner=?, lease_token_hash=?, lease_fencing_token=?,
                lease_expires_at=?, aggregate_version=?, updated_at=?
                WHERE timer_id=? AND aggregate_version=?""",
            (
                record.status.value, _time(record.due_at), _time(record.fired_at),
                record.lease_owner, record.lease_token_hash,
                record.lease_fencing_token, _time(record.lease_expires_at),
                record.aggregate_version.value, _time(record.updated_at),
                str(record.timer_id), expected.value,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(record.timer_id)
            raise ConcurrencyConflictError(
                record.timer_id, expected.value,
                -1 if current is None else current.aggregate_version.value,
            )
        self._write("after_timer_update")

    def list_due(self, now, *, after: TimerScanCursor | None = None, limit=100):
        self._limit(limit)
        sql = "SELECT * FROM durable_timers WHERE status='scheduled' AND due_at <= ?"
        params: list[object] = [_time(now)]
        if after is not None:
            sql += """ AND (due_at > ? OR (due_at = ? AND created_at > ?) OR
                (due_at = ? AND created_at = ? AND timer_id > ?))"""
            params.extend([
                _time(after.due_at), _time(after.due_at), _time(after.created_at),
                _time(after.due_at), _time(after.created_at), str(after.timer_id),
            ])
        sql += " ORDER BY due_at, created_at, timer_id LIMIT ?"
        params.append(limit)
        return tuple(timer_record_from_row(row) for row in self.connection.execute(sql, params).fetchall())

    def list_by_run(self, run_id):
        return tuple(
            timer_record_from_row(row) for row in self.connection.execute(
                "SELECT * FROM durable_timers WHERE run_id=? ORDER BY created_at, timer_id",
                (str(run_id),),
            ).fetchall()
        )

    def list_expired_leases(self, now, *, limit=100):
        self._limit(limit)
        return tuple(
            timer_record_from_row(row) for row in self.connection.execute(
                """SELECT * FROM durable_timers
                   WHERE status='leased' AND lease_expires_at <= ?
                   ORDER BY lease_expires_at, timer_id LIMIT ?""",
                (_time(now), limit),
            ).fetchall()
        )
