"""Timeout detection and automatic recovery for workflow tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .runner_protocol import structured_upstream
from .store import InvalidInputError, Store
from .workflow_config import read_workflow_config, workflow_config_for_task
from .workflow_graph import active_step_assignees, workflow_graph

@dataclass(frozen=True)
class RecoveryCallbacks:
    notify_hub: Callable[[Store, str], str]
    materializes_step_cards: Callable[[dict[str, Any]], bool]
    dispatch_step: Callable[..., dict[str, Any] | None]
    step_agent_command: Callable[[dict[str, Any], str], str]
    step_round_robin_assignee: Callable[..., str]
    root_goal_decompose_step_id: Callable[..., str | None]
    business_subtasks_for_goal: Callable[..., list[dict[str, Any]]]
    finish_goal_workflow: Callable[..., Any]
    recompute_parent_goal_status: Callable[..., Any]
    workflow_engine_agent: str = "workflow"

def check_workflow_step_timeouts(
    store: Store,
    project_root: str | None,
    now: datetime | None = None,
    *,
    callbacks: RecoveryCallbacks,
) -> list[dict[str, Any]]:
    """Notify hub once when an active step exceeds its configured timeout."""
    now = now or datetime.now(timezone.utc)
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    actions: list[dict[str, Any]] = []
    for task in store.list_active_workflow_tasks():
        task_id = task["id"]
        transitions = store.list_task_transitions(task_id)
        for step_id, assignee in active_step_assignees(transitions).items():
            step = steps.get(step_id)
            if not step or int(step.get("timeout_minutes") or 0) <= 0:
                continue
            timeout_minutes = int(step["timeout_minutes"])
            last_dispatch = max(
                (
                    t for t in transitions
                    if t["outcome"] == "dispatched" and t["to_step"] == step_id
                ),
                key=lambda t: t["id"],
                default=None,
            )
            if last_dispatch is None:
                continue
            try:
                dispatched_at = datetime.fromisoformat(last_dispatch["created_at"])
            except ValueError:
                continue
            age_minutes = (now - dispatched_at).total_seconds() / 60
            if age_minutes < timeout_minutes:
                continue
            already_handled = any(
                t["id"] > last_dispatch["id"]
                and t["outcome"] in ("timeout", "reassigned")
                and t["from_step"] == step_id
                for t in transitions
            )
            if already_handled:
                continue
            store.record_task_transition(
                task_id,
                step_id,
                step_id,
                callbacks.workflow_engine_agent,
                "timeout",
                f"step timed out after {timeout_minutes}m on {assignee}",
            )
            notice = callbacks.notify_hub(
                store,
                f"Task #{task_id} step '{step_id}' timed out on {assignee} after "
                f"{timeout_minutes}m. Please intervene (complete_step as hub, or "
                f"re-run the step).",
            )
            actions.append({
                "task_id": task_id, "step": step_id, "action": "notified_hub",
                "from": assignee, "notice": notice,
            })
    return actions


# Runs left in these states mean a runner is gone, not working.
_RUN_DEAD_STATUSES = ("orphaned", "failed")
_PENDING_WORKFLOW_ACTION_STALE_SECONDS = 30
AUTO_RECOVERY_MAX_ATTEMPTS = 2


def latest_run_for_step(
    store: Store,
    task: dict[str, Any],
    step_id: str,
    *,
    callbacks: RecoveryCallbacks,
) -> dict[str, Any] | None:
    """Newest run for a step, read from its run holder (the goal's step card
    when materialized, else the task itself — same target run_step_worker
    records on)."""
    holder_id = task["id"]
    if callbacks.materializes_step_cards(task):
        card = store.find_open_step_card(task["id"], step_id)
        if card:
            holder_id = card["id"]
    runs = store.list_task_runs(holder_id, limit=1)
    return runs[0] if runs else None


def run_is_after(run: dict[str, Any], transition: dict[str, Any]) -> bool:
    """Whether a run started at or after a transition — used to confirm a dead
    run belongs to the current dispatch rather than a superseded one."""
    try:
        return datetime.fromisoformat(run["started_at"]) >= datetime.fromisoformat(
            transition["created_at"]
        )
    except (ValueError, TypeError, KeyError):
        return True


def task_has_running_run(store: Store, task_id: int) -> bool:
    holders = [task_id] + [c["id"] for c in store.list_tasks_by_parent(task_id)]
    for holder in holders:
        if any(r["status"] == "running" for r in store.list_task_runs(holder, limit=5)):
            return True
    return False


def is_stale_timestamp(value: str, now: datetime, seconds: int) -> bool:
    try:
        created_at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    return (now - created_at).total_seconds() >= seconds


def auto_recover_step(
    store: Store, project_root: str | None, task: dict[str, Any],
    step: dict[str, Any], assignee: str, upstream_result: str, reason: str,
    *, callbacks: RecoveryCallbacks,
) -> dict[str, Any]:
    """Idempotently re-dispatch a deterministic missing-step failure.

    Recovery attempts are persisted as workflow actions.  After two attempts we
    stop retrying and send one structured Hub escalation instead of looping.
    """
    task_id, step_id = int(task["id"]), step["id"]
    if store.has_open_run_job(task_id, step_id):
        return {"action": "already_queued"}
    actions = [
        a for a in store.list_workflow_actions("all", 500)
        if a["task_id"] == task_id and a["action_type"] == "recover_step"
        and a["step"] == step_id and a["note"].startswith(reason)
    ]
    if len(actions) >= AUTO_RECOVERY_MAX_ATTEMPTS:
        if not any(
            a["task_id"] == task_id and a["action_type"] == "recovery_escalation"
            and a["step"] == step_id for a in store.list_workflow_actions("all", 500)
        ):
            action = store.create_workflow_action(
                task_id, "recovery_escalation", step_id, assignee,
                note=f"{reason}; automatic recovery limit reached",
            )
            notice = callbacks.notify_hub(
                store,
                f"Task #{task_id} step '{step_id}' could not be auto-recovered "
                f"after {AUTO_RECOVERY_MAX_ATTEMPTS} attempts ({reason}). "
                "Choose reassign, rework, budget change, or force-end.",
            )
            if action:
                store.finish_workflow_action(action["id"], "alerted", "hub escalated")
            return {"action": "escalated", "notice": notice}
        return {"action": "escalated"}
    action = store.create_workflow_action(
        task_id, "recover_step", step_id, assignee, note=reason,
    )
    job = callbacks.dispatch_step(
        store, project_root, task, step,
        {
            "agent_name": assignee,
            "runner_command": callbacks.step_agent_command(step, assignee),
        },
        upstream_result,
    )
    if action:
        store.finish_workflow_action(
            action["id"], "done" if job else "failed",
            f"recovery {'queued' if job else 'could not dispatch'}",
        )
    return {"action": "recovered" if job else "dispatch_failed", "job_id": job and job["id"]}
def check_task_health(
    store: Store,
    project_root: str | None,
    now: datetime | None = None,
    *,
    callbacks: RecoveryCallbacks,
) -> list[dict[str, Any]]:
    """Bottom-line watchdog for the otherwise purely event-driven engine: scan
    non-terminal tasks and notify the hub about stuck states nothing else
    recovers — (A) a step whose runner died (orphaned/failed run, none running),
    (B) a rework transition that never dispatched its target, and (C) a task or
    directly-executing goal with no active step and no run in progress. Alerts
    are deduped via a `health_alert` transition so the hub is not re-pinged
    every cycle for the same unchanged problem."""
    cfg = read_workflow_config(project_root)
    back = workflow_graph(cfg)
    main_steps = {s["id"]: s for s in cfg["steps"]}
    alerts: list[dict[str, Any]] = []
    now = now or datetime.now(timezone.utc)
    for action in store.list_workflow_actions(status="pending", limit=500):
        if action.get("action_type") != "dispatch_step":
            continue
        if not is_stale_timestamp(
            action.get("created_at", ""), now, _PENDING_WORKFLOW_ACTION_STALE_SECONDS
        ):
            continue
        task = store.get_task(action["task_id"])
        if not task or task.get("task_status") in {"closed", "accepted", "blocked"}:
            store.finish_workflow_action(action["id"], "done")
            continue
        notice = callbacks.notify_hub(
            store,
            f"Task #{action['task_id']} has a pending dispatch action for step "
            f"'{action['step']}' to {action['assignee']} that did not complete. "
            "The server may have stopped mid-advance; inspect the task and re-run "
            "or complete_step as hub.",
        )
        store.finish_workflow_action(action["id"], "alerted", "health alert sent")
        alerts.append({
            "task_id": action["task_id"],
            "step": action["step"],
            "problem": "pending workflow action",
            "action_id": action["id"],
            "notice": notice,
        })

    for task in store.list_non_terminal_tasks():
        if task.get("task_status") == "blocked":
            continue
        task_id = task["id"]
        # A subflow child traverses its own graph: resolve its step map so its
        # steps recover like main-graph ones (and its finished flow is not
        # misread as "no forward target" against the main graph). A dangling
        # workflow_ref is skipped rather than aborting the sweep.
        steps = main_steps
        if str(task.get("workflow_ref") or "").strip():
            try:
                task_cfg = workflow_config_for_task(project_root, task)
            except InvalidInputError:
                continue
            steps = {s["id"]: s for s in task_cfg["steps"]}
        transitions = store.list_task_transitions(task_id)
        active = active_step_assignees(transitions)

        # A. A step is active but its runner is dead and nothing is running.
        for step_id, assignee in active.items():
            if step_id not in steps:
                continue
            run = latest_run_for_step(
                store, task, step_id, callbacks=callbacks
            )
            if not run or run["status"] not in _RUN_DEAD_STATUSES:
                continue
            last_dispatch = max(
                (t for t in transitions
                 if t["outcome"] == "dispatched" and t["to_step"] == step_id),
                key=lambda t: t["id"], default=None,
            )
            if last_dispatch is None:
                continue
            # The dead run must belong to the current dispatch. If it predates
            # the latest (re)dispatch, a fresh runner is still spawning — a new
            # run just has not been recorded yet — so hold off.
            if not run_is_after(run, last_dispatch):
                continue
            recovered = auto_recover_step(
                store,
                project_root,
                task,
                steps[step_id],
                assignee,
                "",
                "dead runner",
                callbacks=callbacks,
            )
            if recovered["action"] == "escalated":
                alerts.append({"task_id": task_id, "step": step_id,
                               "problem": "dead runner", **recovered})

        # B. advance_workflow_task records rework before dispatching the target.
        # If the server dies in that small window, the old step is settled but
        # the target never becomes active.
        undispatched_rework = False
        if transitions and not active and not task_has_running_run(store, task_id):
            reworks = [
                t for t in transitions
                if t["outcome"] == "rework" and t["to_step"] in steps
            ]
            if reworks:
                last_rework = reworks[-1]
                target = last_rework["to_step"]
                dispatched = any(
                    t["id"] > last_rework["id"]
                    and t["outcome"] == "dispatched"
                    and t["to_step"] == target
                    for t in transitions
                )
                undispatched_rework = not dispatched
                if not dispatched:
                    recovered = auto_recover_step(
                        store, project_root, task, steps[target],
                        callbacks.step_round_robin_assignee(
                            store, steps[target], transitions
                        ),
                        structured_upstream(last_rework.get("note", "")),
                        "undispatched rework",
                        callbacks=callbacks,
                    )
                    if recovered["action"] == "recovered":
                        undispatched_rework = False
                    elif recovered["action"] == "escalated":
                        alerts.append({"task_id": task_id, "step": target,
                                       "problem": "undispatched rework", **recovered})

        # The rework recovery branch above owns this state, including after its
        # alert has been deduplicated. Do not also classify the same gap as a
        # generic orphan merely because every active phase now projects to the
        # common in_progress task status.
        if undispatched_rework:
            continue

        # C. A regular task — or a goal still driving the workflow itself (no
        # decompose step, or the split hasn't produced work items yet) — has no
        # active step/run after an interrupted advance. Recover it into a
        # visible state, or finish it if the terminal settle was recorded but
        # the final status write was interrupted. Goals WITH work items are
        # owned by the roll-up path instead.
        direct_goal = bool(
            task.get("is_goal")
            and (
                callbacks.root_goal_decompose_step_id(cfg, back) is None
                or not any(
                    child.get("source_message_id") is not None
                    for child in callbacks.business_subtasks_for_goal(store, task_id)
                )
            )
        )
        if (
            (not task.get("is_goal") or direct_goal)
            and task.get("task_status")
            in {"in_progress", "new", "running", "decomposing"}
            and transitions
            and not active
            and not task_has_running_run(store, task_id)
        ):
            last_settle = max(
                (t for t in transitions if t["outcome"] in ("done", "rework")),
                key=lambda t: t["id"], default=None,
            )
            stalled_step = (last_settle or {}).get("to_step", "")
            if stalled_step and stalled_step in steps:
                recovered = auto_recover_step(
                    store, project_root, task, steps[stalled_step],
                    callbacks.step_round_robin_assignee(
                        store, steps[stalled_step], transitions
                    ),
                    structured_upstream((last_settle or {}).get("note", "")),
                    "advance interrupted before dispatch",
                    callbacks=callbacks,
                )
                if recovered["action"] == "escalated":
                    alerts.append({"task_id": task_id, "step": stalled_step,
                                   "problem": "orphaned -> escalation", **recovered})
            else:
                # No forward target left — the task really finished.
                if direct_goal:
                    callbacks.finish_goal_workflow(store, project_root, task)
                else:
                    store.set_task_workflow_state(
                        task_id, workflow_step="", task_status="closed"
                    )
                    callbacks.recompute_parent_goal_status(
                        store, task, project_root
                    )
                alerts.append({"task_id": task_id, "step": None,
                               "problem": (
                                   "orphaned -> accepted" if direct_goal
                                   else "orphaned -> closed"
                               ), "notice": ""})
    return alerts
