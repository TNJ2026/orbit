"""Self-contained JSON Schema subset for frozen workflow contracts."""

from __future__ import annotations

from types import MappingProxyType
from collections.abc import Mapping as MappingABC, Sequence
from datetime import datetime
import re
from typing import Any, Mapping

from .serialization import freeze_json


_ID = {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,31}:.+$"}
_DATE_TIME = {"type": "string", "format": "date-time"}
_REVISION = {"type": "integer", "minimum": 1}
_HASH = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}


def _object_schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }

COMMAND_ENVELOPE_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "$id": "orbit://workflow/contracts/command-envelope/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "command_id",
            "command_type",
            "aggregate_id",
            "correlation_id",
            "expected_version",
            "idempotency_key",
            "actor",
            "issued_at",
            "payload",
        ],
        "properties": {
            "command_id": _ID,
            "command_type": {"type": "string"},
            "aggregate_id": _ID,
            "correlation_id": _ID,
            "expected_version": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
            "actor": {"type": "string", "minLength": 1},
            "issued_at": _DATE_TIME,
            "payload": {"type": "object"},
        },
    }
)

EVENT_ENVELOPE_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "$id": "orbit://workflow/contracts/event-envelope/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "event_id",
            "event_type",
            "event_version",
            "aggregate_id",
            "sequence",
            "correlation_id",
            "causation_id",
            "occurred_at",
            "payload",
        ],
        "properties": {
            "event_id": _ID,
            "event_type": {"type": "string"},
            "event_version": {"type": "integer", "minimum": 1},
            "aggregate_id": _ID,
            "sequence": {"type": "integer", "minimum": 1},
            "correlation_id": _ID,
            "causation_id": _ID,
            "occurred_at": _DATE_TIME,
            "payload": {"type": "object"},
        },
    }
)

WORKFLOW_VERSION_REF_SCHEMA = _object_schema(
    ["workflow_id", "version", "definition_hash"],
    {"workflow_id": _ID, "version": _REVISION, "definition_hash": _HASH},
)

WORKFLOW_RUN_REF_SCHEMA = _object_schema(
    ["run_id", "workflow_version"],
    {"run_id": _ID, "workflow_version": WORKFLOW_VERSION_REF_SCHEMA},
)

EXECUTION_PLAN_REF_SCHEMA = _object_schema(
    ["plan_id", "run_id", "plan_version", "workflow_version"],
    {
        "plan_id": _ID,
        "run_id": _ID,
        "plan_version": _REVISION,
        "workflow_version": WORKFLOW_VERSION_REF_SCHEMA,
    },
)

NODE_RUN_REF_SCHEMA = _object_schema(
    ["node_run_id", "run_id", "plan_version", "node_id"],
    {
        "node_run_id": _ID,
        "run_id": _ID,
        "plan_version": _REVISION,
        "node_id": {"type": "string", "minLength": 1},
    },
)

ATTEMPT_REF_SCHEMA = _object_schema(
    ["attempt_id", "node_run_id", "number"],
    {"attempt_id": _ID, "node_run_id": _ID, "number": _REVISION},
)

ERROR_INFO_SCHEMA = _object_schema(
    ["code", "category", "message", "source", "details", "cause"],
    {
        "code": {"type": "string", "minLength": 1},
        "category": {"type": "string"},
        "message": {"type": "string", "minLength": 1},
        "source": {"type": "string", "minLength": 1},
        "details": {"type": "object"},
        "cause": {"type": ["string", "null"]},
    },
)

USAGE_SNAPSHOT_SCHEMA = _object_schema(
    [
        "attempt_id",
        "sequence",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "provider_request_id",
        "observed_at",
    ],
    {
        "attempt_id": _ID,
        "sequence": _REVISION,
        "input_tokens": {"type": "integer", "minimum": 0},
        "output_tokens": {"type": "integer", "minimum": 0},
        "tool_calls": {"type": "integer", "minimum": 0},
        "provider_request_id": {"type": ["string", "null"]},
        "observed_at": _DATE_TIME,
    },
)

HANDLER_RESULT_SCHEMA = _object_schema(
    [
        "status", "output", "error", "usage", "usage_incomplete",
        "external_effect", "provider_request_id", "diagnostics",
        "artifact_refs",
    ],
    {
        "status": {
            "enum": [
                "succeeded", "failed", "cancelled",
                "unknown_external_result",
            ]
        },
        "output": {"type": ["object", "null"]},
        "error": {**ERROR_INFO_SCHEMA, "type": ["object", "null"]},
        "usage": {**USAGE_SNAPSHOT_SCHEMA, "type": ["object", "null"]},
        "usage_incomplete": {"type": "boolean"},
        "external_effect": {
            "enum": ["none", "known_applied", "unknown"]
        },
        "provider_request_id": {"type": ["string", "null"]},
        "diagnostics": {"type": "array", "items": {"type": "object"}},
        "artifact_refs": {"type": "array", "items": _ID},
    },
)

RESOURCE_PROFILE_SCHEMA = _object_schema(
    [
        "max_input_tokens", "max_output_tokens", "max_tool_calls",
        "max_duration_seconds", "max_cost_microunits", "cost_class",
    ],
    {
        "max_input_tokens": {"type": "integer", "minimum": 0},
        "max_output_tokens": {"type": "integer", "minimum": 0},
        "max_tool_calls": {"type": "integer", "minimum": 0},
        "max_duration_seconds": {"type": "integer", "minimum": 1},
        "max_cost_microunits": {"type": "integer", "minimum": 0},
        "cost_class": {"type": "string", "minLength": 1},
    },
)

HANDLER_MANIFEST_SCHEMA = _object_schema(
    [
        "name", "version", "node_kinds", "inputs", "outputs",
        "config_schema", "execution_safety", "resource_profile",
        "result_schema_id", "capabilities", "required_secrets",
        "supports_cancel", "supports_recover", "manifest_version",
    ],
    {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "string", "pattern": r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"},
        "node_kinds": {"type": "array", "items": {"type": "string"}},
        "inputs": {"type": "object"},
        "outputs": {"type": "object"},
        "config_schema": {"type": "object"},
        "execution_safety": {"enum": ["replay_safe", "unknown_on_lease_loss"]},
        "resource_profile": RESOURCE_PROFILE_SCHEMA,
        "result_schema_id": {"type": "string", "minLength": 1},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "required_secrets": {"type": "array", "items": {"type": "string"}},
        "supports_cancel": {"type": "boolean"},
        "supports_recover": {"type": "boolean"},
        "manifest_version": {"enum": ["1.0"]},
    },
)

