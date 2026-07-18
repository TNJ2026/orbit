"""SQLite repositories for durable Planner Migration v6."""

from __future__ import annotations

import json

from ..domain.ids import EntityId
from ..domain.planner import (
    ActionProposal, PlannerAction, PlannerActionKind, PlannerAttemptRecord,
    PlannerAttemptStatus, PlannerProposalRecord, PlannerProposalStatus,
    PlannerUsage, PlanningContext,
)
from ..domain.serialization import canonical_json, to_primitive
from ..domain.versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion
from .repositories import _Repository, _datetime, _time


def _context(value):
    return PlanningContext(
        SchemaVersion(value["schema_version"]), EntityId.parse(value["run_id"]),
        Revision(value["plan_version"]), value["goal"], value["graph_summary"],
        tuple(value["available_data_manifest"]), tuple(value["available_capabilities"]),
        value["remaining_limits"], tuple(value["recent_events"]),
    )


def _proposal(value):
    return ActionProposal(
        SchemaVersion(value["schema_version"]), EntityId.parse(value["proposal_id"]),
        EntityId.parse(value["run_id"]), Revision(value["base_plan_version"]),
        PlannerAction(PlannerActionKind(value["action"]["kind"]), value["action"]["arguments"]),
        value["reason"],
    )


