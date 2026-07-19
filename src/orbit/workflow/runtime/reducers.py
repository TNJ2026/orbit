"""Pure reducers for Runtime aggregate and run-view events."""

from __future__ import annotations

from typing import Any, Mapping

from ..domain.envelopes import EventEnvelope
from ..domain.persistence import StoredEvent
from ..domain.states import (
    AttemptStatus, JobStatus, LeaseStatus, NodeRunStatus, TimerStatus,
    WorkflowRunStatus, validate_transition,
)


def _transition(state, event: EventEnvelope, expected_type: str, enum_type, initial):
    if event.event_type != expected_type:
        return state
    source = enum_type(event.payload["from"])
    target = enum_type(event.payload["to"])
    state = initial if state is None else state
    if state != source:
        raise ValueError(f"event source {source.value} does not match state {state.value}")
    validate_transition(source, target)
    return target


def reduce_workflow_run(state: WorkflowRunStatus | None, event: EventEnvelope):
    return _transition(
        state, event, "workflow_run_transitioned", WorkflowRunStatus,
        WorkflowRunStatus.CREATED,
    )


def reduce_node_run(state: NodeRunStatus | None, event: EventEnvelope):
    return _transition(
        state, event, "node_run_transitioned", NodeRunStatus,
        NodeRunStatus.PENDING,
    )


def reduce_attempt(state: AttemptStatus | None, event: EventEnvelope):
    return _transition(
        state, event, "attempt_transitioned", AttemptStatus,
        AttemptStatus.CREATED,
    )