BUDGET_RESERVATION_SCHEMA = _object_schema(
    ["reservation_id", "run_id", "amount_microunits"],
    {
        "reservation_id": _ID,
        "run_id": _ID,
        "amount_microunits": {"type": "integer", "minimum": 1},
    },
)

BUDGET_ACCOUNT_SCHEMA = _object_schema(
    [
        "run_id",
        "total_microunits",
        "reserved_microunits",
        "consumed_microunits",
        "version",
    ],
    {
        "run_id": _ID,
        "total_microunits": {"type": "integer", "minimum": 0},
        "reserved_microunits": {"type": "integer", "minimum": 0},
        "consumed_microunits": {"type": "integer", "minimum": 0},
        "version": _REVISION,
    },
)

VALUE_SCHEMA = _object_schema(
    ["name", "schema_id", "data"],
    {
        "name": {"type": "string", "minLength": 1},
        "schema_id": {"type": "string", "minLength": 1},
        "data": {},
    },
)

ARTIFACT_REF_SCHEMA = _object_schema(
    ["artifact_id", "schema_id", "content_type", "checksum", "size_bytes"],
    {
        "artifact_id": _ID,
        "schema_id": {"type": "string", "minLength": 1},
        "content_type": {"type": "string", "minLength": 1},
        "checksum": _HASH,
        "size_bytes": {"type": "integer", "minimum": 0},
    },
)

PORT_DATA_POLICY_SCHEMA = _object_schema(
    ["transport", "max_size_bytes", "content_types", "visibility"],
    {
        "transport": {"enum": ["inline", "artifact_ref", "secret_ref"]},
        "max_size_bytes": {"type": "integer", "minimum": 0},
        "content_types": {"type": "array", "items": {"type": "string"}},
        "visibility": {
            "type": ["string", "null"],
            "enum": ["node", "run", "subflow", "workflow", None],
        },
    },
)

SECRET_REF_SCHEMA = _object_schema(
    ["logical_name", "version", "provider_hint"],
    {
        "logical_name": {"type": "string", "minLength": 1},
        "version": {"type": ["string", "null"]},
        "provider_hint": {"type": ["string", "null"]},
    },
)

VALUE_COMMIT_SCHEMA = _object_schema(
    ["port_id", "schema_id", "data", "checksum", "size_bytes"],
    {
        "port_id": {"type": "string", "minLength": 1},
        "schema_id": {"type": "string", "minLength": 1},
        "data": {}, "checksum": _HASH,
        "size_bytes": {"type": "integer", "minimum": 0},
    },
)

STAGED_ARTIFACT_COMMIT_SCHEMA = _object_schema(
    ["port_id", "artifact_id", "checksum", "size_bytes"],
    {
        "port_id": {"type": "string", "minLength": 1},
        "artifact_id": _ID, "checksum": _HASH,
        "size_bytes": {"type": "integer", "minimum": 0},
    },
)

DATA_COMMIT_MANIFEST_SCHEMA = _object_schema(
    ["run_id", "owner_kind", "owner_id", "values", "artifacts"],
    {
        "run_id": _ID,
        "owner_kind": {"enum": ["run_input", "node_input", "attempt_output"]},
        "owner_id": _ID,
        "values": {"type": "array", "items": VALUE_COMMIT_SCHEMA},
        "artifacts": {"type": "array", "items": STAGED_ARTIFACT_COMMIT_SCHEMA},
    },
)

VALUE_RECORD_SCHEMA = _object_schema(
    [
        "value_id", "run_id", "owner_kind", "owner_id", "port_id",
        "schema_id", "data", "checksum", "size_bytes",
        "created_event_id", "created_at",
    ],
    {
        "value_id": _ID, "run_id": _ID,
        "owner_kind": {"enum": ["run_input", "node_input", "attempt_output"]},
        "owner_id": _ID, "port_id": {"type": "string", "minLength": 1},
        "schema_id": {"type": "string", "minLength": 1}, "data": {},
        "checksum": _HASH, "size_bytes": {"type": "integer", "minimum": 0},
        "created_event_id": _ID, "created_at": _DATE_TIME,
    },
)

VALUE_LINK_SCHEMA = _object_schema(
    [
        "link_id", "run_id", "source_value_id", "target_value_id",
        "link_type", "mapping_hash", "created_event_id", "created_at",
    ],
    {
        "link_id": _ID, "run_id": _ID, "source_value_id": _ID,
        "target_value_id": _ID,
        "link_type": {"enum": ["mapped_from", "consumed_by"]},
        "mapping_hash": {"type": ["string", "null"]},
        "created_event_id": _ID, "created_at": _DATE_TIME,
    },
)

ARTIFACT_METADATA_SCHEMA = _object_schema(
    [
        "artifact_id", "run_id", "workflow_id", "producer_type",
        "producer_id", "producer_node_run_id", "output_port_id", "schema_id",
        "content_type", "checksum", "size_bytes", "blob_key", "visibility",
        "scope_id", "status", "created_at", "committed_at", "created_event_id",
    ],
    {
        "artifact_id": _ID, "run_id": _ID, "workflow_id": _ID,
        "producer_type": {"enum": ["attempt", "run_ingress"]},
        "producer_id": _ID,
        "producer_node_run_id": {**_ID, "type": ["string", "null"]},
        "output_port_id": {"type": "string", "minLength": 1},
        "schema_id": {"type": "string", "minLength": 1},
        "content_type": {"type": "string", "minLength": 3},
        "checksum": _HASH, "size_bytes": {"type": "integer", "minimum": 0},
        "blob_key": _HASH,
        "visibility": {"enum": ["node", "run", "subflow", "workflow"]},
        "scope_id": _ID, "status": {"enum": ["staged", "committed", "abandoned"]},
        "created_at": _DATE_TIME,
        "committed_at": {"type": ["string", "null"]},
        "created_event_id": {**_ID, "type": ["string", "null"]},
    },
)

ARTIFACT_LINK_SCHEMA = _object_schema(
    [
        "link_id", "workflow_id", "run_id", "artifact_id", "link_type",
        "target_id", "created_event_id", "created_at",
    ],
    {
        "link_id": _ID, "workflow_id": _ID, "run_id": _ID,
        "artifact_id": _ID,
        "link_type": {"enum": ["producer", "consumer", "derived_from"]},
        "target_id": _ID, "created_event_id": _ID, "created_at": _DATE_TIME,
    },
)

