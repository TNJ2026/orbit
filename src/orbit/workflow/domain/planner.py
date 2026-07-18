"""Planner protocol facts for non-deterministic decisions made durable in Step 9."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from .ids import EntityId
from .serialization import canonical_json, freeze_json, to_primitive
from .versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion


PLANNER_SCHEMA_VERSION = SchemaVersion("1.0")
MAX_PLANNING_CONTEXT_BYTES = 262_144
MAX_RAW_RESPONSE_BYTES = 1_048_576
PLANNER_EVENT_VERSIONS = MappingProxyType({
    "planner_decision_requested": 1,
    "planner_attempt_started": 1,
    "planner_response_received": 1,
    "planner_proposal_parsed": 1,
    "planner_proposal_accepted": 1,
    "planner_proposal_rejected": 1,
    "planner_attempt_unknown": 1,
    "planner_attempt_failed": 1,
    "planner_late_response_recorded": 1,
    "planner_escalation_requested": 1,
})


class PlannerActionKind(str, Enum):
    DISPATCH = "dispatch"
    REWORK = "rework"
    REQUEST_INPUT = "request_input"
    REQUEST_APPROVAL = "request_approval"
    CANCEL_BRANCH = "cancel_branch"
    FINISH = "finish"
    FAIL = "fail"


class PlannerAttemptStatus(str, Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    RESPONSE_RECEIVED = "response_received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    FAILED = "failed"


class PlannerProposalStatus(str, Enum):
    PARSED = "parsed"
    PROTOCOL_ACCEPTED = "protocol_accepted"
    PROTOCOL_REJECTED = "protocol_rejected"
    CONSUMED = "consumed"


PLANNER_ATTEMPT_TRANSITIONS = MappingProxyType({
    PlannerAttemptStatus.REQUESTED: frozenset({PlannerAttemptStatus.RUNNING}),
    PlannerAttemptStatus.RUNNING: frozenset({
        PlannerAttemptStatus.RESPONSE_RECEIVED, PlannerAttemptStatus.UNKNOWN,
        PlannerAttemptStatus.FAILED,
    }),
    PlannerAttemptStatus.RESPONSE_RECEIVED: frozenset({
        PlannerAttemptStatus.ACCEPTED, PlannerAttemptStatus.REJECTED,
    }),
    PlannerAttemptStatus.ACCEPTED: frozenset(),
    PlannerAttemptStatus.REJECTED: frozenset(),
    PlannerAttemptStatus.UNKNOWN: frozenset(),
    PlannerAttemptStatus.FAILED: frozenset(),
})


def validate_planner_transition(source: PlannerAttemptStatus, target: PlannerAttemptStatus) -> None:
    if target not in PLANNER_ATTEMPT_TRANSITIONS[source]:
        raise ValueError(f"invalid PlannerAttempt transition: {source.value} -> {target.value}")


_ACTION_FIELDS = MappingProxyType({
    PlannerActionKind.DISPATCH: frozenset({"handler", "inputs", "config"}),
    PlannerActionKind.REWORK: frozenset({"node_id", "reason"}),
    PlannerActionKind.REQUEST_INPUT: frozenset({"prompt", "schema"}),
    PlannerActionKind.REQUEST_APPROVAL: frozenset({"operation", "scope"}),
    PlannerActionKind.CANCEL_BRANCH: frozenset({"node_id", "reason"}),
    PlannerActionKind.FINISH: frozenset({"outputs"}),
    PlannerActionKind.FAIL: frozenset({"code", "message"}),
})


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _hash(value: Any) -> DefinitionHash:
    return DefinitionHash("sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest())


@dataclass(frozen=True)
class PlanningContext:
    schema_version: SchemaVersion
    run_id: EntityId
    plan_version: Revision
    goal: str
    graph_summary: Mapping[str, Any]
    available_data_manifest: tuple[Mapping[str, Any], ...]
    available_capabilities: tuple[str, ...]
    remaining_limits: Mapping[str, int]
    recent_events: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLANNER_SCHEMA_VERSION:
            raise ValueError("unsupported PlanningContext version")
        if self.run_id.kind != "run" or not self.goal.strip():
            raise ValueError("PlanningContext requires a run and goal")
        capabilities = tuple(sorted(set(self.available_capabilities)))
        if any(not item.strip() for item in capabilities):
            raise ValueError("PlanningContext capability cannot be empty")
        limits = dict(self.remaining_limits)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in limits.values()):
            raise ValueError("PlanningContext limits must be non-negative integers")
        object.__setattr__(self, "graph_summary", freeze_json(self.graph_summary))
        object.__setattr__(self, "available_data_manifest", tuple(freeze_json(item) for item in self.available_data_manifest))
        object.__setattr__(self, "available_capabilities", capabilities)
        object.__setattr__(self, "remaining_limits", freeze_json(limits))
        object.__setattr__(self, "recent_events", tuple(freeze_json(item) for item in self.recent_events))
        if len(canonical_json(self).encode()) > MAX_PLANNING_CONTEXT_BYTES:
            raise ValueError("PlanningContext exceeds size limit")

    @property
    def context_hash(self) -> DefinitionHash:
        return _hash(self)


@dataclass(frozen=True)
class PlannerAction:
    kind: PlannerActionKind
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        arguments = dict(self.arguments)
        allowed = _ACTION_FIELDS[self.kind]
        if set(arguments) != allowed:
            raise ValueError(
                f"{self.kind.value} arguments must be exactly {sorted(allowed)}"
            )
        object_fields = {
            PlannerActionKind.DISPATCH: {"inputs", "config"},
            PlannerActionKind.REQUEST_INPUT: {"schema"},
            PlannerActionKind.REQUEST_APPROVAL: {"scope"},
            PlannerActionKind.FINISH: {"outputs"},
        }.get(self.kind, set())
        string_fields = allowed - object_fields
        if any(not isinstance(arguments[name], Mapping) for name in object_fields):
            raise ValueError(f"{self.kind.value} object arguments are invalid")
        if any(not isinstance(arguments[name], str) or not arguments[name].strip() for name in string_fields):
            raise ValueError(f"{self.kind.value} string arguments are invalid")
        object.__setattr__(self, "arguments", freeze_json(arguments))


@dataclass(frozen=True)
class ActionProposal:
    schema_version: SchemaVersion
    proposal_id: EntityId
    run_id: EntityId
    base_plan_version: Revision
    action: PlannerAction
    reason: str

    def __post_init__(self) -> None:
        if self.schema_version != PLANNER_SCHEMA_VERSION:
            raise ValueError("unsupported ActionProposal version")
        if self.proposal_id.kind != "proposal" or self.run_id.kind != "run":
            raise ValueError("invalid ActionProposal identifier")
        if not self.reason.strip():
            raise ValueError("ActionProposal reason is required")

    @property
    def content_hash(self) -> DefinitionHash:
        return _hash(self)


@dataclass(frozen=True)
class PlannerUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0
    incomplete: bool = False

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cost_microunits"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class PlannerAttemptRecord:
    attempt_id: EntityId
    run_id: EntityId
    attempt_number: Revision
    status: PlannerAttemptStatus
    context: PlanningContext
    prompt_hash: DefinitionHash
    capability_manifest_hash: DefinitionHash
    model_id: str
    provider_id: str
    request_fingerprint: DefinitionHash
    raw_response: str | None
    raw_response_checksum: DefinitionHash | None
    provider_request_id: str | None
    usage: PlannerUsage | None
    proposal_id: EntityId | None
    error: Mapping[str, Any] | None
    lease_owner: str | None
    lease_token_hash: str | None
    fencing_token: int
    lease_expires_at: datetime | None
    aggregate_version: AggregateVersion
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.attempt_id.kind != "planner_attempt" or self.run_id.kind != "run":
            raise ValueError("invalid PlannerAttempt identifier")
        if not self.model_id.strip() or not self.provider_id.strip():
            raise ValueError("PlannerAttempt model and provider are required")
        if self.context.run_id != self.run_id:
            raise ValueError("PlannerAttempt context crosses Run boundary")
        if self.proposal_id is not None and self.proposal_id.kind != "proposal":
            raise ValueError("invalid PlannerAttempt proposal id")
        if self.raw_response is not None:
            encoded = self.raw_response.encode()
            if len(encoded) > MAX_RAW_RESPONSE_BYTES:
                raise ValueError("Planner raw response exceeds size limit")
            expected = _hash(self.raw_response)
            if self.raw_response_checksum != expected:
                raise ValueError("Planner raw response checksum mismatch")
        elif self.raw_response_checksum is not None:
            raise ValueError("raw checksum requires raw response")
        if self.fencing_token < 0:
            raise ValueError("Planner fencing token cannot be negative")
        leased = self.status is PlannerAttemptStatus.RUNNING
        if leased != all((self.lease_owner, self.lease_token_hash, self.lease_expires_at)):
            raise ValueError("running PlannerAttempt requires complete lease authority")
        if not leased and any((self.lease_owner, self.lease_token_hash, self.lease_expires_at)):
            raise ValueError("terminal/non-running PlannerAttempt cannot retain a lease")
        _aware(self.created_at); _aware(self.updated_at)
        if self.lease_expires_at is not None: _aware(self.lease_expires_at)
        object.__setattr__(self, "error", None if self.error is None else freeze_json(self.error))


@dataclass(frozen=True)
class PlannerProposalRecord:
    proposal: ActionProposal
    attempt_id: EntityId
    status: PlannerProposalStatus
    validation: Mapping[str, Any]
    raw_response_checksum: DefinitionHash
    created_at: datetime

    def __post_init__(self) -> None:
        if self.attempt_id.kind != "planner_attempt":
            raise ValueError("PlannerProposal requires PlannerAttempt")
        _aware(self.created_at)
        object.__setattr__(self, "validation", freeze_json(self.validation))


def planner_request_fingerprint(
    context: PlanningContext,
    *,
    prompt_hash: DefinitionHash,
    capability_manifest_hash: DefinitionHash,
    model_id: str,
    provider_id: str,
) -> DefinitionHash:
    return _hash({
        "context_hash": context.context_hash.value,
        "prompt_hash": prompt_hash.value,
        "capability_manifest_hash": capability_manifest_hash.value,
        "model_id": model_id,
        "provider_id": provider_id,
    })


def derive_planner_attempt_id(run_id: EntityId, number: Revision, fingerprint: DefinitionHash) -> EntityId:
    digest = hashlib.sha256(f"{run_id}|{number.value}|{fingerprint.value}".encode()).hexdigest()
    return EntityId("planner_attempt", digest)


def strict_parse_proposal(raw: str, *, expected_run_id: EntityId) -> ActionProposal:
    if len(raw.encode()) > MAX_RAW_RESPONSE_BYTES:
        raise ValueError("Planner raw response exceeds size limit")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Planner response must be strict JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError("Planner response must be a JSON object")
    required = {"schema_version", "proposal_id", "run_id", "base_plan_version", "action", "reason"}
    if set(value) != required:
        raise ValueError(f"ActionProposal fields must be exactly {sorted(required)}")
    if value["run_id"] != str(expected_run_id):
        raise ValueError("ActionProposal crosses Run boundary")
    action = value["action"]
    if not isinstance(action, dict) or set(action) != {"kind", "arguments"}:
        raise ValueError("ActionProposal action must contain kind and arguments")
    if not isinstance(action["arguments"], dict):
        raise ValueError("ActionProposal action arguments must be an object")
    return ActionProposal(
        SchemaVersion(value["schema_version"]), EntityId.parse(value["proposal_id"]),
        EntityId.parse(value["run_id"]), Revision(value["base_plan_version"]),
        PlannerAction(PlannerActionKind(value["action"]["kind"]), value["action"]["arguments"]),
        value["reason"],
    )


def proposal_json(proposal: ActionProposal) -> str:
    return canonical_json(to_primitive(proposal))
