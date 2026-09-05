"""One public LangGraph Run projection shared by HTTP and MCP."""

from __future__ import annotations

from typing import Any


def langgraph_run_dto(run, *, can_write: bool) -> dict[str, Any]:
    """Project a Run without leaking its internal owner identity.

    Commands are authorization-dependent affordances, not status-derived
    client knowledge.  Both transports therefore pass the caller's write
    verdict into this one projection.
    """

    commands = []
    if can_write and run.status == "interrupted":
        commands.append({
            "command": "langgraph_run.resume",
            "label": "Resume LangGraph workflow",
            "method": "POST",
            "href": f"/api/v1/langgraph-runs/{run.run_id}/resume",
            "target_aggregate_id": run.run_id,
            "expected_version": run.revision,
            "payload_schema": "langgraph-run-resume/1.0",
        })
    if can_write and run.status in {"running", "waiting", "interrupted"}:
        commands.append({
            "command": "langgraph_run.cancel",
            "label": "Cancel LangGraph workflow",
            "method": "POST",
            "href": f"/api/v1/langgraph-runs/{run.run_id}/cancel",
            "target_aggregate_id": run.run_id,
            "expected_version": run.revision,
            "payload_schema": "langgraph-run-cancel/1.0",
            "confirmation": "explicit",
        })
    return {
        "run_id": run.run_id,
        "goal": run.goal,
        # What the run was asked to work on, beside what it answered. `goal` is
        # the label the work was given and is often a summary of this rather
        # than a copy of it — a reader shown only the label cannot see the
        # request. Safe to return to a caller who is by definition the one who
        # sent it, and `result`, already here, is derived from it.
        "inputs": dict(run.inputs),
        "artifact_count": run.artifact_count,
        "workflow_id": run.workflow_id,
        "workflow_version": run.workflow_version,
        "template_id": run.template_id,
        "agent_binding": run.agent_binding,
        "execution_mode": run.execution_mode,
        "status": run.status,
        "revision": run.revision,
        "result": run.result,
        "interrupts": list(run.interrupts),
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "allowed_commands": commands,
    }