INPUT_MANIFEST_ITEM_SCHEMA = _object_schema(
    ["port_id", "transport", "schema_id", "value", "artifact", "secret"],
    {
        "port_id": {"type": "string", "minLength": 1},
        "transport": {"enum": ["inline", "artifact_ref", "secret_ref"]},
        "schema_id": {"type": "string", "minLength": 1},
        "value": {**VALUE_COMMIT_SCHEMA, "type": ["object", "null"]},
        "artifact": {**ARTIFACT_REF_SCHEMA, "type": ["object", "null"]},
        "secret": {**SECRET_REF_SCHEMA, "type": ["object", "null"]},
    },
)

INPUT_MANIFEST_SCHEMA = _object_schema(
    ["run_id", "node_run_id", "attempt_id", "items"],
    {
        "run_id": _ID, "node_run_id": _ID, "attempt_id": _ID,
        "items": {"type": "array", "items": INPUT_MANIFEST_ITEM_SCHEMA},
    },
)

RUNTIME_COMMAND_PAYLOAD_SCHEMAS = {
    "start-run": _object_schema(
        ["workflow_id", "workflow_version", "definition_hash"],
        {
            "workflow_id": _ID, "workflow_version": _REVISION,
            "definition_hash": _HASH, "input": {"type": "object"},
            "artifact_inputs": {"type": "array", "items": {"type": "object"}},
        },
    ),
    "schedule-node": _object_schema(
        ["run_id", "node_id"],
        {
            "run_id": _ID, "node_id": {"type": "string", "minLength": 1},
            "plan_version": _REVISION, "input": {"type": "object"},
        },
    ),
    "start-attempt": _object_schema([], {}),
    "complete-attempt": _object_schema(
        ["output"], {
            "output": {"type": "object"},
            "artifact_refs": {"type": "array", "items": _ID, "uniqueItems": True},
        }
    ),
    "fail-attempt": _object_schema(
        ["error"], {"error": ERROR_INFO_SCHEMA}
    ),
    "cancel-run": _object_schema([], {"reason": {"type": "string"}}),
    "cancel-node": _object_schema([], {"reason": {"type": "string"}}),
    "advance-graph": _object_schema([], {"plan_version": _REVISION}),
    "submit-human-task": _object_schema(
        ["submission_token", "decision"],
        {
            "submission_token": {"type": "string", "minLength": 1},
            "decision": {
                "enum": ["approve", "reject", "provide_input", "withdraw"]
            },
            "value": {},
        },
    ),
}

_TRANSITION_BASE = {
    "machine": {"type": "string", "minLength": 1},
    "from": {"type": "string", "minLength": 1},
    "to": {"type": "string", "minLength": 1},
}
RUNTIME_EVENT_PAYLOAD_SCHEMAS = {
    "workflow-run-transitioned": _object_schema(
        ["machine", "from", "to"],
        {
            **_TRANSITION_BASE, "workflow_id": _ID,
            "workflow_version": _REVISION, "definition_hash": _HASH,
            "plan_id": _ID, "plan_version": _REVISION,
            "input": {"type": "object"},
            "artifact_refs": {"type": "array", "items": _ID, "uniqueItems": True},
            "reason": {"type": "string"},
        },
    ),
    "node-run-transitioned": _object_schema(
        ["machine", "from", "to", "node_id"],
        {
            **_TRANSITION_BASE, "node_id": {"type": "string", "minLength": 1},
            "run_id": _ID, "plan_version": _REVISION,
            "generation": _REVISION,
            "activation_key": {"type": "string", "minLength": 1},
        },
    ),
    "attempt-transitioned": _object_schema(
        ["machine", "from", "to", "node_run_id", "attempt_number"],
        {
            **_TRANSITION_BASE, "node_run_id": _ID,
            "attempt_number": _REVISION, "run_id": _ID,
        },
    ),
    "node-input-prepared": _object_schema(
        ["run_id", "node_id", "input"],
        {"run_id": _ID, "node_id": {"type": "string"}, "input": {"type": "object"}},
    ),
    "attempt-output-recorded": _object_schema(
        ["run_id", "node_run_id", "output"],
        {
            "run_id": _ID, "node_run_id": _ID, "output": {"type": "object"},
            "artifact_refs": {"type": "array", "items": _ID, "uniqueItems": True},
        },
    ),
    "attempt-failed-recorded": _object_schema(
        ["run_id", "node_run_id", "error"],
        {"run_id": _ID, "node_run_id": _ID, "error": ERROR_INFO_SCHEMA},
    ),
    "graph-route-decided": _object_schema(
        ["run_id", "node_run_id", "decision"],
        {"run_id": _ID, "node_run_id": _ID, "decision": {"type": "object"}},
    ),
    "branch-token-transitioned": _object_schema(
        ["machine", "from", "to", "run_id", "edge_id", "target_node_id", "target_generation"],
        {
            **_TRANSITION_BASE, "run_id": _ID,
            "edge_id": {"type": "string", "minLength": 1},
            "target_node_id": {"type": "string", "minLength": 1},
            "target_generation": _REVISION,
            "scope": {"type": "object"},
        },
    ),
    "join-decided": _object_schema(
        ["run_id", "join_group_id", "decision"],
        {"run_id": _ID, "join_group_id": _ID, "decision": {"type": "object"}, "input": {}},
    ),
    "control-counter-incremented": _object_schema(
        ["run_id", "policy_id", "scope_key", "value", "limit"],
        {
            "run_id": _ID, "policy_id": {"type": "string", "minLength": 1},
            "scope_key": {"type": "string", "minLength": 1},
            "value": _REVISION, "limit": _REVISION,
        },
    ),
}

