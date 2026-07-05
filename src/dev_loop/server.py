"""FastMCP server exposing the dev_loop mailbox tools."""

from __future__ import annotations

import atexit
import ipaddress
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .project_index import list_projects
from .store import DEFAULT_LEASE_SECONDS, InvalidInputError, Store, UnknownAgentError

MAX_WAIT_SECONDS = 60
MAX_LEASE_SECONDS = 3600
POLL_INTERVAL = 0.5
_WORKFLOW_ENGINE_LOCK = threading.Lock()

# The HTTP API is a local-only control surface: agents act on what lands in
# their inboxes, so a forged request is a prompt-injection channel. Defense is
# layered: the peer socket IP must be loopback (the load-bearing check — it is
# not client-controllable, so it holds even when bound beyond loopback with
# --host 0.0.0.0), the Host header must be a loopback hostname (blocks DNS
# rebinding), and any browser Origin must be a loopback origin (blocks CSRF).
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
_AGENT_TOOL_CANDIDATES = [
    {
        "id": "claude",
        "name": "Claude Code",
        "command": "claude",
        "agent_name": "claude-code",
        "description": "Claude Code CLI",
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "command": "codex",
        "agent_name": "codex",
        "description": "OpenAI Codex CLI",
    },
    {
        "id": "gemini",
        "name": "Gemini CLI",
        "command": "gemini",
        "agent_name": "gemini",
        "description": "Google Gemini CLI",
    },
    {
        "id": "agy",
        "name": "Antigravity CLI",
        "command": "agy",
        "agent_name": "antigravity",
        "description": "Google Antigravity CLI",
    },
    {
        "id": "hermes",
        "name": "Hermes",
        "command": "hermes",
        "agent_name": "hermes",
        "description": "Hermes agent CLI",
    },
]
_TASK_RUN_FILES = {
    "events": "events.jsonl",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "result": "result.md",
    "diff": "diff.patch",
}
REQUIRED_TEAM_ROLES = {"hub", "implementer", "reviewer"}
TASK_IMPORTANCE_SCORES = {"low": 0, "normal": 10, "high": 25, "critical": 40}
TASK_SIZE_SCORES = {"small": 0, "medium": 8, "large": 18}
TASK_RISK_SCORES = {"low": 0, "medium": 10, "high": 25}

# Store uses synchronous sqlite3; run every call in a worker thread so it
# never blocks the event loop (many concurrent long-polling clients).
_to_thread = anyio.to_thread.run_sync

_UI_HTML = (
    resources.files("dev_loop").joinpath("static/ui.html").read_text(encoding="utf-8")
)


async def _read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname in _LOCAL_HOSTNAMES:
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "content-type",
            "vary": "Origin",
        }
    return {}


def _json(request: Request, data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers=_cors_headers(request))


def _json_error(
    message: str, status_code: int = 400, request: Request | None = None
) -> JSONResponse:
    headers = _cors_headers(request) if request is not None else None
    return JSONResponse({"error": message}, status_code=status_code, headers=headers)


def _is_loopback_peer(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        ip = ipaddress.ip_address(client.host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except ValueError:
        return False


def _forbid_non_local(request: Request) -> JSONResponse | None:
    # Peer IP is not client-controllable — this is the check that holds even
    # when the server is bound beyond loopback (--host 0.0.0.0).
    if not _is_loopback_peer(request):
        return _json_error("API is only served to local clients", 403, request)
    if request.url.hostname not in _LOCAL_HOSTNAMES:
        return _json_error("API is only served to local hostnames", 403, request)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in _LOCAL_HOSTNAMES:
        return _json_error("cross-origin requests are not allowed", 403, request)
    return None


def _parse_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidInputError(f"{name} must be an integer, got {value!r}") from None


def _is_valid_role_id(role_id: str) -> bool:
    return bool(role_id) and role_id.isidentifier() and not role_id.startswith("_")


def _validate_role_content(content: Any) -> str:
    if content is None:
        raise InvalidInputError("Missing content")
    if not isinstance(content, str):
        raise InvalidInputError("Content must be a string")
    return content


def _agent_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug or "profile"


def _project_root(project_root: str | None) -> Path:
    return Path(project_root).resolve() if project_root else Path.cwd().resolve()


def _team_config_path(project_root: str | None) -> Path:
    return _project_root(project_root) / ".dev_loop" / "team.json"


def _workflow_config_path(project_root: str | None) -> Path:
    return _project_root(project_root) / ".dev_loop" / "workflow.json"


# Default canvas layout. Nodes carry explicit x/y because the flow is not a
# simple row: product design fans out to two parallel branches (UI design and
# architecture) that merge back into implementation, and review loops back to
# implementation on rework.
_DEFAULT_STEP_MID_Y = 160


def default_workflow_steps() -> list[dict[str, Any]]:
    # (id, name, role_id, task_status, required, x, y)
    specs = [
        ("intake", "Intake", "hub", "created", True, 40, _DEFAULT_STEP_MID_Y),
        ("product_design", "Product Design", "product_designer", "assigned", True, 360, _DEFAULT_STEP_MID_Y),
        # Parallel branch cards stack vertically at x=700; keep enough gap
        # for a full card (~400px tall with the name/timeout fields).
        ("ui_design", "UI Design", "ui_designer", "assigned", False, 700, 40),
        ("architecture", "Architecture", "architect", "assigned", True, 700, 500),
        ("implement", "Implement", "implementer", "in_progress", True, 1060, _DEFAULT_STEP_MID_Y),
        ("test", "Test", "tester", "testing", False, 1400, _DEFAULT_STEP_MID_Y),
        ("review", "Review", "reviewer", "replied", True, 1740, _DEFAULT_STEP_MID_Y),
        ("accept", "Accept", "hub", "accepted", True, 2080, _DEFAULT_STEP_MID_Y),
    ]
    return [
        {
            "id": step_id,
            "name": name,
            "role_id": role_id,
            "task_status": task_status,
            "required": required,
            "x": x,
            "y": y,
        }
        for step_id, name, role_id, task_status, required, x, y in specs
    ]


def default_workflow_edges() -> list[dict[str, str]]:
    # Demonstrates the three branching patterns:
    #   parallel : product_design fans out to ui_design + architecture
    #   merge    : ui_design + architecture both feed implement
    #   loop-back: review returns to implement on rework
    return [
        {"from": "intake", "to": "product_design"},
        {"from": "product_design", "to": "ui_design"},      # parallel
        {"from": "product_design", "to": "architecture"},   # parallel
        {"from": "ui_design", "to": "implement"},           # merge
        {"from": "architecture", "to": "implement"},        # merge
        {"from": "implement", "to": "test"},
        {"from": "test", "to": "review"},
        {"from": "review", "to": "accept"},
        {"from": "review", "to": "implement"},              # loop-back (rework)
    ]


def _normalize_workflow_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise InvalidInputError("workflow step must be an object")
    step_id = _agent_slug(str(step.get("id", "") or f"step-{index + 1}"))
    name = str(step.get("name", "") or step_id).strip()
    role_id = str(step.get("role_id", "")).strip()
    task_status = str(step.get("task_status", "")).strip()
    if not name:
        raise InvalidInputError("workflow step name is required")
    if not _is_valid_role_id(role_id):
        raise InvalidInputError("workflow step role_id is invalid")
    if task_status and task_status not in {
        "created",
        "assigned",
        "in_progress",
        "testing",
        "bugfixing",
        "replied",
        "accepted",
        "needs_changes",
        "blocked",
        "closed",
    }:
        raise InvalidInputError("workflow step task_status is invalid")
    def _coord(key: str, default: float) -> float:
        raw = step.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise InvalidInputError(f"workflow step {key} must be a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidInputError(f"workflow step {key} must be finite")
        return max(0.0, round(value, 2))

    try:
        timeout_minutes = int(step.get("timeout_minutes", 0) or 0)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow step timeout_minutes must be an integer") from None
    if timeout_minutes < 0:
        raise InvalidInputError("workflow step timeout_minutes must be >= 0")

    # Steps run by the mandatory team roles (hub/implementer/reviewer) are the
    # indispensable core of the dev loop; they are always required and the
    # flag cannot be toggled off.
    required_locked = role_id in REQUIRED_TEAM_ROLES
    return {
        "id": step_id,
        "name": name,
        "role_id": role_id,
        "task_status": task_status or "created",
        "required": True if required_locked else bool(step.get("required", False)),
        "required_locked": required_locked,
        "timeout_minutes": timeout_minutes,
        "x": _coord("x", 40 + index * 320),
        "y": _coord("y", _DEFAULT_STEP_MID_Y),
    }


def _normalize_workflow_edges(
    edges: Any, valid_ids: set[str]
) -> list[dict[str, str]]:
    if edges is None:
        return []
    if not isinstance(edges, list):
        raise InvalidInputError("workflow edges must be a list")
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise InvalidInputError("workflow edge must be an object")
        src = _agent_slug(str(edge.get("from", "")))
        dst = _agent_slug(str(edge.get("to", "")))
        if src not in valid_ids or dst not in valid_ids:
            raise InvalidInputError("workflow edge references an unknown step")
        if src == dst:
            continue  # self-loops are meaningless; drop silently
        key = (src, dst)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"from": src, "to": dst})
    return normalized


# Structural problems are warnings, not errors: the canvas saves after every
# drag/add, so a half-connected graph is a normal intermediate state.
def _workflow_graph_warnings(
    steps: list[dict[str, Any]], edges: list[dict[str, str]]
) -> list[str]:
    ids = [step["id"] for step in steps]
    if len(ids) <= 1:
        return []
    adj: dict[str, list[str]] = {}
    radj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge["from"], []).append(edge["to"])
        radj.setdefault(edge["to"], []).append(edge["from"])
    entries = [i for i in ids if i not in radj]
    terminals = [i for i in ids if i not in adj]

    def _reach(seeds: list[str], graph: dict[str, list[str]]) -> set[str]:
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, []))
        return seen

    warnings = []
    if not entries:
        warnings.append("no entry step: every step has an incoming connection")
    else:
        unreachable = [i for i in ids if i not in _reach(entries, adj)]
        if unreachable:
            warnings.append("unreachable steps: " + ", ".join(unreachable))
    if not terminals:
        warnings.append("no terminal step: every step has an outgoing connection")
    else:
        stuck = [i for i in ids if i not in _reach(terminals, radj)]
        if stuck:
            warnings.append("steps with no path to an end: " + ", ".join(stuck))
    return warnings


