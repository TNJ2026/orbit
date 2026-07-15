"""Goal orchestration, step cards, dispatch, and workflow state transitions."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .process_control import terminate_pid_tree as _terminate_pid_tree
from .runner_prompts import (
    step_agent_command as _step_agent_command,
    step_round_robin_assignee as _step_round_robin_assignee,
)
from .runner_protocol import (
    parse_step_output_metadata as _parse_step_output_metadata,
    structured_upstream as _structured_upstream,
    tail as _tail,
)
from .settings import read_settings
from .store import InvalidInputError, Store, UnknownAgentError
from .verification import detect_goal_verify as _detect_goal_verify
from .workflow_config import _project_root, read_workflow_config
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
        goals.append({
            **task,
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
    missing: list[str] = []
    for step in _main_workflow_reachable_steps(cfg, back):
        if step.get("approval"):
            continue
        agents = step.get("agents") or []
        if not agents:
            missing.append(f"{step['id']}: no agent selected")
            continue
        for agent in agents:
            if not _step_agent_command(step, agent):
                missing.append(f"{step['id']} ({agent}): no command")
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
) -> list[dict[str, Any]]:
    """Create each business subtask and start it in the workflow. By default a
    subtask starts at the entry step (splits at intake). When `target_steps` is
    given (a later decompose step's successors, `from_step` being that decompose
    step), the subtask instead begins there with `upstream_result` — the goal's
    shared design/architecture output — as its upstream context, so those steps
    run once on the goal, not per subtask."""
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
    started: list[dict[str, Any]] = []
    for idx, subtask in enumerate(subtasks):
        task = created[idx]
        if subtask.get("deps"):
            started.append({"task": store.get_task(task["id"]), "held": True})
            continue
        result = _dispatch_business_subtask(
            store, project_root, actor, task["id"],
            from_step, target_steps, upstream_result,
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
) -> dict[str, Any]:
    """Start one business subtask in the workflow — at the entry step, or at the
    decompose step's successors when the goal split after its design phase."""
    if target_steps is None:
        return _start_workflow_task_locked(store, project_root, actor, task_id)
    return _start_workflow_task_at_locked(
        store, project_root, actor, task_id,
        from_step or "", target_steps, upstream_result,
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


def _recompute_parent_goal_status(
    store: Store, task: dict[str, Any], project_root: str | None = None
) -> None:
    """Roll a subtask status change up to its parent goal:
    all business subtasks closed -> accepted; any blocked -> stalled;
    otherwise in_progress. A goal that was explicitly closed is left as-is."""
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
            store, project_root, goal, actor, subtasks
        )
    else:
        started = start_goal_business_subtasks(
            store, project_root, goal, actor, subtasks,
            from_step=step["id"],
            target_steps=target_steps,
            upstream_result=result,
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
            card["id"], step_inputs=step_inputs, result_summary="", artifacts=[]
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
) -> None:
    """Attach structured output to the current step execution holder."""
    holder_id = task["id"]
    if _materializes_step_cards(task):
        card = store.find_open_step_card(task["id"], step_id)
        if card:
            holder_id = card["id"]
    summary, artifacts = _parse_step_output_metadata(result)
    store.update_task_step_details(
        holder_id, result_summary=summary, artifacts=artifacts
    )