_LEASE_AUTH = {
    "lease_id": _ID,
    "lease_token": {"type": "string", "minLength": 1},
    "fencing_token": _REVISION,
}
_TIMER_LEASE_AUTH = {
    "lease_token": {"type": "string", "minLength": 1},
    "fencing_token": _REVISION,
}
EXECUTION_METADATA_SCHEMA = _object_schema(
    ["usage", "usage_incomplete", "provider_request_id"],
    {
        "usage": {**USAGE_SNAPSHOT_SCHEMA, "type": ["object", "null"]},
        "usage_incomplete": {"type": "boolean"},
        "provider_request_id": {"type": ["string", "null"]},
    },
)
DURABLE_COMMAND_PAYLOAD_SCHEMAS = {
    "claim-job": _object_schema(
        ["worker_id", "lease_id", "token_hash", "token_hash_version", "lease_expires_at", "observed_at"],
        {
            "worker_id": {"type": "string", "minLength": 1}, "lease_id": _ID,
            "token_hash": {"type": "string", "minLength": 1},
            "token_hash_version": {"type": "string", "minLength": 1},
            "lease_expires_at": _DATE_TIME, "observed_at": _DATE_TIME,
        },
    ),
    "start-job": _object_schema(list(_LEASE_AUTH), dict(_LEASE_AUTH)),
    "release-job": _object_schema(list(_LEASE_AUTH), dict(_LEASE_AUTH)),
    "defer-job": _object_schema(
        [*list(_LEASE_AUTH), "available_at", "reason"],
        {**_LEASE_AUTH, "available_at": _DATE_TIME, "reason": {"type": "string", "minLength": 1}},
    ),
    "complete-job": _object_schema(
        [*list(_LEASE_AUTH), "output"],
        {
            **_LEASE_AUTH, "output": {"type": "object"},
            "execution_metadata": EXECUTION_METADATA_SCHEMA,
            "artifact_refs": {"type": "array", "items": _ID, "uniqueItems": True},
        },
    ),
    "fail-job": _object_schema(
        [*list(_LEASE_AUTH), "error"],
        {**_LEASE_AUTH, "error": ERROR_INFO_SCHEMA, "execution_metadata": EXECUTION_METADATA_SCHEMA},
    ),
    "report-unknown-job-result": _object_schema(
        [*list(_LEASE_AUTH), "error", "execution_metadata"],
        {**_LEASE_AUTH, "error": ERROR_INFO_SCHEMA, "execution_metadata": EXECUTION_METADATA_SCHEMA},
    ),
    "expire-lease": _object_schema(
        ["observed_at", "fencing_token"], {"observed_at": _DATE_TIME, "fencing_token": _REVISION},
    ),
    "cancel-job": _object_schema([], {"reason": {"type": "string"}}),
    "schedule-timer": _object_schema(
        ["purpose", "dedupe_key", "target_type", "target_id", "payload_schema_version", "payload", "due_at"],
        {
            "purpose": {"type": "string", "minLength": 1},
            "dedupe_key": {"type": "string", "minLength": 1},
            "target_type": {"type": "string", "minLength": 1}, "target_id": _ID,
            "payload_schema_version": {"type": "string", "minLength": 1},
            "payload": {"type": "object"}, "due_at": _DATE_TIME,
        },
    ),
    "claim-timer": _object_schema(
        ["worker_id", "token_hash", "lease_expires_at", "observed_at"],
        {
            "worker_id": {"type": "string", "minLength": 1},
            "token_hash": {"type": "string", "minLength": 1},
            "lease_expires_at": _DATE_TIME, "observed_at": _DATE_TIME,
        },
    ),
    "fire-timer": _object_schema(
        list(_TIMER_LEASE_AUTH), dict(_TIMER_LEASE_AUTH)
    ),
    "expire-timer-lease": _object_schema(
        ["observed_at", "fencing_token"], {"observed_at": _DATE_TIME, "fencing_token": _REVISION},
    ),
    "cancel-timer": _object_schema([], {"reason": {"type": "string"}}),
    "materialize-job": _object_schema([], {}),
}

DURABLE_EVENT_PAYLOAD_SCHEMAS = {
    "job-created": _object_schema(
        ["run_id", "node_run_id", "job_kind", "execution_safety"],
        {
            "run_id": _ID, "node_run_id": _ID,
            "job_kind": {"type": "string", "minLength": 1},
            "execution_safety": {"enum": ["replay_safe", "unknown_on_lease_loss"]},
        },
    ),
    "job-transitioned": _object_schema(
        ["machine", "from", "to"],
        {**_TRANSITION_BASE, "available_at": _DATE_TIME},
    ),
    "job-attempt-assigned": _object_schema(
        ["attempt_id", "attempt_number", "lease_id", "fencing_token"],
        {"attempt_id": _ID, "attempt_number": _REVISION, "lease_id": _ID, "fencing_token": _REVISION},
    ),
    "lease-created": _object_schema(
        ["job_id", "attempt_id", "worker_id", "fencing_token", "expires_at"],
        {
            "job_id": _ID, "attempt_id": _ID,
            "worker_id": {"type": "string", "minLength": 1},
            "fencing_token": _REVISION, "expires_at": _DATE_TIME,
        },
    ),
    "lease-transitioned": _object_schema(
        ["machine", "from", "to"], dict(_TRANSITION_BASE)
    ),
    "timer-created": _object_schema(
        ["run_id", "purpose", "dedupe_key", "target_type", "target_id", "due_at"],
        {
            "run_id": _ID, "purpose": {"type": "string", "minLength": 1},
            "dedupe_key": {"type": "string", "minLength": 1},
            "target_type": {"type": "string", "minLength": 1}, "target_id": _ID,
            "due_at": _DATE_TIME,
        },
    ),
    "timer-transitioned": _object_schema(
        ["machine", "from", "to"], dict(_TRANSITION_BASE)
    ),
    "timer-fired": _object_schema(
        ["fired_at", "outcome"],
        {"fired_at": _DATE_TIME, "outcome": {"enum": ["applied", "obsolete"]}},
    ),
    "attempt-usage-recorded": _object_schema(
        ["usage", "usage_incomplete", "provider_request_id", "recorded_at"],
        {
            "usage": {**USAGE_SNAPSHOT_SCHEMA, "type": ["object", "null"]},
            "usage_incomplete": {"type": "boolean"},
            "provider_request_id": {"type": ["string", "null"]},
            "recorded_at": _DATE_TIME,
        },
    ),
}

EXECUTION_PLAN_PORT_SCHEMA = _object_schema(
    [
        "id", "schema_id", "required", "has_default", "default",
        "description", "data_policy",
    ],
    {
        "id": {"type": "string", "minLength": 1},
        "schema_id": {"type": "string", "minLength": 1},
        "required": {"type": "boolean"}, "has_default": {"type": "boolean"},
        "default": {}, "description": {"type": "string"},
        "data_policy": PORT_DATA_POLICY_SCHEMA,
    },
)