def read_workflow_config(project_root: str | None = None) -> dict[str, Any]:
    path = _workflow_config_path(project_root)
    if not path.exists():
        # Normalize the defaults too so unsaved workflows carry the same
        # derived fields (required_locked, timeout_minutes) as saved ones.
        return {
            "steps": [
                _normalize_workflow_step(step, index)
                for index, step in enumerate(default_workflow_steps())
            ],
            "edges": default_workflow_edges(),
            "path": str(path),
            "warnings": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidInputError(f"invalid workflow config: {exc}") from exc
    steps = data.get("steps", []) if isinstance(data, dict) else []
    if not isinstance(steps, list):
        raise InvalidInputError("workflow steps must be a list")
    normalized = [
        _normalize_workflow_step(step, index) for index, step in enumerate(steps)
    ]
    valid_ids = {step["id"] for step in normalized}
    raw_edges = data.get("edges") if isinstance(data, dict) else None
    if raw_edges is None:
        # Legacy config with no edges: connect steps in stored order so
        # existing linear workflows still render as a connected chain.
        edges = [
            {"from": normalized[i]["id"], "to": normalized[i + 1]["id"]}
            for i in range(len(normalized) - 1)
        ]
    else:
        edges = _normalize_workflow_edges(raw_edges, valid_ids)
    return {
        "steps": normalized,
        "edges": edges,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, edges),
    }


def write_workflow_config(
    steps: list[Any],
    project_root: str | None = None,
    edges: Any = None,
) -> dict[str, Any]:
    if not isinstance(steps, list):
        raise InvalidInputError("steps must be a list")
    normalized = [_normalize_workflow_step(step, index) for index, step in enumerate(steps)]
    if not normalized:
        raise InvalidInputError("workflow must include at least one step")
    valid_ids = {step["id"] for step in normalized}
    if len(valid_ids) != len(normalized):
        raise InvalidInputError("workflow step ids must be unique")
    # The UI hides Remove on core-role cards; enforce the same rule here so
    # a raw POST can't save a workflow with the core loop deleted. Reads stay
    # lenient so legacy configs still load.
    missing_core = sorted(
        REQUIRED_TEAM_ROLES - {step["role_id"] for step in normalized}
    )
    if missing_core:
        raise InvalidInputError(
            "workflow must keep steps for core roles: " + ", ".join(missing_core)
        )
    _reject_unknown_roles({step["role_id"] for step in normalized}, project_root)
    normalized_edges = _normalize_workflow_edges(edges, valid_ids)
    path = _workflow_config_path(project_root)
    project_root_path = _project_root(project_root)
    resolved_path = path.resolve()
    if project_root_path not in (resolved_path, *resolved_path.parents):
        raise InvalidInputError("workflow config path escapes project root")
    path.parent.mkdir(parents=True, exist_ok=True)
    # required_locked is derived from the role on every read; keep it out of
    # the persisted file so hand-edits can't desync it.
    persisted = [
        {k: v for k, v in step.items() if k != "required_locked"}
        for step in normalized
    ]
    data = {"steps": persisted, "edges": normalized_edges}
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "steps": normalized,
        "edges": normalized_edges,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, normalized_edges),
    }


def _normalize_team_member(member: Any) -> dict[str, Any]:
    if not isinstance(member, dict):
        raise InvalidInputError("team member must be an object")
    agent_name = str(member.get("agent_name", "")).strip()
    role_id = str(member.get("role_id", "")).strip()
    if not agent_name:
        raise InvalidInputError("agent_name is required")
    if not _is_valid_role_id(role_id):
        raise InvalidInputError("role_id is invalid")
    capabilities = member.get("capabilities", [])
    if isinstance(capabilities, str):
        capabilities = [
            capability.strip()
            for capability in capabilities.split(",")
            if capability.strip()
        ]
    if not isinstance(capabilities, list) or not all(
        isinstance(capability, str) for capability in capabilities
    ):
        raise InvalidInputError("capabilities must be a list of strings")
    try:
        legacy_priority = int(member.get("weight", member.get("priority", 75)))
        expertise_level = int(
            member.get("expertise_level", _legacy_priority_to_expertise(legacy_priority))
        )
        max_concurrent_tasks = int(member.get("max_concurrent_tasks", 1))
    except (TypeError, ValueError):
        raise InvalidInputError(
            "expertise_level and max_concurrent_tasks must be integers"
        ) from None
    return {
        "agent_name": agent_name,
        "role_id": role_id,
        "enabled": bool(member.get("enabled", True)),
        "expertise_level": max(1, min(expertise_level, 5)),
        "max_concurrent_tasks": max(1, min(max_concurrent_tasks, 3)),
        "capabilities": [capability.strip() for capability in capabilities if capability.strip()],
        "notes": str(member.get("notes", "")).strip(),
        # Auto-runner: spawn a one-shot CLI per dispatched step instead of
        # waiting for a live session to poll the inbox.
        "auto_run": bool(member.get("auto_run", False)),
        "runner_command": str(member.get("runner_command", "")).strip(),
    }


def _legacy_priority_to_expertise(priority: int) -> int:
    if priority >= 120:
        return 5
    if priority >= 100:
        return 4
    if priority >= 75:
        return 3
    if priority >= 50:
        return 2
    return 1


def required_expertise_for_task(task: dict[str, Any]) -> int:
    importance = str(task.get("importance") or "normal")
    size = str(task.get("size") or "medium")
    risk = str(task.get("risk") or "medium")
    required = 2
    if importance in {"normal", "high", "critical"}:
        required += 1
    if importance in {"high", "critical"}:
        required += 1
    if importance == "critical":
        required += 1
    if size == "large":
        required += 1
    if risk == "high":
        required += 1
    return max(1, min(required, 5))


