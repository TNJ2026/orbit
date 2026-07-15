"""Starlette application composition for Orbit's local control plane."""

from __future__ import annotations

import atexit
import json
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .store import Store
from .local_runtime import LocalRuntime


_RUNTIME_NAMES = (
    "DEFAULT_LEASE_SECONDS", "GOAL_VERIFY_POLL_SECONDS", "HUB_NOTIFY_AGENT",
    "HUB_SWEEP_POLL_SECONDS", "InvalidInputError", "MAX_CONCURRENT_MAX",
    "MAX_CONCURRENT_MIN", "MAX_LEASE_SECONDS", "MAX_REWORK_MAX",
    "MAX_REWORK_MIN", "SCHEDULER_POLL_SECONDS", "TASK_STATUSES",
    "UnknownAgentError", "WORKFLOW_TIMEOUT_POLL_SECONDS", "_DAGRE_JS", "_LOG",
    "_UI_HTML", "__version__", "_append_run_file", "_coerce_token_budget",
    "_command_for_agent", "_cors_headers", "_forbid_non_local", "_json",
    "_json_error", "_manual_status_rejection", "_mark_task_running", "_parse_int",
    "_project_root", "_project_workflow_task_status", "_read_json", "_read_run_file",
    "_recompute_parent_goal_status", "_sweep_task_worktrees", "_task_blocked_reason",
    "_task_run_dir", "_to_thread", "_validate_goal_auto_runners",
    "_workflow_api_actor", "_write_run_file", "active_goal_conflict_reason",
    "advance_workflow_task", "check_task_health", "check_workflow_step_timeouts",
    "default_workflow_edges", "default_workflow_steps", "detect_agent_tools",
    "force_close_goal", "goal_verify_sweep", "goals_summary", "hub_inspect_sweep",
    "list_projects", "read_settings", "read_workflow_config",
    "reimplement_workflow_task", "rerun_workflow_step", "resume_goal_after_budget_increase",
    "runner_loop", "scheduler_tick", "skip_workflow_step", "start_workflow_task",
    "workflow_locked_reason", "workflow_task_state", "write_settings",
    "workflow_template_definition", "workflow_template_summaries",
    "write_workflow_config",
)


def _bind_runtime(runtime: Any) -> None:
    """Bind the compatibility facade used by route closures.

    Keeping the binding explicit preserves existing tests and integrations that
    patch ``orbit.server`` while the actual HTTP composition lives here.
    """
    namespace = globals()
    for name in _RUNTIME_NAMES:
        namespace[name] = getattr(runtime, name)