EXECUTION_PLAN_NODE_SCHEMA = _object_schema(
    [
        "node_id", "kind", "handler_name", "handler_version",
        "handler_manifest_fingerprint", "inputs", "outputs", "config",
    ],
    {
        "node_id": {"type": "string", "minLength": 1},
        "kind": {"enum": ["action", "human", "decision", "join", "terminal"]},
        "handler_name": {"type": ["string", "null"]},
        "handler_version": {"type": ["string", "null"]},
        "handler_manifest_fingerprint": {**_HASH, "type": ["string", "null"]},
        "inputs": {"type": "array", "items": EXECUTION_PLAN_PORT_SCHEMA},
        "outputs": {"type": "array", "items": EXECUTION_PLAN_PORT_SCHEMA},
        "config": {},
    },
)

EXECUTION_PLAN_SCHEMA = _object_schema(
    [
        "schema_version", "plan_id", "run_id", "plan_version", "workflow_id",
        "workflow_version", "workflow_definition_hash", "entry_node_id",
        "terminal_node_id", "ordered_node_ids", "nodes", "successors", "mappings",
    ],
    {
        "schema_version": {"enum": ["1.1"]}, "plan_id": _ID, "run_id": _ID,
        "plan_version": _REVISION, "workflow_id": _ID,
        "workflow_version": _REVISION, "workflow_definition_hash": _HASH,
        "entry_node_id": {"type": "string", "minLength": 1},
        "terminal_node_id": {"type": "string", "minLength": 1},
        "ordered_node_ids": {"type": "array", "items": {"type": "string"}},
        "nodes": {"type": "array", "items": EXECUTION_PLAN_NODE_SCHEMA},
        "successors": {"type": "object"}, "mappings": {"type": "object"},
    },
)

# Static Graph 1.2 contracts are registered independently from ExecutionPlan
# 1.1.  The compiler switches to them in Step 8 T02/T03; this separation keeps
# the currently running linear Kernel compatible while the contracts settle.
TOKEN_SCOPE_1_2_SCHEMA = _object_schema(
    ["plan_version", "edge_id", "target_node_id", "target_generation", "branch_group"],
    {
        "plan_version": _REVISION,
        "edge_id": {"type": "string", "minLength": 1},
        "target_node_id": {"type": "string", "minLength": 1},
        "target_generation": _REVISION,
        "branch_group": {"type": ["string", "null"]},
    },
)

PLAN_EDGE_1_2_SCHEMA = _object_schema(
    [
        "edge_id", "source_node_id", "target_node_id", "route", "priority",
        "source_port", "target_port", "condition", "mapping", "back_edge",
        "policy_ref",
    ],
    {
        "edge_id": {"type": "string", "minLength": 1},
        "source_node_id": {"type": "string", "minLength": 1},
        "target_node_id": {"type": "string", "minLength": 1},
        "route": {"enum": ["success", "error", "timeout", "cancel"]},
        "priority": {"type": "integer", "minimum": 0},
        "source_port": {"type": ["string", "null"]},
        "target_port": {"type": ["string", "null"]},
        "condition": {},
        "mapping": {},
        "back_edge": {"type": "boolean"},
        "policy_ref": {"type": ["string", "null"]},
    },
)

RETRY_POLICY_1_2_SCHEMA = _object_schema(
    ["max_attempts", "backoff_seconds", "categories"],
    {
        "max_attempts": _REVISION,
        "backoff_seconds": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "categories": {
            "type": "array",
            "items": {
                "enum": [
                    "validation_error", "policy_rejected", "transient_error",
                    "permanent_error", "timeout", "cancelled", "lost",
                ]
            },
        },
    },
)

REWORK_POLICY_1_2_SCHEMA = _object_schema(
    ["max_generations", "exhaustion"],
    {
        "max_generations": _REVISION,
        "exhaustion": {"enum": ["fail", "error_route"]},
    },
)

LOOP_POLICY_1_2_SCHEMA = _object_schema(
    ["max_iterations", "exhaustion"],
    {
        "max_iterations": _REVISION,
        "exhaustion": {"enum": ["fail", "error_route"]},
    },
)

JOIN_POLICY_1_2_SCHEMA = _object_schema(
    ["mode", "merge_mode", "threshold", "deadline_seconds", "min_successful"],
    {
        "mode": {"enum": ["all", "any", "n_of_m", "all_successful", "deadline"]},
        "merge_mode": {
            "enum": ["single", "array_by_edge", "object_by_edge", "first_by_priority"]
        },
        "threshold": {"type": ["integer", "null"], "minimum": 1},
        "deadline_seconds": {"type": ["integer", "null"], "minimum": 1},
        "min_successful": {"type": ["integer", "null"], "minimum": 1},
    },
)

ROUTE_DECISION_1_2_SCHEMA = _object_schema(
    [
        "node_run_id", "route", "mode", "evaluated_edge_ids",
        "selected_edge_ids", "not_selected_edge_ids", "context_hash",
    ],
    {
        "node_run_id": _ID,
        "route": {"enum": ["success", "error", "timeout", "cancel"]},
        "mode": {"enum": ["exclusive", "parallel"]},
        "evaluated_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "selected_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "not_selected_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "context_hash": _HASH,
    },
)

JOIN_DECISION_1_2_SCHEMA = _object_schema(
    [
        "join_group_id", "disposition", "participant_edge_ids",
        "settled_edge_ids", "winner_edge_ids", "ignored_edge_ids",
        "merged_input_hash",
    ],
    {
        "join_group_id": _ID,
        "disposition": {"enum": ["wait", "open", "fail", "timed_out"]},
        "participant_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "settled_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "winner_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "ignored_edge_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "merged_input_hash": {**_HASH, "type": ["string", "null"]},
    },
)

COMPLETION_DECISION_1_2_SCHEMA = _object_schema(
    [
        "disposition", "reason", "terminal_node_run_ids",
        "active_responsibility_ids", "waiting_reason",
    ],
    {
        "disposition": {"enum": ["continue", "wait", "succeed", "fail"]},
        "reason": {"type": "string", "minLength": 1},
        "terminal_node_run_ids": {"type": "array", "items": _ID},
        "active_responsibility_ids": {"type": "array", "items": _ID},
        "waiting_reason": {"type": ["string", "null"]},
    },
)

