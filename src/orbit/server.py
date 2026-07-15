"""Starlette server hosting the orbit Web UI, HTTP API, and workflow engine."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import anyio

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__
from .agent_tools import (
    AGENT_RUNNER_COMMANDS as _AGENT_RUNNER_COMMANDS,
    AGENT_TOOL_CANDIDATES as _AGENT_TOOL_CANDIDATES,
    agent_slug as _agent_slug,
    command_for_agent as _command_for_agent,
    detect_agent_tools as _detect_agent_tools,
    detect_hermes_profiles as _detect_hermes_profiles,
)
from .http_api import (
    LOCAL_HOSTNAMES as _LOCAL_HOSTNAMES,
    cors_headers as _cors_headers,
    forbid_non_local as _forbid_non_local,
    is_loopback_peer as _is_loopback_peer,
    json_error as _json_error,
    json_response as _json,
    read_json as _read_json,
)
from .hub_inspection import (
    HUB_INSPECT_TIMEOUT_SECONDS,
    HUB_SWEEP_POLL_SECONDS,
    HUB_SWEEP_STATE as _HUB_SWEEP_STATE,
    RUNNER_SOFT_TIMEOUT_SECONDS,
    hub_command as _inspect_hub_command,
    hub_inspect_batch as _inspect_hub_batch,
    hub_inspect_sweep as _inspect_hub_sweep,
    workflow_task_id_for_run as _workflow_task_id_for_run,
)
from .project_index import list_projects
from .process_control import (
    IS_WINDOWS as _IS_WINDOWS,
    descendant_pids as _descendant_pids,
    detached_process_kwargs as _detached_process_kwargs,
    kill_process_group as _kill_process_group,
    snapshot_ppids as _snapshot_ppids,
    snapshot_ppids_libproc as _snapshot_ppids_libproc,
    snapshot_ppids_procfs as _snapshot_ppids_procfs,
    snapshot_ppids_ps as _snapshot_ppids_ps,
    snapshot_ppids_windows as _snapshot_ppids_windows,
    taskkill_tree as _taskkill_tree,
    terminate_pid_tree as _terminate_pid_tree,
)
from .recovery import (
    AUTO_RECOVERY_MAX_ATTEMPTS,
    RecoveryCallbacks,
    _PENDING_WORKFLOW_ACTION_STALE_SECONDS,
    _RUN_DEAD_STATUSES,
    auto_recover_step as _recovery_auto_recover_step,
    check_task_health as _recovery_check_task_health,
    check_workflow_step_timeouts as _recovery_check_step_timeouts,
    is_stale_timestamp as _is_stale_timestamp,
    latest_run_for_step as _recovery_latest_run_for_step,
    run_is_after as _run_is_after,
    task_has_running_run as _task_has_running_run,
)
from .run_logs import (
    append_run_event as _append_run_event,
    append_run_file as _append_run_file,
    read_run_output_tail as _read_run_output_tail,
    read_run_file as _read_run_file,
    run_last_output_at as _run_last_output_at,
    stream_process_output as _stream_process_output,
    task_run_dir as _task_run_dir,
    task_run_file as _task_run_file,
    task_runs_root as _task_runs_root,
    write_process_stdin as _write_process_stdin,
    write_run_file as _write_run_file,
)
from .runner_protocol import (
    normalize_agent_output as _normalize_agent_output,
    parse_claude_json_output as _parse_claude_json_output,
    parse_gemini_json_output as _parse_gemini_json_output,
    parse_run_tokens as _parse_run_tokens,
    parse_runner_port as _parse_runner_port,
    parse_runner_verdict as _parse_runner_verdict,
    parse_step_output_metadata as _parse_step_output_metadata,
    structured_upstream as _structured_upstream,
    tail as _tail,
)
from .runner_prompts import (
    build_step_prompt as _build_runner_step_prompt,
    step_agent_command as _step_agent_command,
    step_assignee as _step_assignee,
    step_can_rework as _step_can_rework,
    step_command as _step_command,
    step_round_robin_assignee as _step_round_robin_assignee,
    triage_config_snapshot as _triage_config_snapshot,
)
from .runner import (
    RUNNER_CANCEL_POLL_SECONDS,
    RUNNER_DEFAULT_TIMEOUT_SECONDS,
    RUNNER_HARD_TIMEOUT_SECONDS,
    RUNNER_STREAM_DRAIN_SECONDS,
    SCHEDULER_APPLYING_LEASE_SECONDS,
    SCHEDULER_POLL_SECONDS,
    SCHEDULER_RUNNER_NAME,
    WORKFLOW_TIMEOUT_POLL_SECONDS,
    _configured_step_scope,
    _mark_task_running,
    _spawn_step_worker,
    _task_blocked_reason,
    apply_run_outcome,
    run_queued_job,
    run_step_worker as _runner_run_step_worker,
    runner_loop,
    scheduler_loop,
    scheduler_tick,
)
from .settings import (
    MAX_CONCURRENT_MAX,
    MAX_CONCURRENT_MIN,
    MAX_REWORK_MAX,
    MAX_REWORK_MIN,
    clamp_int as _clamp_int,
    read_settings,
    settings_config_path as _settings_config_path,
    write_settings,
)
from .token_usage import (
    AGY_COMMAND_RE as _AGY_COMMAND_RE,
    HERMES_COMMAND_RE as _HERMES_COMMAND_RE,
    HERMES_TOKEN_COLUMNS as _HERMES_TOKEN_COLUMNS,
    LOCAL_STORE_CORRELATION_BUFFER_MS as _LOCAL_STORE_CORRELATION_BUFFER_MS,
    LOCAL_STORE_TOKEN_READERS as _LOCAL_STORE_TOKEN_READERS,
    OPENCODE_COMMAND_RE as _OPENCODE_COMMAND_RE,
    antigravity_conversation_usage as _antigravity_conversation_usage,
    antigravity_conversations_dir as _antigravity_conversations_dir,
    antigravity_gen_tokens as _antigravity_gen_tokens,
    antigravity_run_tokens as _antigravity_run_tokens,
    hermes_run_tokens as _hermes_run_tokens,
    hermes_state_db as _hermes_state_db,
    opencode_db as _opencode_db,
    opencode_message_tokens as _opencode_message_tokens,
    opencode_run_tokens as _opencode_run_tokens,
    path_match_keys as _path_match_keys,
    pb_iter_fields as _pb_iter_fields,
    pb_message_field as _pb_message_field,
    pb_read_varint as _pb_read_varint,
    pb_string_field as _pb_string_field,
    pb_timestamp_ms as _pb_timestamp_ms,
    pb_varint_field as _pb_varint_field,
    sqlite_ro_uri as _sqlite_ro_uri,
)
from .workflow_config import (
    DEFAULT_STEP_PROMPTS,
    ENGINE_STEP_CONTRACTS,
    _DEFAULT_STEP_MID_Y,
    _MULTI_AGENT_STEP_IDS,
    _WF_MIN_STEP_DX,
    _WF_NODE_WIDTH,
    _WF_ROW_TOLERANCE,
    _WORKFLOW_STATUS_LABELS,
    _normalize_agents,
    _normalize_step_agent_commands,
    _normalize_workflow_edges,
    _normalize_workflow_step,
    _project_root,
    _separate_overlapping_steps,
    _workflow_config_path,
    _workflow_graph_warnings as _config_workflow_graph_warnings,
    default_workflow_edges,
    default_workflow_statuses,
    default_workflow_steps,
    read_workflow_config,
    write_workflow_config,
)
from .workflow_graph import (
    _STEP_FINISHING_OUTCOMES,
    _WORKFLOW_STATUS_OVERRIDES,
    active_step_assignees as _active_step_assignees,
    active_steps as _active_steps,
    dispatched_since as _dispatched_since,
    forward_out as _forward_out,
    join_ready as _join_ready,
    last_dispatch_and_finish as _last_dispatch_and_finish,
    latest_inbound_completion_id as _latest_inbound_completion_id,
    main_workflow_reachable_steps as _main_workflow_reachable_steps,
    running_steps as _running_steps,
    workflow_derived_task_status as _workflow_derived_task_status,
    workflow_entry_steps as _workflow_entry_steps,
    workflow_execution_errors as _workflow_execution_errors,
    workflow_graph as _workflow_graph,
    workflow_terminal_steps as _workflow_terminal_steps,
)
from .workflow_engine import (
    HUB_NOTIFY_AGENT,
    MAX_REWORK_ROUNDS,
    WORKFLOW_ENGINE_AGENT,
    WORKFLOW_OUTCOMES,
    _WORKFLOW_ENGINE_LOCK,
    _active_workflow_task_ids,
    _advance_workflow_task_locked,
    _business_subtasks_for_goal,
    _coerce_token_budget,
    _complete_goal_intake_locked as _engine_complete_goal_intake_locked,
    _dispatch_business_subtask,
    _dispatch_step,
    _dispatch_targets,
    _enforce_goal_token_budget,
    _ensure_engine_agent,
    _extract_json_object,
    _finish_goal_workflow,
    _goal_decompose_upstream_result,
    _goal_status_for_step,
    _is_root_goal_decompose_step,
    _manual_status_rejection,
    _materializes_step_cards,
    _notify_hub,
    _parse_goal_subtasks,
    _parse_subtask_deps,
    _project_workflow_task_status,
    _recompute_parent_goal_status,
    _record_step_result,
    _reject_dependency_cycles,
    _release_ready_subtasks,
    _reimplement_workflow_task_locked,
    _resume_goal_after_budget_increase_locked,
    _root_goal_decompose_step_id,
    _root_goal_id,
    _settle_step_card,
    _start_goal_business_subtasks,
    _start_workflow_task_at_locked,
    _start_workflow_task_locked,
    _upsert_step_card,
    _validate_goal_auto_runners,
    _workflow_api_actor,
    active_goal_conflict_reason,
    advance_workflow_task,
    force_close_goal,
    goals_summary,
    reimplement_workflow_task,
    rerun_workflow_step,
    resume_goal_after_budget_increase,
    skip_workflow_step,
    start_workflow_task,
    workflow_locked_reason,
)
from .worktrees import (
    WORKTREE_LOCK_RETRIES,
    branch_exists as _branch_exists,
    commit_goal_design_artifacts as _commit_goal_design_artifacts,
    ensure_git_repo as _ensure_git_repo,
    ensure_state_dir_gitignored as _ensure_state_dir_gitignored,
    ensure_task_worktree as _ensure_task_worktree,
    git as _git,
    git_available as _git_available,
    is_git_repo as _is_git_repo,
    remove_task_worktree as _remove_task_worktree,
    sweep_task_worktrees as _sweep_task_worktrees,
    task_workflow_finished as _task_workflow_finished,
    task_worktree_dir as _task_worktree_dir,
    workflow_needs_git as _workflow_needs_git,
    worktree_base_ref as _worktree_base_ref,
    worktree_branch as _worktree_branch,
    worktree_registered as _worktree_registered,
)
from .verification import (
    GOAL_VERIFY_POLL_SECONDS,
    GOAL_VERIFY_STALE_SECONDS,
    VERIFY_HARD_TIMEOUT_SECONDS,
    detect_goal_verify as _detect_goal_verify,
    effective_goal_verify as _effective_goal_verify,
    goal_verify_sweep,
    iso_age_seconds as _iso_age_seconds,
    run_step_verify as _run_step_verify,
)
from .store import (
    DEFAULT_LEASE_SECONDS,
    InvalidInputError,
    Store,
    TASK_STATUSES,
    UnknownAgentError,
)

MAX_LEASE_SECONDS = 3600
_LOG = logging.getLogger(__name__)

# The HTTP API is a local-only control surface: agents act on what lands in
# their inboxes, so a forged request is a prompt-injection channel. Defense is
# layered: the peer socket IP must be loopback (the load-bearing check — it is
# not client-controllable, so it holds even when bound beyond loopback with
# --host 0.0.0.0), the Host header must be a loopback hostname (blocks DNS
# rebinding), and any browser Origin must be a loopback origin (blocks CSRF).
TASK_IMPORTANCE_SCORES = {"low": 0, "normal": 10, "high": 25, "critical": 40}
TASK_SIZE_SCORES = {"small": 0, "medium": 8, "large": 18}
TASK_RISK_SCORES = {"low": 0, "medium": 10, "high": 25}

# Store uses synchronous sqlite3; run every call in a worker thread so it
# never blocks the event loop (many concurrent long-polling clients).
_to_thread = anyio.to_thread.run_sync

_UI_HTML = (
    resources.files("orbit").joinpath("static/ui.html").read_text(encoding="utf-8")
)

# Vendored, self-contained: the dagre layout engine (bundles graphlib, exposes a
# global `dagre`). Served from our own origin so auto-layout works offline and
# the app keeps its "no CDN, no build step" contract. Loaded lazily so a missing
# vendor file only disables the Auto-layout button, never breaks the page.
try:
    _DAGRE_JS = (
        resources.files("orbit")
        .joinpath("static/vendor/dagre.min.js")
        .read_text(encoding="utf-8")
    )
except (FileNotFoundError, ModuleNotFoundError, OSError):
    _DAGRE_JS = ""


def _parse_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidInputError(f"{name} must be an integer, got {value!r}") from None


def _workflow_graph_warnings(
    steps: list[dict[str, Any]], edges: list[dict[str, str]]
) -> list[str]:
    """Compatibility wrapper preserving server-level Git detection patches."""
    return _config_workflow_graph_warnings(
        steps, edges, git_available=_git_available
    )


def _coerce_token_budget(value: Any) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return 0
    return budget if budget > 0 else 0


def _complete_goal_intake_locked(
    store: Store,
    project_root: str | None,
    goal: dict[str, Any],
    step: dict[str, Any],
    actor: str,
    result: str,
) -> dict[str, Any]:
    """Compatibility adapter preserving server-level intake monkeypatches."""
    return _engine_complete_goal_intake_locked(
        store,
        project_root,
        goal,
        step,
        actor,
        result,
        parse_goal_subtasks=_parse_goal_subtasks,
        start_goal_business_subtasks=_start_goal_business_subtasks,
    )


# Compatibility adapters keep long-standing server-level monkeypatch points
# working while the implementation lives in orbit.runner.
def _build_step_prompt(
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    upstream_result: str,
    can_rework: bool = False,
    isolated: bool = False,
) -> str:
    return _build_runner_step_prompt(
        project_root,
        task,
        step,
        upstream_result,
        can_rework=can_rework,
        isolated=isolated,
        is_root_goal_decompose_step=_is_root_goal_decompose_step,
    )


def run_step_worker(
    store: Store,
    project_root: str | None,
    task_id: int,
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str = "",
    timeout_seconds: float | None = None,
    advance: bool = True,
) -> dict[str, Any]:
    return _runner_run_step_worker(
        store,
        project_root,
        task_id,
        step,
        member,
        upstream_result,
        timeout_seconds=timeout_seconds,
        advance=advance,
        build_prompt=_build_step_prompt,
        stream_drain_seconds=RUNNER_STREAM_DRAIN_SECONDS,
    )


def _hub_command(project_root: str | None) -> list[str]:
    return _inspect_hub_command(project_root)


def _hub_inspect_batch(
    store: Store,
    project_root: str | None,
    items: list[dict[str, Any]],
) -> dict[int, str]:
    return _inspect_hub_batch(
        store,
        project_root,
        items,
        command_loader=_hub_command,
        timeout_seconds=HUB_INSPECT_TIMEOUT_SECONDS,
    )


def hub_inspect_sweep(store: Store, project_root: str | None) -> list[int]:
    return _inspect_hub_sweep(
        store,
        project_root,
        apply_outcome=apply_run_outcome,
        inspect_batch=_hub_inspect_batch,
        soft_timeout_seconds=RUNNER_SOFT_TIMEOUT_SECONDS,
    )

def _recovery_callbacks() -> RecoveryCallbacks:
    return RecoveryCallbacks(
        notify_hub=_notify_hub,
        materializes_step_cards=_materializes_step_cards,
        dispatch_step=_dispatch_step,
        step_agent_command=_step_agent_command,
        step_round_robin_assignee=_step_round_robin_assignee,
        root_goal_decompose_step_id=_root_goal_decompose_step_id,
        business_subtasks_for_goal=_business_subtasks_for_goal,
        finish_goal_workflow=_finish_goal_workflow,
        recompute_parent_goal_status=_recompute_parent_goal_status,
        workflow_engine_agent=WORKFLOW_ENGINE_AGENT,
    )


def check_workflow_step_timeouts(
    store: Store, project_root: str | None, now: datetime | None = None
) -> list[dict[str, Any]]:
    with _WORKFLOW_ENGINE_LOCK:
        return _check_workflow_step_timeouts_locked(store, project_root, now)


def _check_workflow_step_timeouts_locked(
    store: Store, project_root: str | None, now: datetime | None = None
) -> list[dict[str, Any]]:
    return _recovery_check_step_timeouts(
        store, project_root, now, callbacks=_recovery_callbacks()
    )


def _latest_run_for_step(
    store: Store, task: dict[str, Any], step_id: str
) -> dict[str, Any] | None:
    return _recovery_latest_run_for_step(
        store, task, step_id, callbacks=_recovery_callbacks()
    )


def _auto_recover_step_locked(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    assignee: str,
    upstream_result: str,
    reason: str,
) -> dict[str, Any]:
    return _recovery_auto_recover_step(
        store,
        project_root,
        task,
        step,
        assignee,
        upstream_result,
        reason,
        callbacks=_recovery_callbacks(),
    )


def check_task_health(
    store: Store, project_root: str | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    with _WORKFLOW_ENGINE_LOCK:
        return _check_task_health_locked(store, project_root, now)


def _check_task_health_locked(
    store: Store, project_root: str | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    return _recovery_check_task_health(
        store, project_root, now, callbacks=_recovery_callbacks()
    )


def workflow_task_state(
    store: Store, project_root: str | None, task_id: int
) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    transitions = store.list_task_transitions(task_id)
    cfg = read_workflow_config(project_root)
    active_steps = _active_steps(transitions)
    return {
        "task_id": task_id,
        "status": _workflow_derived_task_status(task, transitions, cfg),
        "active_steps": active_steps,
        "transitions": transitions,
    }


def detect_agent_tools() -> list[dict[str, Any]]:
    # Pass dependencies explicitly so existing callers/tests may replace the
    # server-level detector or shutil.which without reaching into the module.
    return _detect_agent_tools(
        which=shutil.which,
        profile_loader=detect_hermes_profiles,
    )


def detect_hermes_profiles(profile_root: Path | None = None) -> list[dict[str, str]]:
    return _detect_hermes_profiles(profile_root)




def create_server(
    host: str = "127.0.0.1",
    port: int = 8848,
    db_path: str | None = None,
    project: dict[str, Any] | None = None,
    run_worker: bool = True,
    worker_concurrency: int = 5,
) -> Starlette:
    """Compatibility entry point; HTTP composition lives in :mod:`orbit.app`."""
    from .app import create_server as create_app

    return create_app(
        host,
        port,
        db_path,
        project,
        run_worker,
        worker_concurrency,
        runtime=sys.modules[__name__],
    )