class SQLitePlannerAttemptRepository(_Repository):
    def create(self, record):
        self._write("before_planner_attempt_create")
        self._ensure_absent("planner_attempts", "attempt_id", record.attempt_id)
        self.connection.execute(
            """INSERT INTO planner_attempts(
                attempt_id, run_id, attempt_number, status, context_json,
                context_hash, prompt_hash, capability_manifest_hash, model_id,
                provider_id, request_fingerprint, raw_response,
                raw_response_checksum, provider_request_id, usage_json,
                proposal_id, error_json, lease_owner, lease_token_hash,
                fencing_token, lease_expires_at, aggregate_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            self._values(record),
        )
        self._write("after_planner_attempt_create")

    @staticmethod
    def _values(record):
        return (
            str(record.attempt_id), str(record.run_id), record.attempt_number.value,
            record.status.value, canonical_json(record.context), record.context.context_hash.value,
            record.prompt_hash.value, record.capability_manifest_hash.value,
            record.model_id, record.provider_id, record.request_fingerprint.value,
            record.raw_response,
            None if record.raw_response_checksum is None else record.raw_response_checksum.value,
            record.provider_request_id,
            None if record.usage is None else canonical_json(record.usage),
            None if record.proposal_id is None else str(record.proposal_id),
            None if record.error is None else canonical_json(record.error),
            record.lease_owner, record.lease_token_hash, record.fencing_token,
            None if record.lease_expires_at is None else _time(record.lease_expires_at),
            record.aggregate_version.value, _time(record.created_at), _time(record.updated_at),
        )

    def get(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM planner_attempts WHERE attempt_id = ?", (str(identifier),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_run(self, run_id):
        return tuple(self._record(row) for row in self.connection.execute(
            "SELECT * FROM planner_attempts WHERE run_id = ? ORDER BY attempt_number, attempt_id",
            (str(run_id),),
        ))

    def list_claimable(self, *, limit=100):
        return tuple(self._record(row) for row in self.connection.execute(
            "SELECT * FROM planner_attempts WHERE status = 'requested' ORDER BY created_at, attempt_id LIMIT ?",
            (limit,),
        ))

    def list_expired(self, now, *, limit=100):
        return tuple(self._record(row) for row in self.connection.execute(
            """SELECT * FROM planner_attempts WHERE status = 'running'
                AND lease_expires_at <= ? ORDER BY lease_expires_at, attempt_id LIMIT ?""",
            (_time(now), limit),
        ))

    def list_ready_to_parse(self, *, limit=100):
        return tuple(self._record(row) for row in self.connection.execute(
            "SELECT * FROM planner_attempts WHERE status = 'response_received' ORDER BY updated_at, attempt_id LIMIT ?",
            (limit,),
        ))

    def update(self, record, expected):
        self._write("before_planner_attempt_update")
        cursor = self.connection.execute(
            """UPDATE planner_attempts SET status=?, raw_response=?,
                raw_response_checksum=?, provider_request_id=?, usage_json=?,
                proposal_id=?, error_json=?, lease_owner=?, lease_token_hash=?,
                fencing_token=?, lease_expires_at=?, aggregate_version=?, updated_at=?
                WHERE attempt_id=? AND aggregate_version=?""",
            (
                record.status.value, record.raw_response,
                None if record.raw_response_checksum is None else record.raw_response_checksum.value,
                record.provider_request_id,
                None if record.usage is None else canonical_json(record.usage),
                None if record.proposal_id is None else str(record.proposal_id),
                None if record.error is None else canonical_json(record.error),
                record.lease_owner, record.lease_token_hash, record.fencing_token,
                None if record.lease_expires_at is None else _time(record.lease_expires_at),
                record.aggregate_version.value, _time(record.updated_at), str(record.attempt_id), expected.value,
            ),
        )
        self._conflict(record.attempt_id, expected, cursor, "planner_attempts", "attempt_id")
        self._write("after_planner_attempt_update")

    @staticmethod
    def _record(row):
        usage = None if row["usage_json"] is None else PlannerUsage(**json.loads(row["usage_json"]))
        return PlannerAttemptRecord(
            EntityId.parse(row["attempt_id"]), EntityId.parse(row["run_id"]),
            Revision(row["attempt_number"]), PlannerAttemptStatus(row["status"]),
            _context(json.loads(row["context_json"])), DefinitionHash(row["prompt_hash"]),
            DefinitionHash(row["capability_manifest_hash"]), row["model_id"], row["provider_id"],
            DefinitionHash(row["request_fingerprint"]), row["raw_response"],
            None if row["raw_response_checksum"] is None else DefinitionHash(row["raw_response_checksum"]),
            row["provider_request_id"], usage,
            None if row["proposal_id"] is None else EntityId.parse(row["proposal_id"]),
            None if row["error_json"] is None else json.loads(row["error_json"]),
            row["lease_owner"], row["lease_token_hash"], row["fencing_token"],
            None if row["lease_expires_at"] is None else _datetime(row["lease_expires_at"]),
            AggregateVersion(row["aggregate_version"]), _datetime(row["created_at"]),
            _datetime(row["updated_at"]),
        )


class SQLitePlannerProposalRepository(_Repository):
    def create(self, record):
        self._write("before_planner_proposal_create")
        self._ensure_absent("planner_proposals", "proposal_id", record.proposal.proposal_id)
        self.connection.execute(
            """INSERT INTO planner_proposals(
                proposal_id, attempt_id, run_id, base_plan_version, status,
                proposal_json, action_json, reason, content_hash,
                validation_json, raw_response_checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(record.proposal.proposal_id), str(record.attempt_id),
                str(record.proposal.run_id), record.proposal.base_plan_version.value,
                record.status.value, canonical_json(record.proposal),
                canonical_json(record.proposal.action), record.proposal.reason,
                record.proposal.content_hash.value, canonical_json(record.validation),
                record.raw_response_checksum.value, _time(record.created_at),
            ),
        )
        self._write("after_planner_proposal_create")

    def get(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM planner_proposals WHERE proposal_id = ?", (str(identifier),)
        ).fetchone()
        return None if row is None else self._record(row)

    def list_by_run(self, run_id):
        return tuple(self._record(row) for row in self.connection.execute(
            "SELECT * FROM planner_proposals WHERE run_id = ? ORDER BY created_at, proposal_id",
            (str(run_id),),
        ))

    def find_by_hash(self, run_id, content_hash):
        row = self.connection.execute(
            "SELECT * FROM planner_proposals WHERE run_id = ? AND content_hash = ?",
            (str(run_id), content_hash.value),
        ).fetchone()
        return None if row is None else self._record(row)

    @staticmethod
    def _record(row):
        return PlannerProposalRecord(
            _proposal(json.loads(row["proposal_json"])), EntityId.parse(row["attempt_id"]),
            PlannerProposalStatus(row["status"]), json.loads(row["validation_json"]),
            DefinitionHash(row["raw_response_checksum"]), _datetime(row["created_at"]),
        )