GRAPH_EXECUTION_PLAN_SCHEMA = _object_schema(
    [
        "schema_version", "plan_id", "run_id", "plan_version", "workflow_id",
        "workflow_version", "workflow_definition_hash", "entry_node_ids",
        "terminal_node_ids", "ordered_node_ids", "nodes", "edges",
        "outgoing_edges", "incoming_edges", "policies",
    ],
    {
        "schema_version": {"enum": ["1.2"]}, "plan_id": _ID, "run_id": _ID,
        "plan_version": _REVISION, "workflow_id": _ID,
        "workflow_version": _REVISION, "workflow_definition_hash": _HASH,
        "entry_node_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "terminal_node_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "ordered_node_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "nodes": {"type": "array", "items": EXECUTION_PLAN_NODE_SCHEMA},
        "edges": {"type": "array", "items": PLAN_EDGE_1_2_SCHEMA},
        "outgoing_edges": {"type": "object"},
        "incoming_edges": {"type": "object"},
        "policies": {"type": "object"},
    },
)

def _planner_action_variant(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _object_schema(
        ["kind", "arguments"],
        {"kind": {"const": kind}, "arguments": _object_schema(list(arguments), arguments)},
    )


PLANNER_ACTION_SCHEMA = {
    "type": "object",
    "oneOf": [
        _planner_action_variant("dispatch", {"handler": {"type": "string", "minLength": 1}, "inputs": {"type": "object"}, "config": {"type": "object"}}),
        _planner_action_variant("rework", {"node_id": {"type": "string", "minLength": 1}, "reason": {"type": "string", "minLength": 1}}),
        _planner_action_variant("request_input", {"prompt": {"type": "string", "minLength": 1}, "schema": {"type": "object"}}),
        _planner_action_variant("request_approval", {"operation": {"type": "string", "minLength": 1}, "scope": {"type": "object"}}),
        _planner_action_variant("cancel_branch", {"node_id": {"type": "string", "minLength": 1}, "reason": {"type": "string", "minLength": 1}}),
        _planner_action_variant("finish", {"outputs": {"type": "object"}}),
        _planner_action_variant("fail", {"code": {"type": "string", "minLength": 1}, "message": {"type": "string", "minLength": 1}}),
    ],
    "discriminator": {"propertyName": "kind"},
}

PLANNING_CONTEXT_SCHEMA = _object_schema(
    [
        "schema_version", "run_id", "plan_version", "goal", "graph_summary",
        "available_data_manifest", "available_capabilities", "remaining_limits",
        "recent_events",
    ],
    {
        "schema_version": {"const": "1.0"}, "run_id": _ID,
        "plan_version": _REVISION, "goal": {"type": "string", "minLength": 1},
        "graph_summary": {"type": "object"},
        "available_data_manifest": {"type": "array", "items": {"type": "object"}},
        "available_capabilities": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
        "remaining_limits": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
        "recent_events": {"type": "array", "items": {"type": "object"}},
    },
)

ACTION_PROPOSAL_SCHEMA = _object_schema(
    ["schema_version", "proposal_id", "run_id", "base_plan_version", "action", "reason"],
    {
        "schema_version": {"const": "1.0"}, "proposal_id": _ID,
        "run_id": _ID, "base_plan_version": _REVISION,
        "action": PLANNER_ACTION_SCHEMA, "reason": {"type": "string", "minLength": 1},
    },
)

PLANNER_USAGE_SCHEMA = _object_schema(
    ["input_tokens", "output_tokens", "cost_microunits", "incomplete"],
    {
        "input_tokens": {"type": "integer", "minimum": 0},
        "output_tokens": {"type": "integer", "minimum": 0},
        "cost_microunits": {"type": "integer", "minimum": 0},
        "incomplete": {"type": "boolean"},
    },
)

PLANNER_ATTEMPT_SCHEMA = _object_schema(
    [
        "attempt_id", "run_id", "attempt_number", "status", "context",
        "prompt_hash", "capability_manifest_hash", "model_id", "provider_id",
        "request_fingerprint", "raw_response", "raw_response_checksum",
        "provider_request_id", "usage", "proposal_id", "error", "lease_owner",
        "lease_token_hash", "fencing_token", "lease_expires_at",
        "aggregate_version", "created_at", "updated_at",
    ],
    {
        "attempt_id": _ID, "run_id": _ID, "attempt_number": _REVISION,
        "status": {"enum": ["requested", "running", "response_received", "accepted", "rejected", "unknown", "failed"]},
        "context": PLANNING_CONTEXT_SCHEMA, "prompt_hash": _HASH,
        "capability_manifest_hash": _HASH, "model_id": {"type": "string", "minLength": 1},
        "provider_id": {"type": "string", "minLength": 1}, "request_fingerprint": _HASH,
        "raw_response": {"type": ["string", "null"]},
        "raw_response_checksum": {**_HASH, "type": ["string", "null"]},
        "provider_request_id": {"type": ["string", "null"]},
        "usage": {**PLANNER_USAGE_SCHEMA, "type": ["object", "null"]},
        "proposal_id": {**_ID, "type": ["string", "null"]},
        "error": {"type": ["object", "null"]}, "lease_owner": {"type": ["string", "null"]},
        "lease_token_hash": {"type": ["string", "null"]},
        "fencing_token": {"type": "integer", "minimum": 0},
        "lease_expires_at": {"type": ["string", "null"], "format": "date-time"},
        "aggregate_version": {"type": "integer", "minimum": 0},
        "created_at": _DATE_TIME, "updated_at": _DATE_TIME,
    },
)

PLANNER_PROPOSAL_RECORD_SCHEMA = _object_schema(
    ["proposal", "attempt_id", "status", "validation", "raw_response_checksum", "created_at"],
    {
        "proposal": ACTION_PROPOSAL_SCHEMA, "attempt_id": _ID,
        "status": {"enum": ["parsed", "protocol_accepted", "protocol_rejected", "consumed"]},
        "validation": {"type": "object"}, "raw_response_checksum": _HASH,
        "created_at": _DATE_TIME,
    },
)

PLANNER_EVENT_PAYLOAD_SCHEMAS = {
    "planner-decision-requested": _object_schema(
        ["run_id", "attempt_number", "context_hash", "prompt_hash", "capability_manifest_hash", "model_id", "provider_id", "request_fingerprint"],
        {"run_id": _ID, "attempt_number": _REVISION, "context_hash": _HASH, "prompt_hash": _HASH,
         "capability_manifest_hash": _HASH, "model_id": {"type": "string"}, "provider_id": {"type": "string"}, "request_fingerprint": _HASH},
    ),
    "planner-attempt-started": _object_schema(
        ["worker_id", "fencing_token", "lease_expires_at"],
        {"worker_id": {"type": "string", "minLength": 1}, "fencing_token": _REVISION, "lease_expires_at": _DATE_TIME},
    ),
    "planner-response-received": _object_schema(
        ["raw_response_checksum", "raw_response_size", "provider_request_id", "usage"],
        {"raw_response_checksum": _HASH, "raw_response_size": {"type": "integer", "minimum": 0},
         "provider_request_id": {"type": ["string", "null"]}, "usage": PLANNER_USAGE_SCHEMA},
    ),
    "planner-proposal-parsed": _object_schema(
        ["proposal_id", "content_hash"], {"proposal_id": _ID, "content_hash": _HASH},
    ),
    "planner-proposal-accepted": _object_schema(
        ["proposal_id", "protocol_only"], {"proposal_id": _ID, "protocol_only": {"const": True}},
    ),
    "planner-proposal-rejected": _object_schema(
        ["code", "message"], {"code": {"type": "string"}, "message": {"type": "string"}},
    ),
    "planner-attempt-unknown": _object_schema(
        ["reason", "usage"], {"reason": {"type": "string"}, "usage": PLANNER_USAGE_SCHEMA},
    ),
    "planner-attempt-failed": _object_schema(
        ["code", "message"], {"code": {"type": "string"}, "message": {"type": "string"}},
    ),
    "planner-late-response-recorded": _object_schema(
        ["raw_response_checksum", "provider_request_id", "usage"],
        {"raw_response_checksum": _HASH, "provider_request_id": {"type": ["string", "null"]}, "usage": PLANNER_USAGE_SCHEMA},
    ),
    "planner-escalation-requested": _object_schema(
        ["reason", "attempts_exhausted"],
        {"reason": {"type": "string", "minLength": 1}, "attempts_exhausted": {"type": "integer", "minimum": 1}},
    ),
}

PATCH_OPERATION_SCHEMA = _object_schema(
    ["kind", "target_id", "value"],
    {"kind": {"enum": ["add_node", "add_edge", "remove_pending_node", "remove_pending_edge", "replace_pending_node"]},
     "target_id": {"type": "string", "minLength": 1}, "value": {"type": ["object", "null"]}},
)
PLAN_PATCH_SCHEMA = _object_schema(
    ["patch_id", "proposal_id", "run_id", "base_plan_version", "reason", "operations", "content_hash", "schema_version"],
    {"patch_id": _ID, "proposal_id": _ID, "run_id": _ID, "base_plan_version": _REVISION,
     "reason": {"type": "string", "minLength": 1}, "operations": {"type": "array", "items": PATCH_OPERATION_SCHEMA},
     "content_hash": _HASH, "schema_version": {"const": "1.0"}},
)
POLICY_DECISION_SCHEMA = _object_schema(
    ["decision_id", "run_id", "patch_id", "input_hash", "rule_set_version", "allowed", "requires_approval", "results", "reasons"],
    {"decision_id": _ID, "run_id": _ID, "patch_id": _ID, "input_hash": _HASH,
     "rule_set_version": {"type": "string"}, "allowed": {"type": "boolean"},
     "requires_approval": {"type": "boolean"}, "results": {"type": "array", "items": {"type": "object"}},
     "reasons": {"type": "array", "items": {"type": "string"}},},
)
HUMAN_TASK_SCHEMA = _object_schema(
    ["task_id", "run_id", "kind", "status", "request_hash", "submission_token_hash", "payload", "assignee", "role", "form_schema", "participants", "quorum", "quorum_count", "deadline_at", "reminder_interval_seconds", "escalation_policy", "version"],
    {"task_id": _ID, "run_id": _ID, "kind": {"enum": ["approval", "input", "budget", "recovery"]},
     "status": {"enum": ["waiting", "claimed", "completed", "rejected", "cancelled", "expired"]},
     "request_hash": {"type": "string", "minLength": 1}, "submission_token_hash": {"type": "string", "minLength": 1},
     "payload": {"type": "object"}, "assignee": {"type": ["string", "null"]}, "role": {"type": ["string", "null"]},
     "form_schema": {"type": ["object", "null"]}, "participants": {"type": "array", "items": {"type": "string"}},
     "quorum": {"enum": ["any", "all", "n_of_m"]}, "quorum_count": _REVISION,
     "deadline_at": {"type": ["string", "null"], "format": "date-time"},
     "reminder_interval_seconds": {"type": ["integer", "null"], "minimum": 1},
     "escalation_policy": {"type": ["object", "null"]}, "version": {"type": "integer", "minimum": 0}},
)
ITEM_SCOPE_SCHEMA = _object_schema(
    ["run_id", "group_id", "item_id", "item_key", "item_index", "plan_version", "artifact_ids", "secret_refs"],
    {"run_id": _ID, "group_id": _ID, "item_id": _ID, "item_key": {"type": "string", "minLength": 1},
     "item_index": {"type": "integer", "minimum": 0}, "plan_version": _REVISION,
     "artifact_ids": {"type": "array", "items": _ID}, "secret_refs": {"type": "array", "items": {"type": "string"}}},
)


CONTRACT_SCHEMAS = MappingProxyType(
    {
        "command-envelope/1.0": freeze_json(COMMAND_ENVELOPE_SCHEMA),
        "event-envelope/1.0": freeze_json(EVENT_ENVELOPE_SCHEMA),
        "workflow-version-ref/1.0": freeze_json(WORKFLOW_VERSION_REF_SCHEMA),
        "workflow-run-ref/1.0": freeze_json(WORKFLOW_RUN_REF_SCHEMA),
        "execution-plan-ref/1.0": freeze_json(EXECUTION_PLAN_REF_SCHEMA),
        "node-run-ref/1.0": freeze_json(NODE_RUN_REF_SCHEMA),
        "attempt-ref/1.0": freeze_json(ATTEMPT_REF_SCHEMA),
        "error-info/1.0": freeze_json(ERROR_INFO_SCHEMA),
        "usage-snapshot/1.0": freeze_json(USAGE_SNAPSHOT_SCHEMA),
        "handler-result/1.0": freeze_json(HANDLER_RESULT_SCHEMA),
        "resource-profile/1.0": freeze_json(RESOURCE_PROFILE_SCHEMA),
        "handler-manifest/1.0": freeze_json(HANDLER_MANIFEST_SCHEMA),
        "budget-reservation/1.0": freeze_json(BUDGET_RESERVATION_SCHEMA),
        "budget-account/1.0": freeze_json(BUDGET_ACCOUNT_SCHEMA),
        "value/1.0": freeze_json(VALUE_SCHEMA),
        "artifact-ref/1.0": freeze_json(ARTIFACT_REF_SCHEMA),
        "port-data-policy/1.0": freeze_json(PORT_DATA_POLICY_SCHEMA),
        "secret-ref/1.0": freeze_json(SECRET_REF_SCHEMA),
        "value-commit/1.0": freeze_json(VALUE_COMMIT_SCHEMA),
        "staged-artifact-commit/1.0": freeze_json(STAGED_ARTIFACT_COMMIT_SCHEMA),
        "data-commit-manifest/1.0": freeze_json(DATA_COMMIT_MANIFEST_SCHEMA),
        "value-record/1.0": freeze_json(VALUE_RECORD_SCHEMA),
        "value-link/1.0": freeze_json(VALUE_LINK_SCHEMA),
        "artifact-metadata/1.0": freeze_json(ARTIFACT_METADATA_SCHEMA),
        "artifact-link/1.0": freeze_json(ARTIFACT_LINK_SCHEMA),
        "input-manifest/1.0": freeze_json(INPUT_MANIFEST_SCHEMA),
        "execution-plan/1.1": freeze_json(EXECUTION_PLAN_SCHEMA),
        "execution-plan/1.2": freeze_json(GRAPH_EXECUTION_PLAN_SCHEMA),
        "graph-token-scope/1.2": freeze_json(TOKEN_SCOPE_1_2_SCHEMA),
        "graph-plan-edge/1.2": freeze_json(PLAN_EDGE_1_2_SCHEMA),
        "graph-retry-policy/1.2": freeze_json(RETRY_POLICY_1_2_SCHEMA),
        "graph-rework-policy/1.2": freeze_json(REWORK_POLICY_1_2_SCHEMA),
        "graph-loop-policy/1.2": freeze_json(LOOP_POLICY_1_2_SCHEMA),
        "graph-join-policy/1.2": freeze_json(JOIN_POLICY_1_2_SCHEMA),
        "graph-route-decision/1.2": freeze_json(ROUTE_DECISION_1_2_SCHEMA),
        "graph-join-decision/1.2": freeze_json(JOIN_DECISION_1_2_SCHEMA),
        "graph-completion-decision/1.2": freeze_json(COMPLETION_DECISION_1_2_SCHEMA),
        "planning-context/1.0": freeze_json(PLANNING_CONTEXT_SCHEMA),
        "planner-action/1.0": freeze_json(PLANNER_ACTION_SCHEMA),
        "action-proposal/1.0": freeze_json(ACTION_PROPOSAL_SCHEMA),
        "planner-usage/1.0": freeze_json(PLANNER_USAGE_SCHEMA),
        "planner-attempt/1.0": freeze_json(PLANNER_ATTEMPT_SCHEMA),
        "planner-proposal-record/1.0": freeze_json(PLANNER_PROPOSAL_RECORD_SCHEMA),
        "plan-patch/1.0": freeze_json(PLAN_PATCH_SCHEMA),
        "policy-decision/1.0": freeze_json(POLICY_DECISION_SCHEMA),
        "human-task/1.0": freeze_json(HUMAN_TASK_SCHEMA),
        "foreach-item-scope/1.0": freeze_json(ITEM_SCOPE_SCHEMA),
        **{
            f"planner-event/{name}/1.0": freeze_json(schema)
            for name, schema in PLANNER_EVENT_PAYLOAD_SCHEMAS.items()
        },
        **{
            f"runtime-command/{name}/1.0": freeze_json(schema)
            for name, schema in RUNTIME_COMMAND_PAYLOAD_SCHEMAS.items()
        },
        **{
            f"runtime-event/{name}/1.0": freeze_json(schema)
            for name, schema in RUNTIME_EVENT_PAYLOAD_SCHEMAS.items()
        },
        **{
            f"durable-command/{name}/1.0": freeze_json(schema)
            for name, schema in DURABLE_COMMAND_PAYLOAD_SCHEMAS.items()
        },
        **{
            f"durable-event/{name}/1.0": freeze_json(schema)
            for name, schema in DURABLE_EVENT_PAYLOAD_SCHEMAS.items()
        },
    }
)


def schema_for(contract: str) -> Mapping[str, Any]:
    try:
        return CONTRACT_SCHEMAS[contract]
    except KeyError:
        raise KeyError(f"unknown workflow contract schema: {contract}") from None


class SchemaValidationError(ValueError):
    """A schema error with an exact JSON path."""

    def __init__(self, path: tuple[str | int, ...], message: str) -> None:
        self.path = path
        self.json_path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in path
        )
        self.message = message
        super().__init__(f"{self.json_path}: {message}")


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, MappingABC),
        "array": isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray)),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def validate_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: tuple[str | int, ...] = (),
) -> None:
    """Validate the JSON Schema subset used by the 1.0 domain contracts."""

    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(path, f"value must equal {schema['const']!r}")

    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                validate_schema(value, candidate, path=path)
                matches += 1
            except SchemaValidationError:
                pass
        if matches != 1:
            raise SchemaValidationError(path, "value must match exactly one schema variant")

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, item) for item in expected_types):
            raise SchemaValidationError(path, f"expected {' or '.join(expected_types)}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(path, f"value must be one of {schema['enum']!r}")

    if isinstance(value, MappingABC):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                raise SchemaValidationError(path + (required,), "required field is missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    raise SchemaValidationError(path + (key,), "additional property is not allowed")
        for key, child_schema in properties.items():
            if key in value:
                validate_schema(value[key], child_schema, path=path + (key,))

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, path=path + (index,))

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if minimum_length is not None and len(value) < minimum_length:
            raise SchemaValidationError(path, f"minimum length is {minimum_length}")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise SchemaValidationError(path, f"value does not match pattern {pattern!r}")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise SchemaValidationError(path, "invalid date-time") from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise SchemaValidationError(path, "date-time must include a timezone")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise SchemaValidationError(path, f"minimum value is {minimum}")


def validate_contract(value: Any, contract: str) -> None:
    validate_schema(value, schema_for(contract))