def _dispatch_step(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str,
) -> dict[str, Any] | None:
    assignee = member["agent_name"]
    task_id = task["id"]
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
    content = (
        f"[workflow step: {step['id']}] Task #{task_id}: {task.get('title') or 'untitled'}\n\n"
        f"{task.get('content', '')}\n"
        + (f"\nUpstream result:\n{upstream_result}\n" if upstream_result else "")
        + f"\nYou are running step '{step['name']}'.\n"
        f"When finished call complete_step(agent=\"{assignee}\", task_id={task_id}, "
        f"step=\"{step['id']}\", outcome=\"done\"|\"rework\"|\"blocked\", result=\"...\")."
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
    }
    if _materializes_step_cards(task):
        _upsert_step_card(
            store, project_root, task, step, assignee, step_inputs
        )
    else:
        store.update_task_step_details(
            task_id, step_inputs=step_inputs, result_summary="", artifacts=[]
        )
    # An explicit dispatch override (manual Re-run) wins; otherwise the round-
    # robin Agent's per-step command (or its built-in CLI) is used.
    command = (
        str(member.get("runner_command") or "").strip()
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
        if target in _running_steps(transitions):
            continue  # a runner is still executing this step
        if _dispatched_since(
            transitions, target, _latest_inbound_completion_id(transitions, target)
        ):
            continue  # already dispatched for this target's current cycle
        if not _join_ready(target, cfg, back, steps, transitions):
            notices.append(f"step {target} is waiting for other required branches")
            continue
        step = steps[target]
        assignee = HUB_NOTIFY_AGENT if step.get("approval") else _step_round_robin_assignee(store, step, transitions)
        action = store.create_workflow_action(
            task_id,
            "dispatch_step",
            step=target,
            assignee=assignee,
            note=f"dispatch step {target} to {assignee}",
        )
        try:
            _dispatch_step(
                store, project_root, task, step,
                {"agent_name": assignee},
                upstream_result,
            )
        except Exception as exc:
            if action:
                store.finish_workflow_action(action["id"], "failed", str(exc))
            raise
        if action:
            store.finish_workflow_action(action["id"], "done")
        dispatched.append({"step": target, "assignee": assignee})
    transitions = store.list_task_transitions(task_id)
    active = _active_steps(transitions)
    if active:
        store.set_task_workflow_state(
            task_id,
            workflow_step=",".join(active),
        )
    return dispatched, notices


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
        cfg = read_workflow_config(project_root)
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
        cfg = read_workflow_config(project_root)
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
        if step_def.get("integrate") or step_def.get("decompose"):
            raise InvalidInputError(
                f"step {step_id!r} is structural (integrate/decompose) and cannot be skipped"
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
    cfg = read_workflow_config(project_root)
    steps = {step["id"]: step for step in cfg["steps"]}
    step = steps.get(step_id)
    if not step:
        raise InvalidInputError(f"frozen step {step_id!r} is no longer in the workflow")
    transitions = store.list_task_transitions(task["id"])
    agent = _step_round_robin_assignee(store, step, transitions)
    store.update_task_metadata(goal_id, token_budget=budget)
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
    store: Store, project_root: str | None, agent: str, task_id: int
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
    cfg = read_workflow_config(project_root)
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
    dispatched, notices = _dispatch_targets(
        store, project_root, task, entries, cfg, back, ""
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
) -> dict[str, Any]:
    """Start a fresh task partway through the workflow — at `target_steps` (the
    decompose step's successors) instead of the entry. Used for decompose subtasks
    that begin after the goal's shared design steps, inheriting the goal's
    decompose output as their upstream."""
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    valid = {s["id"] for s in cfg["steps"]}
    targets = [s for s in target_steps if s in valid]
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
    if outcome not in WORKFLOW_OUTCOMES:
        raise InvalidInputError(
            f"invalid outcome: {outcome!r} (expected one of {sorted(WORKFLOW_OUTCOMES)})"
        )
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
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    if step not in steps:
        raise InvalidInputError(f"unknown workflow step: {step}")
    transitions = store.list_task_transitions(task_id)
    active_assignees = _active_step_assignees(transitions)
    if step not in active_assignees:
        raise InvalidInputError(f"workflow step {step} is not active for task {task_id}")
    assigned_agent = active_assignees[step]
    # Constraint: only the agent that was dispatched the active step may
    # complete it. The hub agent can override any active step for recovery.
    if agent != assigned_agent and agent != HUB_NOTIFY_AGENT:
        raise InvalidInputError(
            f"agent {agent} is not assigned to active step {step} "
            f"(assigned to {assigned_agent})"
        )
    _record_step_result(store, task, step, result)
    store.cancel_pending_run_jobs(
        task_id,
        step,
        f"step settled by {agent} with outcome {outcome}",
    )
    back = _workflow_graph(cfg)
    forward = [
        e["to"] for e in cfg["edges"]
        if e["from"] == step and (e["from"], e["to"]) not in back
    ]
    backward = [
        e["to"] for e in cfg["edges"]
        if e["from"] == step and (e["from"], e["to"]) in back
    ]

    # Approval is a state of this completed step, not another node in the
    # graph. The runner has finished, but the step remains active so the hub can
    # later submit `done` (continue) or `rework` (use its loop-back edge).
    if outcome == "done" and steps[step].get("approval_required") and agent != HUB_NOTIFY_AGENT:
        store.record_task_transition(task_id, step, step, agent, "approval", result)
        store.set_task_workflow_state(task_id, task_status="blocked")
        _recompute_parent_goal_status(store, task, project_root)
        return {
            "task_id": task_id, "step": step, "outcome": "approval",
            "dispatched": [], "notices": [], "awaiting_approval": True,
        }

    if outcome == "approval":
        raise InvalidInputError("approval is an engine-managed step state")
    if agent == HUB_NOTIFY_AGENT and task.get("task_status") == "blocked":
        store.set_task_workflow_state(task_id, task_status="in_progress")

    if outcome == "blocked":
        store.record_task_transition(task_id, step, step, agent, "blocked", result)
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
        notice = _notify_hub(
            store,
            f"Task #{task_id} blocked at step '{step}' by {agent}: {result or 'no details'}",
        )
        return {
            "task_id": task_id, "step": step, "outcome": "blocked",
            "dispatched": [], "notices": [notice],
        }

    if outcome == "rework":
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
        max_rework = read_settings(project_root)["max_rework_rounds"]
        if prior_rework >= max_rework:
            store.record_task_transition(
                task_id, step, step, agent, "blocked",
                f"rework limit reached ({max_rework} rounds); {result}",
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
        targets = forward

    for target in targets:
        store.record_task_transition(task_id, step, target, agent, outcome, result)

    if outcome == "done" and not targets:
        # Terminal step completed: the task leaves the workflow.
        store.record_task_transition(task_id, step, "", agent, "done", result)
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
            "task_id": task_id, "step": step, "outcome": "done",
            "closed": True, "goal_status": final_status if task.get("is_goal") else None,
            "dispatched": [], "notices": [],
        }

    _settle_step_card(store, task, step, outcome)
    # Forward flow hands the next step the structured upstream block (summary +
    # artifact references it reads directly). Rework keeps the raw feedback:
    # the reviewer's itemized reasons ARE the instructions for the redo, and
    # collapsing them to a one-line summary would lose exactly what matters.
    upstream = _structured_upstream(result) if outcome == "done" else result
    dispatched, notices = _dispatch_targets(
        store, project_root, task, targets, cfg, back, upstream
    )
    return {
        "task_id": task_id, "step": step, "outcome": outcome,
        "dispatched": dispatched, "notices": notices,
    }
