"""SQLite repositories for static Graph v5 projections."""

from __future__ import annotations

from dataclasses import replace
import json

from ..domain.graph import JoinMergeMode, JoinMode, JoinPolicy
from ..domain.graph_persistence import (
    ControlCounterRecord, JoinGroupRecord, JoinGroupStatus,
)
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, to_primitive
from ..domain.versions import AggregateVersion
from .repositories import _Repository, _datetime, _time


def _policy(value) -> JoinPolicy:
    return JoinPolicy(
        JoinMode(value["mode"]), JoinMergeMode(value["merge_mode"]),
        value.get("threshold"), value.get("deadline_seconds"),
        value.get("min_successful"),
    )


class SQLiteJoinGroupRepository(_Repository):
    def create(self, record: JoinGroupRecord) -> None:
        self._write("before_join_group_create")
        self._ensure_absent("join_groups", "join_group_id", record.join_group_id)
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new JoinGroup must start at version 0")
        self.connection.execute(
            """INSERT INTO join_groups(
                join_group_id, run_id, node_id, generation, policy_json,
                participant_edge_ids_json, status, decision_json,
                aggregate_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(record.join_group_id), str(record.run_id), record.node_id,
                record.generation, canonical_json(record.policy),
                canonical_json(record.participant_edge_ids), record.status.value,
                None if record.decision is None else canonical_json(record.decision),
                record.aggregate_version.value, _time(record.created_at),
                _time(record.updated_at),
            ),
        )
        self._write("after_join_group_create")

    def get(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM join_groups WHERE join_group_id = ?", (str(identifier),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_run(self, run_id, *, waiting_only=False):
        sql, args = "SELECT * FROM join_groups WHERE run_id = ?", [str(run_id)]
        if waiting_only:
            sql += " AND status = 'waiting'"
        sql += " ORDER BY join_group_id"
        return tuple(self._record(row) for row in self.connection.execute(sql, args))

    def update(self, record, expected):
        self._write("before_join_group_update")
        cursor = self.connection.execute(
            """UPDATE join_groups SET status = ?, decision_json = ?,
                aggregate_version = ?, updated_at = ?
                WHERE join_group_id = ? AND aggregate_version = ?""",
            (
                record.status.value,
                None if record.decision is None else canonical_json(record.decision),
                record.aggregate_version.value, _time(record.updated_at),
                str(record.join_group_id), expected.value,
            ),
        )
        self._conflict(record.join_group_id, expected, cursor, "join_groups", "join_group_id")
        self._write("after_join_group_update")

    @staticmethod
    def _record(row):
        decision = None if row["decision_json"] is None else json.loads(row["decision_json"])
        return JoinGroupRecord(
            EntityId.parse(row["join_group_id"]), EntityId.parse(row["run_id"]),
            row["node_id"], row["generation"], _policy(json.loads(row["policy_json"])),
            tuple(json.loads(row["participant_edge_ids_json"])),
            JoinGroupStatus(row["status"]), decision,
            AggregateVersion(row["aggregate_version"]), _datetime(row["created_at"]),
            _datetime(row["updated_at"]),
        )


class SQLiteControlCounterRepository(_Repository):
    def create(self, record):
        self._write("before_control_counter_create")
        self._ensure_absent("graph_control_counters", "counter_id", record.counter_id)
        self.connection.execute(
            """INSERT INTO graph_control_counters(
                counter_id, run_id, policy_id, scope_key, value, limit_value,
                aggregate_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(record.counter_id), str(record.run_id), record.policy_id,
                record.scope_key, record.value, record.limit,
                record.aggregate_version.value, _time(record.updated_at),
            ),
        )
        self._write("after_control_counter_create")

    def get(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM graph_control_counters WHERE counter_id = ?", (str(identifier),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_run(self, run_id):
        return tuple(
            self._record(row) for row in self.connection.execute(
                "SELECT * FROM graph_control_counters WHERE run_id = ? ORDER BY counter_id",
                (str(run_id),),
            )
        )

    def increment(self, identifier, expected, now):
        record = self.get(identifier)
        if record is None:
            raise ValueError("ControlCounter was not found")
        if record.aggregate_version != expected:
            from ..domain.persistence import ConcurrencyConflictError
            raise ConcurrencyConflictError(identifier, expected.value, record.aggregate_version.value)
        if record.value >= record.limit:
            raise ValueError("ControlCounter hard limit exhausted")
        updated = replace(
            record, value=record.value + 1,
            aggregate_version=record.aggregate_version.next(), updated_at=now,
        )
        cursor = self.connection.execute(
            """UPDATE graph_control_counters SET value = ?, aggregate_version = ?,
                updated_at = ? WHERE counter_id = ? AND aggregate_version = ?""",
            (updated.value, updated.aggregate_version.value, _time(now), str(identifier), expected.value),
        )
        self._conflict(identifier, expected, cursor, "graph_control_counters", "counter_id")
        return updated

    @staticmethod
    def _record(row):
        return ControlCounterRecord(
            EntityId.parse(row["counter_id"]), EntityId.parse(row["run_id"]),
            row["policy_id"], row["scope_key"], row["value"], row["limit_value"],
            AggregateVersion(row["aggregate_version"]), _datetime(row["updated_at"]),
        )
