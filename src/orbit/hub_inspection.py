"""Hub-assisted detection of silent or stuck workflow runs."""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .process_control import detached_process_kwargs, kill_process_group
from .run_logs import read_run_output_tail, run_last_output_at
from .runner_prompts import step_agent_command, step_assignee
from .runner_protocol import tail
from .store import Store
from .workflow_config import _project_root as resolve_project_root, read_workflow_config

RUNNER_SOFT_TIMEOUT_SECONDS = 600
HUB_INSPECT_TIMEOUT_SECONDS = 180
HUB_SWEEP_POLL_SECONDS = 60
HUB_SWEEP_STATE: dict[int, dict[str, float]] = {}


def root_goal_decompose_step_id(cfg: dict[str, Any]) -> str | None:
    flagged = [step["id"] for step in cfg["steps"] if step.get("decompose")]
    return flagged[0] if flagged else None


def hub_command(project_root: str | None) -> str:
    """Runner command for hub-supervision CLI calls (timeout KILL/CONTINUE): the
    Decompose step's first Agent (its per-step command, else that Agent's built-in
    CLI). No decompose step — or its Agent has no command — disables supervision."""
    cfg = read_workflow_config(project_root)
    decompose_id = root_goal_decompose_step_id(cfg)
    if not decompose_id:
        return ""
    step = next((s for s in cfg["steps"] if s["id"] == decompose_id), None)
    if not step:
        return ""
    return step_agent_command(step, step_assignee(step))