def create_server(
    host: str = "127.0.0.1",
    port: int = 8848,
    db_path: str | None = None,
    project: dict[str, Any] | None = None,
    run_worker: bool = True,
    worker_concurrency: int = 5,
    *,
    runtime: Any,
) -> Starlette:
    _bind_runtime(runtime)
    store = Store(db_path)
    background_error_lock = threading.Lock()
    background_errors: dict[str, dict[str, str | int]] = {}

    def _record_background_error(component: str, exc: Exception) -> None:
        # Daemon loops must survive an individual failed pass, but hiding the
        # exception turns a stalled workflow into an opaque failure. Keep a
        # compact, inspectable summary as well as the full server log.
        _LOG.exception("background %s pass failed", component)
        with background_error_lock:
            previous = background_errors.get(component, {})
            background_errors[component] = {
                "count": int(previous.get("count", 0)) + 1,
                "last_failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_error": f"{type(exc).__name__}: {exc}",
            }

    def _background_error_snapshot() -> dict[str, dict[str, str | int]]:
        with background_error_lock:
            return {component: dict(error) for component, error in background_errors.items()}

    current_project = project or {
        "id": "",
        "name": "",
        "project_root": "",
        "db_path": str(store.db_path),
        "server_url": f"http://{host}:{port}",
        "host": host,
        "port": port,
        "last_seen": "",
    }

    local_runtime = LocalRuntime(
        store,
        current_project.get("project_root"),
        run_worker=run_worker,
        worker_concurrency=worker_concurrency,
        scheduler_tick=scheduler_tick,
        maintenance=[
            ("workflow_timeouts", WORKFLOW_TIMEOUT_POLL_SECONDS, check_workflow_step_timeouts),
            ("task_health", WORKFLOW_TIMEOUT_POLL_SECONDS, check_task_health),
            ("worktree_sweep", WORKFLOW_TIMEOUT_POLL_SECONDS, _sweep_task_worktrees),
        ],
        hub_sweep=(HUB_SWEEP_POLL_SECONDS, hub_inspect_sweep),
        goal_verify=(GOAL_VERIFY_POLL_SECONDS, goal_verify_sweep),
        record_error=_record_background_error,
    )
    atexit.register(local_runtime.stop)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        local_runtime.start()
        try:
            yield
        finally:
            local_runtime.stop()

    # Plain Starlette app: collect route definitions as they are declared, then
    # build the app at the end. `route` is a thin decorator shim so the endpoint
    # bodies read the same as before the switch to plain Starlette.
    routes: list[Route] = []

    def route(path: str, methods: list[str]):
        def decorator(fn):
            routes.append(Route(path, fn, methods=methods))
            return fn
        return decorator

    async def _deliver(
        sender: str,
        to: str,
        content: str,
        reply_to: int | None,
        kind: str,
        title: str,
        task_status: str,
    ) -> dict:
        """Shared delivery path for the HTTP API."""
        await _to_thread(store.touch_agent, sender)
        try:
            ids = await _to_thread(
                store.send_message,
                sender,
                to,
                content,
                reply_to,
                kind,
                title,
                task_status,
            )
        except (UnknownAgentError, InvalidInputError) as exc:
            return {"delivered": 0, "message_ids": [], "error": str(exc)}
        if not ids:
            return {
                "delivered": 0,
                "message_ids": [],
                "note": "no recipients (broadcast with no other registered agents?)",
            }
        return {"delivered": len(ids), "message_ids": ids}

    def _engine_start(agent: str, task_id: int) -> dict:
        return start_workflow_task(
            store, current_project.get("project_root"), agent, task_id
        )

    def _engine_advance(
        agent: str, task_id: int, step: str, outcome: str, result: str
    ) -> dict:
        return advance_workflow_task(
            store, current_project.get("project_root"),
            agent, task_id, step, outcome, result,
        )

    def _engine_state(task_id: int) -> dict:
        return workflow_task_state(
            store, current_project.get("project_root"), task_id
        )

    def _engine_rerun(task_id: int, agent: str, step: str) -> dict:
        return rerun_workflow_step(
            store, current_project.get("project_root"), task_id, agent, step or None
        )

    def _engine_reimplement(task_id: int, agent: str) -> dict:
        return reimplement_workflow_task(
            store, current_project.get("project_root"), task_id, agent
        )

    def _engine_skip(task_id: int, step: str) -> dict:
        return skip_workflow_step(
            store, current_project.get("project_root"), task_id, step or None,
            actor=HUB_NOTIFY_AGENT,
        )

    def _engine_resume_goal_budget(goal_id: int, token_budget: Any) -> dict:
        return resume_goal_after_budget_increase(
            store, current_project.get("project_root"), goal_id, token_budget
        )

    def _engine_force_close(task_id: int) -> dict:
        return force_close_goal(
            store, current_project.get("project_root"), task_id
        )

    def _engine_health_check() -> dict:
        # The same watchdog the background loop runs, on demand: re-dispatch dead
        # runners, recover interrupted advances, and alert the hub on anything it
        # can't auto-recover. Also flags active steps past their timeout.
        root = current_project.get("project_root")
        recovered = check_task_health(store, root)
        timeouts = check_workflow_step_timeouts(store, root)
        return {
            "recovered": recovered,
            "timeouts": timeouts,
            "recovered_count": len(recovered),
            "timeout_count": len(timeouts),
        }

    @route("/", methods=["GET"])
    async def index(_: Request) -> RedirectResponse:
        return RedirectResponse("/ui")

    @route("/ui", methods=["GET"])
    async def ui(request: Request) -> HTMLResponse | JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return HTMLResponse(_UI_HTML)

    @route("/static/dagre.min.js", methods=["GET"])
    async def static_dagre(request: Request) -> Response:
        if forbidden := _forbid_non_local(request):
            return forbidden
        if not _DAGRE_JS:
            return Response("// dagre vendor bundle not installed", status_code=404)
        return Response(
            _DAGRE_JS,
            media_type="application/javascript",
            headers={"cache-control": "max-age=86400"},
        )

    @route("/api/{path:path}", methods=["OPTIONS"])
    async def api_options(request: Request) -> Response:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return Response(status_code=204, headers=_cors_headers(request))

    @route("/api/agents", methods=["GET"])
    async def api_list_agents(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents = await _to_thread(store.list_agents)
        return _json(request, {"agents": agents})

    @route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return _json(
            request,
            {
                "version": __version__,
                "db_path": str(store.db_path),
                "project": {**current_project, "db_path": str(store.db_path)},
                "background_errors": _background_error_snapshot(),
            },
        )

    @route("/api/projects", methods=["GET"])
    async def api_projects(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        projects = [{**current_project, "current": True, "online": True}]
        return _json(
            request,
            {
                "current_project_id": current_project.get("id"),
                "projects": projects,
            },
        )

    @route("/api/agent-tools", methods=["GET"])
    async def api_agent_tools(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents = await _to_thread(store.list_agents)
        registered = {agent["name"]: agent for agent in agents}
        tools = await _to_thread(detect_agent_tools)
        for tool in tools:
            agent = registered.get(tool["agent_name"])
            tool["registered"] = agent is not None
            tool["last_seen"] = agent["last_seen"] if agent else None
            tool["built_in_command"] = _command_for_agent(tool["agent_name"])
        return _json(request, {"tools": tools})

    @route("/api/workflow", methods=["GET"])
    async def api_get_workflow(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        try:
            workflow = await _to_thread(
                read_workflow_config, current_project.get("project_root")
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, workflow)

    @route("/api/workflow", methods=["POST"])
    async def api_save_workflow(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        locked = await _to_thread(workflow_locked_reason, store)
        if locked:
            return _json_error(locked, 409, request)
        try:
            workflow = await _to_thread(
                write_workflow_config,
                data.get("steps", []),
                current_project.get("project_root"),
                data.get("edges"),
                data.get("subflows"),
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"success": True, **workflow})

    @route("/api/workflow/reset", methods=["POST"])
    async def api_reset_workflow(request: Request) -> JSONResponse:
        # Overwrite the project's workflow with the packaged default flow, with no
        # Agents on any step (the default already carries none). The template in
        # code is untouched — this only rewrites this project's .orbit/workflow.json.
        if forbidden := _forbid_non_local(request):
            return forbidden
        locked = await _to_thread(workflow_locked_reason, store)
        if locked:
            return _json_error(locked, 409, request)
        data = await _read_json(request)
        template_id = str(data.get("template") or "software").strip().lower()
        try:
            template_steps, template_edges = workflow_template_definition(template_id)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        workflow = await _to_thread(
            write_workflow_config,
            template_steps,
            current_project.get("project_root"),
            template_edges,
        )
        return _json(request, {"success": True, "template": template_id, **workflow})

    @route("/api/settings", methods=["GET"])
    async def api_get_settings(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        settings = await _to_thread(read_settings, current_project.get("project_root"))
        return _json(request, {
            **settings,
            "max_rework_range": [MAX_REWORK_MIN, MAX_REWORK_MAX],
            "max_concurrent_range": [MAX_CONCURRENT_MIN, MAX_CONCURRENT_MAX],
        })

    @route("/api/settings", methods=["POST"])
    async def api_save_settings(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        settings = await _to_thread(
            write_settings,
            current_project.get("project_root"),
            data.get("max_rework_rounds"),
            data.get("max_concurrent_tasks"),
        )
        return _json(request, {"success": True, **settings})

    @route("/api/agents", methods=["POST"])
    async def api_register_agent(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        try:
            agents = await _to_thread(store.register_agent, name, description)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"registered": name, "agents": agents})

    @route("/api/messages", methods=["GET"])
    async def api_list_messages(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        params = request.query_params
        agent = params.get("agent") or None
        status = params.get("status", "all")
        kind = params.get("kind", "all")
        task_status = params.get("task_status", "all")
        try:
            limit = _parse_int(params.get("limit", "100"), "limit")
            messages = await _to_thread(
                store.list_messages, agent, status, kind, task_status, limit
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"messages": messages})

    @route("/api/tasks", methods=["GET"])
    async def api_list_tasks(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        params = request.query_params
        status = params.get("status", "all")
        assignee = params.get("assignee") or None
        try:
            limit = _parse_int(params.get("limit", "200"), "limit")

            def _load() -> list[dict[str, Any]]:
                filtered = status != "all"
                if filtered and status not in TASK_STATUSES:
                    raise InvalidInputError(
                        f"invalid task_status: {status!r} "
                        f"(expected one of {sorted(s for s in TASK_STATUSES if s)})"
                    )
                # The visible status is derived per row (workflow projection), so
                # a status filter must scan every task and filter after
                # projecting — a stored-status WHERE would miss/mismatch rows.
                # The projection short-circuits goals/overrides, so this stays
                # one indexed transitions query per in-flight row.
                rows = store.list_tasks("all", assignee, -1 if filtered else limit)
                cfg = read_workflow_config(current_project.get("project_root"))
                related_ids = [int(row["id"]) for row in rows]
                related_ids.extend(
                    int(row["parent_task_id"])
                    for row in rows
                    if row.get("parent_task_id")
                )
                transitions = store.list_task_transitions_for_tasks(related_ids)
                rows = [
                    _project_workflow_task_status(
                        store,
                        current_project.get("project_root"),
                        row,
                        cfg,
                        transitions.get(int(row["id"]), []),
                    )
                    for row in rows
                ]
                if filtered:
                    rows = [row for row in rows if row.get("task_status") == status]
                    if limit >= 0:
                        rows = rows[:limit]
                # Attach the block reason so every render path (the drawer
                # re-renders from this list) can show why a task is blocked.
                for row in rows:
                    if row.get("task_status") == "blocked":
                        row["blocked_reason"] = _task_blocked_reason(
                            store, row, transitions
                        )
                return rows

            tasks = await _to_thread(_load)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"tasks": tasks})

    @route("/api/tasks/{task_id:int}", methods=["GET"])
    async def api_get_task(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])

        def _load() -> dict[str, Any] | None:
            t = store.get_task(task_id)
            if t is not None:
                t = _project_workflow_task_status(
                    store, current_project.get("project_root"), t
                )
                t["blocked_reason"] = _task_blocked_reason(store, t)
            return t

        task = await _to_thread(_load)
        if task is None:
            return _json_error("task not found", 404, request)
        return _json(request, {"task": task})

    @route("/api/goals", methods=["GET"])
    async def api_list_goals(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        goals = await _to_thread(
            goals_summary, store, current_project.get("project_root")
        )
        return _json(request, {"goals": goals})

    @route("/api/run-jobs", methods=["GET"])
    async def api_list_run_jobs(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        status = request.query_params.get("status", "all")
        try:
            limit = _parse_int(request.query_params.get("limit", "100"), "limit")
            jobs = await _to_thread(store.list_run_jobs, status, limit)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"jobs": jobs})

    @route("/api/goals", methods=["POST"])
    async def api_create_goal(request: Request) -> JSONResponse:
        """Create a goal and enter it into the workflow in one shot, so a
        failed follow-up request can't leave a goal stranded outside the
        workflow waiting for a manual start."""
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        content = str(data.get("content") or "").strip()
        if not content:
            return _json_error("content is required", request=request)
        title = str(data.get("title") or "").strip() or content.splitlines()[0][:80]

        def _create_and_start() -> dict:
            conflict = active_goal_conflict_reason(store)
            if conflict:
                raise InvalidInputError(conflict)
            actor = _workflow_api_actor(
                str(data.get("agent") or ""), current_project.get("project_root")
            )
            _validate_goal_auto_runners(
                store, current_project.get("project_root"), title, content
            )
            store.register_agent(actor, "hub (goal start via UI)")
            [message_id] = store.send_message(
                actor, actor, content, kind="task", title=title
            )
            task = next(
                t for t in store.list_tasks(limit=10)
                if t["source_message_id"] == message_id
            )
            store.update_task_metadata(
                task["id"], is_goal=True,
                token_budget=_coerce_token_budget(data.get("token_budget")),
                goal_verify=str(data.get("goal_verify") or "").strip(),
            )
            try:
                started = start_workflow_task(
                    store, current_project.get("project_root"), actor, task["id"]
                )
            except (InvalidInputError, UnknownAgentError):
                # Don't strand a goal outside the workflow: close it so the
                # UI never needs a manual "start workflow" recovery click.
                store.set_task_workflow_state(task["id"], task_status="closed")
                raise
            return {"goal": store.get_task(task["id"]), **started}

        try:
            result = await _to_thread(_create_and_start)
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"goal creation failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/goals/{goal_id:int}/resume-budget", methods=["POST"])
    async def api_resume_goal_budget(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        goal_id = int(request.path_params["goal_id"])
        data = await _read_json(request)
        try:
            result = await _to_thread(
                _engine_resume_goal_budget, goal_id, data.get("token_budget")
            )
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"goal budget resume failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/messages", methods=["POST"])
    async def api_send_message(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        sender = str(data.get("sender", "")).strip()
        to = str(data.get("to", "")).strip()
        content = str(data.get("content", "")).strip()
        kind = str(data.get("kind", "message")).strip()
        title = str(data.get("title", "")).strip()
        task_status = str(data.get("task_status", "")).strip()
        reply_to = data.get("reply_to")
        if not sender or not to or not content:
            return _json_error("sender, to, and content are required", request=request)
        try:
            reply_to = None if reply_to in ("", None) else _parse_int(reply_to, "reply_to")
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        result = await _deliver(sender, to, content, reply_to, kind, title, task_status)
        if result.get("error"):
            return _json_error(result["error"], request=request)
        return _json(request, result)

    @route("/api/messages/{message_id:int}/task-status", methods=["POST"])
    async def api_update_task_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        message_id = int(request.path_params["message_id"])
        task_status = str(data.get("task_status", "")).strip()
        if not task_status:
            return _json_error("task_status is required", request=request)
        try:
            updated = await _to_thread(store.update_task_status, message_id, task_status)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(
            request,
            {"updated": updated, "message_id": message_id, "task_status": task_status}
        )

    @route("/api/tasks/{task_id:int}/status", methods=["POST"])
    async def api_update_task_item_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        task_id = int(request.path_params["task_id"])
        task_status = str(data.get("task_status", "")).strip()
        if not task_status:
            return _json_error("task_status is required", request=request)
        try:
            # Reject writes the workflow projection would hide (explicit error
            # instead of a silently-invisible store write).
            rejection = await _to_thread(
                lambda: _manual_status_rejection(
                    store, store.get_task(task_id), task_status
                )
            )
            if rejection:
                return _json_error(rejection, request=request)
            updated = await _to_thread(
                store.update_task_item_status, task_id, task_status
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        # Roll a manual subtask status change up to its parent goal.
        if updated:
            task = await _to_thread(store.get_task, task_id)
            if task:
                await _to_thread(
                    _recompute_parent_goal_status, store, task,
                    current_project.get("project_root"),
                )
        return _json(
            request,
            {"updated": updated, "task_id": task_id, "task_status": task_status},
        )

    @route("/api/tasks/{task_id:int}/metadata", methods=["POST"])
    async def api_update_task_metadata(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        task_id = int(request.path_params["task_id"])
        try:
            task = await _to_thread(
                store.update_task_metadata,
                task_id,
                data.get("importance"),
                data.get("size"),
                data.get("risk"),
                data.get("required_capabilities"),
                data.get("exclusive_workspace"),
                data.get("is_goal"),
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        if task is None:
            return _json_error("task not found", 404, request)
        return _json(request, {"task": task})

    @route("/api/tasks/{task_id:int}/workflow", methods=["GET"])
    async def api_task_workflow_state(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        try:
            state = await _to_thread(_engine_state, task_id)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, state)

    @route("/api/tasks/{task_id:int}/workflow/start", methods=["POST"])
    async def api_task_workflow_start(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        data = await _read_json(request)
        try:
            agent = await _to_thread(
                _workflow_api_actor,
                str(data.get("agent") or ""),
                current_project.get("project_root"),
            )
            result = await _to_thread(_engine_start, agent, task_id)
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:  # log the full story, surface a readable error
            traceback.print_exc()
            return _json_error(f"workflow start failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/tasks/{task_id:int}/workflow/complete", methods=["POST"])
    async def api_task_workflow_complete(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        data = await _read_json(request)
        try:
            agent = await _to_thread(
                _workflow_api_actor,
                str(data.get("agent") or ""),
                current_project.get("project_root"),
            )
            result = await _to_thread(
                _engine_advance,
                agent,
                task_id,
                str(data.get("step") or ""),
                str(data.get("outcome") or "done"),
                str(data.get("result") or ""),
            )
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"workflow step failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/tasks/{task_id:int}/rerun", methods=["POST"])
    async def api_task_rerun(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        data = await _read_json(request)
        agent = str(data.get("agent") or "").strip()
        if not agent:
            return _json_error("agent is required", request=request)
        try:
            result = await _to_thread(
                _engine_rerun, task_id, agent, str(data.get("step") or "")
            )
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"re-run failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/tasks/{task_id:int}/reimplement", methods=["POST"])
    async def api_task_reimplement(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        data = await _read_json(request)
        agent = str(data.get("agent") or "").strip()
        if not agent:
            return _json_error("agent is required", request=request)
        try:
            result = await _to_thread(_engine_reimplement, task_id, agent)
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"re-implement failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/tasks/{task_id:int}/skip", methods=["POST"])
    async def api_task_skip(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        data = await _read_json(request)
        try:
            result = await _to_thread(_engine_skip, task_id, str(data.get("step") or ""))
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"skip failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/tasks/{task_id:int}/force-close", methods=["POST"])
    async def api_task_force_close(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        try:
            result = await _to_thread(_engine_force_close, task_id)
        except (InvalidInputError, UnknownAgentError) as exc:
            return _json_error(str(exc), request=request)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"force-close failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/health-check", methods=["POST"])
    async def api_health_check(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        try:
            result = await _to_thread(_engine_health_check)
        except Exception as exc:
            traceback.print_exc()
            return _json_error(f"health check failed: {exc!r}", 500, request)
        return _json(request, result)

    @route("/api/project-file", methods=["GET"])
    async def api_project_file(request: Request) -> Response:
        if forbidden := _forbid_non_local(request):
            return forbidden
        raw_path = str(request.query_params.get("path") or "")
        root = _project_root(current_project.get("project_root"))
        path = (root / raw_path).resolve()
        docs_root = (root / "docs").resolve()
        if docs_root not in (path, *path.parents) or path.suffix.lower() != ".md":
            return _json_error("only Markdown files under docs/ may be opened", 400, request)
        try:
            return Response(path.read_text(encoding="utf-8"), media_type="text/markdown")
        except OSError as exc:
            return _json_error(f"cannot read project file: {exc}", 404, request)

    @route("/api/tasks/{task_id:int}/runs", methods=["GET"])
    async def api_list_task_runs(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        try:
            limit = _parse_int(request.query_params.get("limit", "20"), "limit")
            runs = await _to_thread(store.list_task_runs, task_id, limit)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"runs": runs})

    @route("/api/tasks/{task_id:int}/runs", methods=["POST"])
    async def api_create_task_run(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        task_id = int(request.path_params["task_id"])
        worker = str(data.get("worker", "")).strip()
        status = str(data.get("status", "running")).strip() or "running"
        run = await _to_thread(store.create_task_run, task_id, "", worker, status)
        if run is None:
            return _json_error("task not found", 404, request)
        if status == "running":
            await _to_thread(_mark_task_running, store, task_id)
        run_dir = _task_run_dir(
            current_project.get("project_root"), task_id, int(run["attempt"])
        )

        def _init_run_dir() -> None:
            run_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "type": "run_created",
                "run_id": run["id"],
                "task_id": task_id,
                "attempt": run["attempt"],
                "worker": worker,
                "created_at": run["started_at"],
            }
            (run_dir / "events.jsonl").write_text(
                json.dumps(event, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            for name in ("stdout.log", "stderr.log"):
                (run_dir / name).touch()

        await _to_thread(_init_run_dir)
        updated = await _to_thread(store.update_task_run_log_dir, run["id"], str(run_dir))
        return _json(request, {"run": updated or run})

    @route("/api/task-runs/{run_id:int}/events", methods=["POST"])
    async def api_append_task_run_event(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        run_id = int(request.path_params["run_id"])
        run = await _to_thread(store.get_task_run, run_id)
        if run is None:
            return _json_error("run not found", 404, request)
        event = data.get("event", data)
        if not isinstance(event, dict):
            return _json_error("event must be an object", request=request)
        event = {"run_id": run_id, **event}
        if "created_at" not in event:
            event["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            result = await _to_thread(_append_run_file, run, "events", line)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, result)

    @route("/api/task-runs/{run_id:int}/logs", methods=["POST"])
    async def api_append_task_run_log(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        run_id = int(request.path_params["run_id"])
        stream = str(data.get("stream", "stdout")).strip()
        content = data.get("content", "")
        if stream not in {"stdout", "stderr"}:
            return _json_error("stream must be stdout or stderr", request=request)
        run = await _to_thread(store.get_task_run, run_id)
        if run is None:
            return _json_error("run not found", 404, request)
        try:
            result = await _to_thread(_append_run_file, run, stream, content)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, result)

    @route("/api/task-runs/{run_id:int}/result", methods=["POST"])
    async def api_write_task_run_result(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        run_id = int(request.path_params["run_id"])
        content = data.get("content", "")
        status = str(data.get("status", "completed")).strip() or "completed"
        raw_exit_code = data.get("exit_code")
        try:
            exit_code = (
                None
                if raw_exit_code in ("", None)
                else _parse_int(raw_exit_code, "exit_code")
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        run = await _to_thread(store.get_task_run, run_id)
        if run is None:
            return _json_error("run not found", 404, request)
        try:
            write_result = await _to_thread(_write_run_file, run, "result", content)
            updated = await _to_thread(store.finish_task_run, run_id, status, exit_code)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"run": updated, "result": write_result})

    @route("/api/task-runs/{run_id:int}/files/{file_key}", methods=["GET"])
    async def api_read_task_run_file(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        run_id = int(request.path_params["run_id"])
        file_key = str(request.path_params["file_key"])
        run = await _to_thread(store.get_task_run, run_id)
        if run is None:
            return _json_error("run not found", 404, request)
        try:
            tail = _parse_int(request.query_params.get("tail", "65536"), "tail")
            result = await _to_thread(_read_run_file, run, file_key, tail)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, result)

    @route("/api/inbox/check", methods=["POST"])
    async def api_check_inbox(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        agent = str(data.get("agent", "")).strip()
        if not agent:
            return _json_error("agent is required", request=request)
        try:
            lease_seconds = _parse_int(
                data.get("lease_seconds", DEFAULT_LEASE_SECONDS), "lease_seconds"
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        lease_seconds = max(1, min(lease_seconds, MAX_LEASE_SECONDS))
        await _to_thread(store.touch_agent, agent)
        try:
            messages = await _to_thread(store.fetch_unread, agent, lease_seconds)
        except UnknownAgentError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"agent": agent, "count": len(messages), "messages": messages})

    @route("/api/messages/{message_id:int}/ack", methods=["POST"])
    async def api_ack_message(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        agent = str(data.get("agent", "")).strip()
        lease_token = str(data.get("lease_token", "")).strip()
        message_id = int(request.path_params["message_id"])
        if not agent or not lease_token:
            return _json_error("agent and lease_token are required", request=request)
        await _to_thread(store.touch_agent, agent)
        try:
            acked = await _to_thread(store.ack_message, agent, message_id, lease_token)
        except UnknownAgentError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"acked": acked, "message_id": message_id})

    @route("/api/thread/{message_id:int}", methods=["GET"])
    async def api_get_thread(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        message_id = int(request.path_params["message_id"])
        thread = await _to_thread(store.get_thread, message_id)
        return _json(request, {"messages": thread})

    application = Starlette(routes=routes, lifespan=lifespan)
    application.state.store = store
    application.state.runtime = local_runtime
    return application