def reduce_run_view(state: Mapping[str, Any], stored: StoredEvent) -> dict[str, Any]:
    result = {
        "run_status": state.get("run_status"),
        "nodes": dict(state.get("nodes", {})),
        "attempts": dict(state.get("attempts", {})),
        "outputs": dict(state.get("outputs", {})),
        "jobs": dict(state.get("jobs", {})),
        "leases": dict(state.get("leases", {})),
        "timers": dict(state.get("timers", {})),
        "usage": dict(state.get("usage", {})),
    }
    for key in (
        "tokens", "joins", "routes", "counters", "foreach",
        "planner_attempts", "planner_proposals", "advanced",
    ):
        if key in state:
            result[key] = dict(state.get(key, {}))
    event = stored.envelope
    if event.event_type == "workflow_run_transitioned":
        current = result["run_status"] or WorkflowRunStatus.CREATED.value
        if current != event.payload["from"]:
            raise ValueError("RunView workflow transition source mismatch")
        result["run_status"] = event.payload["to"]
    elif event.event_type == "node_run_transitioned":
        prior = result["nodes"].get(str(event.aggregate_id), {}).get(
            "status", NodeRunStatus.PENDING.value
        )
        if prior != event.payload["from"]:
            raise ValueError("RunView node transition source mismatch")
        current_node = dict(result["nodes"].get(str(event.aggregate_id), {}))
        current_node.update({
            "node_id": event.payload["node_id"], "status": event.payload["to"]
        })
        for field in ("generation", "activation_key"):
            if field in event.payload:
                current_node[field] = event.payload[field]
        result["nodes"][str(event.aggregate_id)] = current_node
    elif event.event_type == "attempt_transitioned":
        prior = result["attempts"].get(str(event.aggregate_id), {}).get(
            "status", AttemptStatus.CREATED.value
        )
        if prior != event.payload["from"]:
            raise ValueError("RunView attempt transition source mismatch")
        result["attempts"][str(event.aggregate_id)] = {
            "node_run_id": event.payload["node_run_id"], "status": event.payload["to"]
        }
    elif event.event_type == "attempt_output_recorded":
        result["outputs"][str(event.aggregate_id)] = event.payload["output"]
    elif event.event_type == "attempt_usage_recorded":
        result["usage"][str(event.aggregate_id)] = dict(event.payload)
    elif event.event_type in {"node_input_prepared", "attempt_failed_recorded"}:
        pass
    elif event.event_type == "job_created":
        result["jobs"][str(event.aggregate_id)] = {
            "node_run_id": event.payload["node_run_id"], "status": JobStatus.READY.value
        }
    elif event.event_type == "job_transitioned":
        prior = result["jobs"].get(str(event.aggregate_id), {}).get("status", JobStatus.READY.value)
        if prior != event.payload["from"]:
            raise ValueError("RunView job transition source mismatch")
        current = dict(result["jobs"].get(str(event.aggregate_id), {}))
        current["status"] = event.payload["to"]
        if "available_at" in event.payload:
            current["available_at"] = event.payload["available_at"]
        result["jobs"][str(event.aggregate_id)] = current
    elif event.event_type == "job_attempt_assigned":
        current = dict(result["jobs"].get(str(event.aggregate_id), {}))
        current["attempt_id"] = event.payload["attempt_id"]
        result["jobs"][str(event.aggregate_id)] = current
    elif event.event_type == "lease_created":
        result["leases"][str(event.aggregate_id)] = {
            "job_id": event.payload["job_id"], "status": LeaseStatus.ACTIVE.value
        }
    elif event.event_type == "lease_transitioned":
        prior = result["leases"].get(str(event.aggregate_id), {}).get("status", LeaseStatus.ACTIVE.value)
        if prior != event.payload["from"]:
            raise ValueError("RunView lease transition source mismatch")
        current = dict(result["leases"].get(str(event.aggregate_id), {}))
        current["status"] = event.payload["to"]
        result["leases"][str(event.aggregate_id)] = current
    elif event.event_type == "timer_created":
        result["timers"][str(event.aggregate_id)] = {
            "purpose": event.payload["purpose"], "status": TimerStatus.SCHEDULED.value
        }
    elif event.event_type == "timer_transitioned":
        prior = result["timers"].get(str(event.aggregate_id), {}).get("status", TimerStatus.SCHEDULED.value)
        if prior != event.payload["from"]:
            raise ValueError("RunView timer transition source mismatch")
        current = dict(result["timers"].get(str(event.aggregate_id), {}))
        current["status"] = event.payload["to"]
        result["timers"][str(event.aggregate_id)] = current
    elif event.event_type == "timer_fired":
        pass
    elif event.event_type == "graph_route_decided":
        result.setdefault("routes", {})[str(event.aggregate_id)] = dict(event.payload["decision"])
    elif event.event_type == "branch_token_transitioned":
        tokens = result.setdefault("tokens", {})
        prior = tokens.get(str(event.aggregate_id), {}).get("status", "active")
        if prior != event.payload["from"]:
            raise ValueError("RunView BranchToken transition source mismatch")
        tokens[str(event.aggregate_id)] = {
            "edge_id": event.payload["edge_id"], "status": event.payload["to"],
            "target_node_id": event.payload["target_node_id"],
            "target_generation": event.payload["target_generation"],
            **({"scope": dict(event.payload["scope"])} if "scope" in event.payload else {}),
        }
    elif event.event_type == "join_decided":
        result.setdefault("joins", {})[str(event.aggregate_id)] = dict(event.payload["decision"])
    elif event.event_type == "control_counter_incremented":
        result.setdefault("counters", {})[str(event.aggregate_id)] = {
            "policy_id": event.payload["policy_id"], "scope_key": event.payload["scope_key"],
            "value": event.payload["value"], "limit": event.payload["limit"],
        }
    elif event.event_type == "foreach_advanced":
        result.setdefault("foreach", {})[str(event.aggregate_id)] = {
            "status": event.payload["status"],
            "item_count": event.payload["item_count"],
        }
    elif event.event_type == "planner_decision_requested":
        result.setdefault("planner_attempts", {})[str(event.aggregate_id)] = {
            "status": "requested", "attempt_number": event.payload["attempt_number"],
            "context_hash": event.payload["context_hash"],
        }
    elif event.event_type == "planner_attempt_started":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["status"] = "running"; current["fencing_token"] = event.payload["fencing_token"]
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_response_received":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["status"] = "response_received"
        current["raw_response_checksum"] = event.payload["raw_response_checksum"]
        current["usage"] = dict(event.payload["usage"])
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_proposal_parsed":
        result.setdefault("planner_proposals", {})[event.payload["proposal_id"]] = {
            "status": "parsed", "content_hash": event.payload["content_hash"],
            "attempt_id": str(event.aggregate_id),
        }
    elif event.event_type == "planner_proposal_accepted":
        proposal = result.setdefault("planner_proposals", {}).setdefault(event.payload["proposal_id"], {})
        proposal["status"] = "protocol_accepted"
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["status"] = "accepted"; current["proposal_id"] = event.payload["proposal_id"]
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_proposal_rejected":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["status"] = "rejected"; current["error"] = dict(event.payload)
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_attempt_unknown":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["status"] = "unknown"; current["usage"] = dict(event.payload["usage"])
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_attempt_failed":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["status"] = "failed"; current["error"] = dict(event.payload)
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_late_response_recorded":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["late_response_checksum"] = event.payload["raw_response_checksum"]
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type == "planner_escalation_requested":
        current = dict(result.setdefault("planner_attempts", {}).get(str(event.aggregate_id), {}))
        current["escalation_requested"] = True
        current["escalation_reason"] = event.payload["reason"]
        result["planner_attempts"][str(event.aggregate_id)] = current
    elif event.event_type in {
        "plan_patch_committed", "plan_patch_rejected",
        "human_task_created", "human_task_claimed", "human_task_submitted",
        "human_task_cancelled", "human_task_escalated",
        "budget_account_opened", "budget_reserved", "budget_usage_reported",
        "budget_reservation_settled", "budget_reservation_released", "budget_added",
        "foreach_group_created", "foreach_item_transitioned", "foreach_aggregated",
        "subflow_link_created", "subflow_link_transitioned",
        "recovery_action_applied", "capability_issued", "capability_revoked",
    }:
        advanced = result.setdefault("advanced", {})
        advanced[str(event.aggregate_id)] = {
            "event_type": event.event_type, "sequence": event.sequence.value,
            "payload": dict(event.payload),
        }
    else:
        raise ValueError(f"unknown Runtime event type {event.event_type!r}")
    return result
