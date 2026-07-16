"""Goal orchestration, step cards, dispatch, and workflow state transitions."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .node_handlers import get_node_handler, handler_requires_agent
from .process_control import terminate_pid_tree as _terminate_pid_tree
from .runner_prompts import (
    step_agent_command as _step_agent_command,
    step_round_robin_assignee as _step_round_robin_assignee,
)
from .runner_protocol import (
    normalized_step_result as _normalized_step_result,
    structured_upstream as _structured_upstream,
    tail as _tail,
)
from .workflow_data import (
    apply_input_mapping,
    build_mapping_context,
    evaluate_jsonlogic,
    resolve_path,
    validate_json_schema,
)
from .settings import read_settings
from .store import InvalidInputError, Store, UnknownAgentError
from .verification import detect_goal_verify as _detect_goal_verify
from .workflow_config import (
    _project_root,
    read_workflow_config,
    workflow_config_for_task,
)
from .workflow_graph import (
    _WORKFLOW_STATUS_OVERRIDES,
    active_step_assignees as _active_step_assignees,
    active_steps as _active_steps,
    dispatched_since as _dispatched_since,
    forward_out as _forward_out,
    join_ready as _join_ready,
    latest_inbound_completion_id as _latest_inbound_completion_id,
    main_workflow_reachable_steps as _main_workflow_reachable_steps,
    running_steps as _running_steps,
    workflow_derived_task_status as _workflow_derived_task_status,
    workflow_entry_steps as _workflow_entry_steps,
    workflow_execution_errors as _workflow_execution_errors,
    workflow_graph as _workflow_graph,
)
from .worktrees import (
    commit_goal_design_artifacts as _commit_goal_design_artifacts,
    ensure_git_repo as _ensure_git_repo,
    workflow_needs_git as _workflow_needs_git,
)

_WORKFLOW_ENGINE_LOCK = threading.RLock()

_log = logging.getLogger("orbit.workflow_engine")


def _ensure_workflow_run(
    store: Store,
    task_id: int,
    cfg: dict[str, Any],
    entry_steps: list[str],
    parent_run_id: int | None = None,
    parent_node_run_id: int | None = None,
) -> None:
    """Dual-write hook: open the workflow_run record (with the definition
    snapshot read at start, §4.1) when a task enters the workflow. Every later
    record-layer write hangs off this row and is mirrored inside the store's
    own transaction hooks. `parent_run_id`/`parent_node_run_id` link the run
    into the cross-run lineage tree (decompose split, subflow node). The record
    layer never drives routing, so a failure here is logged and swallowed —
    the engine keeps running on the old tables."""
    try:
        # Migration-period dual-write (design §11): a goal's token budget is
        # recorded in the run's variables at creation, but tasks.token_budget
        # remains the authoritative source the budget guard reads.
        variables: dict[str, Any] = {}
        task = store.get_task(task_id)
        if task and task.get("is_goal"):
            variables["token_budget"] = _coerce_token_budget(task.get("token_budget"))
        store.create_workflow_run(
            task_id,
            cfg,
            variables=variables or None,
            entry_steps=entry_steps,
            parent_run_id=parent_run_id,
            parent_node_run_id=parent_node_run_id,
        )
    except Exception:
        _log.exception(
            "workflow_run dual-write failed for task %s (non-fatal)", task_id
        )


def _run_lineage_for_step(
    store: Store, parent_task_id: int, step_id: str
) -> tuple[int | None, int | None]:
    """(parent_run_id, parent_node_run_id) anchoring a child run to the node
    execution of `step_id` in the parent's run. Best-effort: the record layer
    may be absent for pre-existing data, in which case lineage stays NULL."""
    try:
        run = store.get_workflow_run_by_task(parent_task_id)
        if not run:
            return None, None
        node = store.latest_node_run(int(run["id"]), step_id) if step_id else None
        return int(run["id"]), int(node["id"]) if node else None
    except Exception:
        _log.exception(
            "workflow run lineage lookup failed for task %s step %s (non-fatal)",
            parent_task_id, step_id,
        )
        return None, None


def _coerce_token_budget(value: Any) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return 0
    return budget if budget > 0 else 0

def goals_summary(
    store: Store, project_root: str | None = None
) -> list[dict[str, Any]]:
    """Goals with aggregated subtask progress for the Goals page. Children
    are linked via parent_task_id (subtasks reply to the goal's message).
    With a project_root, each subtask's visible status is workflow-projected so
    the Goals page and task board show the same lifecycle state; closed/blocked
    counters use override statuses, which projection preserves."""
    tasks = store.list_goals_with_children()
    cfg = read_workflow_config(project_root) if project_root is not None else None

    def _visible_status(sub: dict[str, Any]) -> str:
        if cfg is None:
            return sub["task_status"]
        return _project_workflow_task_status(store, project_root, sub, cfg)[
            "task_status"
        ]

    children: dict[int, list[dict[str, Any]]] = {}
    for task in tasks:
        parent = task.get("parent_task_id")
        if parent:
            children.setdefault(parent, []).append(task)
    goals = []
    for task in tasks:
        if not task.get("is_goal"):
            continue
        tokens_total = store.sum_goal_tokens(task["id"])
        token_budget = _coerce_token_budget(task.get("token_budget"))
        subs = [
            child for child in children.get(task["id"], [])
            if child.get("source_message_id") is not None
        ]
        goal_steps = [
            child for child in children.get(task["id"], [])
            if child.get("source_message_id") is None
        ]
        # Appended (never replacing) field: the new-runtime run record, so the
        # UI can surface it without any change to the projected v1 fields.
        try:
            run = store.get_workflow_run_by_task(task["id"])
        except Exception:
            run = None
        goals.append({
            **task,
            "workflow_run": (
                {"id": run["id"], "status": run["status"]} if run else None
            ),
            "subtask_total": len(subs),
            "subtask_closed": sum(1 for s in subs if s["task_status"] == "closed"),
            "subtask_blocked": sum(1 for s in subs if s["task_status"] == "blocked"),
            "tokens_total": tokens_total,
            "budget_exceeded": bool(token_budget and tokens_total > token_budget),
            "budget_overage": max(0, tokens_total - token_budget) if token_budget else 0,
            "steps": [
                {
                    "id": step["id"],
                    "workflow_step": step.get("workflow_step", ""),
                    "title": step.get("title", ""),
                    "task_status": step["task_status"],
                    "assignee": step.get("assignee", ""),
                    "step_inputs": step.get("step_inputs") or {},
                    "result_summary": step.get("result_summary", ""),
                    "step_output": step.get("step_output") or {},
                    "artifacts": step.get("artifacts") or [],
                }
                for step in sorted(goal_steps, key=lambda item: item["id"])
            ],
            "subtasks": [
                {
                    "id": s["id"],
                    "title": s["title"],
                    "task_status": _visible_status(s),
                    "workflow_step": s.get("workflow_step", ""),
                    "assignee": s.get("assignee", ""),
                    "step_total": len(children.get(s["id"], [])),
                    "step_closed": sum(
                        1 for c in children.get(s["id"], [])
                        if c["task_status"] == "closed"
                    ),
                    "step_blocked": sum(
                        1 for c in children.get(s["id"], [])
                        if c["task_status"] == "blocked"
                    ),
                }
                for s in subs
            ],
        })
    return goals


def active_goal_conflict_reason(
    store: Store, exclude_task_id: int | None = None
) -> str | None:
    """Only one goal may be active at a time.

    A blocked/stalled goal still counts as active because it has not been
    accepted or explicitly force-closed yet; starting another goal would make
    the board and runner queue mix two top-level objectives.
    """
    for task in store.list_goals_with_children():
        if not task.get("is_goal"):
            continue
        if exclude_task_id is not None and task["id"] == exclude_task_id:
            continue
        if task.get("task_status") in {"closed", "accepted"}:
            continue
        if task.get("task_status") == "created" and not task.get("workflow_step"):
            continue
        title = (task.get("title") or "untitled").strip()
        return (
            f"goal #{task['id']} is already active ({task.get('task_status')}: "
            f"{title}); finish or force-end it before starting another goal"
        )
    return None


def workflow_locked_reason(store: Store) -> str | None:
    # A blocked/stalled goal can still be resumed, re-run, or re-implemented.
    # Changing its graph underneath it would strand transitions that reference
    # the old steps, so workflow writes stay locked until the goal is terminal.
    if goal_reason := active_goal_conflict_reason(store):
        return (
            "workflow config is locked while a goal is active; finish or force-end "
            f"it before editing the workflow ({goal_reason})"
        )
    busy = _active_workflow_task_ids(store)
    if not busy:
        return None
    ids = ", ".join(f"#{task_id}" for task_id in busy[:10])
    return (
        f"workflow config is locked while workflow tasks are running ({ids}); "
        "wait for them to finish, or block/close them first"
    )


def _active_workflow_task_ids(store: Store) -> list[int]:
    return [
        task["id"]
        for task in store.list_active_workflow_tasks()
        if task.get("workflow_step")
        and task.get("task_status") not in ("blocked", "closed")
    ]


# --- Workflow constraint engine ---------------------------------------------
# The workflow graph drives task routing. Completing a step advances the task
# along forward edges (layer increases); "rework" follows loop-back edges;
# merge steps wait until every *required* forward predecessor has completed;
# each dispatched step goes to the agent the step names (step.agent, else the
# step id). All movements are recorded in task_transitions.

WORKFLOW_ENGINE_AGENT = "workflow"
# In-app recipient for engine blocker/timeout notices. A human or supervising
# agent registered under this name sees them; if none is, the notice is dropped.
HUB_NOTIFY_AGENT = "hub"
WORKFLOW_OUTCOMES = {"done", "rework", "blocked", "approval"}
# How many times a loop-back (rework) target may be re-entered before the engine
# stops looping and blocks the task for the hub. Prevents review/implement
# rework from spinning forever when feedback is not being resolved.
MAX_REWORK_ROUNDS = 3


def _record_branch_closure_lineage(
    store: Store,
    task_id: int,
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    source_step: str,
    branch_root: str,
    port: str,
    outcome: str = "not_selected",
    note: str = "",
) -> list[tuple[str, str]]:
    """Close merge boundaries downstream of a branch that will never arrive.

    A direct ``source -> branch_root`` closure transition is useful for audit,
    but an implicit join waits on its own immediate predecessors. Walk the
    closed forward-only subgraph and add a closure event at every first merge
    boundary, without walking beyond that merge (the surviving branches own the
    lineage after it activates). Used for excluded conditional branches
    (`not_selected`) and for branches that terminally failed (`blocked`,
    `cancelled`). Returns the (predecessor, merge) boundaries recorded.
    """
    forward_edges = [
        edge for edge in cfg["edges"]
        if (edge["from"], edge["to"]) not in back
    ]
    incoming: dict[str, int] = {}
    outgoing: dict[str, list[str]] = {}
    for edge in forward_edges:
        incoming[edge["to"]] = incoming.get(edge["to"], 0) + 1
        outgoing.setdefault(edge["from"], []).append(edge["to"])
    queue = [branch_root]
    visited: set[str] = set()
    boundaries: list[tuple[str, str]] = []
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        for target in outgoing.get(node, []):
            if incoming.get(target, 0) > 1:
                store.record_task_transition(
                    task_id,
                    node,
                    target,
                    WORKFLOW_ENGINE_AGENT,
                    outcome,
                    note or f"branch excluded by {source_step} before merge {target}",
                    port,
                )
                boundaries.append((node, target))
            else:
                queue.append(target)
    return boundaries


def _branch_merge_boundaries(
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    origin: str,
    branch_root: str,
) -> list[tuple[str, str, int]]:
    """Return (predecessor, merge, depth) boundaries along one branch.

    Traversal continues beyond an inner merge so an outer fork can establish
    both its child Join activation and the enclosing Join activation.
    """
    forward_edges = [
        edge for edge in cfg["edges"]
        if (edge["from"], edge["to"]) not in back
    ]
    incoming: dict[str, int] = {}
    outgoing: dict[str, list[str]] = {}
    for edge in forward_edges:
        incoming[edge["to"]] = incoming.get(edge["to"], 0) + 1
        outgoing.setdefault(edge["from"], []).append(edge["to"])
    if incoming.get(branch_root, 0) > 1:
        return [(origin, branch_root, 0)]
    boundaries: list[tuple[str, str, int]] = []
    queue = [(branch_root, 0)]
    visited: set[str] = set()
    while queue:
        node, depth = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        for target in outgoing.get(node, []):
            if incoming.get(target, 0) > 1:
                boundary = (node, target, depth + 1)
                if boundary not in boundaries:
                    boundaries.append(boundary)
            queue.append((target, depth + 1))
    return boundaries


def _persist_routing_correlations(
    store: Store,
    task_id: int,
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    source_step: str,
    audited_edges: list[dict[str, Any]],
    routed_edges: list[dict[str, Any]],
) -> None:
    if len(audited_edges) < 2:
        return
    routed_ids = {id(edge) for edge in routed_edges}
    steps = {step["id"]: step for step in cfg["steps"]}
    by_join: dict[str, dict[str, Any]] = {}
    for edge in audited_edges:
        state = "selected" if id(edge) in routed_ids else "not_selected"
        for predecessor, join_step, depth in _branch_merge_boundaries(
            cfg, back, source_step, edge["to"]
        ):
            join_info = by_join.setdefault(join_step, {"depth": depth, "branches": {}})
            join_info["depth"] = max(int(join_info["depth"]), depth)
            existing = join_info["branches"].get(predecessor)
            if existing and existing["state"] == "selected":
                continue
            join_info["branches"][predecessor] = {
                "predecessor_step": predecessor,
                "branch_root": edge["to"],
                "state": state,
            }
    explicit_joins = {
        join_step
        for join_step in by_join
        if get_node_handler(steps.get(join_step, {})).dispatch_mode == "join"
    }
    forward_outgoing: dict[str, list[str]] = {}
    for edge in cfg["edges"]:
        if (edge["from"], edge["to"]) not in back:
            forward_outgoing.setdefault(edge["from"], []).append(edge["to"])

    def enclosing_join(join_step: str) -> str | None:
        queue = list(forward_outgoing.get(join_step, []))
        visited: set[str] = set()
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            if node in explicit_joins:
                return node
            queue.extend(forward_outgoing.get(node, []))
        return None

    parent_by_join = {join_step: enclosing_join(join_step) for join_step in explicit_joins}
    persisted: dict[str, dict[str, Any]] = {}

    def persist_join(join_step: str) -> dict[str, Any]:
        if join_step in persisted:
            return persisted[join_step]
        parent_step = parent_by_join[join_step]
        parent = persist_join(parent_step) if parent_step is not None else None
        join_info = by_join[join_step]
        join_def = steps.get(join_step, {})
        policy = str(join_def.get("join_policy") or "all_activated")
        correlation = store.ensure_workflow_correlation(
            task_id,
            source_step,
            join_step,
            policy,
            list(join_info["branches"].values()),
            parent["id"] if parent is not None else None,
        )
        persisted[join_step] = correlation
        return correlation

    for join_step in explicit_joins:
        persist_join(join_step)


def _correlation_ready(
    correlation: dict[str, Any], join_def: dict[str, Any] | None = None
) -> bool:
    states = [branch["state"] for branch in correlation.get("branches") or []]
    if not states:
        return False
    policy = str(correlation.get("policy") or "")
    if policy == "any":
        return "arrived" in states
    if policy in {"quorum", "count"}:
        # quorum and count share one counting mechanism; the threshold lives on
        # the join step definition, not in the persisted activation.
        threshold = max(1, int((join_def or {}).get("join_threshold") or 0))
        return states.count("arrived") >= threshold
    # all_activated / all_successful: every routed branch must close. A blocked
    # branch keeps the activation unready; for all_successful the dispatcher
    # turns that into a blocked join instead of an indefinite wait.
    return all(state in {"arrived", "not_selected", "cancelled"} for state in states)


def _failed_join_branches(
    target: str,
    correlation: dict[str, Any] | None,
    transitions: list[dict[str, Any]],
) -> list[str]:
    """Predecessor branches of `target` that closed as failed/blocked."""
    if correlation is not None:
        return sorted({
            branch["predecessor_step"]
            for branch in correlation.get("branches") or []
            if branch["state"] in {"failed", "blocked"}
        })
    return sorted({
        t["from_step"] for t in transitions
        if t["to_step"] == target and t["from_step"] and t["outcome"] == "blocked"
    })


def _cancel_remaining_join_branches(
    store: Store,
    task: dict[str, Any],
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    join_step: str,
    correlation: dict[str, Any],
) -> list[str]:
    """Cancel the branches a satisfied any/quorum/count join no longer waits on.

    Marks the activation's still-selected branches cancelled, closes them in the
    transition ledger, and stops their in-flight work: queued runner jobs are
    cancelled, a running run is flagged for its owning runner to kill, and each
    active step is settled with `reassigned` so the killed runner's late report
    cannot re-advance (or block) the task.
    """
    task_id = task["id"]
    remaining = store.cancel_workflow_correlation_branches(correlation["id"])
    if not remaining:
        return []
    note = (
        f"join '{join_step}' already satisfied "
        f"(policy {correlation.get('policy')}, remaining: cancel)"
    )
    branch_roots = {
        branch["predecessor_step"]: branch["branch_root"]
        for branch in correlation.get("branches") or []
    }
    forward_outgoing: dict[str, list[str]] = {}
    for edge in cfg["edges"]:
        if (edge["from"], edge["to"]) not in back:
            forward_outgoing.setdefault(edge["from"], []).append(edge["to"])
    # Every node on a cancelled branch: forward walk from its root, stopping at
    # the join (nodes past the join belong to the surviving flow).
    branch_nodes: set[str] = set()
    for predecessor in remaining:
        queue = [branch_roots.get(predecessor) or predecessor]
        while queue:
            node = queue.pop(0)
            if node == join_step or node in branch_nodes:
                continue
            branch_nodes.add(node)
            queue.extend(forward_outgoing.get(node, []))
    for predecessor in remaining:
        store.record_task_transition(
            task_id, predecessor, join_step, WORKFLOW_ENGINE_AGENT,
            "cancelled", note, "cancelled",
        )
    transitions = store.list_task_transitions(task_id)
    active = _active_step_assignees(transitions)
    cancelled_steps: list[str] = []
    for step_id in sorted(branch_nodes & set(active)):
        store.cancel_pending_run_jobs(task_id, step_id, note)
        run_holder_id = task_id
        if _materializes_step_cards(task):
            card = store.find_open_step_card(task_id, step_id)
            if card:
                run_holder_id = card["id"]
        runs = store.list_task_runs(run_holder_id, limit=1)
        if (
            runs
            and runs[0].get("status") == "running"
            and (runs[0].get("workflow_step") or step_id) == step_id
        ):
            store.request_run_kill(int(runs[0]["id"]), note)
        store.record_task_transition(
            task_id, step_id, step_id, WORKFLOW_ENGINE_AGENT, "reassigned", note
        )
        _settle_step_card(store, task, step_id, "cancelled")
        cancelled_steps.append(step_id)
    if cancelled_steps:
        return [f"{note}; cancelled in-flight step(s): {', '.join(cancelled_steps)}"]
    return []


def _foreach_scope_specs(step: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        items = resolve_path(context, str(step.get("items") or "$.input.items"))
    except (KeyError, ValueError) as exc:
        raise InvalidInputError(f"workflow foreach items cannot be resolved: {exc}") from None
    if not isinstance(items, list):
        raise InvalidInputError("workflow foreach items path must resolve to a list")
    scopes: list[dict[str, Any]] = []
    key_path = str(step.get("item_key") or "")
    depends_path = str(step.get("item_depends_on") or "")
    for index, item in enumerate(items):
        key: Any = index
        if key_path:
            try:
                key = resolve_path(item, key_path)
            except (KeyError, ValueError):
                raise InvalidInputError(
                    f"workflow foreach item {index} key path is missing"
                ) from None
        dependencies: Any = []
        if depends_path:
            try:
                dependencies = resolve_path(item, depends_path)
            except KeyError:
                dependencies = []
            except ValueError as exc:
                raise InvalidInputError(
                    f"workflow foreach item_depends_on is invalid: {exc}"
                ) from None
        if dependencies is None:
            dependencies = []
        if not isinstance(dependencies, list):
            raise InvalidInputError(
                f"workflow foreach item {index} dependencies must resolve to a list"
            )
        scopes.append({"key": key, "value": item, "depends_on": dependencies})
    return scopes


def _foreach_item_upstream(
    upstream_result: str, mapped_inputs: dict[str, Any], scope: dict[str, Any]
) -> str:
    payload = {
        "item": scope.get("item_value"),
        "item_meta": {
            "id": scope.get("id"),
            "group_id": scope.get("group_id"),
            "index": scope.get("item_index"),
            "key": scope.get("scope_key"),
            "depends_on": scope.get("depends_on") or [],
        },
        "input": mapped_inputs,
    }
    parts = [upstream_result.strip()] if upstream_result.strip() else []
    parts.append(
        "FOREACH_ITEM_SCOPE:\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return "\n\n".join(parts)


def _queue_ready_foreach_scopes(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    group: dict[str, Any],
    upstream_result: str,
    mapped_inputs: dict[str, Any],
) -> list[dict[str, Any]]:
    agents = step.get("agents") or []
    if not agents:
        raise InvalidInputError(
            f"foreach step {step['id']!r} requires at least one Agent"
        )
    dispatched: list[dict[str, Any]] = []
    for scope in group.get("scopes") or []:
        if scope.get("status") != "ready" or store.has_open_item_run_job(scope["id"]):
            continue
        if _enforce_goal_token_budget(store, project_root, task):
            reason = "goal token budget exceeded; foreach dispatch frozen"
            store.update_workflow_item_scope(scope["id"], "blocked", {}, reason)
            store.cancel_open_workflow_item_scopes(group["id"], reason)
            transitions = store.list_task_transitions(task["id"])
            if step["id"] in _running_steps(transitions):
                store.record_task_transition(
                    task["id"], step["id"], step["id"], WORKFLOW_ENGINE_AGENT,
                    "blocked", reason, "blocked",
                )
            store.set_task_workflow_state(task["id"], task_status="blocked")
            return dispatched
        assignee = agents[int(scope.get("item_index") or 0) % len(agents)]
        command = _step_agent_command(step, assignee)
        if not command:
            raise InvalidInputError(
                f"foreach step {step['id']!r} item {scope['scope_key']!r} "
                f"has no runnable command for Agent {assignee!r}"
            )
        if not store.agent_exists(assignee):
            store.register_agent(assignee, f"workflow agent for step {step['id']}")
        job = store.create_run_job(
            task["id"],
            step["id"],
            assignee,
            command,
            _foreach_item_upstream(upstream_result, mapped_inputs, scope),
            note=f"queued foreach item {scope['scope_key']}",
            item_scope_id=int(scope["id"]),
        )
        if job:
            dispatched.append(
                {
                    "step": step["id"],
                    "assignee": assignee,
                    "item_scope_id": scope["id"],
                    "item_key": scope["scope_key"],
                    "queued_job_id": job["id"],
                }
            )
    return dispatched


def _foreach_group_result(group: dict[str, Any]) -> str:
    items = []
    artifacts: list[Any] = []
    for scope in group.get("scopes") or []:
        normalized = scope.get("output") or {}
        item_artifacts = normalized.get("artifacts") or []
        artifacts.extend(item_artifacts)
        items.append(
            {
                "scope_id": scope["id"],
                "key": scope["scope_key"],
                "index": scope["item_index"],
                "item": scope.get("item_value"),
                "output": normalized.get("output") or {},
                "summary": str(normalized.get("summary") or ""),
                "artifacts": item_artifacts,
            }
        )
    return "WORKFLOW_RESULT: " + json.dumps(
        {
            "port": "success",
            "output": {"items": items, "count": len(items)},
            "summary": f"Processed {len(items)} foreach item(s)",
            "artifacts": artifacts,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _finalize_foreach_group_locked(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    group: dict[str, Any],
) -> dict[str, Any]:
    if group.get("status") != "completed" or group.get("advanced_at"):
        return {"task_id": task["id"], "step": step["id"], "dispatched": []}
    transitions = store.list_task_transitions(task["id"])
    already_advanced = any(
        int(transition["id"]) > int(group.get("transition_cursor") or 0)
        and transition["from_step"] == step["id"]
        and transition["outcome"] == "done"
        for transition in transitions
    )
    if already_advanced:
        store.mark_workflow_item_group_advanced(group["id"])
        return {"task_id": task["id"], "step": step["id"], "dispatched": []}
    report = _advance_workflow_task_locked(
        store,
        project_root,
        WORKFLOW_ENGINE_AGENT,
        task["id"],
        step["id"],
        "done",
        _foreach_group_result(group),
    )
    store.mark_workflow_item_group_advanced(group["id"])
    return report


def _project_workflow_task_status(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    transitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a task with its lifecycle status ready for API presentation."""
    if task.get("is_goal"):
        return task
    # Override statuses win regardless of transitions (see
    # _workflow_derived_task_status), so skip the per-task transitions query for
    # them — most rows in a long-lived DB are closed, and the board poll
    # projects every row.
    if (task.get("task_status") or task.get("status") or "") in _WORKFLOW_STATUS_OVERRIDES:
        return task
    if transitions is None:
        transitions = store.list_task_transitions(int(task["id"]))
    if not transitions:
        return task
    cfg = cfg or read_workflow_config(project_root)
    projected = dict(task)
    status = _workflow_derived_task_status(projected, transitions, cfg)
    projected["task_status"] = status
    projected["status"] = status
    return projected


def _manual_status_rejection(
    store: Store, task: dict[str, Any] | None, status: str
) -> str | None:
    """Why a manual status write would be invisible, or None when it sticks.

    While a task has active workflow steps its visible status is derived from
    the workflow, so only the override statuses survive projection; silently
    accepting anything else would store a value the board never shows."""
    if task is None or task.get("is_goal"):
        return None
    if status in _WORKFLOW_STATUS_OVERRIDES:
        return None
    active = _active_steps(store.list_task_transitions(int(task["id"])))
    if not active:
        return None
    return (
        f"task {task['id']} is at workflow step(s) {', '.join(sorted(active))}; "
        "its visible status is derived from the workflow, so a manual "
        f"{status!r} would not show. Use one of "
        f"{sorted(_WORKFLOW_STATUS_OVERRIDES)}, or complete/rework the step."
    )


def _ensure_engine_agent(store: Store) -> None:
    if not store.agent_exists(WORKFLOW_ENGINE_AGENT):
        store.register_agent(
            WORKFLOW_ENGINE_AGENT,
            "workflow engine: routes tasks along the configured workflow",
        )


def _notify_hub(store: Store, text: str) -> str:
    hub_agent = HUB_NOTIFY_AGENT
    try:
        if not store.agent_exists(hub_agent):
            return f"hub agent {hub_agent!r} not registered; notice dropped"
        _ensure_engine_agent(store)
        store.send_message(WORKFLOW_ENGINE_AGENT, hub_agent, text)
        return f"notified {hub_agent}"
    except (UnknownAgentError, InvalidInputError) as exc:
        return f"hub notification failed: {exc}"


def _workflow_api_actor(raw_agent: str, project_root: str | None) -> str:
    # The UI sends no agent name (older pages sent "ui"); it acts as the hub so
    # the engine's assignee/override constraint recognizes it.
    agent = (raw_agent or "").strip()
    if agent and agent != "ui":
        return agent
    return HUB_NOTIFY_AGENT


def _validate_goal_auto_runners(
    store: Store, project_root: str | None, title: str, content: str
) -> None:
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    errors = _workflow_execution_errors(cfg, back)
    if errors:
        raise InvalidInputError(
            "workflow is not executable: "
            + "; ".join(errors)
            + ". Check the Workflow page warnings."
        )
    def _collect_missing_agents(
        steps: list[dict[str, Any]], prefix: str = ""
    ) -> list[str]:
        problems: list[str] = []
        for step in steps:
            if not handler_requires_agent(step):
                continue
            label = f"{prefix}{step['id']}"
            agents = step.get("agents") or []
            if not agents:
                problems.append(f"{label}: no agent selected")
                continue
            for agent in agents:
                if not _step_agent_command(step, agent):
                    problems.append(f"{label} ({agent}): no command")
        return problems

    reachable = _main_workflow_reachable_steps(cfg, back)
    missing = _collect_missing_agents(reachable)
    # A reachable subflow node runs its whole subgraph, so those steps must be
    # runnable too before the goal may start.
    subflows = cfg.get("subflows") or {}
    for step in reachable:
        if step.get("type") != "subflow":
            continue
        sub = subflows.get(str(step.get("subflow") or ""))
        if sub is None:
            missing.append(
                f"{step['id']}: unknown subflow {str(step.get('subflow') or '')!r}"
            )
            continue
        sub_reachable = _main_workflow_reachable_steps(sub, _workflow_graph(sub))
        missing.extend(
            _collect_missing_agents(sub_reachable, prefix=f"{step['id']}/")
        )
    if missing:
        raise InvalidInputError(
            "goal cannot start until every step has a runnable Agent — open the "
            "Workflow page and select at least one Agent per step (steps start "
            "unassigned; set a runnable command where an Agent has no built-in): "
            + "; ".join(missing)
        )


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise InvalidInputError("intake produced no JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise InvalidInputError("intake output is not JSON") from None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidInputError(f"invalid intake JSON: {exc}") from None
    if not isinstance(data, dict):
        raise InvalidInputError("intake JSON must be an object")
    return data


def _parse_subtask_deps(raw: Any, index: int, count: int) -> list[int]:
    """Normalize a subtask's `depends_on` (1-based indices of other tasks in the
    same batch) to sorted 0-based indices. Rejects out-of-range and self refs."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidInputError(
            f"task {index} depends_on must be a list of task numbers"
        )
    out: set[int] = set()
    for value in raw:
        try:
            ref = int(value)
        except (TypeError, ValueError):
            raise InvalidInputError(
                f"task {index} depends_on has a non-numeric entry: {value!r}"
            ) from None
        if ref < 1 or ref > count:
            raise InvalidInputError(
                f"task {index} depends_on references task {ref}, out of range 1..{count}"
            )
        if ref == index:
            raise InvalidInputError(f"task {index} cannot depend on itself")
        out.add(ref - 1)
    return sorted(out)


def _reject_dependency_cycles(tasks: list[dict[str, Any]]) -> None:
    """A dependency cycle would never release (each waits on the other), so
    reject it at parse time — the goal blocks and the hub re-decomposes."""
    state = [0] * len(tasks)  # 0=unseen, 1=on-stack, 2=done

    def visit(i: int) -> None:
        if state[i] == 1:
            raise InvalidInputError("subtask dependencies form a cycle")
        if state[i] == 2:
            return
        state[i] = 1
        for dep in tasks[i]["deps"]:
            visit(dep)
        state[i] = 2

    for i in range(len(tasks)):
        visit(i)


def _parse_goal_subtasks(text: str) -> list[dict[str, Any]]:
    data = _extract_json_object(text)
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise InvalidInputError('intake JSON must include a non-empty "tasks" list')
    count = len(raw_tasks)
    tasks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise InvalidInputError(f"task {index} must be an object")
        title = str(raw.get("title") or "").strip()
        content = str(raw.get("content") or "").strip()
        acceptance = str(raw.get("acceptance") or "").strip()
        if not title:
            raise InvalidInputError(f"task {index} title is required")
        if not content:
            raise InvalidInputError(f"task {index} content is required")
        if "agent" in raw:
            raise InvalidInputError(
                f"task {index} must not set agent; each workflow step owns its Agents"
            )
        body = content
        if acceptance:
            body += f"\n\nAcceptance:\n{acceptance}"
        deps = _parse_subtask_deps(raw.get("depends_on"), index, count)
        tasks.append({"title": title[:160], "content": body, "deps": deps})
    _reject_dependency_cycles(tasks)
    return tasks


def _start_goal_business_subtasks(
    store: Store,
    project_root: str | None,
    goal: dict[str, Any],
    actor: str,
    subtasks: list[dict[str, str]],
    from_step: str | None = None,
    target_steps: list[str] | None = None,
    upstream_result: str = "",
    decompose_step: str = "",
) -> list[dict[str, Any]]:
    """Create each business subtask and start it in the workflow. By default a
    subtask starts at the entry step (splits at intake). When `target_steps` is
    given (a later decompose step's successors, `from_step` being that decompose
    step), the subtask instead begins there with `upstream_result` — the goal's
    shared design/architecture output — as its upstream context, so those steps
    run once on the goal, not per subtask. `decompose_step` names the goal step
    that produced the split for run lineage (defaults to `from_step`)."""
    source_message_id = goal.get("source_message_id")
    if source_message_id is None:
        raise InvalidInputError("goal is missing source_message_id")
    # 1. Create every subtask row first, so `depends_on` (referenced by 1-based
    #    index in the batch) can be resolved to real task ids before any dispatch.
    created: list[dict[str, Any]] = []
    for subtask in subtasks:
        [message_id] = store.send_message(
            actor,
            actor,
            subtask["content"],
            reply_to=source_message_id,
            kind="task",
            title=subtask["title"],
        )
        task = store.get_task_by_source_message(message_id)
        if not task:
            raise InvalidInputError(f"task not created for message: {message_id}")
        # send_message stamps assignee = recipient (the decompose actor). A
        # subtask has no real owner until a step dispatches it, so clear it —
        # otherwise held subtasks show the decompose runner as if pre-assigned,
        # and the implement round-robin only sets the true owner at dispatch.
        store.set_task_workflow_state(task["id"], assignee="")
        created.append(task)
    # 2. Persist each subtask's prerequisite task ids. Agent selection belongs
    #    to each workflow step and is resolved when that step is dispatched.
    for idx, subtask in enumerate(subtasks):
        dep_ids = [created[d]["id"] for d in subtask.get("deps", [])]
        if dep_ids:
            store.update_task_metadata(created[idx]["id"], depends_on=dep_ids)
    # 3. Dispatch only the dependency-free subtasks; the rest stay held (status
    #    "created", no workflow_step) until _release_ready_subtasks starts them
    #    once their prerequisites close (and are thus integrated on main).
    parent_run_id, parent_node_run_id = _run_lineage_for_step(
        store, goal["id"], decompose_step or from_step or ""
    )
    started: list[dict[str, Any]] = []
    for idx, subtask in enumerate(subtasks):
        task = created[idx]
        if subtask.get("deps"):
            started.append({"task": store.get_task(task["id"]), "held": True})
            continue
        result = _dispatch_business_subtask(
            store, project_root, actor, task["id"],
            from_step, target_steps, upstream_result,
            parent_run_id=parent_run_id,
            parent_node_run_id=parent_node_run_id,
        )
        started.append({"task": store.get_task(task["id"]), **result})
    return started


def _dispatch_business_subtask(
    store: Store,
    project_root: str | None,
    actor: str,
    task_id: int,
    from_step: str | None,
    target_steps: list[str] | None,
    upstream_result: str,
    parent_run_id: int | None = None,
    parent_node_run_id: int | None = None,
) -> dict[str, Any]:
    """Start one business subtask in the workflow — at the entry step, or at the
    decompose step's successors when the goal split after its design phase. The
    subtask's run records the goal's run/decompose node as its lineage parent."""
    if target_steps is None:
        return _start_workflow_task_locked(
            store, project_root, actor, task_id,
            parent_run_id=parent_run_id,
            parent_node_run_id=parent_node_run_id,
        )
    return _start_workflow_task_at_locked(
        store, project_root, actor, task_id,
        from_step or "", target_steps, upstream_result,
        parent_run_id=parent_run_id,
        parent_node_run_id=parent_node_run_id,
    )


def _goal_decompose_upstream_result(
    store: Store, goal_id: int, decompose_step: str
) -> str:
    if not decompose_step:
        return ""
    transitions = store.list_task_transitions(goal_id)
    for transition in reversed(transitions):
        if (
            transition.get("from_step") == decompose_step
            and transition.get("to_step") == ""
            and transition.get("outcome") == "done"
        ):
            return transition.get("note") or ""
    return ""


def _release_ready_subtasks(
    store: Store, project_root: str | None, goal_id: int, actor: str
) -> list[int]:
    """Dispatch any held business subtasks whose prerequisites have all closed.

    A subtask with `depends_on` is created but held (status 'created', no
    workflow_step) until every task it depends on reaches 'closed' — by then that
    work is integrated on main, so the released subtask's worktree branches off
    it. Returns the ids dispatched this pass. Idempotent: a dispatched subtask no
    longer matches the held filter."""
    subtasks = _business_subtasks_for_goal(store, goal_id)
    by_id = {s["id"]: s for s in subtasks}
    held = [
        s for s in subtasks
        if s.get("depends_on")
        and s.get("task_status") == "created"
        and not s.get("workflow_step")
    ]
    if not held:
        return []
    # Released subtasks begin where their siblings did — the decompose step's
    # successors when the goal split after its design phase, else the entry step.
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    decompose_id = _root_goal_decompose_step_id(cfg, back)
    entries = set(_workflow_entry_steps(cfg, back))
    target_steps: list[str] | None = None
    from_step = ""
    if decompose_id and decompose_id not in entries:
        target_steps = _forward_out(cfg, back, decompose_id)
        from_step = decompose_id
    upstream_result = _goal_decompose_upstream_result(store, goal_id, from_step)
    parent_run_id, parent_node_run_id = _run_lineage_for_step(
        store, goal_id, decompose_id or ""
    )
    _ensure_engine_agent(store)
    released: list[int] = []
    for s in held:
        prereqs = s.get("depends_on") or []
        if not all(
            (by_id.get(pid) or {}).get("task_status") == "closed" for pid in prereqs
        ):
            continue
        _dispatch_business_subtask(
            store, project_root, actor, s["id"], from_step, target_steps,
            upstream_result,
            parent_run_id=parent_run_id,
            parent_node_run_id=parent_node_run_id,
        )
        released.append(s["id"])
    return released


def _business_subtasks_for_goal(store: Store, goal_id: int) -> list[dict[str, Any]]:
    return store.list_tasks_by_parent(goal_id)


def _root_goal_id(store: Store, task: dict[str, Any]) -> int | None:
    """Walk parent_task_id up to the owning goal (subtasks are children of the
    goal). Returns the goal's id, or None if the task isn't under a goal."""
    cur: dict[str, Any] | None = task
    seen: set[int] = set()
    while cur:
        if cur.get("is_goal"):
            return int(cur["id"])
        parent_id = cur.get("parent_task_id")
        if not parent_id or int(parent_id) in seen:
            return None
        seen.add(int(parent_id))
        cur = store.get_task(int(parent_id))
    return None


def _enforce_goal_token_budget(
    store: Store, project_root: str | None, task: dict[str, Any]
) -> bool:
    """Hard token ceiling: if the task's goal has spent more than its own
    token_budget, freeze the goal (block + notify hub, once) and return True so
    the caller skips dispatch. Returns False when the goal set no budget (0 =
    unlimited) or is still within it. Budget is per goal, set when the goal is
    started. Tokens are self-reported by agents, so this bounds — not perfectly
    meters — runaway cost; unreported tokens count as zero."""
    goal_id = _root_goal_id(store, task)
    if goal_id is None:
        return False
    goal = store.get_task(goal_id)
    if not goal:
        return False
    # Guard reads tasks.token_budget (authoritative); the workflow run's
    # variables are a migration-period dual-write record only (design §11).
    budget = _coerce_token_budget(goal.get("token_budget"))
    if budget <= 0:
        return False
    total = store.sum_goal_tokens(goal_id)
    if total <= budget:
        return False
    if not store.has_workflow_action(goal_id, "budget_exceeded"):
        store.create_workflow_action(
            goal_id, "budget_exceeded",
            note=f"goal tokens {total} exceed budget {budget}",
        )
        store.set_task_workflow_state(goal_id, task_status="stalled")
        # Record-layer mirror of the freeze: usage snapshot + frozen marker in
        # the goal run's variables. Never read for routing; non-fatal on error.
        try:
            run = store.get_workflow_run_by_task(goal_id)
            if run is not None:
                store.update_workflow_run_variables(
                    int(run["id"]),
                    tokens_total=total,
                    budget_frozen_at=datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                )
        except Exception:
            _log.exception(
                "budget freeze mirror failed for goal %s (non-fatal)", goal_id
            )
        _notify_hub(
            store,
            f"目标 #{goal_id} 触及 token 硬预算：已用 {total} > 预算 {budget}。"
            "已冻结后续派发，需人工介入（提高预算 / 重新拆分 / 终止目标）。",
        )
    return True


def _finish_goal_workflow(
    store: Store, project_root: str | None, goal: dict[str, Any]
) -> str:
    """Finish a non-decomposing goal after its terminal workflow step.

    A goal with work items converges through _recompute_parent_goal_status.
    A goal that owns the workflow directly has no child status change to trigger
    that path, so it performs the equivalent verify-or-accept decision here.
    Returns the persisted goal status.
    """
    own = str(goal.get("goal_verify") or "").strip()
    goal_verify = own or _detect_goal_verify(_project_root(project_root))
    if goal_verify:
        if not store.has_pending_workflow_action(goal["id"], "goal_verify"):
            note = "goal workflow completed; goal verification queued"
            if not own:
                note += f" (auto-detected: {goal_verify})"
            store.create_workflow_action(goal["id"], "goal_verify", note=note)
        status = "verifying"
    else:
        status = "accepted"
    store.set_task_workflow_state(
        goal["id"], workflow_step="", task_status=status
    )
    return status


def _complete_end_node(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
) -> str:
    """Complete an explicit End node without dispatching an Agent runner."""
    store.record_task_transition(
        task["id"], step["id"], "", WORKFLOW_ENGINE_AGENT, "done",
        "explicit end node reached", "success",
    )
    _settle_step_card(store, task, step["id"], "done")
    if task.get("is_goal"):
        return _finish_goal_workflow(store, project_root, task)
    store.set_task_workflow_state(task["id"], workflow_step="", task_status="closed")
    _recompute_parent_goal_status(store, task, project_root)
    return "closed"


def _finalize_subflow_locked(
    store: Store, project_root: str | None, child: dict[str, Any]
) -> dict[str, Any] | None:
    """Settle the parent's subflow node once its child task reaches a terminal
    state: child closed -> complete the node (passing the child's terminal port
    through when the node declares it), child blocked -> block the node and let
    the failure path notify the hub. Idempotent: a node no longer active on the
    parent (already settled, superseded, or force-closed) is left alone."""
    ref = str(child.get("workflow_ref") or "").strip()
    if not ref or not child.get("parent_task_id"):
        return None
    # Callers pass pre-update snapshots; re-read the child's settled status.
    child = store.get_task(int(child["id"])) or child
    status = str(child.get("task_status") or "")
    if status not in {"closed", "blocked"}:
        return None
    parent_id = int(child["parent_task_id"])
    parent = store.get_task(parent_id)
    if not parent or parent.get("task_status") == "closed":
        return None
    try:
        cfg = workflow_config_for_task(project_root, parent)
    except InvalidInputError:
        return None
    steps = {s["id"]: s for s in cfg["steps"]}
    transitions = store.list_task_transitions(parent_id)
    active = _active_step_assignees(transitions)
    # Which subflow node spawned this child: the recorded run lineage names the
    # exact node execution; fall back to the sole active node referencing the
    # same subflow when the record layer has no lineage for this child.
    parent_step_id = ""
    try:
        child_run = store.get_workflow_run_by_task(int(child["id"]))
        if child_run and child_run.get("parent_node_run_id"):
            node = store.get_node_run(int(child_run["parent_node_run_id"]))
            if node:
                parent_step_id = str(node["step"])
    except Exception:
        parent_step_id = ""
    if not parent_step_id:
        candidates = [
            step_id for step_id in active
            if (steps.get(step_id) or {}).get("type") == "subflow"
            and str(steps[step_id].get("subflow") or "") == ref
        ]
        if len(candidates) != 1:
            return None
        parent_step_id = candidates[0]
    step_def = steps.get(parent_step_id)
    if (
        parent_step_id not in active
        or not step_def
        or get_node_handler(step_def).dispatch_mode != "subflow"
    ):
        return None
    if status == "blocked":
        # A blocked child may block again (or later resume and close); block
        # the parent node only once per dispatch cycle.
        last_dispatch_id = max(
            (t["id"] for t in transitions
             if t["outcome"] == "dispatched" and t["to_step"] == parent_step_id),
            default=0,
        )
        if any(
            t["id"] > last_dispatch_id
            and t["from_step"] == parent_step_id
            and t["outcome"] == "blocked"
            for t in transitions
        ):
            return None
        return _advance_workflow_task_locked(
            store, project_root, WORKFLOW_ENGINE_AGENT, parent_id,
            parent_step_id, "blocked",
            f"subflow '{ref}' task #{child['id']} blocked",
        )
    # Child closed: hand the child's terminal structured output to the parent
    # node. Its port passes through only when the parent node declares it.
    results = store.list_workflow_node_results(int(child["id"]))
    final = results[-1] if results else {}
    port = str(final.get("port") or "success")
    if port not in set(step_def.get("ports") or ["success"]):
        port = str(step_def.get("default_port") or "success")
    payload = "WORKFLOW_RESULT: " + json.dumps(
        {
            "port": port,
            "output": final.get("output") or {},
            "summary": str(
                final.get("summary") or f"subflow '{ref}' completed"
            ),
            "artifacts": final.get("artifacts") or [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _advance_workflow_task_locked(
        store, project_root, WORKFLOW_ENGINE_AGENT, parent_id,
        parent_step_id, "done", payload,
    )


def _recompute_parent_goal_status(
    store: Store, task: dict[str, Any], project_root: str | None = None
) -> None:
    """Roll a subtask status change up to its parent goal:
    all business subtasks closed -> accepted; any blocked -> stalled;
    otherwise in_progress. A goal that was explicitly closed is left as-is.
    Also the terminal-propagation seam for subflow children: a child task
    reaching closed/blocked settles the parent's subflow node here."""
    _finalize_subflow_locked(store, project_root, task)
    parent_id = task.get("parent_task_id")
    if not parent_id:
        return
    parent = store.get_task(parent_id)
    if not parent or not parent.get("is_goal"):
        return
    if parent.get("task_status") == "closed":
        return  # respect an explicit close of the whole goal
    # A subtask just changed state; release any held dependents whose
    # prerequisites have now all closed (runs before the roll-up below, so a
    # freshly-released subtask counts as still-running, not "all closed").
    _release_ready_subtasks(store, project_root, parent_id, WORKFLOW_ENGINE_AGENT)
    subtasks = [
        subtask
        for subtask in _business_subtasks_for_goal(store, parent_id)
        if subtask.get("source_message_id") is not None
    ]
    if not subtasks:
        return
    statuses = [subtask["task_status"] for subtask in subtasks]
    if all(status == "closed" for status in statuses):
        # Goal convergence gate: subtasks passed their own (isolated) tests, but
        # the integrated main can still fail. If a goal_verify command is set,
        # queue an objective check on main and let the async sweep accept or
        # stall the goal — don't accept on aggregation alone. Runs once per goal.
        own = str(parent.get("goal_verify") or "").strip()
        goal_verify = own or _detect_goal_verify(_project_root(project_root))
        if goal_verify:
            # Already verified and accepted: nothing to do.
            if parent.get("task_status") == "accepted":
                return
            # A verify is in flight (pending/running): the sweep owns the final
            # decision — don't queue a duplicate. But a prior *failed* verify does
            # NOT block re-queue, so a goal reworked after a failed verification
            # (subtasks reopened then re-closed) gets verified again.
            if not store.has_pending_workflow_action(parent_id, "goal_verify"):
                note = "all subtasks closed; goal verification queued"
                if not own:
                    note += f" (auto-detected: {goal_verify})"
                store.create_workflow_action(
                    parent_id, "goal_verify", note=note,
                )
                if parent.get("task_status") != "verifying":
                    store.set_task_workflow_state(parent_id, task_status="verifying")
            return
        new_status = "accepted"
    elif any(status == "blocked" for status in statuses):
        new_status = "stalled"
    else:
        new_status = "running"
    if parent.get("task_status") != new_status:
        store.set_task_workflow_state(parent_id, task_status=new_status)


def _materializes_step_cards(task: dict[str, Any]) -> bool:
    return bool(
        task.get("is_goal")
        or (
            task.get("parent_task_id")
            and task.get("source_message_id") is not None
        )
    )


def _root_goal_decompose_step_id(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> str | None:
    """The explicitly configured step at which a root goal splits into work items.

    No flag means no split: the goal itself traverses the complete workflow.
    This keeps decomposition a workflow choice instead of an implicit property
    of every goal."""
    flagged = [s["id"] for s in cfg["steps"] if s.get("decompose")]
    return flagged[0] if flagged else None


def _is_root_goal_decompose_step(
    project_root: str | None, task: dict[str, Any], step: dict[str, Any]
) -> bool:
    if not task.get("is_goal") or task.get("parent_task_id"):
        return False
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    return step["id"] == _root_goal_decompose_step_id(cfg, back)


def _goal_status_for_step(project_root: str | None, step_id: str) -> str:
    """A root goal's own lifecycle status while it sits at `step_id`. A goal
    may traverse the whole workflow itself or split at an explicit decompose
    step, so its status stays domain-neutral outside that boundary."""
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    if step_id in set(_workflow_entry_steps(cfg, back)):
        return "new"
    if step_id == _root_goal_decompose_step_id(cfg, back):
        return "decomposing"
    return "running"


def _complete_goal_intake_locked(
    store: Store,
    project_root: str | None,
    goal: dict[str, Any],
    step: dict[str, Any],
    actor: str,
    result: str,
    *,
    parse_goal_subtasks: Callable[[str], list[dict[str, Any]]] | None = None,
    start_goal_business_subtasks: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    parse_goal_subtasks = parse_goal_subtasks or _parse_goal_subtasks
    start_goal_business_subtasks = (
        start_goal_business_subtasks or _start_goal_business_subtasks
    )
    # Record the raw output first so a parse failure (bad JSON) still leaves the
    # decompose step's result inspectable on its card.
    _record_step_result(store, goal, step["id"], result)
    subtasks = parse_goal_subtasks(result)
    intake_card = store.find_open_step_card(goal["id"], step["id"])
    if intake_card:
        store.update_task_step_details(
            intake_card["id"],
            result_summary=f"Created {len(subtasks)} work item(s)",
        )
    # Re-validate workflow/state before dispatching the business subtasks.
    # The goal passed this gate at creation, but the workflow config can change
    # while intake is being worked; refuse to dispatch a batch that would only
    # strand subtasks as blocked, and surface the reason to hub. Runs before the
    # settle below so a failed precondition leaves intake open for retry.
    _validate_goal_auto_runners(
        store, project_root, goal.get("title", ""), goal.get("content", "")
    )
    # Resolve where the subtasks begin — and validate it — BEFORE the settle
    # below, so a failed precondition leaves the decompose step open for retry
    # instead of stranding the goal (settled + dropped out) with no subtasks.
    #  - an explicitly flagged decompose at the entry: work items run the whole
    #    workflow from the entry (target_steps stays None).
    #  - decompose at a later step (after goal-level design/architecture): subtasks
    #    begin at that step's forward successors, carrying the goal's decompose
    #    output forward, so the design steps run once on the goal, not per subtask.
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    target_steps: list[str] | None = None
    if step["id"] not in set(_workflow_entry_steps(cfg, back)):
        target_steps = _forward_out(cfg, back, step["id"])
        if not target_steps:
            raise InvalidInputError(
                f"decompose step '{step['id']}' has no forward successor"
            )
    # Settle the goal's own intake card and record the intake before dispatching
    # the business subtasks — subtask dispatch can raise, and if it did after
    # this point the intake card would be left stuck in_progress forever.
    store.cancel_pending_run_jobs(
        goal["id"],
        step["id"],
        f"goal intake settled by {actor}",
    )
    store.record_task_transition(goal["id"], step["id"], "", actor, "done", result)
    store.set_task_workflow_state(
        goal["id"], workflow_step="", task_status="running"
    )
    _settle_step_card(store, goal, step["id"], "done")
    # Persist the design docs to the base branch before any isolated subtask
    # worktree is cut, so implementers actually find the docs/ paths the subtasks
    # reference (worktrees branch off HEAD and can't see uncommitted files).
    _commit_goal_design_artifacts(project_root)
    if target_steps is None:
        started = start_goal_business_subtasks(
            store, project_root, goal, actor, subtasks,
            decompose_step=step["id"],
        )
    else:
        started = start_goal_business_subtasks(
            store, project_root, goal, actor, subtasks,
            from_step=step["id"],
            target_steps=target_steps,
            upstream_result=result,
            decompose_step=step["id"],
        )
    return {
        "task_id": goal["id"],
        "step": step["id"],
        "outcome": "done",
        "created_subtasks": [item["task"] for item in started],
        "started": started,
        "dispatched": [
            dispatched
            for item in started
            for dispatched in item.get("dispatched", [])
        ],
        "notices": [
            notice
            for item in started
            for notice in item.get("notices", [])
        ],
    }


# --- Step cards --------------------------------------------------------------
# For goal tasks every dispatched workflow step is materialized as its own
# subtask card (parent_task_id = goal), so the kanban shows the flow as cards
# moving through columns instead of one invisible goal row. The engine still
# tracks the workflow on the goal task itself; cards are a projection.

def _upsert_step_card(
    store: Store,
    project_root: str | None,
    parent: dict[str, Any],
    step: dict[str, Any],
    assignee: str,
    step_inputs: dict[str, Any],
) -> dict[str, Any]:
    card = store.find_open_step_card(parent["id"], step["id"])
    if card and str(card.get("task_status")) == "blocked":
        # A blocked card is a settled attempt. Re-dispatching after a block (manual
        # re-run / auto-recovery) must open a FRESH card, not reuse the stale one —
        # otherwise the retry lands on the blocked card's old board position (before
        # any implement cards that ran since) instead of after them. Close it so the
        # retry sequences cleanly and find_open_step_card won't resurface it later.
        store.set_task_workflow_state(card["id"], task_status="closed")
        card = None
    if card:
        # Redispatch (rework loop / timeout reassign): reuse the still-open card.
        store.set_task_workflow_state(
            card["id"], task_status="assigned", assignee=assignee
        )
        return store.update_task_step_details(
            card["id"], step_inputs=step_inputs, result_summary="", step_output={}, artifacts=[]
        ) or card
    # Title = step type + what THIS task is actually about (the parent task's
    # title), so each card reflects its own work — not a generic step label.
    work = (parent.get("title") or "").strip()
    title = f"{step['name']} · {work[:60]}" if work else step["name"]
    return store.create_step_card(
        parent_task_id=parent["id"],
        workflow_step=step["id"],
        title=title,
        content=(
            f"Workflow step '{step['name']}' of task #{parent['id']}"
            f"\n\n{parent.get('content', '')}"
        ),
        sender=WORKFLOW_ENGINE_AGENT,
        assignee=assignee,
        status="assigned",
        step_inputs=step_inputs,
    )


def _settle_step_card(
    store: Store, goal: dict[str, Any], step_id: str, outcome: str
) -> None:
    if not _materializes_step_cards(goal):
        return
    card = store.find_open_step_card(goal["id"], step_id)
    if not card:
        return
    status = "blocked" if outcome == "blocked" else "closed"
    store.set_task_workflow_state(card["id"], task_status=status)


def _record_step_result(
    store: Store,
    task: dict[str, Any],
    step_id: str,
    result: str,
    *,
    port: str = "",
    output_schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Attach structured output to the current step execution holder."""
    holder_id = task["id"]
    if _materializes_step_cards(task):
        card = store.find_open_step_card(task["id"], step_id)
        if card:
            holder_id = card["id"]
    normalized, parse_error = _normalized_step_result(result, port)
    schema_errors = validate_json_schema(
        normalized.get("output") or {}, output_schema or {}
    )
    errors = ([parse_error] if parse_error else []) + schema_errors
    summary = str(normalized.get("summary") or "")
    artifact_refs: list[str] = []
    for artifact in normalized.get("artifacts") or []:
        value = (
            str(artifact.get("uri") or "").strip()
            if isinstance(artifact, dict)
            else str(artifact).strip()
        )
        if value and value not in artifact_refs:
            artifact_refs.append(value)
    store.record_workflow_node_result(
        task["id"],
        step_id,
        port=str(normalized.get("port") or port),
        output=normalized.get("output") or {},
        summary=summary,
        artifacts=normalized.get("artifacts") or [],
    )
    store.update_task_step_details(
        holder_id,
        result_summary=summary,
        step_output=normalized.get("output") or {},
        artifacts=artifact_refs,
    )
    return normalized, errors


def _dispatch_step(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str,
    mapped_inputs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    assignee = member["agent_name"]
    task_id = task["id"]
    handler = get_node_handler(step)
    # Hard token ceiling: never dispatch new work for a goal that has blown its
    # budget. Covers every dispatch path (initial, rework, timeout-reassign,
    # manual rerun) since all funnel through here.
    if _enforce_goal_token_budget(store, project_root, task):
        store.record_task_transition(
            task_id, "", step["id"], WORKFLOW_ENGINE_AGENT, "blocked",
            "goal token budget exceeded; dispatch frozen",
        )
        # A goal row uses its own vocabulary ("stalled"); a subtask stays "blocked".
        store.set_task_workflow_state(
            task_id, task_status="stalled" if task.get("is_goal") else "blocked"
        )
        return None
    _ensure_engine_agent(store)
    if not store.agent_exists(assignee):
        # Pre-register so the dispatch waits in their inbox until they poll.
        store.register_agent(assignee, f"workflow agent for step {step['id']}")
    completion_hint = (
        f"Choose one declared outcome port ({', '.join(step.get('ports') or ['success'])}) "
        "when completing this approval."
        if handler.dispatch_mode == "human"
        else (
            f"When finished call complete_step(agent=\"{assignee}\", task_id={task_id}, "
            f"step=\"{step['id']}\", outcome=\"done\"|\"rework\"|\"blocked\", result=\"...\")."
        )
    )
    content = (
        f"[workflow step: {step['id']}] Task #{task_id}: {task.get('title') or 'untitled'}\n\n"
        f"{task.get('content', '')}\n"
        + (f"\nUpstream result:\n{upstream_result}\n" if upstream_result else "")
        + f"\nYou are running step '{step['name']}'.\n"
        + completion_hint
    )
    store.send_message(
        WORKFLOW_ENGINE_AGENT, assignee, content,
        reply_to=task.get("source_message_id"),
    )
    store.record_task_transition(
        task_id, "", step["id"], WORKFLOW_ENGINE_AGENT, "dispatched", assignee
    )
    # A root goal keeps its own lifecycle status (new/designing/decomposing).
    # Regular tasks become assigned until their runner actually starts.
    if task.get("is_goal"):
        store.set_task_workflow_state(
            task_id,
            task_status=_goal_status_for_step(project_root, step["id"]),
            assignee=assignee,
        )
    else:
        store.set_task_workflow_state(
            task_id, task_status="assigned", assignee=assignee
        )
    step_inputs = {
        "task": {
            "id": task_id,
            "title": task.get("title") or "",
            "content": task.get("content") or "",
        },
        "step": {
            "id": step["id"],
            "name": step.get("name") or step["id"],
        },
        "upstream_result": upstream_result or "",
        "mapped": mapped_inputs or {},
    }
    if _materializes_step_cards(task):
        _upsert_step_card(
            store, project_root, task, step, assignee, step_inputs
        )
    else:
        store.update_task_step_details(
            task_id, step_inputs=step_inputs, result_summary="", step_output={}, artifacts=[]
        )
    # An explicit dispatch override (manual Re-run) wins; otherwise the round-
    # robin Agent's per-step command (or its built-in CLI) is used.
    command = (
        str(member.get("runner_command") or "").strip()
        or (str(step.get("command") or "").strip() if handler.name == "command" else "")
        or _step_agent_command(step, assignee)
    )
    if command:
        return store.create_run_job(
            task_id,
            step["id"],
            assignee,
            command,
            upstream_result,
            note=f"queued runner for step {step['id']}",
        )
    return None


def _dispatch_targets(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    targets: list[str],
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    upstream_result: str,
    mapped_inputs_by_target: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    steps = {s["id"]: s for s in cfg["steps"]}
    task_id = task["id"]
    dispatched: list[dict[str, Any]] = []
    notices: list[str] = []
    queue = list(targets)
    while queue:
        # Budget ceiling before any dispatch bookkeeping, so an over-budget goal
        # halts cleanly without leaving a dangling pending dispatch action.
        if _enforce_goal_token_budget(store, project_root, task):
            store.set_task_workflow_state(task_id, task_status="blocked")
            notices.append("goal token budget exceeded; dispatch frozen")
            break
        target = queue.pop(0)
        transitions = store.list_task_transitions(task_id)
        correlation = store.sync_workflow_correlation(task_id, target)
        if target in _running_steps(transitions):
            if correlation is not None and _correlation_ready(correlation, steps.get(target)):
                # Recovery for a crash after dispatch but before the activation
                # was marked consumed. The active step proves dispatch won.
                store.consume_workflow_correlation(correlation["id"])
            continue  # a runner is still executing this step
        if _dispatched_since(
            transitions, target, _latest_inbound_completion_id(transitions, target)
        ):
            if correlation is not None and _correlation_ready(correlation, steps.get(target)):
                store.consume_workflow_correlation(correlation["id"])
            continue  # already dispatched for this target's current cycle
        if steps[target].get("join_policy") == "all_successful":
            # An all_successful join must not wait out a branch that already
            # terminally failed: the join itself blocks for hub recovery.
            failed_branches = _failed_join_branches(target, correlation, transitions)
            if failed_branches:
                already_blocked = any(
                    t["from_step"] == target
                    and t["to_step"] == target
                    and t["outcome"] == "blocked"
                    for t in transitions
                )
                if not already_blocked:
                    reason = (
                        f"join {target!r} policy all_successful: upstream "
                        f"branch(es) failed: {', '.join(failed_branches)}"
                    )
                    store.record_task_transition(
                        task_id, target, target, WORKFLOW_ENGINE_AGENT,
                        "blocked", reason, "blocked",
                    )
                    store.set_task_workflow_state(task_id, task_status="blocked")
                    if correlation is not None:
                        store.consume_workflow_correlation(correlation["id"])
                    notices.append(_notify_hub(store, f"Task #{task_id} {reason}"))
                continue
        join_is_ready = (
            _correlation_ready(correlation, steps.get(target))
            if correlation is not None
            else _join_ready(target, cfg, back, steps, transitions)
        )
        if not join_is_ready:
            notices.append(f"step {target} is waiting for other required branches")
            continue
        step = steps[target]
        handler = get_node_handler(step)
        mapped_inputs = (mapped_inputs_by_target or {}).get(target, {})
        input_errors = [] if handler.dispatch_mode == "join" else validate_json_schema(
            mapped_inputs, step.get("input_schema") or {}, "$.input"
        )
        if input_errors:
            reason = f"step {target!r} input validation failed: {'; '.join(input_errors)}"
            store.record_task_transition(
                task_id, "", target, WORKFLOW_ENGINE_AGENT, "dispatched", reason
            )
            store.record_task_transition(
                task_id, target, target, WORKFLOW_ENGINE_AGENT, "blocked", reason, "blocked"
            )
            store.set_task_workflow_state(task_id, task_status="blocked")
            notices.append(_notify_hub(store, f"Task #{task_id} {reason}"))
            break
        if handler.dispatch_mode == "foreach":
            foreach_context = build_mapping_context(
                {"input": mapped_inputs},
                task,
                store.list_workflow_node_results(task_id),
                target,
            )
            foreach_context["input"] = mapped_inputs
            try:
                scope_specs = _foreach_scope_specs(step, foreach_context)
                agents = step.get("agents") or []
                if not agents:
                    raise InvalidInputError(
                        f"foreach step {target!r} requires at least one Agent"
                    )
                used_agents = {
                    agents[index % len(agents)] for index in range(len(scope_specs))
                }
                missing_commands = [
                    agent for agent in used_agents
                    if not _step_agent_command(step, agent)
                ]
                if missing_commands:
                    raise InvalidInputError(
                        f"foreach step {target!r} has no runnable command for: "
                        + ", ".join(sorted(missing_commands))
                    )
                group = store.create_workflow_item_group(
                    task_id,
                    target,
                    scope_specs,
                    max_concurrency=int(step.get("max_concurrency") or 1),
                )
            except InvalidInputError as exc:
                reason = str(exc)
                store.record_task_transition(
                    task_id, target, target, WORKFLOW_ENGINE_AGENT,
                    "blocked", reason, "blocked",
                )
                store.set_task_workflow_state(task_id, task_status="blocked")
                notices.append(_notify_hub(store, f"Task #{task_id} {reason}"))
                break
            display_assignee = (step.get("agents") or [WORKFLOW_ENGINE_AGENT])[0]
            foreach_inputs = {
                "task": {
                    "id": task_id,
                    "title": task.get("title") or "",
                    "content": task.get("content") or "",
                },
                "step": {"id": target, "name": step.get("name") or target},
                "upstream_result": upstream_result or "",
                "mapped": mapped_inputs,
                "foreach": {
                    "group_id": group["id"],
                    "activation": group["activation"],
                    "item_count": len(group.get("scopes") or []),
                },
            }
            store.record_task_transition(
                task_id, "", target, WORKFLOW_ENGINE_AGENT, "dispatched",
                display_assignee,
            )
            if task.get("is_goal"):
                store.set_task_workflow_state(
                    task_id,
                    task_status=_goal_status_for_step(project_root, target),
                    assignee=display_assignee,
                )
            else:
                store.set_task_workflow_state(
                    task_id, task_status="in_progress", assignee=display_assignee
                )
            if _materializes_step_cards(task):
                _upsert_step_card(
                    store, project_root, task, step, display_assignee, foreach_inputs
                )
            else:
                store.update_task_step_details(
                    task_id,
                    step_inputs=foreach_inputs,
                    result_summary="",
                    step_output={},
                    artifacts=[],
                )
            item_dispatches = _queue_ready_foreach_scopes(
                store, project_root, task, step, group, upstream_result, mapped_inputs
            )
            dispatched.append(
                {
                    "step": target,
                    "assignee": display_assignee,
                    "item_group_id": group["id"],
                }
            )
            dispatched.extend(item_dispatches)
            if group.get("status") == "completed":
                foreach_report = _finalize_foreach_group_locked(
                    store, project_root, task, step, group
                )
                dispatched.extend(foreach_report.get("dispatched") or [])
                notices.extend(foreach_report.get("notices") or [])
            if correlation is not None:
                store.consume_workflow_correlation(correlation["id"])
            continue
        if handler.dispatch_mode == "subflow":
            subflow_name = str(step.get("subflow") or "")
            if subflow_name not in (cfg.get("subflows") or {}):
                # The config changed under an in-flight task; block for the hub
                # instead of stranding the step in a dispatch loop.
                reason = f"step {target!r} references unknown subflow: {subflow_name!r}"
                store.record_task_transition(
                    task_id, target, target, WORKFLOW_ENGINE_AGENT,
                    "blocked", reason, "blocked",
                )
                store.set_task_workflow_state(task_id, task_status="blocked")
                notices.append(_notify_hub(store, f"Task #{task_id} {reason}"))
                break
            _ensure_engine_agent(store)
            subflow_inputs = {
                "task": {
                    "id": task_id,
                    "title": task.get("title") or "",
                    "content": task.get("content") or "",
                },
                "step": {"id": target, "name": step.get("name") or target},
                "upstream_result": upstream_result or "",
                "mapped": mapped_inputs,
                "subflow": {"name": subflow_name},
            }
            store.record_task_transition(
                task_id, "", target, WORKFLOW_ENGINE_AGENT, "dispatched",
                WORKFLOW_ENGINE_AGENT,
            )
            if task.get("is_goal"):
                store.set_task_workflow_state(
                    task_id,
                    task_status=_goal_status_for_step(project_root, target),
                    assignee=WORKFLOW_ENGINE_AGENT,
                )
            else:
                store.set_task_workflow_state(
                    task_id, task_status="in_progress", assignee=WORKFLOW_ENGINE_AGENT
                )
            if _materializes_step_cards(task):
                _upsert_step_card(
                    store, project_root, task, step,
                    WORKFLOW_ENGINE_AGENT, subflow_inputs,
                )
            else:
                store.update_task_step_details(
                    task_id, step_inputs=subflow_inputs,
                    result_summary="", step_output={}, artifacts=[],
                )
            # The subflow node's execution IS the child task: a fresh task whose
            # workflow_ref routes it through the subflow's own graph. The parent
            # step stays active until the child reaches a terminal state (see
            # _finalize_subflow_locked).
            work = (task.get("title") or "").strip()
            child = store.create_subflow_task(
                parent_task_id=task_id,
                workflow_ref=subflow_name,
                title=(f"{step.get('name') or target} · {work[:60]}" if work
                       else str(step.get("name") or target))[:160],
                content=task.get("content") or "",
                sender=WORKFLOW_ENGINE_AGENT,
            )
            parent_run_id, parent_node_run_id = _run_lineage_for_step(
                store, task_id, target
            )
            child_upstream = upstream_result
            if mapped_inputs:
                mapped_json = json.dumps(
                    mapped_inputs, ensure_ascii=False, sort_keys=True
                )
                child_upstream = (
                    f"{upstream_result}\n\nMAPPED_INPUTS:\n{mapped_json}".strip()
                )
            child_report = _start_workflow_task_locked(
                store, project_root, WORKFLOW_ENGINE_AGENT, child["id"],
                parent_run_id=parent_run_id,
                parent_node_run_id=parent_node_run_id,
                upstream_result=child_upstream,
            )
            dispatched.append(
                {
                    "step": target,
                    "assignee": WORKFLOW_ENGINE_AGENT,
                    "subflow": subflow_name,
                    "subflow_task_id": child["id"],
                }
            )
            dispatched.extend(child_report.get("dispatched") or [])
            notices.extend(child_report.get("notices") or [])
            if correlation is not None:
                store.consume_workflow_correlation(correlation["id"])
            continue
        if handler.dispatch_mode == "end":
            _complete_end_node(store, project_root, task, step)
            if correlation is not None:
                store.consume_workflow_correlation(correlation["id"])
            dispatched.append({"step": target, "assignee": WORKFLOW_ENGINE_AGENT})
            queue.clear()
            break
        if handler.dispatch_mode == "join":
            if (
                step.get("join_policy") in {"any", "quorum", "count"}
                and store.list_workflow_node_results(task_id, target)
            ):
                continue
            latest_to_join: dict[str, dict[str, Any]] = {}
            for transition in transitions:
                if (
                    transition["to_step"] == target
                    and transition["from_step"]
                    and (
                        correlation is None
                        or transition["id"] > correlation["transition_cursor"]
                    )
                ):
                    latest_to_join[transition["from_step"]] = transition
            join_inputs: list[dict[str, Any]] = []
            artifacts: list[Any] = []
            seen_sources: set[str] = set()
            for edge in cfg["edges"]:
                source = edge["from"]
                if (
                    edge["to"] != target
                    or (source, target) in back
                    or source in seen_sources
                ):
                    continue
                seen_sources.add(source)
                arrival = latest_to_join.get(source) or {}
                if arrival.get("outcome") not in {"done", "skipped"}:
                    continue
                stored_results = store.list_workflow_node_results(task_id, source)
                stored = stored_results[-1] if stored_results else {}
                item = {
                    "source": source,
                    "port": str(stored.get("port") or arrival.get("port") or ""),
                    "output": stored.get("output") or {},
                    "summary": str(stored.get("summary") or arrival.get("note") or ""),
                    "artifacts": stored.get("artifacts") or [],
                }
                join_inputs.append(item)
                artifacts.extend(item["artifacts"])
            aggregation = step.get("aggregation") or "list"
            if aggregation == "object_by_source":
                value: Any = {
                    item["source"]: {
                        "port": item["port"],
                        "output": item["output"],
                        "summary": item["summary"],
                        "artifacts": item["artifacts"],
                    }
                    for item in join_inputs
                }
            elif aggregation == "first":
                value = join_inputs[0] if join_inputs else None
            else:
                value = join_inputs
            join_output = {"inputs": join_inputs, "value": value}
            join_input_errors = validate_json_schema(
                join_output, step.get("input_schema") or {}, "$.input"
            )
            if join_input_errors:
                reason = (
                    f"step {target!r} input validation failed: "
                    + "; ".join(join_input_errors)
                )
                store.record_task_transition(
                    task_id, target, target, WORKFLOW_ENGINE_AGENT,
                    "blocked", reason, "blocked",
                )
                store.set_task_workflow_state(task_id, task_status="blocked")
                notices.append(_notify_hub(store, f"Task #{task_id} {reason}"))
                break
            _ensure_engine_agent(store)
            join_step_inputs = {
                "task": {
                    "id": task_id,
                    "title": task.get("title") or "",
                    "content": task.get("content") or "",
                },
                "step": {"id": target, "name": step.get("name") or target},
                "upstream_result": upstream_result or "",
                "mapped": join_output,
            }
            store.record_task_transition(
                task_id, "", target, WORKFLOW_ENGINE_AGENT, "dispatched",
                WORKFLOW_ENGINE_AGENT,
            )
            if task.get("is_goal"):
                store.set_task_workflow_state(
                    task_id,
                    task_status=_goal_status_for_step(project_root, target),
                    assignee=WORKFLOW_ENGINE_AGENT,
                )
            else:
                store.set_task_workflow_state(
                    task_id, task_status="in_progress", assignee=WORKFLOW_ENGINE_AGENT
                )
            if _materializes_step_cards(task):
                _upsert_step_card(
                    store, project_root, task, step,
                    WORKFLOW_ENGINE_AGENT, join_step_inputs,
                )
            else:
                store.update_task_step_details(
                    task_id, step_inputs=join_step_inputs,
                    result_summary="", step_output={}, artifacts=[],
                )
            # A satisfied any/quorum/count join no longer needs its slower
            # branches; `remaining: cancel` stops their in-flight work before
            # the join's own advance dispatches downstream.
            if (
                correlation is not None
                and step.get("join_remaining") == "cancel"
                and step.get("join_policy") in {"any", "quorum", "count"}
            ):
                notices.extend(
                    _cancel_remaining_join_branches(
                        store, task, cfg, back, target, correlation
                    )
                )
            join_result = "WORKFLOW_RESULT: " + json.dumps(
                {
                    "port": step.get("default_port") or "success",
                    "output": join_output,
                    "summary": f"Joined {len(join_inputs)} input(s) using {aggregation}",
                    "artifacts": artifacts,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            dispatched.append({"step": target, "assignee": WORKFLOW_ENGINE_AGENT})
            join_report = _advance_workflow_task_locked(
                store, project_root, WORKFLOW_ENGINE_AGENT,
                task_id, target, "done", join_result,
            )
            dispatched.extend(join_report.get("dispatched") or [])
            notices.extend(join_report.get("notices") or [])
            if correlation is not None:
                store.consume_workflow_correlation(correlation["id"])
            continue
        if handler.dispatch_mode == "decision":
            _ensure_engine_agent(store)
            decision_inputs = {
                "task": {
                    "id": task_id,
                    "title": task.get("title") or "",
                    "content": task.get("content") or "",
                },
                "step": {"id": step["id"], "name": step.get("name") or step["id"]},
                "upstream_result": upstream_result or "",
                "mapped": mapped_inputs,
            }
            store.record_task_transition(
                task_id, "", target, WORKFLOW_ENGINE_AGENT, "dispatched",
                WORKFLOW_ENGINE_AGENT,
            )
            if task.get("is_goal"):
                store.set_task_workflow_state(
                    task_id,
                    task_status=_goal_status_for_step(project_root, target),
                    assignee=WORKFLOW_ENGINE_AGENT,
                )
            else:
                store.set_task_workflow_state(
                    task_id, task_status="in_progress", assignee=WORKFLOW_ENGINE_AGENT
                )
            if _materializes_step_cards(task):
                _upsert_step_card(
                    store, project_root, task, step, WORKFLOW_ENGINE_AGENT, decision_inputs
                )
            else:
                store.update_task_step_details(
                    task_id,
                    step_inputs=decision_inputs,
                    result_summary="",
                    step_output={},
                    artifacts=[],
                )
            selected_port = str(step.get("default_port") or "default")
            for rule in step.get("rules") or []:
                if evaluate_jsonlogic(rule["when"], mapped_inputs):
                    selected_port = str(rule["port"])
                    break
            decision_result = "WORKFLOW_RESULT: " + json.dumps(
                {
                    "port": selected_port,
                    "output": mapped_inputs,
                    "summary": f"Decision selected {selected_port}",
                    "artifacts": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            dispatched.append({"step": target, "assignee": WORKFLOW_ENGINE_AGENT})
            decision_report = _advance_workflow_task_locked(
                store,
                project_root,
                WORKFLOW_ENGINE_AGENT,
                task_id,
                target,
                "done",
                decision_result,
            )
            dispatched.extend(decision_report.get("dispatched") or [])
            notices.extend(decision_report.get("notices") or [])
            if correlation is not None:
                store.consume_workflow_correlation(correlation["id"])
            continue
        assignee = (
            HUB_NOTIFY_AGENT
            if handler.dispatch_mode == "human"
            else _step_round_robin_assignee(store, step, transitions)
            if handler.requires_agent
            else WORKFLOW_ENGINE_AGENT
        )
        action = store.create_workflow_action(
            task_id,
            "dispatch_step",
            step=target,
            assignee=assignee,
            note=f"dispatch step {target} to {assignee}",
        )
        try:
            target_upstream = upstream_result
            if mapped_inputs:
                mapped_json = json.dumps(mapped_inputs, ensure_ascii=False, sort_keys=True)
                target_upstream = (
                    f"{upstream_result}\n\nMAPPED_INPUTS:\n{mapped_json}".strip()
                )
            _dispatch_step(
                store, project_root, task, step,
                {"agent_name": assignee},
                target_upstream,
                mapped_inputs,
            )
        except Exception as exc:
            if action:
                store.finish_workflow_action(action["id"], "failed", str(exc))
            raise
        if action:
            store.finish_workflow_action(action["id"], "done")
        if correlation is not None:
            store.consume_workflow_correlation(correlation["id"])
        dispatched.append({"step": target, "assignee": assignee})
        if handler.dispatch_mode == "human":
            store.record_task_transition(
                task_id, target, target, WORKFLOW_ENGINE_AGENT, "approval",
                "waiting for human decision", step.get("default_port") or "approved",
            )
            store.set_task_workflow_state(task_id, task_status="blocked")
    transitions = store.list_task_transitions(task_id)
    active = _active_steps(transitions)
    if active:
        store.set_task_workflow_state(
            task_id,
            workflow_step=",".join(active),
        )
    return dispatched, notices


def _foreach_saved_inputs(
    store: Store, task: dict[str, Any], step_id: str
) -> tuple[str, dict[str, Any]]:
    holder = task
    if _materializes_step_cards(task):
        card = store.find_open_step_card(task["id"], step_id)
        if card:
            holder = card
    step_inputs = holder.get("step_inputs") or {}
    return (
        str(step_inputs.get("upstream_result") or ""),
        step_inputs.get("mapped") or {},
    )


def apply_foreach_item_outcome(
    store: Store,
    project_root: str | None,
    item_scope_id: int,
    assignee: str,
    outcome: str,
    result: str,
) -> dict[str, Any]:
    """Settle one item run, release siblings, and advance the parent foreach."""
    with _WORKFLOW_ENGINE_LOCK:
        scope = store.get_workflow_item_scope(item_scope_id)
        if not scope:
            return {"item_scope_id": item_scope_id, "error": "unknown foreach item scope"}
        group = store.get_workflow_item_group(int(scope["group_id"]))
        if not group:
            return {"item_scope_id": item_scope_id, "error": "unknown foreach item group"}
        task = store.get_task(int(group["task_id"]))
        if not task:
            return {"item_scope_id": item_scope_id, "error": "unknown workflow task"}
        if task.get("task_status") == "closed":
            for open_scope in group.get("scopes") or []:
                if open_scope.get("status") in {"pending", "ready", "running"}:
                    store.update_workflow_item_scope(
                        int(open_scope["id"]), "cancelled", {}, "workflow task is closed"
                    )
            store.cancel_open_workflow_item_scopes(
                group["id"], "workflow task is closed"
            )
            return {
                "task_id": task["id"],
                "item_scope_id": item_scope_id,
                "closed": True,
                "dispatched": [],
            }
        cfg = workflow_config_for_task(project_root, task)
        step = next(
            (item for item in cfg["steps"] if item["id"] == group["foreach_step"]),
            None,
        )
        if not step or get_node_handler(step).dispatch_mode != "foreach":
            return {"item_scope_id": item_scope_id, "error": "foreach step is unavailable"}

        if scope.get("status") in {"completed", "blocked", "cancelled"}:
            if group.get("status") == "completed":
                report = _finalize_foreach_group_locked(
                    store, project_root, task, step, group
                )
                return {**report, "item_scope_id": item_scope_id, "replayed": True}
            if scope.get("status") == "completed" and group.get("status") == "active":
                upstream_result, mapped_inputs = _foreach_saved_inputs(
                    store, task, step["id"]
                )
                dispatched = _queue_ready_foreach_scopes(
                    store, project_root, task, step, group, upstream_result, mapped_inputs
                )
                return {
                    "task_id": task["id"],
                    "step": step["id"],
                    "item_scope_id": item_scope_id,
                    "replayed": True,
                    "dispatched": dispatched,
                    "notices": [],
                }
            return {
                "task_id": task["id"],
                "step": step["id"],
                "item_scope_id": item_scope_id,
                "replayed": True,
                "dispatched": [],
            }

        normalized, parse_error = _normalized_step_result(result, "success")
        schema_errors = validate_json_schema(
            normalized.get("output") or {},
            step.get("item_output_schema") or {},
            "$.item.output",
        )
        errors = ([parse_error] if parse_error else []) + schema_errors
        if outcome != "done":
            errors.insert(0, f"item runner outcome was {outcome or 'blocked'}")
        if errors:
            reason = f"foreach item {scope['scope_key']!r} failed: {'; '.join(errors)}"
            store.update_workflow_item_scope(
                item_scope_id, "blocked", normalized, reason
            )
            store.cancel_open_workflow_item_scopes(group["id"], reason)
            transitions = store.list_task_transitions(task["id"])
            if step["id"] in _running_steps(transitions):
                store.record_task_transition(
                    task["id"], step["id"], step["id"], assignee,
                    "blocked", reason, "blocked",
                )
            store.set_task_workflow_state(task["id"], task_status="blocked")
            _settle_step_card(store, task, step["id"], "blocked")
            notice = _notify_hub(store, f"Task #{task['id']} {reason}")
            return {
                "task_id": task["id"],
                "step": step["id"],
                "item_scope_id": item_scope_id,
                "blocked": True,
                "dispatched": [],
                "notices": [notice],
            }

        store.update_workflow_item_scope(
            item_scope_id, "completed", normalized
        )
        group = store.get_workflow_item_group(group["id"])
        if group and group.get("status") == "completed":
            report = _finalize_foreach_group_locked(
                store, project_root, task, step, group
            )
            return {**report, "item_scope_id": item_scope_id}
        upstream_result, mapped_inputs = _foreach_saved_inputs(store, task, step["id"])
        dispatched = _queue_ready_foreach_scopes(
            store, project_root, task, step, group or {}, upstream_result, mapped_inputs
        )
        return {
            "task_id": task["id"],
            "step": step["id"],
            "item_scope_id": item_scope_id,
            "dispatched": dispatched,
            "notices": [],
        }


def recover_foreach_item_groups(
    store: Store, project_root: str | None
) -> list[dict[str, Any]]:
    """Resume ready scopes and completed-but-unadvanced groups after restart."""
    recovered: list[dict[str, Any]] = []
    with _WORKFLOW_ENGINE_LOCK:
        for group in store.list_recoverable_workflow_item_groups():
            task = store.get_task(int(group["task_id"]))
            if not task:
                continue
            # Per-task config: a subflow child's foreach step lives in its own
            # graph. A dangling workflow_ref must not abort the whole sweep.
            try:
                cfg = workflow_config_for_task(project_root, task)
            except InvalidInputError:
                continue
            steps = {step["id"]: step for step in cfg["steps"]}
            step = steps.get(str(group["foreach_step"]))
            if not step or get_node_handler(step).dispatch_mode != "foreach":
                continue
            if task.get("task_status") == "closed":
                for scope in group.get("scopes") or []:
                    if scope.get("status") in {"pending", "ready", "running"}:
                        store.update_workflow_item_scope(
                            int(scope["id"]), "cancelled", {}, "workflow task is closed"
                        )
                store.cancel_open_workflow_item_scopes(
                    group["id"], "workflow task is closed"
                )
                continue
            if group.get("status") == "completed":
                report = _finalize_foreach_group_locked(
                    store, project_root, task, step, group
                )
                recovered.append({"group_id": group["id"], "report": report})
                continue
            failed_scope = None
            for scope in group.get("scopes") or []:
                if scope.get("status") != "running":
                    continue
                if store.has_open_item_run_job(scope["id"]):
                    continue
                jobs = store.list_run_jobs_for_item_scope(scope["id"])
                if jobs and jobs[-1].get("status") in {"done", "failed", "cancelled"}:
                    failed_scope = (scope, jobs[-1])
                    break
            if failed_scope:
                scope, job = failed_scope
                report = apply_foreach_item_outcome(
                    store,
                    project_root,
                    int(scope["id"]),
                    str(job.get("assignee") or WORKFLOW_ENGINE_AGENT),
                    "blocked",
                    str(job.get("note") or "foreach runner job failed"),
                )
                recovered.append({"group_id": group["id"], "report": report})
                continue
            upstream_result, mapped_inputs = _foreach_saved_inputs(
                store, task, step["id"]
            )
            dispatched = _queue_ready_foreach_scopes(
                store, project_root, task, step, group, upstream_result, mapped_inputs
            )
            if dispatched:
                recovered.append({"group_id": group["id"], "dispatched": dispatched})
    return recovered


def start_workflow_task(
    store: Store, project_root: str | None, agent: str, task_id: int
) -> dict[str, Any]:
    with _WORKFLOW_ENGINE_LOCK:
        return _start_workflow_task_locked(store, project_root, agent, task_id)


def rerun_workflow_step(
    store: Store,
    project_root: str | None,
    task_id: int,
    agent: str,
    step: str | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Re-run a blocked (or active) workflow step with a chosen agent.

    Used by the task panel to recover a step that blocked — e.g. the assigned
    CLI hit a rate/session limit — by re-dispatching it to a different agent
    (a different model) without editing the workflow. Records a fresh dispatch to
    `agent` for the step and spawns its runner; on success the engine advances
    as usual."""
    agent = (agent or "").strip()
    if not agent:
        raise InvalidInputError("agent is required to re-run a step")
    with _WORKFLOW_ENGINE_LOCK:
        task = store.get_task(task_id)
        if not task:
            raise InvalidInputError(f"unknown task: {task_id}")
        transitions = store.list_task_transitions(task_id)
        if not transitions:
            # The board shows step cards, not the workflow task; a card has no
            # transitions of its own. Redirect a re-run on a card to its parent
            # workflow task, defaulting the step to the card's own step.
            parent_id = task.get("parent_task_id")
            parent = store.get_task(parent_id) if parent_id else None
            if parent and store.list_task_transitions(parent_id):
                if not (step or "").strip():
                    step = task.get("workflow_step") or ""
                task, task_id = parent, parent_id
                transitions = store.list_task_transitions(parent_id)
            else:
                raise InvalidInputError(
                    f"task {task_id} has not entered the workflow yet; start it first"
                )
        cfg = workflow_config_for_task(project_root, task)
        steps = {s["id"]: s for s in cfg["steps"]}
        step_id = (step or "").strip()
        if not step_id:
            # Prefer the most recently blocked step; fall back to whatever step
            # is currently active.
            blocked = [t for t in transitions if t["outcome"] == "blocked"]
            if blocked:
                last = blocked[-1]
                step_id = last["to_step"] or last["from_step"]
            else:
                active = _active_steps(transitions)
                step_id = active[-1] if active else ""
        if step_id not in steps:
            raise InvalidInputError(
                f"cannot determine a workflow step to re-run for task {task_id}"
                + (f" (unknown step {step_id!r})" if step_id else "")
            )
        step_def = steps[step_id]
        # Guard against double-running: if a runner is still in flight for this
        # step, a second dispatch would race (two runners advancing the same
        # step). Runs are recorded on the goal's step card when materialized.
        run_holder_id = task_id
        if _materializes_step_cards(task):
            card = store.find_open_step_card(task_id, step_id)
            if card:
                run_holder_id = card["id"]
        # Only the latest attempt can represent the current in-flight runner.
        # Older attempts may be stale leftovers (for example a lease was
        # reclaimed and a newer attempt already failed); those must not block
        # manual recovery via Re-run.
        runs = store.list_task_runs(run_holder_id, limit=1)
        if runs and runs[0].get("status") == "running":
            raise InvalidInputError(
                f"a run is already in progress for step {step_id!r}; wait for it "
                "to finish before re-running"
            )
        # The chosen agent's per-step command wins; else its built-in CLI.
        rerun_command = _step_agent_command(step_def, agent)
        member = {"agent_name": agent, "runner_command": rerun_command}
        if not rerun_command:
            raise InvalidInputError(
                f"step {step_id!r} has no runnable agent (select an installed "
                "agent or set the agent's command), so it cannot auto-run"
            )
        # Carry the upstream step's result forward so the re-run has the same
        # context the original dispatch had (empty for entry steps). The note is
        # the raw transcript (truncated at record time); collapse it to the same
        # structured block a live advance would have handed this step.
        upstream = [
            t for t in transitions
            if t["outcome"] == "done" and t["to_step"] == step_id
        ]
        upstream_result = (
            _structured_upstream(upstream[-1].get("note", "")) if upstream else ""
        )
        if context.strip():
            upstream_result = "\n\n".join(
                part for part in (upstream_result, context.strip()) if part
            )
        job = _dispatch_step(
            store, project_root, task, step_def, member, upstream_result
        )
        return {
            "task_id": task_id,
            "step": step_id,
            "assignee": agent,
            "runner_command": rerun_command,
            "queued_job_id": job["id"] if job else None,
            "reran": True,
        }


def skip_workflow_step(
    store: Store,
    project_root: str | None,
    task_id: int,
    step: str | None = None,
    actor: str = WORKFLOW_ENGINE_AGENT,
) -> dict[str, Any]:
    """Skip an unproductive gate and advance the task past it.

    Records the step as `skipped` — which the join gate already treats like
    `done` — settles its card, and dispatches the step's forward successor with
    the last real upstream result (a skip produces no result of its own). Refuses
    structural steps (integrate/decompose) and any step with no forward successor.
    Meant to break an impl<->review rework loop by accepting the current
    implementation and moving on, without editing the workflow."""
    with _WORKFLOW_ENGINE_LOCK:
        task = store.get_task(task_id)
        if not task:
            raise InvalidInputError(f"unknown task: {task_id}")
        transitions = store.list_task_transitions(task_id)
        if not transitions:
            # The board shows step cards; a card has no transitions of its own,
            # so redirect a skip on a card to its parent workflow task.
            parent_id = task.get("parent_task_id")
            parent = store.get_task(parent_id) if parent_id else None
            if parent and store.list_task_transitions(parent_id):
                if not (step or "").strip():
                    step = task.get("workflow_step") or ""
                task, task_id = parent, parent_id
                transitions = store.list_task_transitions(parent_id)
            else:
                raise InvalidInputError(
                    f"task {task_id} has not entered the workflow yet; start it first"
                )
        cfg = workflow_config_for_task(project_root, task)
        steps = {s["id"]: s for s in cfg["steps"]}
        back = _workflow_graph(cfg)
        step_id = (step or "").strip()
        if not step_id:
            # Prefer the most recently blocked step; fall back to the active one.
            blocked = [t for t in transitions if t["outcome"] == "blocked"]
            if blocked:
                last = blocked[-1]
                step_id = last["to_step"] or last["from_step"]
            else:
                active = _active_steps(transitions)
                step_id = active[-1] if active else ""
        if step_id not in steps:
            raise InvalidInputError(
                f"cannot determine a workflow step to skip for task {task_id}"
                + (f" (unknown step {step_id!r})" if step_id else "")
            )
        step_def = steps[step_id]
        forward = [
            e["to"] for e in cfg["edges"]
            if e["from"] == step_id and (e["from"], e["to"]) not in back
        ]
        if not forward:
            raise InvalidInputError(f"step {step_id!r} has no forward step to skip to")
        if not step_def.get("skippable", True):
            raise InvalidInputError(
                f"step {step_id!r} is not skippable"
            )
        # Never skip past an in-flight runner — wait for it (or block) first.
        run_holder_id = task_id
        if _materializes_step_cards(task):
            card = store.find_open_step_card(task_id, step_id)
            if card:
                run_holder_id = card["id"]
        runs = store.list_task_runs(run_holder_id, limit=1)
        if runs and runs[0].get("status") == "running":
            raise InvalidInputError(
                f"a run is still in progress for step {step_id!r}; wait for it to "
                "finish before skipping"
            )
        # Carry the last real upstream (e.g. the implement output) forward so the
        # next step keeps its context. Prefer a completion recorded INTO the
        # skipped step; if it never completed (only blocked/reworked), fall back to
        # this task's most recent `done` output so the next step isn't left blind.
        upstream = [
            t for t in transitions
            if t["outcome"] == "done" and t["to_step"] == step_id
        ] or [t for t in transitions if t["outcome"] == "done"]
        upstream_result = (
            _structured_upstream(upstream[-1].get("note", "")) if upstream else ""
        )
        note = f"step '{step_id}' skipped by {actor}"
        _record_step_result(store, task, step_id, note)
        store.cancel_pending_run_jobs(task_id, step_id, note)
        for target in forward:
            store.record_task_transition(task_id, step_id, target, actor, "skipped", note)
        _settle_step_card(store, task, step_id, "skipped")
        dispatched, notices = _dispatch_targets(
            store, project_root, task, forward, cfg, back, upstream_result
        )
        return {
            "task_id": task_id,
            "step": step_id,
            "skipped_to": forward,
            "dispatched": dispatched,
            "notices": notices,
            "skipped": True,
        }


def reimplement_workflow_task(
    store: Store, project_root: str | None, task_id: int, agent: str,
) -> dict[str, Any]:
    # Hold the engine lock across the reads AND the rerun it delegates to (the
    # RLock lets rerun re-acquire) so the state it inspects can't shift under it.
    with _WORKFLOW_ENGINE_LOCK:
        return _reimplement_workflow_task_locked(store, project_root, task_id, agent)


def _reimplement_workflow_task_locked(
    store: Store,
    project_root: str | None,
    task_id: int,
    agent: str,
) -> dict[str, Any]:
    """Resume a task stopped by the rework cap at ``implement``.

    This is intentionally distinct from retrying the blocked reviewer.  It
    sends the most recent rework verdict to the chosen implementer, so a human
    can change models and continue without raising the project's rework limit
    or losing the review trail.
    """
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    transitions = store.list_task_transitions(task_id)
    if not transitions and task.get("parent_task_id"):
        parent = store.get_task(task["parent_task_id"])
        if parent:
            task, task_id = parent, parent["id"]
            transitions = store.list_task_transitions(task_id)
    if task.get("task_status") != "blocked":
        raise InvalidInputError("re-implement is only available for blocked tasks")
    blocked = next(
        (t for t in reversed(transitions) if t["outcome"] == "blocked"), None
    )
    rework_cap = next(
        (
            t for t in reversed(transitions)
            if t["outcome"] == "blocked"
            and (t.get("note") or "").startswith("rework limit reached")
        ),
        None,
    )
    latest_note = (blocked or {}).get("note") or ""
    if not rework_cap or not (
        latest_note.startswith("rework limit reached")
        or latest_note.startswith("goal token budget exceeded")
    ):
        raise InvalidInputError("task is not blocked by the rework limit")
    # At the cap the engine records the rejected review as the detail after
    # the semicolon on the blocking transition (rather than a separate rework
    # transition), so prefer that most recent verdict.
    feedback_text = (rework_cap.get("note") or "").partition("; ")[2].strip()
    if not feedback_text:
        feedback = next(
            (
                t for t in reversed(transitions)
                if t["outcome"] == "rework" and t.get("to_step") == "implement"
            ),
            None,
        )
        feedback_text = (feedback or {}).get("note", "").strip()
    if not feedback_text:
        raise InvalidInputError("no review feedback is available to re-implement")
    context = "Latest review feedback to resolve before requesting review again:\n" + _tail(
        feedback_text, 20_000
    )
    return rerun_workflow_step(
        store, project_root, task_id, agent, step="implement", context=context
    )


def resume_goal_after_budget_increase(
    store: Store, project_root: str | None, goal_id: int, token_budget: Any
) -> dict[str, Any]:
    """Raise a frozen goal's budget and resume its latest blocked dispatch."""
    # One atomic critical section: pick the frozen step and dispatch it without a
    # concurrent advance moving the task in between (RLock — the delegated
    # rerun/reimplement re-acquire it).
    with _WORKFLOW_ENGINE_LOCK:
        return _resume_goal_after_budget_increase_locked(
            store, project_root, goal_id, token_budget
        )


def _resume_goal_after_budget_increase_locked(
    store: Store, project_root: str | None, goal_id: int, token_budget: Any
) -> dict[str, Any]:
    goal = store.get_task(goal_id)
    if not goal or not goal.get("is_goal"):
        raise InvalidInputError(f"unknown goal: {goal_id}")
    budget = _coerce_token_budget(token_budget)
    total = store.sum_goal_tokens(goal_id)
    if budget and budget <= total:
        raise InvalidInputError(
            f"new token budget must exceed current usage ({total}), or be 0 for unlimited"
        )

    descendants: list[dict[str, Any]] = []
    pending = [goal]
    while pending:
        current = pending.pop()
        descendants.append(current)
        pending.extend(store.list_tasks_by_parent(current["id"]))
    frozen: tuple[int, dict[str, Any], dict[str, Any]] | None = None
    for task in descendants:
        for transition in store.list_task_transitions(task["id"]):
            if (
                transition["outcome"] == "blocked"
                and (transition.get("note") or "").startswith("goal token budget exceeded")
            ):
                candidate = (int(transition["id"]), task, transition)
                if frozen is None or candidate[0] > frozen[0]:
                    frozen = candidate
    if frozen is None:
        raise InvalidInputError("no budget-frozen workflow step is available to resume")

    _, task, transition = frozen
    step_id = transition.get("to_step") or transition.get("from_step")
    cfg = workflow_config_for_task(project_root, task)
    steps = {step["id"]: step for step in cfg["steps"]}
    step = steps.get(step_id)
    if not step:
        raise InvalidInputError(f"frozen step {step_id!r} is no longer in the workflow")
    transitions = store.list_task_transitions(task["id"])
    agent = _step_round_robin_assignee(store, step, transitions)
    # update_task_metadata mirrors the new budget into the goal run's variables
    # (migration-period dual-write, design §11); clear the frozen marker and
    # refresh the usage snapshot here. Record layer only — non-fatal on error.
    store.update_task_metadata(goal_id, token_budget=budget)
    try:
        run = store.get_workflow_run_by_task(goal_id)
        if run is not None:
            store.update_workflow_run_variables(
                int(run["id"]), tokens_total=total, budget_frozen_at=None
            )
    except Exception:
        _log.exception(
            "budget resume mirror failed for goal %s (non-fatal)", goal_id
        )
    if step_id == "implement" and any(
        t["outcome"] == "blocked"
        and (t.get("note") or "").startswith("rework limit reached")
        for t in transitions
    ):
        resumed = reimplement_workflow_task(store, project_root, task["id"], agent)
    else:
        resumed = rerun_workflow_step(
            store, project_root, task["id"], agent, step=step_id
        )
    store.set_task_workflow_state(goal_id, task_status="running")
    return {**resumed, "goal_id": goal_id, "token_budget": budget, "tokens_total": total}


def force_close_goal(
    store: Store, project_root: str | None, task_id: int
) -> dict[str, Any]:
    """Force-end a goal (or any task): terminate every runner still running in
    its subtree and close the whole tree. Straggler runners that finish
    afterwards no-op (advance ignores terminal tasks), so the goal stays closed."""
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    with _WORKFLOW_ENGINE_LOCK:
        pids = store.running_run_pids_in_tree(task_id)
        killed = sum(1 for pid in pids if _terminate_pid_tree(pid))
        closed = store.close_task_tree(task_id)
    return {
        "task_id": task_id,
        "closed_tasks": closed,
        "killed_runners": killed,
    }


def _start_workflow_task_locked(
    store: Store,
    project_root: str | None,
    agent: str,
    task_id: int,
    parent_run_id: int | None = None,
    parent_node_run_id: int | None = None,
    upstream_result: str = "",
) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    if task.get("is_goal"):
        conflict = active_goal_conflict_reason(store, exclude_task_id=task_id)
        if conflict:
            raise InvalidInputError(conflict)
    transitions = store.list_task_transitions(task_id)
    if transitions:
        raise InvalidInputError(
            f"task {task_id} is already in the workflow "
            f"(active steps: {', '.join(_active_steps(transitions)) or 'none'})"
        )
    cfg = workflow_config_for_task(project_root, task)
    back = _workflow_graph(cfg)
    execution_errors = _workflow_execution_errors(cfg, back)
    if execution_errors:
        raise InvalidInputError(
            "workflow is not executable: "
            + "; ".join(execution_errors)
            + ". Check the Workflow page warnings."
        )
    # Isolate/integrate steps need a git repo with a base commit. Provision one
    # now (init + first commit) rather than letting steps silently degrade; if
    # git isn't installed the workflow still runs unisolated (integrate no-ops).
    if _workflow_needs_git(cfg):
        _ensure_git_repo(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    entries = [
        step_id for step_id in _workflow_entry_steps(cfg, back)
        if steps[step_id]["required"] or _forward_out(cfg, back, step_id)
    ]
    # Dual-write: open the run record with entry tokens before the first
    # dispatch, so the dispatch mirror finds its inbound token to consume.
    _ensure_workflow_run(
        store, task_id, cfg, entry_steps=entries,
        parent_run_id=parent_run_id, parent_node_run_id=parent_node_run_id,
    )
    dispatched, notices = _dispatch_targets(
        store, project_root, task, entries, cfg, back, upstream_result
    )
    return {
        "task_id": task_id,
        "started": True,
        "dispatched": dispatched,
        "notices": notices,
    }


def _start_workflow_task_at_locked(
    store: Store,
    project_root: str | None,
    agent: str,
    task_id: int,
    from_step: str,
    target_steps: list[str],
    upstream_result: str = "",
    parent_run_id: int | None = None,
    parent_node_run_id: int | None = None,
) -> dict[str, Any]:
    """Start a fresh task partway through the workflow — at `target_steps` (the
    decompose step's successors) instead of the entry. Used for decompose subtasks
    that begin after the goal's shared design steps, inheriting the goal's
    decompose output as their upstream."""
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    cfg = workflow_config_for_task(project_root, task)
    back = _workflow_graph(cfg)
    valid = {s["id"] for s in cfg["steps"]}
    targets = [s for s in target_steps if s in valid]
    # Dual-write: open the run record first; the seed transitions below emit
    # this run's entry tokens (from_step -> target) through the store mirror.
    _ensure_workflow_run(
        store, task_id, cfg, entry_steps=[],
        parent_run_id=parent_run_id, parent_node_run_id=parent_node_run_id,
    )
    # Seed the pre-split boundary exactly as a normal advance does before it
    # dispatches: record `from_step -> target (done)` so the join gate sees the
    # decompose step as a satisfied predecessor and lets each target dispatch on
    # this otherwise-transitionless subtask. (The goal already ran everything up
    # to and including the decompose step.)
    for target in targets:
        store.record_task_transition(task_id, from_step, target, agent, "done", upstream_result)
    dispatched, notices = _dispatch_targets(
        store, project_root, task, targets, cfg, back, upstream_result
    )
    return {
        "task_id": task_id,
        "started": True,
        "dispatched": dispatched,
        "notices": notices,
    }


def advance_workflow_task(
    store: Store,
    project_root: str | None,
    agent: str,
    task_id: int,
    step: str,
    outcome: str = "done",
    result: str = "",
) -> dict[str, Any]:
    with _WORKFLOW_ENGINE_LOCK:
        return _advance_workflow_task_locked(
            store, project_root, agent, task_id, step, outcome, result
        )


def _advance_workflow_task_locked(
    store: Store,
    project_root: str | None,
    agent: str,
    task_id: int,
    step: str,
    outcome: str = "done",
    result: str = "",
) -> dict[str, Any]:
    outcome = (outcome or "done").strip()
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    if task.get("task_status") == "closed":
        # A straggler runner finishing after the task was force-closed must not
        # re-open or re-dispatch it. (Only "closed" is terminal here — "accepted"
        # is also the accept step's own in-progress status.)
        return {
            "task_id": task_id, "step": step, "outcome": outcome,
            "closed": True, "dispatched": [], "notices": [],
            "note": "task already terminal; advance ignored",
        }
    cfg = workflow_config_for_task(project_root, task)
    steps = {s["id"]: s for s in cfg["steps"]}
    if step not in steps:
        raise InvalidInputError(f"unknown workflow step: {step}")
    declared_ports = set(steps[step].get("ports") or ["success", "rework"])
    reserved_ports = {"success", "rework", "blocked", "error", "timeout", "cancelled"}
    structured_preview, _ = _normalized_step_result(result)
    structured_port = str(structured_preview.get("port") or "").strip()
    if outcome == "done":
        selected_port = structured_port or steps[step].get("default_port") or "success"
        control_outcome = selected_port if selected_port in reserved_ports - {"success"} else "done"
    elif outcome in {"rework", "blocked", "error", "timeout", "cancelled"}:
        selected_port = outcome
        control_outcome = outcome
    elif outcome == "approval":
        raise InvalidInputError("approval is an engine-managed step state")
    elif outcome in declared_ports and outcome not in reserved_ports:
        selected_port = outcome
        control_outcome = "done"
    else:
        allowed = sorted(
            {"done", "rework", "blocked", "error", "timeout", "cancelled"}
            | (declared_ports - reserved_ports)
        )
        raise InvalidInputError(
            f"invalid outcome: {outcome!r} (expected one of {allowed})"
        )
    if structured_port and structured_port not in declared_ports and structured_port not in reserved_ports:
        raise InvalidInputError(
            f"WORKFLOW_RESULT selected undeclared port: {structured_port!r}"
        )
    transitions = store.list_task_transitions(task_id)
    prior_approval = None
    if agent == HUB_NOTIFY_AGENT and outcome == "done":
        prior_approval = next(
            (
                transition for transition in reversed(transitions)
                if transition["from_step"] == step and transition["outcome"] == "approval"
            ),
            None,
        )
        approval_port = (prior_approval or {}).get("port", "")
        if approval_port in declared_ports:
            selected_port = approval_port
    active_assignees = _active_step_assignees(transitions)
    if step not in active_assignees:
        raise InvalidInputError(f"workflow step {step} is not active for task {task_id}")
    assigned_agent = active_assignees[step]
    # Constraint: only the agent that was dispatched the active step may
    # complete it. The hub agent can override any active step for recovery.
    engine_managed = get_node_handler(steps[step]).dispatch_mode in {
        "decision", "join", "foreach", "subflow", "end"
    }
    if (
        agent != assigned_agent
        and agent != HUB_NOTIFY_AGENT
        and not (agent == WORKFLOW_ENGINE_AGENT and engine_managed)
    ):
        raise InvalidInputError(
            f"agent {agent} is not assigned to active step {step} "
            f"(assigned to {assigned_agent})"
        )
    prior_results = (
        store.list_workflow_node_results(task_id, step)
        if prior_approval and steps[step].get("approval_required") and not result.strip()
        else []
    )
    if prior_results:
        latest_result = prior_results[-1]
        normalized_result = {
            "port": selected_port,
            "output": latest_result.get("output") or {},
            "summary": latest_result.get("summary") or "",
            "artifacts": latest_result.get("artifacts") or [],
        }
        normalization_errors = validate_json_schema(
            normalized_result["output"], steps[step].get("output_schema") or {}
        )
    else:
        normalized_result, normalization_errors = _record_step_result(
            store,
            task,
            step,
            result,
            port=selected_port,
            output_schema=steps[step].get("output_schema") or {},
        )
    store.cancel_pending_run_jobs(
        task_id,
        step,
        f"step settled by {agent} with outcome {outcome}",
    )
    if normalization_errors:
        reason = "structured output validation failed: " + "; ".join(normalization_errors)
        max_normalization_retries = int(
            (steps[step].get("retry") or {}).get("normalization", 0) or 0
        )
        prior_normalization_retries = sum(
            1
            for transition in transitions
            if transition["from_step"] == step
            and transition["outcome"] == "reassigned"
            and str(transition.get("note") or "").startswith("normalization retry ")
        )
        if prior_normalization_retries < max_normalization_retries:
            retry_number = prior_normalization_retries + 1
            retry_note = (
                f"normalization retry {retry_number}/{max_normalization_retries}: {reason}"
            )
            retry_holder = task
            if _materializes_step_cards(task):
                retry_card = store.find_open_step_card(task_id, step)
                if retry_card:
                    retry_holder = retry_card
            prior_inputs = retry_holder.get("step_inputs") or {}
            original_upstream = str(prior_inputs.get("upstream_result") or "")
            retry_context = (
                f"{original_upstream}\n\nSTRUCTURED_OUTPUT_RETRY:\n{retry_note}\n"
                "Return a corrected WORKFLOW_RESULT matching the output schema."
            ).strip()
            store.record_task_transition(
                task_id, step, step, agent, "reassigned", retry_note, selected_port
            )
            job = _dispatch_step(
                store,
                project_root,
                task,
                steps[step],
                {"agent_name": assigned_agent},
                retry_context,
                prior_inputs.get("mapped") or {},
            )
            return {
                "task_id": task_id,
                "step": step,
                "outcome": "retry",
                "dispatched": [{"step": step, "assignee": assigned_agent}],
                "notices": [],
                "normalization_errors": normalization_errors,
                "normalization_retry": retry_number,
                "normalization_retry_limit": max_normalization_retries,
                "queued_job_id": job["id"] if job else None,
            }
        store.record_task_transition(
            task_id, step, step, agent, "blocked", f"{reason}; {result}", "blocked"
        )
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
        notice = _notify_hub(store, f"Task #{task_id} at step {step!r}: {reason}")
        return {
            "task_id": task_id,
            "step": step,
            "outcome": "blocked",
            "dispatched": [],
            "notices": [notice],
            "normalization_errors": normalization_errors,
        }
    back = _workflow_graph(cfg)
    def _edge_port(edge: dict[str, Any]) -> str:
        if edge.get("port"):
            return str(edge["port"])
        if edge.get("rework") or (edge["from"], edge["to"]) in back:
            return "rework"
        return "success"

    mapping_context = build_mapping_context(
        normalized_result,
        task,
        store.list_workflow_node_results(task_id),
        step,
    )
    if steps[step].get("type") == "join":
        mapping_context["join"] = {
            "inputs": normalized_result.get("output", {}).get("inputs") or []
        }
    port_edges = [
        edge for edge in cfg["edges"]
        if edge["from"] == step and _edge_port(edge) == selected_port
    ]
    conditional_matches = [
        edge for edge in port_edges
        if "condition" in edge and bool(evaluate_jsonlogic(edge["condition"], mapping_context))
    ]
    if conditional_matches:
        winning_priority = min(int(edge.get("priority", 0)) for edge in conditional_matches)
        routed_edges = [
            edge for edge in conditional_matches
            if int(edge.get("priority", 0)) == winning_priority
        ]
    else:
        routed_edges = [edge for edge in port_edges if "condition" not in edge]
    routed_edge_ids = {id(edge) for edge in routed_edges}
    audited_edges = (
        [edge for edge in cfg["edges"] if edge["from"] == step]
        if steps[step].get("type") == "decision"
        else port_edges
    )
    _persist_routing_correlations(
        store, task_id, cfg, back, step, audited_edges, routed_edges
    )
    for edge in audited_edges:
        if id(edge) not in routed_edge_ids:
            store.record_task_transition(
                task_id,
                step,
                edge["to"],
                WORKFLOW_ENGINE_AGENT,
                "not_selected",
                "edge condition did not select this branch",
                selected_port,
            )
            _record_branch_closure_lineage(
                store,
                task_id,
                cfg,
                back,
                step,
                edge["to"],
                selected_port,
            )
    backward = [
        edge["to"] for edge in routed_edges
        if (edge["from"], edge["to"]) in back and _edge_port(edge) == "rework"
    ]
    port_targets = [edge["to"] for edge in routed_edges]
    failure_ports = {"blocked", "error", "timeout", "cancelled"}

    # Approval is a state of this completed step, not another node in the
    # graph. The runner has finished, but the step remains active so the hub can
    # later submit `done` (continue) or `rework` (use its loop-back edge).
    if (
        control_outcome == "done"
        and selected_port not in failure_ports
        and steps[step].get("approval_required")
        and agent != HUB_NOTIFY_AGENT
    ):
        store.record_task_transition(
            task_id, step, step, agent, "approval", result, selected_port
        )
        store.set_task_workflow_state(task_id, task_status="blocked")
        _recompute_parent_goal_status(store, task, project_root)
        return {
            "task_id": task_id, "step": step, "outcome": "approval",
            "dispatched": [], "notices": [], "awaiting_approval": True,
        }

    if agent == HUB_NOTIFY_AGENT and task.get("task_status") == "blocked":
        store.set_task_workflow_state(task_id, task_status="in_progress")

    if control_outcome in failure_ports and not port_targets:
        store.record_task_transition(
            task_id, step, step, agent, control_outcome, result, selected_port
        )
        # This branch terminally failed with no recovery route. Close its merge
        # boundaries so a downstream join learns the branch will never arrive —
        # an all_successful join turns that closure into its own blocked state.
        _record_branch_closure_lineage(
            store, task_id, cfg, back, step, step, selected_port,
            outcome="cancelled" if control_outcome == "cancelled" else "blocked",
            note=f"branch failed at {step} ({selected_port})",
        )
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
        notice = _notify_hub(
            store,
            f"Task #{task_id} reached {selected_port!r} at step '{step}' by {agent}: "
            f"{result or 'no details'}",
        )
        return {
            "task_id": task_id, "step": step, "outcome": outcome,
            "dispatched": [], "notices": [notice],
        }

    transition_outcome = control_outcome
    if control_outcome in failure_ports:
        targets = port_targets
        # The runner/node run remains failed/blocked/timed-out; the transition is
        # completion-style so the existing active-step and join ledger can move
        # on, while `port` preserves the real recovery reason.
        transition_outcome = "done"
    elif control_outcome == "rework":
        targets = backward
        if not targets:
            raise InvalidInputError(f"step {step} has no rework (loop-back) path")
        # Rework-loop cap: if this step's own loop-back has already been taken
        # the maximum number of times, stop looping and block for the hub instead
        # of dispatching another round that would likely fail the same way.
        # Counted per originating step (from_step == step): a rework from another
        # step into the same target (e.g. test -> implement) has its own budget
        # and must not drain this step's (e.g. review -> implement).
        prior_rework = sum(
            1 for t in transitions
            if t["outcome"] == "rework"
            and t["from_step"] == step
            and t["to_step"] in targets
        )
        edge_rework_limits = [
            int(edge["max_iterations"])
            for edge in cfg["edges"]
            if edge["from"] == step
            and edge["to"] in targets
            and _edge_port(edge) == "rework"
            and edge.get("max_iterations") is not None
        ]
        max_rework = (
            min(edge_rework_limits)
            if edge_rework_limits
            else read_settings(project_root)["max_rework_rounds"]
        )
        if prior_rework >= max_rework:
            store.record_task_transition(
                task_id, step, step, agent, "blocked",
                f"rework limit reached ({max_rework} rounds); {result}", "blocked",
            )
            store.set_task_workflow_state(task_id, task_status="blocked")
            _settle_step_card(store, task, step, "blocked")
            _recompute_parent_goal_status(store, task, project_root)
            notice = _notify_hub(
                store,
                f"Task #{task_id} hit the rework limit ({max_rework} rounds) "
                f"at step '{step}' -> {', '.join(targets)}; blocked instead of "
                f"looping again. Last result: {result or 'no details'}",
            )
            return {
                "task_id": task_id, "step": step, "outcome": "blocked",
                "dispatched": [], "notices": [notice], "rework_limited": True,
            }
    else:
        targets = port_targets

    selected_edges = [edge for edge in routed_edges if edge["to"] in targets]
    exhausted_edge = next(
        (
            edge for edge in selected_edges
            if edge.get("max_iterations") is not None
            and sum(
                1 for transition in transitions
                if transition["from_step"] == step
                and transition["to_step"] == edge["to"]
                and (
                    transition.get("port")
                    or ("rework" if transition["outcome"] == "rework" else "success")
                ) == selected_port
            ) >= int(edge["max_iterations"])
        ),
        None,
    )
    if exhausted_edge is not None:
        limit = int(exhausted_edge["max_iterations"])
        reason = (
            f"edge iteration limit reached ({limit}): {step} "
            f"-[{selected_port}]-> {exhausted_edge['to']}"
        )
        store.record_task_transition(
            task_id, step, step, agent, "blocked", f"{reason}; {result}", selected_port
        )
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
        notice = _notify_hub(store, f"Task #{task_id} {reason}")
        return {
            "task_id": task_id, "step": step, "outcome": "blocked",
            "dispatched": [], "notices": [notice], "iteration_limited": True,
        }

    mapped_inputs_by_target: dict[str, dict[str, Any]] = {}
    try:
        for edge in selected_edges:
            if edge.get("mapping") is not None:
                mapped_inputs_by_target[edge["to"]] = apply_input_mapping(
                    mapping_context, edge.get("mapping")
                )
    except ValueError as exc:
        reason = f"edge input mapping failed at step {step!r}: {exc}"
        store.record_task_transition(
            task_id, step, step, agent, "blocked", reason, "blocked"
        )
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
        notice = _notify_hub(store, f"Task #{task_id} {reason}")
        return {
            "task_id": task_id, "step": step, "outcome": "blocked",
            "dispatched": [], "notices": [notice], "mapping_error": str(exc),
        }

    for target in targets:
        store.record_task_transition(
            task_id, step, target, agent, transition_outcome, result, selected_port
        )

    if (
        control_outcome == "done"
        and not targets
        and steps[step].get("unrouted", "allowed") != "allowed"
    ):
        reason = f"step {step} emitted unrouted port {selected_port!r}"
        store.record_task_transition(
            task_id, step, step, agent, "blocked", f"{reason}; {result}", selected_port
        )
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
        notice = _notify_hub(store, f"Task #{task_id} {reason}")
        return {
            "task_id": task_id, "step": step, "outcome": "blocked",
            "dispatched": [], "notices": [notice], "unrouted_port": selected_port,
        }

    if control_outcome == "done" and not targets:
        # Terminal step completed: the task leaves the workflow.
        store.record_task_transition(task_id, step, "", agent, "done", result, selected_port)
        _settle_step_card(store, task, step, "done")
        if task.get("is_goal"):
            final_status = _finish_goal_workflow(store, project_root, task)
        else:
            final_status = "closed"
            store.set_task_workflow_state(
                task_id, workflow_step="", task_status=final_status
            )
            _recompute_parent_goal_status(store, task, project_root)
        return {
            "task_id": task_id, "step": step, "outcome": outcome,
            "closed": True, "goal_status": final_status if task.get("is_goal") else None,
            "dispatched": [], "notices": [],
        }

    _settle_step_card(
        store, task, step, "done" if control_outcome in failure_ports else control_outcome
    )
    # Forward flow hands the next step the structured upstream block (summary +
    # artifact references it reads directly). Rework keeps the raw feedback:
    # the reviewer's itemized reasons ARE the instructions for the redo, and
    # collapsing them to a one-line summary would lose exactly what matters.
    upstream = (
        _structured_upstream(result)
        if control_outcome == "done" and selected_port not in failure_ports
        else result
    )
    dispatched, notices = _dispatch_targets(
        store, project_root, task, targets, cfg, back, upstream,
        mapped_inputs_by_target,
    )
    report = {
        "task_id": task_id, "step": step, "outcome": outcome,
        "dispatched": dispatched, "notices": notices,
    }
    latest_task = store.get_task(task_id) or {}
    if latest_task.get("task_status") in {"closed", "accepted", "verifying"}:
        report["closed"] = True
    return report