def hub_inspect_batch(
    store: Store,
    project_root: str | None,
    candidates: list[dict[str, Any]],
    *,
    command_loader: Callable[[str | None], str] = hub_command,
    timeout_seconds: float = HUB_INSPECT_TIMEOUT_SECONDS,
) -> dict[int, str]:
    """One hub-agent call to judge several still-running, silent steps at once.
    Returns {run_id: "kill"|"continue"}; anything not clearly marked KILL by the
    hub stays "continue" so a healthy step is never killed on doubt."""
    decisions = {c["run_id"]: "continue" for c in candidates}
    command = command_loader(project_root)
    if not command:
        return decisions
    lines = []
    for i, c in enumerate(candidates, 1):
        out = tail(c.get("output") or "", 500) or "(长时间无输出)"
        lines.append(
            f"[{i}] 任务 #{c['task_id']}: {c.get('title') or ''}\n"
            f"    步骤 {c.get('step') or ''}（{c.get('assignee') or ''} 执行），"
            f"已运行约 {int(c['elapsed'] // 60)} 分钟，"
            f"无新输出约 {int(float(c.get('silent_for') or 0) // 60)} 分钟，输出: {out}"
        )
    prompt = (
        "你是编排 hub。下面这些工作流步骤运行较久且长时间无新输出，逐个判断它是"
        "正常执行还是卡死/出错（长时间零输出常见于后端无响应：API 超时 / 限流 / "
        "额度耗尽）。\n\n" + "\n".join(lines)
        + "\n\n对每个用一行给出裁决（编号对应上面），只输出裁决行：\n"
        "DECISION 1: CONTINUE\n"
        "DECISION 2: KILL\n"
        "（CONTINUE=还在正常执行，让它继续；KILL=卡死或出错，杀掉并标记 blocked）\n"
    )
    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(resolve_project_root(project_root)), **detached_process_kwargs(),
        )
        try:
            out, _ = proc.communicate(
                prompt.encode("utf-8"), timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            proc.wait()
            return decisions
    except OSError:
        return decisions
    text = out.decode("utf-8", errors="replace")
    for m in re.finditer(r"DECISION\s+(\d+)\s*:\s*(KILL|CONTINUE)", text, re.IGNORECASE):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(candidates) and m.group(2).upper() == "KILL":
            decisions[candidates[idx]["run_id"]] = "kill"
    return decisions


def hub_inspect_sweep(
    store: Store,
    project_root: str | None,
    now: float | None = None,
    *,
    apply_outcome: Callable[..., Any],
    inspect_batch: Callable[
        [Store, str | None, list[dict[str, Any]]], dict[int, str]
    ] = hub_inspect_batch,
    soft_timeout_seconds: float = RUNNER_SOFT_TIMEOUT_SECONDS,
) -> list[int]:
    """Central soft-timeout check (one hub call for all): find runs whose latest
    stdout/stderr output is older than the soft timeout (filter A — a run still
    streaming output is working and is skipped), ask the hub whether each is
    stuck, and flag the condemned ones for their owning runner to kill (the
    runner owns the process, so no reused/foreign pid is ever signalled).
    Returns the flagged run ids."""
    now = now if now is not None else time.monotonic()
    now_dt = datetime.now(timezone.utc)
    candidates: list[dict[str, Any]] = []
    live_ids: set[int] = set()
    for run in store.list_running_task_runs():
        rid = int(run["id"])
        live_ids.add(rid)
        if run.get("cancel_requested"):
            # Already condemned by a prior sweep (kill requested + step blocked).
            # Don't burn another hub inspection re-judging it.
            continue
        prev = HUB_SWEEP_STATE.get(rid)
        try:
            started_at = datetime.fromisoformat(run["started_at"])
        except (TypeError, ValueError):
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (now_dt - started_at).total_seconds()
        last_output_at = run_last_output_at(run.get("log_dir"), started_at)
        silent_for = (now_dt - last_output_at).total_seconds()
        HUB_SWEEP_STATE[rid] = {
            "inspected": (prev or {}).get("inspected", 0.0),
            "last_output": last_output_at.timestamp(),
        }
        # filter A: a run still producing output (silent for less than the soft
        # interval) is working — skip it; only inspect genuinely silent runs.
        if silent_for < soft_timeout_seconds:
            continue
        # cadence: inspect a given run at most once per soft interval
        if now - HUB_SWEEP_STATE[rid]["inspected"] < soft_timeout_seconds:
            continue
        task = store.get_task(int(run["task_id"]))
        candidates.append({
            "run_id": rid, "task_id": run["task_id"],
            "title": task.get("title") if task else "",
            "step": run.get("workflow_step") or (task.get("workflow_step") if task else ""),
            "assignee": run.get("worker"), "elapsed": elapsed,
            "silent_for": silent_for,
            "output": read_run_output_tail(run.get("log_dir")),
        })
    for gone in [k for k in HUB_SWEEP_STATE if k not in live_ids]:
        HUB_SWEEP_STATE.pop(gone, None)
    if not candidates:
        return []
    decisions = inspect_batch(store, project_root, candidates)
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    flagged: list[int] = []
    for c in candidates:
        HUB_SWEEP_STATE[c["run_id"]]["inspected"] = now
        if decisions.get(c["run_id"]) != "kill":
            continue
        reason = "hub inspection: stuck/errored"
        # Flag the run so the runner that owns the process kills the actual OS
        # process (only the owner signals it — never a reused/foreign pid).
        if store.request_run_kill(c["run_id"], reason):
            flagged.append(c["run_id"])
        # Immediately drive the workflow step to blocked instead of waiting for
        # the runner to report back: a runner can itself be wedged (e.g. stuck
        # reading a pipe an escaped child holds open), which would otherwise
        # leave the task hung in_progress forever. Blocking is idempotent — a
        # late runner report finds the step inactive and no-ops.
        step = steps.get(c["step"])
        if step is None:
            continue
        wf_task_id = workflow_task_id_for_run(store, int(c["task_id"]))
        try:
            apply_outcome(
                store, project_root, wf_task_id, step, c["assignee"],
                "blocked", reason, status="failed",
            )
        except Exception:
            # One task's block failure must never abort the whole sweep.
            pass
    return flagged


def workflow_task_id_for_run(store: Store, run_task_id: int) -> int:
    """The workflow task the engine advances for a run. Goal step runs are
    recorded on the step card, whose parent is the business subtask the engine
    actually routes; non-card runs are already on their workflow task."""
    task = store.get_task(run_task_id)
    if (
        task
        and task.get("source_message_id") is None
        and task.get("parent_task_id")
        and task.get("workflow_step")
    ):
        return int(task["parent_task_id"])
    return run_task_id
