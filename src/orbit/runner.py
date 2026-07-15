"""One-shot worker execution, run jobs, and scheduler loops."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import traceback
from typing import Any, Callable

from .hub_inspection import (
    HUB_INSPECT_TIMEOUT_SECONDS, RUNNER_SOFT_TIMEOUT_SECONDS,
    hub_command as _inspect_hub_command, hub_inspect_batch as _inspect_hub_batch,
    hub_inspect_sweep as _inspect_hub_sweep,
)
from .process_control import (
    detached_process_kwargs as _detached_process_kwargs,
    kill_process_group as _kill_process_group,
)
from .run_logs import (
    append_run_event as _append_run_event, stream_process_output as _stream_process_output,
    task_run_dir as _task_run_dir, write_process_stdin as _write_process_stdin,
    write_run_file as _write_run_file,
)
from .runner_prompts import (
    build_step_prompt as _build_runner_step_prompt, step_can_rework as _step_can_rework,
    step_command as _step_command,
)
from .runner_protocol import (
    normalize_agent_output as _normalize_agent_output, parse_run_tokens as _parse_run_tokens,
    parse_runner_port as _parse_runner_port, parse_runner_verdict as _parse_runner_verdict,
    tail as _tail,
)
from .settings import read_settings
from .store import DEFAULT_LEASE_SECONDS, InvalidInputError, Store, UnknownAgentError
from .token_usage import LOCAL_STORE_TOKEN_READERS as _LOCAL_STORE_TOKEN_READERS
from .verification import run_step_verify as _run_step_verify
from .workflow_config import _project_root, read_workflow_config
from .workflow_engine import (
    _WORKFLOW_ENGINE_LOCK, _complete_goal_intake_locked, _is_root_goal_decompose_step,
    _materializes_step_cards, _parse_goal_subtasks, advance_workflow_task,
    apply_foreach_item_outcome, recover_foreach_item_groups,
)
from .workflow_graph import workflow_graph as _workflow_graph
from .worktrees import ensure_task_worktree as _ensure_task_worktree

# --- Auto-runner -------------------------------------------------------------
WORKFLOW_TIMEOUT_POLL_SECONDS = 60
SCHEDULER_POLL_SECONDS = 1.0
SCHEDULER_RUNNER_NAME = f"workflow-scheduler-{os.getpid()}"
SCHEDULER_APPLYING_LEASE_SECONDS = 60

# Dispatched steps whose assignee has a runner command spawn a one-shot CLI
# process instead of waiting for a live session to poll the inbox. The command
# receives the prompt on stdin, works in the project root, and its stdout tail
# is submitted via the engine as the step result.
# runner_command on the member overrides the per-tool default.
RUNNER_DEFAULT_TIMEOUT_SECONDS = 1800
# The hard cap force-kills a runner regardless of Hub inspection so nothing
# runs forever.
RUNNER_HARD_TIMEOUT_SECONDS = 1800
# How often a runner re-checks the hard cap and the hub's kill flag while waiting.
RUNNER_CANCEL_POLL_SECONDS = 10
# After a process exits/is killed, how long to wait for the stdout/stderr readers
# to drain before force-closing the pipes — bounds the wedge when an escaped child
# keeps a pipe open so EOF never arrives.
RUNNER_STREAM_DRAIN_SECONDS = 5


def _build_step_prompt(
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    upstream_result: str,
    can_rework: bool = False,
    isolated: bool = False,
) -> str:
    """Compatibility wrapper around the standalone prompt builder."""
    return _build_runner_step_prompt(
        project_root,
        task,
        step,
        upstream_result,
        can_rework,
        isolated,
        is_root_goal_decompose_step=_is_root_goal_decompose_step,
    )


_TASK_RUNNING_TERMINAL = ("closed", "accepted")


def _mark_task_running(store: Store, task_id: int | None) -> None:
    """When a runner starts, flip the task — and every ancestor goal/parent —
    to in_progress so the board reflects that work is actually underway. A run
    starting on any subtask surfaces its parent goal as in_progress too.
    Terminal tasks (closed/accepted) are left untouched."""
    seen: set[int] = set()
    current = task_id
    while current and current not in seen:
        seen.add(current)
        task = store.get_task(current)
        if not task:
            break
        status = task.get("task_status")
        if (
            status not in _TASK_RUNNING_TERMINAL and status != "in_progress"
        ):
            store.set_task_workflow_state(current, task_status="in_progress")
        current = task.get("parent_task_id")


def _hub_command(project_root: str | None) -> str:
    return _inspect_hub_command(project_root)


def _hub_inspect_batch(
    store: Store, project_root: str | None, candidates: list[dict[str, Any]]
) -> dict[int, str]:
    return _inspect_hub_batch(
        store,
        project_root,
        candidates,
        command_loader=_hub_command,
        timeout_seconds=HUB_INSPECT_TIMEOUT_SECONDS,
    )


def hub_inspect_sweep(
    store: Store, project_root: str | None, now: float | None = None
) -> list[int]:
    return _inspect_hub_sweep(
        store,
        project_root,
        now,
        apply_outcome=apply_run_outcome,
        inspect_batch=_hub_inspect_batch,
        soft_timeout_seconds=RUNNER_SOFT_TIMEOUT_SECONDS,
    )


def _task_blocked_reason(
    store: Store,
    task: dict[str, Any],
    transitions_by_task: dict[int, list[dict[str, Any]]] | None = None,
) -> str | None:
    """Why a blocked task is blocked: the note of its most recent 'blocked'
    transition, for the detail view. Step cards carry no transitions of their
    own — the block is recorded on the parent workflow task — so fall back to
    the parent. Returns None for tasks that are not blocked."""
    if task.get("task_status") != "blocked":
        return None

    def _latest(tid: int) -> str | None:
        transitions = (
            transitions_by_task.get(tid, [])
            if transitions_by_task is not None
            else store.list_task_transitions(tid)
        )
        for t in reversed(transitions):
            if t["outcome"] == "blocked" and (t.get("note") or "").strip():
                return t["note"]
        return None

    reason = _latest(int(task["id"]))
    if (
        reason is None
        and task.get("source_message_id") is None
        and task.get("parent_task_id")
    ):
        reason = _latest(int(task["parent_task_id"]))
    return reason


# --- per-task git worktree isolation ---------------------------------------
# Concurrent implementers of different tasks must not share one working tree
# (git checkout is global to a tree). Each isolated step runs in a per-task
# worktree on branch orbit/task-<id>; a single-assignee `integrate` step
# merges that branch back into the main tree, serialized by the hub.

# --- machine verification gate ---------------------------------------------
# The agent self-reports its outcome; a step's `verify` command lets the engine
# objectively check the work (tests/build) and override an over-optimistic
# `done`. Runs in the step's working tree so it sees exactly what the agent
# produced (the per-task worktree for isolated steps).

def run_step_worker(
    store: Store,
    project_root: str | None,
    task_id: int,
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str = "",
    timeout_seconds: float | None = None,
    advance: bool = True,
    *,
    build_prompt: Callable[..., str] | None = None,
    stream_drain_seconds: float | None = None,
    item_scope_id: int | None = None,
) -> dict[str, Any]:
    """Execute one dispatched step via the member's CLI and record the run.

    Exit 0 -> done (stdout tail as result); nonzero/timeout/missing command ->
    blocked. With advance=True the outcome is applied through the engine inline
    (legacy path); with advance=False the run is only executed and recorded and
    the parsed outcome/result is returned for the scheduler to apply."""
    build_prompt = build_prompt or _build_step_prompt
    stream_drain_seconds = (
        RUNNER_STREAM_DRAIN_SECONDS
        if stream_drain_seconds is None
        else float(stream_drain_seconds)
    )
    assignee = member["agent_name"]
    task = store.get_task(task_id)
    if not task:
        return {"error": f"unknown task: {task_id}"}
    # For goal tasks the run is recorded on the step's card, so each card's
    # Runs panel shows its own execution history instead of everything piling
    # up on the goal.
    run_task_id = task_id
    if _materializes_step_cards(task):
        card = store.find_open_step_card(task_id, step["id"])
        if card:
            run_task_id = card["id"]
    command = member.get("runner_command", "") or _step_command(step, project_root)
    run = store.create_task_run(
        run_task_id, worker=assignee, command=command, workflow_step=step["id"]
    )
    if run:
        log_dir = _task_run_dir(project_root, run_task_id, int(run["attempt"]))
        run = store.update_task_run_log_dir(run["id"], str(log_dir)) or run
        try:
            _append_run_event(
                run,
                {
                    "type": "run_created",
                    "workflow_task_id": task_id,
                    "workflow_step": step["id"],
                    "item_scope_id": item_scope_id,
                    "command": command,
                },
            )
        except (InvalidInputError, OSError):
            pass
    # A run has started: mark the running card/task (and its ancestor subtask
    # and goal, reached via parent_task_id) in_progress. Use run_task_id so the
    # step card shown on the board — not just the underlying task — flips too.
    _mark_task_running(store, run_task_id)
    # The runner enforces only the hard cap; the soft-timeout hub inspection is
    # done centrally by hub_inspect_sweep. An explicit timeout_seconds (tests)
    # overrides the hard cap.
    hard_seconds = (
        RUNNER_HARD_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    )

    outcome, result, status, exit_code = "blocked", "", "failed", None
    stdout, stderr = "", ""
    # Normalized (structured-output-decoded) text + native token count; recomputed
    # from the real output below, defaulted here for the no-output/exception paths.
    output_text, native_tokens = "", None
    # Wall-clock window of the actual subprocess, used to correlate an antigravity
    # run to its conversation db (re-stamped right before Popen).
    run_started_ms = int(time.time() * 1000)
    # The goal-intake (Decompose) step emits one JSON object the engine parses
    # into subtasks. Its result must stay whole — see the exit-0 branch below.
    goal_intake = _is_root_goal_decompose_step(project_root, task, step)
    # Defaults so the post-run verify gate can reference these even on the
    # no-command path (where they are never assigned in the else branch).
    exec_dir = _project_root(project_root)
    isolated = False
    can_rework = False
    declared_ports = set(step.get("ports") or [])
    def _reserved_outcome(port: str) -> str:
        return port if port in declared_ports else "blocked"
    if not command:
        outcome = _reserved_outcome("error")
        result = (
            f"no runner command for step {step['id']} (agent {assignee}); select "
            "an installed agent or set the step's command"
        )
    else:
        _cfg = read_workflow_config(project_root)
        can_rework = _step_can_rework(_cfg, _workflow_graph(_cfg), step["id"])
        # Isolated steps run in a per-task git worktree so concurrent
        # implementers of different tasks never share a working tree. Falls back
        # to project_root (no isolation) on non-git projects.
        if step.get("isolate"):
            wt = _ensure_task_worktree(project_root, task_id)
            if wt is not None:
                exec_dir = wt
                isolated = True
        prompt = build_prompt(
            project_root, task, step, upstream_result, can_rework,
            isolated=isolated,
        )
        if run:
            # Persist the exact invocation (command + the prompt piped on stdin)
            # so it's inspectable in the run's "prompt" tab, even if the runner
            # is later killed.
            try:
                _write_run_file(
                    run, "prompt",
                    f"$ {command}\n\n--- prompt (piped on stdin) ---\n{prompt}\n",
                )
            except (InvalidInputError, OSError):
                pass
            try:
                _append_run_event(
                    run,
                    {
                        "type": "runner_started",
                        "workflow_task_id": task_id,
                        "workflow_step": step["id"],
                        "timeout_seconds": timeout_seconds,
                    },
                )
            except (InvalidInputError, OSError):
                pass
        run_started_ms = int(time.time() * 1000)
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(exec_dir),
                # own process group, so force-end can kill the whole CLI tree
                **_detached_process_kwargs(),
            )
            if run:
                try:
                    store.set_task_run_pid(run["id"], proc.pid)
                except (InvalidInputError, OSError):
                    pass
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            stdout_thread = threading.Thread(
                target=_stream_process_output,
                args=(run, proc, "stdout", stdout_chunks),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_stream_process_output,
                args=(run, proc, "stderr", stderr_chunks),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            stdin_errors: list[str] = []
            stdin_thread = threading.Thread(
                target=_write_process_stdin,
                args=(proc, prompt.encode("utf-8"), stdin_errors),
                daemon=True,
            )
            stdin_thread.start()
            # Wait in short chunks: enforce the hard cap, and honour a kill flag
            # set by the central hub sweep. Only this runner — which owns the
            # process — ever kills it, so a reused/foreign pid is never touched.
            start_wait = time.monotonic()
            while True:
                elapsed = time.monotonic() - start_wait
                remaining = hard_seconds - elapsed
                kill_reason = None
                if remaining <= 0:
                    kill_reason = f"runner hard-timed out after {int(hard_seconds)}s"
                    outcome = _reserved_outcome("timeout")
                    status = "timeout"
                elif run and store.run_cancel_requested(run["id"]):
                    kill_reason = f"hub inspection killed the step after {int(elapsed)}s (stuck/errored)"
                    outcome = _reserved_outcome("cancelled")
                    status = "cancelled"
                if kill_reason is not None:
                    _kill_process_group(proc)
                    result = kill_reason
                    if run:
                        try:
                            _append_run_event(run, {
                                "type": "runner_timeout", "workflow_task_id": task_id,
                                "workflow_step": step["id"], "elapsed_seconds": int(elapsed),
                                "reason": kill_reason,
                            })
                        except (InvalidInputError, OSError):
                            pass
                    proc.wait()
                    break
                try:
                    exit_code = proc.wait(timeout=min(RUNNER_CANCEL_POLL_SECONDS, remaining))
                    break  # finished on its own
                except subprocess.TimeoutExpired:
                    continue
            stdin_thread.join(timeout=1)
            # Drain the readers, but never block forever: a child that escaped the
            # process-group kill can inherit these pipes and hold them open, so EOF
            # never arrives. Give the readers a bounded window, then close our read
            # ends to unblock os.read so the join can't wedge the runner thread.
            stdout_thread.join(timeout=stream_drain_seconds)
            stderr_thread.join(timeout=stream_drain_seconds)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                for pipe in (proc.stdout, proc.stderr):
                    try:
                        if pipe is not None:
                            pipe.close()
                    except OSError:
                        pass
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
            # Decode a structured-output agent (e.g. claude --output-format
            # stream-json) to plain text + native tokens; text agents pass through.
            # Result/verdict parse the decoded text, so a JSON envelope is
            # transparent; the raw stdout is still what's written to stdout.log.
            output_text, native_tokens = _normalize_agent_output(stdout, stderr)
            if status in {"timeout", "cancelled"}:
                pass
            elif exit_code == 0:
                # Decompose output is a single JSON object the engine parses into
                # subtasks — keep it whole (tail-truncating would slice off the
                # opening `{"tasks":[` and break the parse). Every other step's
                # result is a human summary, so the tail cap is fine there.
                result = (
                    (output_text.strip() if goal_intake else _tail(output_text, 4000))
                    or "runner finished with no output"
                )
                # A clean exit defaults to done, but the runner can override via
                # a `WORKFLOW_OUTCOME:` line: any step may self-report `blocked`
                # (it ran but the work failed / is stuck); a rework-capable step
                # (e.g. review) may send the task back with `rework`.
                verdict = _parse_runner_verdict(output_text)
                selected_port = _parse_runner_port(output_text)
                if verdict == "blocked":
                    outcome, status = "blocked", "failed"
                elif verdict == "rework" and can_rework:
                    outcome, status = "rework", "succeeded"
                elif selected_port:
                    declared_ports = set(step.get("ports") or [])
                    if selected_port == "success" and selected_port in declared_ports:
                        outcome, status = "done", "succeeded"
                    elif selected_port == "rework" and can_rework:
                        outcome, status = "rework", "succeeded"
                    elif selected_port in {"blocked", "error", "timeout", "cancelled"}:
                        outcome, status = selected_port, "failed"
                    elif selected_port in declared_ports:
                        outcome, status = selected_port, "succeeded"
                    else:
                        outcome, status = "blocked", "failed"
                        result = f"runner selected undeclared WORKFLOW_PORT: {selected_port}"
                elif not output_text.strip():
                    # Clean exit but zero output: the runner ignored the
                    # "print a summary + WORKFLOW_OUTCOME" contract — a silent CLI
                    # failure (e.g. an agent that errored out) lands here. Don't
                    # advance on nothing; block so the hub/rework path handles it
                    # instead of looping on an empty "fix".
                    outcome, status = "blocked", "failed"
                    result = "runner produced no output (silent failure); treating as blocked"
                else:
                    outcome, status = "done", "succeeded"
            elif stdin_errors:
                outcome = _reserved_outcome("error")
                result = f"runner stdin failed: {stdin_errors[-1]}"
            else:
                outcome = _reserved_outcome("error")
                result = f"runner exited {exit_code}: {_tail(stderr or stdout, 2000)}"
        except OSError as exc:
            outcome = _reserved_outcome("error")
            result = f"runner failed to start: {exc}"
            if run:
                try:
                    _append_run_event(
                        run,
                        {
                            "type": "runner_failed_to_start",
                            "workflow_task_id": task_id,
                            "workflow_step": step["id"],
                            "error": str(exc),
                        },
                    )
                except (InvalidInputError, OSError):
                    pass
    success_outcome = outcome == "done" or outcome in set(step.get("ports") or []) - {
        "success", "rework", "blocked", "error", "timeout", "cancelled"
    }
    if success_outcome and goal_intake:
        try:
            _parse_goal_subtasks(result)
        except InvalidInputError as exc:
            outcome = "blocked"
            status = "failed"
            result = str(exc)
    # Machine verification gate: the agent self-reports its outcome and can
    # declare `done` without the work actually passing. If the step defines a
    # `verify` command, run it ourselves in the same working tree (the per-task
    # worktree for isolated steps) — a failing exit code the agent can't fake
    # overrides `done`, sending a rework-capable step back instead of advancing.
    verify_cmd = str(step.get("verify") or "").strip()
    if success_outcome and not goal_intake and verify_cmd:
        v_code, v_out = _run_step_verify(verify_cmd, exec_dir)
        if run:
            try:
                _write_run_file(
                    run, "verify", f"$ {verify_cmd}  (exit {v_code})\n\n{v_out}"
                )
                _append_run_event(
                    run,
                    {
                        "type": "verify",
                        "workflow_task_id": task_id,
                        "workflow_step": step["id"],
                        "command": verify_cmd,
                        "exit_code": v_code,
                    },
                )
            except (InvalidInputError, OSError):
                pass
        if v_code != 0:
            outcome = "rework" if can_rework else "blocked"
            status = "succeeded" if outcome == "rework" else "failed"
            result = (
                f"机器验证失败：`{verify_cmd}` 退出码 {v_code}（引擎判定，非 agent 自报）。"
                "修复后重试。\n"
                f"--- verify 输出（末尾） ---\n{_tail(v_out, 3000)}\n\n"
                f"--- agent 自报产出 ---\n{result}"
            )
    success_outcome = outcome == "done" or outcome in set(step.get("ports") or []) - {
        "success", "rework", "blocked", "error", "timeout", "cancelled"
    }
    # Prefer the count decoded from structured output (accurate); the plain-text
    # path already resolved to the regex-based count inside _normalize_agent_output.
    tokens = native_tokens if native_tokens is not None else _parse_run_tokens(stdout, stderr)
    # Some CLIs (antigravity, hermes, opencode) print no usage but persist an
    # accurate count to a local store; recover it by correlating this run's
    # worktree + wall-clock window. Best-effort — a failure leaves whatever the
    # self-report sentinel produced.
    if tokens is None and command:
        for matcher, reader in _LOCAL_STORE_TOKEN_READERS:
            if not matcher.search(command):
                continue
            try:
                tokens = reader(exec_dir, run_started_ms, int(time.time() * 1000))
            except Exception:
                tokens = None
            break
    if run:
        try:
            # Store the decoded text (a structured-output agent's stdout is a JSON
            # envelope; output_text == stdout for plain-text agents), so the stdout
            # panel shows the readable reply, not raw JSON. Its usage is preserved
            # in the token count. stderr is written verbatim.
            _write_run_file(run, "stdout", output_text)
            _write_run_file(run, "stderr", stderr)
            if success_outcome:
                _write_run_file(run, "result", result)
            _append_run_event(
                run,
                {
                    "type": "runner_finished",
                    "workflow_task_id": task_id,
                    "workflow_step": step["id"],
                    "item_scope_id": item_scope_id,
                    "status": status,
                    "outcome": outcome,
                    "exit_code": exit_code,
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr.encode("utf-8")),
                    "tokens": tokens,
                },
            )
            store.finish_task_run(run["id"], status, exit_code, tokens)
        except (InvalidInputError, OSError):
            pass
    if not advance:
        # Decoupled path: the runner recorded the run and now only reports the
        # outcome; the scheduler (single advance owner) applies it later.
        return {
            "task_id": task_id,
            "step": step["id"],
            "outcome": outcome,
            "result": result,
            "runner_status": status,
            "tokens": tokens,
        }
    return apply_run_outcome(
        store, project_root, task_id, step, assignee, outcome, result, status
    )


def apply_run_outcome(
    store: Store,
    project_root: str | None,
    task_id: int,
    step: dict[str, Any],
    assignee: str,
    outcome: str,
    result: str,
    status: str = "succeeded",
) -> dict[str, Any]:
    """Engine-side reaction to a finished run: for a root goal's intake split
    the goal into subtasks; otherwise advance the workflow (dispatch/rework/
    accept). This is the single point that mutates workflow state — the runner
    process no longer calls it, so advances stay serialized in the scheduler."""
    task = store.get_task(task_id)
    if not task:
        return {"task_id": task_id, "step": step["id"], "error": f"unknown task: {task_id}"}
    # The result is recorded by whichever handler accepts it below
    # (_complete_goal_intake_locked / _advance_workflow_task_locked); recording
    # here too double-writes, and would land a stale runner's output on the
    # current step when the advance rejects it (step reassigned meanwhile).
    goal_intake = _is_root_goal_decompose_step(project_root, task, step)
    if outcome == "done" and goal_intake:
        try:
            with _WORKFLOW_ENGINE_LOCK:
                return _complete_goal_intake_locked(
                    store, project_root, task, step, assignee, result
                )
        except (InvalidInputError, UnknownAgentError) as exc:
            return {"task_id": task_id, "step": step["id"], "error": str(exc)}
    try:
        report = advance_workflow_task(
            store, project_root, assignee, task_id, step["id"], outcome, result
        )
    except (InvalidInputError, UnknownAgentError) as exc:
        # e.g. the step was reassigned while the runner worked
        return {"task_id": task_id, "step": step["id"], "error": str(exc)}
    return {**report, "runner_status": status, "runner_result": result}


def _spawn_step_worker(
    store: Store,
    project_root: str | None,
    task_id: int,
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str,
) -> None:
    def _worker() -> None:
        try:
            run_step_worker(
                store, project_root, task_id, step, member, upstream_result
            )
        except Exception:
            traceback.print_exc()

    threading.Thread(
        target=_worker, name=f"step-runner-{task_id}-{step['id']}", daemon=True
    ).start()


def run_queued_job(
    store: Store,
    project_root: str | None,
    runner_name: str,
    agents: list[str] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    timeout_seconds: float | None = None,
    steps: list[str] | None = None,
    apply_inline: bool = False,
    renew_lease: bool = True,
) -> dict[str, Any] | None:
    """Runner-server entry point: claim one queued runner job and execute it.

    The UI/API server and scheduler only create run_jobs. A separate runner
    process calls this function, owns the subprocess lifetime, and records the
    result through the same run_step_worker path used by the legacy in-process
    runner. `agents`/`steps` narrow which jobs this runner will claim.
    """
    # Global concurrency cap: don't claim a new job while the configured number
    # of steps are already executing. A small race (two workers both pass the
    # check) is bounded by the worker count and self-corrects on the next poll.
    if store.count_running_run_jobs() >= read_settings(project_root)["max_concurrent_tasks"]:
        return None
    job = store.claim_next_run_job(
        runner_name=runner_name,
        agents=agents,
        lease_seconds=lease_seconds,
        steps=steps,
    )
    if not job:
        return None
    try:
        task = store.get_task(int(job["task_id"]))
        if not task:
            store.finish_run_job(
                job["id"], "failed", "task no longer exists",
                runner_name=runner_name, current_status="running",
            )
            return {"job_id": job["id"], "status": "failed", "error": "task missing"}
        cfg = read_workflow_config(project_root)
        step_map = {s["id"]: s for s in cfg["steps"]}
        step = step_map.get(job["step"])
        if not step:
            store.finish_run_job(
                job["id"], "failed", f"unknown step {job['step']}",
                runner_name=runner_name, current_status="running",
            )
            return {"job_id": job["id"], "status": "failed", "error": "step missing"}
        member = {
            "agent_name": job["assignee"],
            "runner_command": job["command"],
        }
        # Heartbeat: a long step can outrun the lease; renew it periodically so
        # another runner does not reclaim this job mid-execution. Renew well
        # before expiry (a third of the lease).
        stop_heartbeat = threading.Event()

        def _heartbeat() -> None:
            interval = max(5.0, lease_seconds / 3)
            while not stop_heartbeat.wait(interval):
                try:
                    store.renew_run_job(job["id"], runner_name, lease_seconds)
                except Exception:
                    pass

        heartbeat = None
        if renew_lease:
            heartbeat = threading.Thread(
                target=_heartbeat, name=f"job-heartbeat-{job['id']}", daemon=True
            )
            heartbeat.start()
        try:
            report = run_step_worker(
                store,
                project_root,
                int(job["task_id"]),
                step,
                member,
                job.get("upstream_result") or "",
                timeout_seconds=timeout_seconds,
                advance=False,  # runner only executes; the scheduler advances
                item_scope_id=job.get("item_scope_id"),
            )
        finally:
            stop_heartbeat.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
        # Hand the parsed outcome/result to the scheduler via the job row. The
        # runner does not advance the workflow itself.
        outcome = report.get("outcome") or "blocked"
        if apply_inline:
            applied = (
                apply_foreach_item_outcome(
                    store,
                    project_root,
                    int(job["item_scope_id"]),
                    job["assignee"],
                    outcome,
                    report.get("result") or "",
                )
                if job.get("item_scope_id") is not None
                else apply_run_outcome(
                    store,
                    project_root,
                    int(job["task_id"]),
                    step,
                    job["assignee"],
                    outcome,
                    report.get("result") or "",
                )
            )
            finished = store.finish_run_job(
                job["id"],
                "done",
                note=str(applied.get("error") or report.get("error") or ""),
                outcome=outcome,
                result=report.get("result") or "",
                runner_name=runner_name,
                current_status="running",
            )
            return {
                "job_id": job["id"],
                "status": "done" if finished else "lost_lease",
                "outcome": outcome,
                "report": applied,
            }
        finished = store.finish_run_job(
            job["id"], "finished",
            note=str(report.get("error") or ""),
            outcome=outcome,
            result=report.get("result") or "",
            runner_name=runner_name,
            current_status="running",
        )
        if finished is None:
            return {
                "job_id": job["id"],
                "status": "lost_lease",
                "outcome": outcome,
            }
        return {"job_id": job["id"], "status": "finished", "outcome": outcome}
    except Exception as exc:
        store.finish_run_job(
            job["id"], "failed", repr(exc),
            runner_name=runner_name, current_status="running",
        )
        raise


def _configured_step_scope(
    project_root: str | None, step_ids: list[str] | None
) -> list[str] | None:
    """Validate a runner's --steps scope against configured workflow step ids."""
    wanted = {step_id.strip() for step_id in (step_ids or []) if step_id.strip()}
    if not wanted:
        return None
    cfg = read_workflow_config(project_root)
    return [step["id"] for step in cfg["steps"] if step["id"] in wanted]


