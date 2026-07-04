"""FastMCP server exposing the dev_loop mailbox tools."""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
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
        return ipaddress.ip_address(client.host).is_loopback
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
        ("ui_design", "UI Design", "ui_designer", "assigned", False, 700, 40),
        ("architecture", "Architecture", "architect", "assigned", True, 700, 300),
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


def read_workflow_config(project_root: str | None = None) -> dict[str, Any]:
    path = _workflow_config_path(project_root)
    if not path.exists():
        return {
            "steps": default_workflow_steps(),
            "edges": default_workflow_edges(),
            "path": str(path),
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
    return {"steps": normalized, "edges": edges, "path": str(path)}


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
    return {"steps": normalized, "edges": normalized_edges, "path": str(path)}


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
    data = file_path.read_bytes()
    truncated = len(data) > tail
    chunk = data[-tail:] if truncated else data
    return {
        "file": file_key,
        "path": str(file_path),
        "content": chunk.decode("utf-8", errors="replace"),
        "bytes": len(data),
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
        project_root = current_project.get("project_root")
        agents_dir = Path(project_root) / "agents" if project_root else Path.cwd() / "agents"
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
        agents_dir = Path(project_root) / "agents" if project_root else Path.cwd() / "agents"
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