def _reject_unknown_roles(role_ids: set[str], project_root: str | None) -> None:
    # Role ids are only syntax-checked during normalization; here they must
    # also match an actual agents/<role>.md, otherwise the UI selects (which
    # can't represent an unknown role) would silently rewrite them. Skipped
    # when no roles can be listed at all, so configs stay writable in bare
    # environments.
    known = {role["id"] for role in list_agent_roles(_agents_dir(project_root))}
    if not known:
        return
    unknown = sorted(role_ids - known)
    if unknown:
        raise InvalidInputError("unknown roles: " + ", ".join(unknown))


def team_locked_reason(store: Store) -> str | None:
    """Team config is frozen while any task is actively executing workflow
    steps: reassignment math, role constraints, and running auto-runners all
    read the team live, so edits mid-flight corrupt routing. Blocked tasks
    do NOT lock — fixing the team is the documented way to unblock them."""
    busy = [
        task["id"]
        for task in store.list_tasks(status="all", limit=500)
        if task.get("workflow_step")
        and task.get("task_status") not in ("blocked", "closed")
    ]
    if not busy:
        return None
    ids = ", ".join(f"#{task_id}" for task_id in busy[:10])
    return (
        f"team config is locked while workflow tasks are running ({ids}); "
        "wait for them to finish, or block/close them first"
    )


def _missing_team_roles(members: list[dict[str, Any]]) -> list[str]:
    enabled_roles = {
        member["role_id"] for member in members if member.get("enabled", True)
    }
    return sorted(REQUIRED_TEAM_ROLES - enabled_roles)


def read_team_config(project_root: str | None = None) -> dict[str, Any]:
    path = _team_config_path(project_root)
    if not path.exists():
        return {
            "members": [],
            "path": str(path),
            "missing_roles": sorted(REQUIRED_TEAM_ROLES),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidInputError(f"invalid team config: {exc}") from exc
    members = data.get("members", []) if isinstance(data, dict) else []
    if not isinstance(members, list):
        raise InvalidInputError("team members must be a list")
    normalized = [_normalize_team_member(member) for member in members]
    return {
        "members": normalized,
        "path": str(path),
        "missing_roles": _missing_team_roles(normalized),
    }


def write_team_config(
    members: list[Any], project_root: str | None = None
) -> dict[str, Any]:
    if not isinstance(members, list):
        raise InvalidInputError("members must be a list")
    normalized = [_normalize_team_member(member) for member in members]
    _reject_unknown_roles({member["role_id"] for member in normalized}, project_root)
    # Missing core roles are reported, not rejected: a hard error here made
    # it impossible to build a team up one member at a time. Readiness is
    # checked where work actually starts.
    missing_roles = _missing_team_roles(normalized)
    path = _team_config_path(project_root)
    project_root_path = _project_root(project_root)
    resolved_path = path.resolve()
    if project_root_path not in (resolved_path, *resolved_path.parents):
        raise InvalidInputError("team config path escapes project root")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"members": normalized}
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"members": normalized, "path": str(path), "missing_roles": missing_roles}


def rank_assignment_candidates(
    task: dict[str, Any],
    members: list[dict[str, Any]],
    active_counts: dict[str, int] | None = None,
    role_id: str | None = None,
) -> dict[str, Any]:
    active_counts = active_counts or {}
    required_role = (role_id or task.get("role_required") or "implementer").strip()
    required_capabilities = set(task.get("required_capabilities") or [])
    importance = str(task.get("importance") or "normal")
    size = str(task.get("size") or "medium")
    risk = str(task.get("risk") or "medium")
    required_expertise = required_expertise_for_task(task)
    importance_bonus = TASK_IMPORTANCE_SCORES.get(importance, 10)
    size_penalty = TASK_SIZE_SCORES.get(size, 8)
    risk_penalty = TASK_RISK_SCORES.get(risk, 10)
    candidates = []
    for member in members:
        if not member.get("enabled", True):
            continue
        if member.get("role_id") != required_role:
            continue
        member_capabilities = set(member.get("capabilities") or [])
        missing_capabilities = sorted(required_capabilities - member_capabilities)
        active_count = active_counts.get(member["agent_name"], 0)
        max_concurrent = int(member.get("max_concurrent_tasks", 1))
        if active_count >= max_concurrent:
            continue
        expertise_level = int(member.get("expertise_level", 3))
        capability_bonus = 15 * (
            len(required_capabilities) - len(missing_capabilities)
        )
        missing_penalty = 100 * len(missing_capabilities)
        expertise_gap = max(0, required_expertise - expertise_level)
        expertise_bonus = 18 * max(0, expertise_level - required_expertise)
        expertise_penalty = 90 * expertise_gap
        load_penalty = 50 * active_count
        exclusive_penalty = 35 * active_count if task.get("exclusive_workspace") else 0
        score = (
            capability_bonus
            + expertise_bonus
            + importance_bonus
            - size_penalty
            - risk_penalty
            - missing_penalty
            - expertise_penalty
            - load_penalty
            - exclusive_penalty
        )
        candidates.append(
            {
                **member,
                "active_tasks": active_count,
                "available_slots": max_concurrent - active_count,
                "missing_capabilities": missing_capabilities,
                "expertise_gap": expertise_gap,
                "score": score,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["score"],
            -item["active_tasks"],
            item.get("expertise_level", 3),
        ),
        reverse=True,
    )
    required_followups = []
    if risk == "high" or importance == "critical":
        required_followups.extend(["reviewer", "tester"])
    return {
        "role_id": required_role,
        "required_expertise_level": required_expertise,
        "candidates": candidates,
        "selected": candidates[0] if candidates else None,
        "required_followups": sorted(set(required_followups)),
    }


# --- Workflow constraint engine ---------------------------------------------
# The workflow graph drives task routing. Completing a step advances the task
# along forward edges (layer increases); "rework" follows loop-back edges;
# merge steps wait until every *required* forward predecessor has completed;
# each dispatched step is assigned to the best-ranked team member for its
# role. All movements are recorded in task_transitions.

WORKFLOW_ENGINE_AGENT = "workflow"
WORKFLOW_OUTCOMES = {"done", "rework", "blocked"}


def _workflow_graph(cfg: dict[str, Any]) -> set[tuple[str, str]]:
    """Classify loop-back edges via DFS (an edge into a node still on the
    DFS stack closes a cycle). Every other edge is forward flow. Layer
    numbers are not used: cycles make longest-path layering ambiguous, so
    forward/backward is decided purely by this classification."""
    ids = [step["id"] for step in cfg["steps"]]
    adj: dict[str, list[str]] = {}
    for edge in cfg["edges"]:
        adj.setdefault(edge["from"], []).append(edge["to"])
    color: dict[str, int] = {}  # missing=white, 1=on stack, 2=done
    back: set[tuple[str, str]] = set()
    for root in ids:
        if color.get(root):
            continue
        color[root] = 1
        stack = [(root, iter(adj.get(root, [])))]
        while stack:
            node, children = stack[-1]
            descended = False
            for child in children:
                if color.get(child, 0) == 0:
                    color[child] = 1
                    stack.append((child, iter(adj.get(child, []))))
                    descended = True
                    break
                if color[child] == 1:
                    back.add((node, child))
            if not descended:
                color[node] = 2
                stack.pop()
    return back


def _forward_out(
    cfg: dict[str, Any], back: set[tuple[str, str]], step_id: str
) -> list[str]:
    return [
        e["to"] for e in cfg["edges"]
        if e["from"] == step_id and (e["from"], e["to"]) not in back
    ]


def _workflow_entry_steps(cfg: dict[str, Any], back: set[tuple[str, str]]) -> list[str]:
    forward_in = {
        e["to"] for e in cfg["edges"] if (e["from"], e["to"]) not in back
    }
    return [s["id"] for s in cfg["steps"] if s["id"] not in forward_in]