def runner_loop(
    store: Store,
    project_root: str | None,
    runner_name: str,
    agents: list[str] | None = None,
    poll_seconds: float = 2.0,
    once: bool = False,
    steps: list[str] | None = None,
    max_concurrency: int = 5,
) -> None:
    """Poll and execute queued run_jobs until interrupted. `agents`/`steps`
    scope which jobs are claimed; max_concurrency runs that many jobs in
    parallel (each parallel worker leases under a distinct name)."""
    claim_steps = _configured_step_scope(project_root, steps)
    # A step filter that matches no step would silently claim nothing; treat an
    # empty resolved set as a hard stop rather than "all steps".
    if steps and not claim_steps:
        print(
            f"runner {runner_name}: no configured steps match {steps}; nothing to do",
            flush=True,
        )
        return

    def _worker(name: str) -> None:
        while True:
            result = run_queued_job(
                store,
                project_root,
                runner_name=name,
                agents=agents,
                steps=claim_steps,
            )
            if result:
                print(f"runner {name}: job #{result['job_id']} {result['status']}", flush=True)
            elif once:
                return
            else:
                time.sleep(max(0.1, float(poll_seconds)))

    workers = max(1, int(max_concurrency))
    if workers == 1:
        _worker(runner_name)
        return
    threads = [
        threading.Thread(
            target=_worker, args=(f"{runner_name}-{i}",), name=f"runner-worker-{i}", daemon=True
        )
        for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def scheduler_tick(
    store: Store, project_root: str | None
) -> list[dict[str, Any]]:
    """Single-owner scheduler: apply the outcome of every finished run job
    (advance the workflow, or split a goal on intake) and mark the job done.
    Runs in one thread in the UI/scheduler process, so all advances are
    serialized here instead of racing across runner processes."""
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    processed: list[dict[str, Any]] = [
        {"foreach_recovery": item}
        for item in recover_foreach_item_groups(store, project_root)
    ]
    # Bound total work per tick by iterations (not just successes) so a burst of
    # failing jobs can't process unboundedly in one pass.
    for _ in range(200):
        job = store.claim_finished_run_job(
            SCHEDULER_RUNNER_NAME, lease_seconds=SCHEDULER_APPLYING_LEASE_SECONDS
        )
        if not job:
            break
        step = steps.get(job["step"])
        if not step:
            store.finish_run_job(
                job["id"], "failed", f"unknown step {job['step']}",
                applied_by=SCHEDULER_RUNNER_NAME,
                current_status="applying",
            )
            continue
        try:
            report = (
                apply_foreach_item_outcome(
                    store,
                    project_root,
                    int(job["item_scope_id"]),
                    job["assignee"],
                    job.get("outcome") or "blocked",
                    job.get("result") or "",
                )
                if job.get("item_scope_id") is not None
                else apply_run_outcome(
                    store,
                    project_root,
                    int(job["task_id"]),
                    step,
                    job["assignee"],
                    job.get("outcome") or "blocked",
                    job.get("result") or "",
                )
            )
            store.finish_run_job(
                job["id"], "done", str(report.get("error") or ""),
                applied_by=SCHEDULER_RUNNER_NAME,
                current_status="applying",
            )
            processed.append({"job_id": job["id"], "task_id": job["task_id"], "report": report})
        except Exception as exc:
            store.finish_run_job(
                job["id"], "failed", repr(exc),
                applied_by=SCHEDULER_RUNNER_NAME,
                current_status="applying",
            )
    return processed


def scheduler_loop(
    store: Store,
    project_root: str | None,
    poll_seconds: float = SCHEDULER_POLL_SECONDS,
    once: bool = False,
) -> None:
    """Poll finished run jobs and advance them until interrupted."""
    while True:
        try:
            scheduler_tick(store, project_root)
        except Exception:
            traceback.print_exc()
        if once:
            return
        time.sleep(max(0.1, float(poll_seconds)))
