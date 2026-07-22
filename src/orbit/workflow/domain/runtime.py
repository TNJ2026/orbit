"""Stable deterministic Runtime Kernel results and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .ids import EntityId
from .serialization import freeze_json
from .versions import AggregateVersion


class CommandResultDisposition(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class KernelDiagnostic:
    code: str
    message: str
    aggregate_id: EntityId | None = None
    expected_version: int | None = None
    actual_version: int | None = None
    retryable: bool = False
    details: Mapping[str, Any] = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("diagnostic code and message are required")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details or {})))


@dataclass(frozen=True)
class CommandResult:
    disposition: CommandResultDisposition
    event_ids: tuple[EntityId, ...] = ()
    primary_version: AggregateVersion | None = None
    diagnostics: tuple[KernelDiagnostic, ...] = ()
    summary: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_ids", tuple(self.event_ids))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "summary", freeze_json(self.summary or {}))


RUNTIME_COMMAND_TYPES = frozenset(
    {"start_run", "schedule_node", "start_attempt", "complete_attempt", "fail_attempt", "cancel_node", "cancel_run", "advance_graph", "advance_foreach", "submit_human_task", "apply_planner_proposal", "reject_planner_proposal", "apply_subflow_result",
     "retry_node_run"}
)

RUNTIME_EVENT_VERSIONS = MappingProxyType(
    {
        "workflow_run_transitioned": 1,
        "node_run_transitioned": 1,
        "attempt_transitioned": 1,
        "node_input_prepared": 1,
        "attempt_output_recorded": 1,
        "attempt_failed_recorded": 1,
        "graph_route_decided": 1,
        "branch_token_transitioned": 1,
        "join_decided": 1,
        "control_counter_incremented": 1,
        "foreach_advanced": 1,
    }
)


_COMMAND_FIELDS = MappingProxyType(
    {
        "start_run": (
            {"workflow_id", "workflow_version", "definition_hash"},
            {"input", "artifact_inputs", "artifact_subjects", "artifact_scope", "budget_microunits", "goal"},
        ),
        "schedule_node": ({"run_id", "node_id"}, {"plan_version", "input"}),
        "start_attempt": (set(), set()),
        "complete_attempt": ({"output"}, {"artifact_refs"}),
        "fail_attempt": ({"error"}, set()),
        "cancel_run": (set(), {"reason"}),
        "cancel_node": (set(), {"reason"}),
        "advance_graph": (set(), {"plan_version"}),
        "advance_foreach": (set(), set()),
        "submit_human_task": (
            {"submission_token", "decision"}, {"value"},
        ),
        "apply_planner_proposal": ({"proposal_id"}, {"plan_version"}),
        "reject_planner_proposal": ({"proposal_id", "error"}, set()),
        "apply_subflow_result": ({"link_id"}, set()),
        "retry_node_run": (set(), {"reason"}),
    }
)


def validate_runtime_command_payload(command_type: str, payload: Mapping[str, Any]) -> None:
    try:
        required, optional = _COMMAND_FIELDS[command_type]
    except KeyError:
        raise ValueError(f"unsupported Runtime command {command_type}") from None
    missing = required - set(payload)
    extra = set(payload) - required - optional
    if missing:
        raise ValueError(f"missing command payload fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"unknown command payload fields: {sorted(extra)}")
    object_fields = {
        "start_run": ("input",), "schedule_node": ("input",),
        "complete_attempt": ("output",), "fail_attempt": ("error",),
        "reject_planner_proposal": ("error",),
    }.get(command_type, ())
    for field in object_fields:
        if field in payload and not isinstance(payload[field], Mapping):
            raise ValueError(f"$.payload.{field} must be an object")
    if command_type == "start_run":
        if not isinstance(payload["workflow_version"], int) or isinstance(payload["workflow_version"], bool):
            raise ValueError("$.payload.workflow_version must be an integer")
    if command_type == "schedule_node" and "plan_version" in payload:
        if not isinstance(payload["plan_version"], int) or isinstance(payload["plan_version"], bool):
            raise ValueError("$.payload.plan_version must be an integer")
    if command_type in {"apply_planner_proposal", "reject_planner_proposal"} and not str(payload["proposal_id"]).startswith("proposal:"):
        raise ValueError("$.payload.proposal_id must be a Proposal id")


def validate_runtime_event_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    if event_type not in RUNTIME_EVENT_VERSIONS:
        raise ValueError(f"unregistered Runtime event type {event_type}")
    required = {
        "workflow_run_transitioned": {"machine", "from", "to"},
        "node_run_transitioned": {"machine", "from", "to", "node_id"},
        "attempt_transitioned": {"machine", "from", "to", "node_run_id", "attempt_number"},
        "node_input_prepared": {"run_id", "node_id", "input"},
        "attempt_output_recorded": {"run_id", "node_run_id", "output"},
        "attempt_failed_recorded": {"run_id", "node_run_id", "error"},
        "graph_route_decided": {"run_id", "node_run_id", "decision"},
        "branch_token_transitioned": {"machine", "from", "to", "run_id", "edge_id", "target_node_id", "target_generation"},
        "join_decided": {"run_id", "join_group_id", "decision"},
        "control_counter_incremented": {"run_id", "policy_id", "scope_key", "value", "limit"},
        "foreach_advanced": {"run_id", "group_id", "status", "item_count"},
    }[event_type]
    missing = required - set(payload)
    if missing:
        raise ValueError(f"missing Runtime event payload fields: {sorted(missing)}")
    optional = {
        "workflow_run_transitioned": {
            "workflow_id", "workflow_version", "definition_hash", "plan_id",
            "plan_version", "input", "goal", "artifact_refs", "reason",
        },
        "node_run_transitioned": {"run_id", "plan_version", "generation", "activation_key"},
        "attempt_transitioned": {"run_id"},
        "node_input_prepared": set(),
        "attempt_output_recorded": {"artifact_refs"},
        "attempt_failed_recorded": set(),
        "graph_route_decided": set(),
        "branch_token_transitioned": {"scope"},
        "join_decided": {"input"},
        "control_counter_incremented": set(),
        "foreach_advanced": set(),
    }[event_type]
    extra = set(payload) - required - optional
    if extra:
        raise ValueError(f"unknown Runtime event payload fields: {sorted(extra)}")