def _workflow_terminal_steps(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> list[str]:
    forward_out_src = {
        e["from"] for e in cfg["edges"] if (e["from"], e["to"]) not in back
    }
    return [s["id"] for s in cfg["steps"] if s["id"] not in forward_out_src]


def _workflow_execution_errors(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> list[str]:
    # Entry/terminal/reachability all use the forward graph (loop-back edges
    # excluded) — the same classification the engine routes by. Raw edges
    # would misjudge legitimate patterns like an accept -> intake reopen
    # loop as a workflow with no entry at all.
    ids = [step["id"] for step in cfg["steps"]]
    steps = {step["id"]: step for step in cfg["steps"]}
    entries = _workflow_entry_steps(cfg, back)
    terminals = _workflow_terminal_steps(cfg, back)

    def _reach(seeds: list[str], reverse: bool = False) -> set[str]:
        graph: dict[str, list[str]] = {}
        for edge in cfg["edges"]:
            if (edge["from"], edge["to"]) in back:
                continue
            src, dst = (
                (edge["to"], edge["from"]) if reverse
                else (edge["from"], edge["to"])
            )
            graph.setdefault(src, []).append(dst)
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, []))
        return seen

    errors: list[str] = []
    if not entries:
        errors.append("no entry step")
    if not terminals:
        errors.append("no terminal step")
    reachable = _reach(entries) if entries else set()
    can_finish = _reach(terminals, reverse=True) if terminals else set()
    required_ids = [step_id for step_id in ids if steps[step_id]["required"]]
    unreachable_required = [step_id for step_id in required_ids if step_id not in reachable]
    if unreachable_required:
        errors.append("required steps unreachable: " + ", ".join(unreachable_required))
    stuck_required = [step_id for step_id in required_ids if step_id not in can_finish]
    if stuck_required:
        errors.append(
            "required steps with no path to terminal: " + ", ".join(stuck_required)
        )
    return errors


def _team_role_of(members: list[dict[str, Any]], agent: str) -> str | None:
    for member in members:
        if member["agent_name"] == agent and member.get("enabled", True):
            return member["role_id"]
    return None


# Outcomes that close out one dispatch of a step: the agent finished it
# (done/rework) or the engine took it away (reassigned on timeout).
_STEP_FINISHING_OUTCOMES = ("done", "rework", "reassigned")


def _active_steps(transitions: list[dict[str, Any]]) -> list[str]:
    dispatched: dict[str, int] = {}
    finished: dict[str, int] = {}
    for t in transitions:
        if t["outcome"] == "dispatched":
            dispatched[t["to_step"]] = dispatched.get(t["to_step"], 0) + 1
        elif t["outcome"] in _STEP_FINISHING_OUTCOMES and t["from_step"]:
            finished[t["from_step"]] = finished.get(t["from_step"], 0) + 1
    return [s for s, n in dispatched.items() if n > finished.get(s, 0)]


def _active_step_assignees(transitions: list[dict[str, Any]]) -> dict[str, str]:
    dispatches: dict[str, list[str]] = {}
    finished: dict[str, int] = {}
    for t in transitions:
        if t["outcome"] == "dispatched":
            dispatches.setdefault(t["to_step"], []).append(t.get("note", ""))
        elif t["outcome"] in _STEP_FINISHING_OUTCOMES and t["from_step"]:
            finished[t["from_step"]] = finished.get(t["from_step"], 0) + 1
    active: dict[str, str] = {}
    for step, assignees in dispatches.items():
        remaining = assignees[finished.get(step, 0):]
        if remaining:
            active[step] = remaining[-1]
    return active


def _latest_rework_transition_id(transitions: list[dict[str, Any]]) -> int:
    return max((t["id"] for t in transitions if t["outcome"] == "rework"), default=0)


def _dispatched_since(
    transitions: list[dict[str, Any]], step: str, transition_id: int
) -> bool:
    return any(
        t["id"] > transition_id
        and t["outcome"] == "dispatched"
        and t["to_step"] == step
        for t in transitions
    )


def _join_ready(
    target: str,
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    steps: dict[str, dict[str, Any]],
    transitions: list[dict[str, Any]],
) -> bool:
    required_preds = [
        e["from"] for e in cfg["edges"]
        if e["to"] == target
        and (e["from"], e["to"]) not in back
        and steps[e["from"]]["required"]
    ]
    arrived = {
        t["from_step"] for t in transitions
        if t["to_step"] == target and t["outcome"] in ("done", "skipped")
    }
    return all(pred in arrived for pred in required_preds)


def _pick_assignee(
    store: Store, task: dict[str, Any], step: dict[str, Any],
    members: list[dict[str, Any]],
) -> str | None:
    ranked = rank_assignment_candidates(
        {**task, "role_required": step["role_id"]},
        members,
        store.active_task_counts(),
        role_id=step["role_id"],
    )
    selected = ranked.get("selected")
    return selected["agent_name"] if selected else None


def _ensure_engine_agent(store: Store) -> None:
    if not store.agent_exists(WORKFLOW_ENGINE_AGENT):
        store.register_agent(
            WORKFLOW_ENGINE_AGENT,
            "workflow engine: routes tasks along the configured workflow",
        )


def _notify_hub(store: Store, members: list[dict[str, Any]], text: str) -> str:
    hub_agent = next(
        (m["agent_name"] for m in members
         if m["role_id"] == "hub" and m.get("enabled", True)),
        "hub",
    )
    try:
        if not store.agent_exists(hub_agent):
            return f"hub agent {hub_agent!r} not registered; notice dropped"
        _ensure_engine_agent(store)
        store.send_message(WORKFLOW_ENGINE_AGENT, hub_agent, text)
        return f"notified {hub_agent}"
    except (UnknownAgentError, InvalidInputError) as exc:
        return f"hub notification failed: {exc}"


def _workflow_api_actor(raw_agent: str, project_root: str | None) -> str:
    # The UI sends no agent name; it acts as the team's hub member so the
    # engine's assignee/hub constraint recognizes it.
    agent = (raw_agent or "").strip()
    if agent:
        return agent
    members = read_team_config(project_root)["members"]
    hub_agent = next(
        (
            m["agent_name"] for m in members
            if m["role_id"] == "hub" and m.get("enabled", True)
        ),
        None,
    )
    if hub_agent is None:
        raise InvalidInputError(
            "team has no enabled hub member to act for the UI; "
            "add one on the Team page or pass an agent name"
        )
    return hub_agent


def _member_named(members: list[dict[str, Any]], agent_name: str) -> dict[str, Any] | None:
    return next((m for m in members if m["agent_name"] == agent_name), None)


def _dispatch_step(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str,
) -> None:
    assignee = member["agent_name"]
    task_id = task["id"]
    _ensure_engine_agent(store)
    if not store.agent_exists(assignee):
        # Pre-register so the dispatch waits in their inbox until they poll.
        store.register_agent(assignee, f"team member (role: {step['role_id']})")
    content = (
        f"[workflow step: {step['id']}] Task #{task_id}: {task.get('title') or 'untitled'}\n\n"
        f"{task.get('content', '')}\n"
        + (f"\nUpstream result:\n{upstream_result}\n" if upstream_result else "")
        + f"\nYou are acting as role '{step['role_id']}' for step '{step['name']}'.\n"
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
    store.set_task_workflow_state(
        task_id, task_status=step["task_status"], assignee=assignee
    )
    if member.get("auto_run"):
        _spawn_step_worker(store, project_root, task_id, step, member, upstream_result)


def _dispatch_targets(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    targets: list[str],
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    members: list[dict[str, Any]],
    upstream_result: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    steps = {s["id"]: s for s in cfg["steps"]}
    task_id = task["id"]
    dispatched: list[dict[str, Any]] = []
    notices: list[str] = []
    queue = list(targets)
    while queue:
        target = queue.pop(0)
        transitions = store.list_task_transitions(task_id)
        if target in _active_steps(transitions):
            continue  # already running this step
        if _dispatched_since(transitions, target, _latest_rework_transition_id(transitions)):
            continue  # already ran in this workflow pass
        if not _join_ready(target, cfg, back, steps, transitions):
            notices.append(f"step {target} is waiting for other required branches")
            continue
        step = steps[target]
        assignee = _pick_assignee(store, task, step, members)
        if assignee is None:
            if step["required"]:
                store.record_task_transition(
                    task_id, "", target, WORKFLOW_ENGINE_AGENT, "blocked",
                    f"no available team member for role {step['role_id']}",
                )
                store.set_task_workflow_state(task_id, task_status="blocked")
                notices.append(_notify_hub(
                    store, members,
                    f"Task #{task_id} blocked: required step '{target}' has no "
                    f"available team member for role {step['role_id']}.",
                ))
                continue
            # Optional step with nobody to run it: pass through.
            for nxt in _forward_out(cfg, back, target):
                store.record_task_transition(
                    task_id, target, nxt, WORKFLOW_ENGINE_AGENT, "skipped",
                    f"no team member for optional step {target}",
                )
                queue.append(nxt)
            notices.append(f"optional step {target} skipped (no team member)")
            continue
        _dispatch_step(
            store, project_root, task, step,
            _member_named(members, assignee) or {"agent_name": assignee},
            upstream_result,
        )
        dispatched.append({"step": target, "assignee": assignee})
    transitions = store.list_task_transitions(task_id)
    active = _active_steps(transitions)
    if active:
        store.set_task_workflow_state(task_id, workflow_step=",".join(active))
    return dispatched, notices


def start_workflow_task(
    store: Store, project_root: str | None, agent: str, task_id: int
) -> dict[str, Any]:
    with _WORKFLOW_ENGINE_LOCK:
        return _start_workflow_task_locked(store, project_root, agent, task_id)


def _start_workflow_task_locked(
    store: Store, project_root: str | None, agent: str, task_id: int
) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
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
    steps = {s["id"]: s for s in cfg["steps"]}
    entries = [
        step_id for step_id in _workflow_entry_steps(cfg, back)
        if steps[step_id]["required"] or _forward_out(cfg, back, step_id)
    ]
    members = read_team_config(project_root)["members"]
    dispatched, notices = _dispatch_targets(
        store, project_root, task, entries, cfg, back, members, ""
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
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    if step not in steps:
        raise InvalidInputError(f"unknown workflow step: {step}")
    members = read_team_config(project_root)["members"]
    transitions = store.list_task_transitions(task_id)
    active_assignees = _active_step_assignees(transitions)
    if step not in active_assignees:
        raise InvalidInputError(f"workflow step {step} is not active for task {task_id}")
    assigned_agent = active_assignees[step]
    # Constraint: only the agent that was dispatched the active step may
    # complete it. Hub can override any active step for recovery.
    actor_role = _team_role_of(members, agent)
    if agent != assigned_agent and actor_role != "hub":
        raise InvalidInputError(
            f"agent {agent} is not assigned to active step {step} "
            f"(assigned to {assigned_agent})"
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

    if outcome == "blocked":
        store.record_task_transition(task_id, step, step, agent, "blocked", result)
        store.set_task_workflow_state(task_id, task_status="blocked")
        notice = _notify_hub(
            store, members,
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
    else:
        targets = forward

    for target in targets:
        store.record_task_transition(task_id, step, target, agent, outcome, result)

    if outcome == "done" and not targets:
        # Terminal step completed: the task leaves the workflow.
        store.record_task_transition(task_id, step, "", agent, "done", result)
        store.set_task_workflow_state(
            task_id, workflow_step="", task_status="closed"
        )
        return {
            "task_id": task_id, "step": step, "outcome": "done",
            "closed": True, "dispatched": [], "notices": [],
        }

    dispatched, notices = _dispatch_targets(
        store, project_root, task, targets, cfg, back, members, result
    )
    return {
        "task_id": task_id, "step": step, "outcome": outcome,
        "dispatched": dispatched, "notices": notices,
    }


# How often the background watcher scans for timed-out steps.
WORKFLOW_TIMEOUT_POLL_SECONDS = 60

# --- Auto-runner -------------------------------------------------------------
# Team members with auto_run enabled get a one-shot CLI process spawned for
# each dispatched step instead of waiting for a live session to poll the
# inbox. The command receives the prompt on stdin, works in the project root,
# and its stdout tail is submitted via the engine as the step result.
# runner_command on the member overrides the per-tool default.
RUNNER_DEFAULT_TIMEOUT_SECONDS = 1800
# Headless CLIs default to asking for permission on every file write / tool
# call, which no one is there to answer — a runner without full permissions
# can only produce inline text. These defaults grant maximum autonomy; set
# runner_command on the member to run with a tighter policy.
_DEFAULT_RUNNER_COMMANDS = {
    "claude-code": "claude -p --dangerously-skip-permissions",
    "codex": "codex exec --dangerously-bypass-approvals-and-sandbox -",
    # gemini additionally refuses to start headless in an untrusted dir.
    "gemini": "GEMINI_CLI_TRUST_WORKSPACE=true gemini --yolo",
}


def _runner_command_for(member: dict[str, Any]) -> str:
    command = str(member.get("runner_command") or "").strip()
    if command:
        return command
    agent_name = member.get("agent_name", "")
    if agent_name in _DEFAULT_RUNNER_COMMANDS:
        return _DEFAULT_RUNNER_COMMANDS[agent_name]
    # Hermes takes the prompt as a -z argument, not on stdin ($(cat) bridges
    # it); profile agents are named hermes-<profile>, so match dynamically.
    if agent_name == "hermes" or agent_name.startswith("hermes-"):
        profile = agent_name[len("hermes-"):] if agent_name.startswith("hermes-") else ""
        profile_arg = f"--profile {shlex.quote(profile)} " if profile else ""
        return f'hermes {profile_arg}--yolo -z "$(cat)"'
    return ""


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[-limit:]


def _build_step_prompt(
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    upstream_result: str,
) -> str:
    roles = {
        role["id"]: role["content"]
        for role in list_agent_roles(_agents_dir(project_root))
    }
    role_text = roles.get(step["role_id"], "")
    return (
        f"你是被工作流引擎派发的一次性 worker，以角色 {step['role_id']} 执行"
        f"步骤 '{step['name']}'。当前工作目录就是项目根目录，直接读写文件完成任务。\n"
        "忽略角色说明里 register_agent / check_inbox / ack 等信箱循环要求——"
        "本次为一次性执行，也不要调用 complete_step，派发器会代为提交结果。"
        "例外：角色说明要求拆分/新建任务时（如 goal 拆分），可以使用 devloop 的 "
        "send_message(kind=\"task\") 与 start_workflow_task 工具。\n\n"
        f"## 角色说明\n{role_text}\n\n"
        f"## 任务 #{task['id']}: {task.get('title') or 'untitled'}\n"
        f"{task.get('content', '')}\n\n"
        + (f"## 上游产出\n{upstream_result}\n\n" if upstream_result else "")
        + "完成后在输出的最后打印一段简短总结：一行结论 + 产物文件路径。"
    )


def run_step_worker(
    store: Store,
    project_root: str | None,
    task_id: int,
    step: dict[str, Any],
    member: dict[str, Any],
    upstream_result: str = "",
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Execute one dispatched step via the member's CLI and submit the
    outcome through the engine. Exit 0 -> done (stdout tail as result);
    nonzero/timeout/missing command -> blocked, which alerts the hub."""
    assignee = member["agent_name"]
    task = store.get_task(task_id)
    if not task:
        return {"error": f"unknown task: {task_id}"}
    run = store.create_task_run(task_id, worker=assignee)
    if run:
        log_dir = _task_run_dir(project_root, task_id, int(run["attempt"]))
        run = store.update_task_run_log_dir(run["id"], str(log_dir)) or run
    command = _runner_command_for(member)
    if timeout_seconds is None:
        step_timeout = int(step.get("timeout_minutes") or 0) * 60
        timeout_seconds = step_timeout or RUNNER_DEFAULT_TIMEOUT_SECONDS

    outcome, result, status, exit_code = "blocked", "", "failed", None
    stdout, stderr = "", ""
    if not command:
        result = (
            f"no runner command for agent {assignee}; set runner_command on "
            "the team member or disable auto_run"
        )
    else:
        prompt = _build_step_prompt(project_root, task, step, upstream_result)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(_project_root(project_root)),
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
            if exit_code == 0:
                outcome = "done"
                status = "succeeded"
                result = _tail(stdout, 4000) or "runner finished with no output"
            else:
                result = f"runner exited {exit_code}: {_tail(stderr or stdout, 2000)}"
        except subprocess.TimeoutExpired as exc:
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "")
            status = "timeout"
            result = f"runner timed out after {int(timeout_seconds)}s"
        except OSError as exc:
            result = f"runner failed to start: {exc}"
    if run:
        try:
            _write_run_file(run, "stdout", stdout)
            _write_run_file(run, "stderr", stderr)
            if outcome == "done":
                _write_run_file(run, "result", result)
            store.finish_task_run(run["id"], status, exit_code)
        except (InvalidInputError, OSError):
            pass
    try:
        report = advance_workflow_task(
            store, project_root, assignee, task_id, step["id"], outcome, result
        )
    except (InvalidInputError, UnknownAgentError) as exc:
        # e.g. the step was reassigned while the runner worked
        return {"task_id": task_id, "step": step["id"], "error": str(exc)}
    return report


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


def check_workflow_step_timeouts(
    store: Store, project_root: str | None, now: datetime | None = None
) -> list[dict[str, Any]]:
    with _WORKFLOW_ENGINE_LOCK:
        return _check_workflow_step_timeouts_locked(store, project_root, now)


def _check_workflow_step_timeouts_locked(
    store: Store, project_root: str | None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Steps with timeout_minutes > 0 whose latest dispatch is older than the
    timeout get reassigned to the best other member for the role; when nobody
    else is available the hub is notified instead. Each dispatch triggers at
    most one action (the timeout/reassigned transition marks it handled)."""
    now = now or datetime.now(timezone.utc)
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    members = read_team_config(project_root)["members"]
    actions: list[dict[str, Any]] = []
    for task in store.list_tasks(status="all", limit=500):
        if not task.get("workflow_step") or task.get("task_status") == "closed":
            continue
        task_id = task["id"]
        transitions = store.list_task_transitions(task_id)
        for step_id, assignee in _active_step_assignees(transitions).items():
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
            other_members = [m for m in members if m["agent_name"] != assignee]
            new_assignee = _pick_assignee(store, task, step, other_members)
            if new_assignee:
                store.record_task_transition(
                    task_id, step_id, step_id, WORKFLOW_ENGINE_AGENT, "reassigned",
                    f"step timed out after {timeout_minutes}m on {assignee}; "
                    f"reassigned to {new_assignee}",
                )
                _dispatch_step(
                    store, project_root, task, step,
                    _member_named(members, new_assignee) or {"agent_name": new_assignee},
                    "",
                )
                notice = _notify_hub(
                    store, members,
                    f"Task #{task_id} step '{step_id}' timed out on {assignee} "
                    f"after {timeout_minutes}m; reassigned to {new_assignee}.",
                )
                actions.append({
                    "task_id": task_id, "step": step_id, "action": "reassigned",
                    "from": assignee, "to": new_assignee, "notice": notice,
                })
            else:
                store.record_task_transition(
                    task_id, step_id, step_id, WORKFLOW_ENGINE_AGENT, "timeout",
                    f"step timed out after {timeout_minutes}m on {assignee}; "
                    f"no alternative member for role {step['role_id']}",
                )
                notice = _notify_hub(
                    store, members,
                    f"Task #{task_id} step '{step_id}' timed out on {assignee} "
                    f"after {timeout_minutes}m and no other member has role "
                    f"{step['role_id']}. Please intervene (complete_step as hub, "
                    f"or adjust the team).",
                )
                actions.append({
                    "task_id": task_id, "step": step_id, "action": "notified_hub",
                    "from": assignee, "notice": notice,
                })
    return actions


def workflow_task_state(
    store: Store, project_root: str | None, task_id: int
) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise InvalidInputError(f"unknown task: {task_id}")
    transitions = store.list_task_transitions(task_id)
    return {
        "task_id": task_id,
        "status": task.get("task_status") or task.get("status"),
        "active_steps": _active_steps(transitions),
        "transitions": transitions,
    }


def _task_runs_root(project_root: str | None) -> Path:
    return _project_root(project_root) / ".dev_loop" / "tasks"


def _task_run_dir(project_root: str | None, task_id: int, attempt: int) -> Path:
    return _task_runs_root(project_root) / str(task_id) / f"run-{attempt:03d}"


def _task_run_file(run: dict[str, Any], file_key: str) -> Path:
    if file_key not in _TASK_RUN_FILES:
        raise InvalidInputError("unknown run file")
    raw_log_dir = str(run.get("log_dir") or "")
    if not raw_log_dir:
        raise InvalidInputError("run log directory is missing")
    log_dir = Path(raw_log_dir).resolve()
    file_path = (log_dir / _TASK_RUN_FILES[file_key]).resolve()
    if log_dir not in (file_path, *file_path.parents):
        raise InvalidInputError("invalid run file path")
    return file_path


def _append_run_file(run: dict[str, Any], file_key: str, content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        raise InvalidInputError("content must be a string")
    file_path = _task_run_file(run, file_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as file:
        file.write(content)
    return {"file": file_key, "path": str(file_path), "bytes": len(content.encode("utf-8"))}


def _write_run_file(run: dict[str, Any], file_key: str, content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        raise InvalidInputError("content must be a string")
    file_path = _task_run_file(run, file_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {"file": file_key, "path": str(file_path), "bytes": len(content.encode("utf-8"))}


def _read_run_file(run: dict[str, Any], file_key: str, tail: int = 65536) -> dict[str, Any]:
    file_path = _task_run_file(run, file_key)
    if not file_path.exists():
        return {"file": file_key, "path": str(file_path), "content": "", "bytes": 0}
    tail = max(1, min(int(tail), 1024 * 1024))
    file_size = file_path.stat().st_size
    truncated = file_size > tail
    if truncated:
        with file_path.open("rb") as f:
            f.seek(-tail, 2)
            chunk = f.read()
    else:
        chunk = file_path.read_bytes()
    return {
        "file": file_key,
        "path": str(file_path),
        "content": chunk.decode("utf-8", errors="replace"),
        "bytes": file_size,
        "truncated": truncated,
    }


def detect_agent_tools() -> list[dict[str, Any]]:
    """Return detected agent tools, with each Hermes profile as its own agent."""
    tools = []

    for candidate in _AGENT_TOOL_CANDIDATES:
        path = shutil.which(candidate["command"])
        tool = {
            **candidate,
            "installed": path is not None,
            "path": path,
        }
        tools.append(tool)
        if candidate["id"] == "hermes":
            profiles = detect_hermes_profiles()
            used_ids = {"hermes"}
            for profile in profiles:
                profile_name = profile["name"]
                base_id = f"hermes-{_agent_slug(profile_name)}"
                profile_id = base_id
                counter = 2
                while profile_id in used_ids:
                    profile_id = f"{base_id}-{counter}"
                    counter += 1
                used_ids.add(profile_id)
                tools.append(
                    {
                        "id": profile_id,
                        "name": f"Hermes {profile_name}",
                        "command": f"hermes --profile {profile_name}",
                        "agent_name": profile_id,
                        "description": f"Hermes agent CLI profile: {profile_name}",
                        "installed": path is not None,
                        "path": path,
                        "profile_name": profile_name,
                        "profile_path": profile["path"],
                    }
                )
    return tools


def detect_hermes_profiles(profile_root: Path | None = None) -> list[dict[str, str]]:
    root = profile_root or (Path.home() / ".hermes" / "profiles")
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return []
    return [
        {"name": path.name, "path": str(path)}
        for path in children
        if path.is_dir() and not path.name.startswith(".")
    ]


def _packaged_role_templates_dir() -> Path:
    return Path(str(resources.files("dev_loop") / "role_templates"))


def _agents_dir(project_root: str | None) -> Path:
    # Prefer the project's own roles, then the server cwd's agents/, and as
    # a last resort the templates bundled in the package — so a fresh project
    # that never ran `dev-loop init` still gets the default role set.
    root = _project_root(project_root) / "agents"
    if root.is_dir():
        return root
    cwd_agents = Path.cwd() / "agents"
    if cwd_agents.is_dir():
        return cwd_agents
    return _packaged_role_templates_dir()


def _materialize_role_templates(agents_dir: Path) -> None:
    """Copy the bundled role set into a project's agents/ dir. Used before
    the first role edit so overriding one role doesn't hide the others."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    templates = _packaged_role_templates_dir()
    if not templates.is_dir():
        return
    for entry in templates.iterdir():
        if entry.suffix != ".md":
            continue
        dest = agents_dir / entry.name
        if not dest.exists():
            dest.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")


def list_agent_roles(agents_dir: Path | None = None) -> list[dict[str, str]]:
    root = agents_dir or (Path.cwd() / "agents")
    try:
        files = sorted(root.glob("*.md"), key=lambda path: path.stem)
    except OSError:
        return []
    roles = []
    for path in files:
        if not _is_valid_role_id(path.stem):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = next(
            (line.lstrip("#").strip() for line in content.splitlines() if line.startswith("#")),
            path.stem,
        )
        roles.append(
            {
                "id": path.stem,
                "name": title,
                "path": str(path),
                "content": content,
            }
        )
    return roles


def create_server(
    host: str = "127.0.0.1",
    port: int = 8848,
    db_path: str | None = None,
    project: dict[str, Any] | None = None,
) -> FastMCP:
    store = Store(db_path)
    # mcp.run() blocks until the process dies (Ctrl-C included), so a plain
    # atexit hook is the reliable place to checkpoint the WAL and close the
    # connection cleanly.
    atexit.register(store.close)
    reaped = store.reap_stale_runs()
    if reaped:
        print(f"note: marked {reaped} stale running task_run(s) as orphaned", flush=True)

    # Step-timeout watchdog: the engine is otherwise purely event-driven, so
    # a dead assignee would leave its step active forever. Daemon thread dies
    # with the process; check errors must never kill the watcher.
    def _timeout_watcher() -> None:
        while True:
            time.sleep(WORKFLOW_TIMEOUT_POLL_SECONDS)
            try:
                check_workflow_step_timeouts(
                    store, current_project.get("project_root")
                )
            except Exception:
                pass

    threading.Thread(
        target=_timeout_watcher, name="workflow-timeout-watcher", daemon=True
    ).start()

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

    mcp = FastMCP(
        "dev-loop",
        instructions=(
            "dev-loop is a local mailbox that lets LLM CLIs and agents pass prompts "
            "to each other. Protocol: 1) call register_agent once with a stable name "
            "(e.g. 'claude-code', 'codex', 'gemini'); 2) use send_message to hand a "
            "prompt to another agent; 3) call check_inbox periodically (use "
            "wait_seconds=30 for near-real-time delivery) to receive prompts sent to "
            "you; 4) after handling a received message, call ack_message so it is "
            "not redelivered; 5) reply with send_message using reply_to so "
            "conversations stay threaded. Hub/orchestrator agents receiving from many agents: run ONE "
            "polling loop only, and process each returned message in id order before "
            "polling again — do not run multiple concurrent check_inbox loops for "
            "the same agent name."
        ),
        host=host,
        port=port,
        stateless_http=True,
    )

    async def _deliver(
        sender: str,
        to: str,
        content: str,
        reply_to: int | None,
        kind: str,
        title: str,
        task_status: str,
    ) -> dict:
        """Shared delivery path for the MCP tool and the HTTP API."""
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

    @mcp.tool()
    async def register_agent(name: str, description: str = "") -> dict:
        """Register yourself (or refresh your registration) in the dev-loop agent
        registry. Call this once at the start of a session with a short stable name
        such as 'claude-code', 'codex', or 'gemini', and a one-line description of
        what you are working on. Returns the full list of registered agents so you
        can see who else is available to message."""
        try:
            agents = await _to_thread(store.register_agent, name, description)
        except InvalidInputError as exc:
            return {"registered": None, "agents": [], "error": str(exc)}
        return {"registered": name.strip(), "agents": agents}

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List all registered agents with their descriptions and last-seen
        timestamps. Use this to discover which agents you can send prompts to."""
        return await _to_thread(store.list_agents)

    @mcp.tool()
    async def send_message(
        sender: str,
        to: str,
        content: str,
        reply_to: int | None = None,
        kind: str = "message",
        title: str = "",
        task_status: str = "",
    ) -> dict:
        """Send a prompt/message to another agent's inbox.

        - sender: your own registered agent name.
        - to: the recipient's agent name, or "*" to broadcast to every registered
          agent except yourself.
        - content: the prompt or message text to deliver.
        - reply_to: optional id of the message you are replying to; set it so the
          recipient can reconstruct the conversation with get_thread.
        - kind: "message" or "task"; programming delegation should use "task".
        - title: optional short task title.
        - task_status: optional task status; defaults to "created" for tasks.
          Invalid values are rejected with an error.

        The message is stored durably and delivered when the recipient next calls
        check_inbox. Returns the created message id(s)."""
        return await _deliver(sender, to, content, reply_to, kind, title, task_status)

    @mcp.tool()
    async def check_inbox(
        agent: str,
        wait_seconds: int = 0,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict:
        """Fetch unread messages addressed to you and lease them for processing.

        - agent: your own registered agent name.
        - wait_seconds: 0 returns immediately; a positive value (max 60,
          recommended 30) long-polls, returning as soon as a message arrives.
        - lease_seconds: how long fetched messages are hidden from other polls
          before being redelivered if not acked; default 300, max 3600.

        Call this periodically while working so prompts from other agents reach
        you. Each message includes its id — pass it as reply_to in send_message
        when responding. After processing each message, call ack_message with
        that id so it will not be delivered again.

        If you are a hub/orchestrator receiving replies from many agents: use a
        single polling loop (never multiple concurrent pollers on the same agent
        name), and when a batch arrives, handle every message one at a time in
        ascending id order before calling check_inbox again."""
        await _to_thread(store.touch_agent, agent)
        wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
        lease = max(1, min(int(lease_seconds), MAX_LEASE_SECONDS))
        deadline = anyio.current_time() + wait
        while True:
            try:
                messages = await _to_thread(store.fetch_unread, agent, lease)
            except UnknownAgentError as exc:
                return {"agent": agent, "count": 0, "messages": [], "error": str(exc)}
            if messages or anyio.current_time() >= deadline:
                return {"agent": agent, "count": len(messages), "messages": messages}
            await anyio.sleep(POLL_INTERVAL)

    @mcp.tool()
    async def ack_message(agent: str, message_id: int, lease_token: str) -> dict:
        """Acknowledge that you finished handling a received message.

        Once acked, the message will not be returned by check_inbox again.
        If a leased message is not acked before its lease expires, it becomes
        available for redelivery.

        lease_token must be copied from the message returned by check_inbox; it
        prevents an expired or unrelated lease from acking a newly redelivered
        message."""
        await _to_thread(store.touch_agent, agent)
        try:
            acked = await _to_thread(store.ack_message, agent, message_id, lease_token)
        except UnknownAgentError as exc:
            return {"acked": False, "message_id": message_id, "error": str(exc)}
        return {"acked": acked, "message_id": message_id}

    # NOTE: the local function must not reuse the module-level engine
    # function's name — it would shadow it inside this closure and break
    # _engine_start with a TypeError on argument count. The MCP tool name
    # stays "start_workflow_task" via the decorator.
    @mcp.tool(name="start_workflow_task")
    async def start_workflow_task_tool(agent: str, task_id: int) -> dict:
        """Enter an existing task (created via send_message kind="task") into
        the configured workflow. The engine dispatches the entry step(s) to the
        best-ranked team member for each step's role; from then on the task
        moves only through complete_step. Fails if the task is already in the
        workflow."""
        await _to_thread(store.touch_agent, agent)
        try:
            return await _to_thread(
                _engine_start, agent, task_id
            )
        except (InvalidInputError, UnknownAgentError) as exc:
            return {"task_id": task_id, "started": False, "error": str(exc)}

    @mcp.tool()
    async def complete_step(
        agent: str,
        task_id: int,
        step: str,
        outcome: str = "done",
        result: str = "",
    ) -> dict:
        """Report the outcome of a workflow step you were dispatched.

        - agent: your registered agent name. It must be the assignee that was
          dispatched the active step; hub may complete active steps for
          recovery.
        - task_id / step: from the dispatch message you received.
        - outcome: "done" advances along forward connections (merge steps wait
          for all required branches); "rework" sends the task back along the
          loop-back connection (e.g. review -> implement); "blocked" pauses the
          task and notifies the hub — use it when you cannot decide and need
          confirmation, putting your question in result.
        - result: summary of what you produced (file paths, conclusions). It is
          forwarded to the next step's assignee.

        Completing a terminal step closes the task."""
        await _to_thread(store.touch_agent, agent)
        try:
            return await _to_thread(
                _engine_advance, agent, task_id, step, outcome, result
            )
        except (InvalidInputError, UnknownAgentError) as exc:
            return {"task_id": task_id, "step": step, "error": str(exc)}

    @mcp.tool()
    async def get_workflow_task_state(task_id: int) -> dict:
        """Show where a task currently is in the workflow: its status, the
        steps being worked on right now, and the full transition history
        (dispatch/done/rework/skipped/blocked records)."""
        try:
            return await _to_thread(_engine_state, task_id)
        except InvalidInputError as exc:
            return {"task_id": task_id, "error": str(exc)}

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

    @mcp.tool()
    async def get_thread(message_id: int) -> list[dict]:
        """Return the full conversation thread containing the given message id:
        the reply_to chain back to the root plus all replies, ordered oldest
        first. Use this to recover context before answering a message that is
        part of an ongoing exchange."""
        return await _to_thread(store.get_thread, message_id)

    @mcp.custom_route("/", methods=["GET"])
    async def index(_: Request) -> RedirectResponse:
        return RedirectResponse("/ui")

    @mcp.custom_route("/ui", methods=["GET"])
    async def ui(request: Request) -> HTMLResponse | JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return HTMLResponse(_UI_HTML)

    @mcp.custom_route("/api/{path:path}", methods=["OPTIONS"])
    async def api_options(request: Request) -> Response:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return Response(status_code=204, headers=_cors_headers(request))

    @mcp.custom_route("/api/agents", methods=["GET"])
    async def api_list_agents(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents = await _to_thread(store.list_agents)
        return _json(request, {"agents": agents})

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return _json(
            request,
            {
                "db_path": str(store.db_path),
                "project": {**current_project, "db_path": str(store.db_path)},
            },
        )

    @mcp.custom_route("/api/projects", methods=["GET"])
    async def api_projects(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        projects = await _to_thread(list_projects, current_project.get("id"))
        if current_project.get("id") and not any(
            project.get("id") == current_project.get("id") for project in projects
        ):
            projects.insert(0, {**current_project, "current": True, "online": True})
        return _json(
            request,
            {
                "current_project_id": current_project.get("id"),
                "projects": projects,
            },
        )

    @mcp.custom_route("/api/agent-tools", methods=["GET"])
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
        return _json(request, {"tools": tools})

    @mcp.custom_route("/api/agent-roles", methods=["GET"])
    async def api_agent_roles(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents_dir = _agents_dir(current_project.get("project_root"))
        roles = await _to_thread(list_agent_roles, agents_dir)
        return _json(request, {"roles": roles})

    @mcp.custom_route("/api/agent-roles/{role_id}", methods=["POST"])
    async def api_save_agent_role(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        role_id = request.path_params.get("role_id")
        if not role_id or not _is_valid_role_id(role_id):
            return _json_error("Invalid role ID", request=request)
        data = await _read_json(request)
        try:
            content = _validate_role_content(data.get("content"))
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        project_root = current_project.get("project_root")
        agents_dir = _agents_dir(project_root)
        if agents_dir == _packaged_role_templates_dir():
            # Never write into the installed package: materialize the full
            # bundled role set into the project and edit that copy, so
            # overriding one role doesn't hide the rest.
            agents_dir = _project_root(project_root) / "agents"
            await _to_thread(_materialize_role_templates, agents_dir)
        if not agents_dir.is_dir():
            return _json_error("Agents directory not found", request=request)
        file_path = (agents_dir / f"{role_id}.md").resolve()
        if not str(file_path).startswith(str(agents_dir.resolve())):
            return _json_error("Access denied", request=request)
        def _write_role():
            file_path.write_text(content, encoding="utf-8")
        await _to_thread(_write_role)
        roles = await _to_thread(list_agent_roles, agents_dir)
        return _json(request, {"success": True, "roles": roles})

    @mcp.custom_route("/api/team", methods=["GET"])
    async def api_get_team(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        try:
            team = await _to_thread(
                read_team_config, current_project.get("project_root")
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, team)

    @mcp.custom_route("/api/team", methods=["POST"])
    async def api_save_team(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        locked = await _to_thread(team_locked_reason, store)
        if locked:
            return _json_error(locked, 409, request)
        try:
            team = await _to_thread(
                write_team_config,
                data.get("members", []),
                current_project.get("project_root"),
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"success": True, **team})

    @mcp.custom_route("/api/workflow", methods=["GET"])
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

    @mcp.custom_route("/api/workflow", methods=["POST"])
    async def api_save_workflow(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        try:
            workflow = await _to_thread(
                write_workflow_config,
                data.get("steps", []),
                current_project.get("project_root"),
                data.get("edges"),
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"success": True, **workflow})

    @mcp.custom_route("/api/agents", methods=["POST"])
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

    @mcp.custom_route("/api/messages", methods=["GET"])
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

    @mcp.custom_route("/api/tasks", methods=["GET"])
    async def api_list_tasks(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        params = request.query_params
        status = params.get("status", "all")
        assignee = params.get("assignee") or None
        try:
            limit = _parse_int(params.get("limit", "200"), "limit")
            tasks = await _to_thread(store.list_tasks, status, assignee, limit)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"tasks": tasks})

    @mcp.custom_route("/api/messages", methods=["POST"])
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

    @mcp.custom_route("/api/messages/{message_id:int}/task-status", methods=["POST"])
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

    @mcp.custom_route("/api/tasks/{task_id:int}/status", methods=["POST"])
    async def api_update_task_item_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        task_id = int(request.path_params["task_id"])
        task_status = str(data.get("task_status", "")).strip()
        if not task_status:
            return _json_error("task_status is required", request=request)
        try:
            updated = await _to_thread(
                store.update_task_item_status, task_id, task_status
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(
            request,
            {"updated": updated, "task_id": task_id, "task_status": task_status},
        )

    @mcp.custom_route("/api/tasks/{task_id:int}/metadata", methods=["POST"])
    async def api_update_task_metadata(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        task_id = int(request.path_params["task_id"])
        try:
            task = await _to_thread(
                store.update_task_metadata,
                task_id,
                data.get("role_required"),
                data.get("importance"),
                data.get("size"),
                data.get("risk"),
                data.get("required_capabilities"),
                data.get("exclusive_workspace"),
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        if task is None:
            return _json_error("task not found", 404, request)
        return _json(request, {"task": task})

    @mcp.custom_route("/api/tasks/{task_id:int}/assignment-candidates", methods=["GET"])
    async def api_task_assignment_candidates(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        task = await _to_thread(store.get_task, task_id)
        if task is None:
            return _json_error("task not found", 404, request)
        try:
            team = await _to_thread(read_team_config, current_project.get("project_root"))
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        active_counts = await _to_thread(store.active_task_counts)
        ranked = rank_assignment_candidates(
            task,
            team["members"],
            active_counts,
            request.query_params.get("role") or None,
        )
        return _json(request, {"task": task, **ranked})

    @mcp.custom_route("/api/tasks/{task_id:int}/workflow", methods=["GET"])
    async def api_task_workflow_state(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        task_id = int(request.path_params["task_id"])
        try:
            state = await _to_thread(_engine_state, task_id)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, state)

    @mcp.custom_route("/api/tasks/{task_id:int}/workflow/start", methods=["POST"])
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

    @mcp.custom_route("/api/tasks/{task_id:int}/workflow/complete", methods=["POST"])
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

    @mcp.custom_route("/api/tasks/{task_id:int}/runs", methods=["GET"])
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

    @mcp.custom_route("/api/tasks/{task_id:int}/runs", methods=["POST"])
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

    @mcp.custom_route("/api/task-runs/{run_id:int}/events", methods=["POST"])
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

    @mcp.custom_route("/api/task-runs/{run_id:int}/logs", methods=["POST"])
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

    @mcp.custom_route("/api/task-runs/{run_id:int}/result", methods=["POST"])
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

    @mcp.custom_route("/api/task-runs/{run_id:int}/files/{file_key}", methods=["GET"])
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

    @mcp.custom_route("/api/inbox/check", methods=["POST"])
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

    @mcp.custom_route("/api/messages/{message_id:int}/ack", methods=["POST"])
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

    @mcp.custom_route("/api/thread/{message_id:int}", methods=["GET"])
    async def api_get_thread(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        message_id = int(request.path_params["message_id"])
        thread = await _to_thread(store.get_thread, message_id)
        return _json(request, {"messages": thread})

    return mcp
