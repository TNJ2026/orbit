"""Starlette server hosting the orbit Web UI, HTTP API, and workflow engine."""

from __future__ import annotations

import atexit
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__
from .project_index import list_projects
from .store import (
    DEFAULT_LEASE_SECONDS,
    InvalidInputError,
    Store,
    TASK_STATUSES,
    UnknownAgentError,
    project_state_dir,
)

MAX_LEASE_SECONDS = 3600
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
    {
        "id": "opencode",
        "name": "OpenCode",
        "command": "opencode",
        "agent_name": "opencode",
        "description": "OpenCode CLI (opencode.ai)",
    },
]
_TASK_RUN_FILES = {
    "events": "events.jsonl",
    "prompt": "prompt.txt",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "result": "result.md",
    "diff": "diff.patch",
}
# Roles a sound default team should provide. The workflow itself remains
# configurable; write_workflow_config only enforces the older core workflow roles
# below so custom workflows without an integrate step stay valid.
REQUIRED_TEAM_ROLES = {"hub", "implementer", "integrator", "reviewer"}
CORE_WORKFLOW_ROLES = {"hub", "implementer", "reviewer"}
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
    return project_state_dir(_project_root(project_root)) / "team.json"


def _workflow_config_path(project_root: str | None) -> Path:
    return project_state_dir(_project_root(project_root)) / "workflow.json"


def _settings_config_path(project_root: str | None) -> Path:
    return project_state_dir(_project_root(project_root)) / "settings.json"


# Project settings editable from the UI Settings page. Each is clamped to its
# range on read and write, so a hand-edited file can never push the engine
# out of bounds.
MAX_REWORK_MIN, MAX_REWORK_MAX, _DEFAULT_MAX_REWORK = 2, 5, 3
MAX_CONCURRENT_MIN, MAX_CONCURRENT_MAX, _DEFAULT_MAX_CONCURRENT = 1, 6, 5


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def read_settings(project_root: str | None = None) -> dict[str, Any]:
    path = _settings_config_path(project_root)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "max_rework_rounds": _clamp_int(
            data.get("max_rework_rounds"), MAX_REWORK_MIN, MAX_REWORK_MAX, _DEFAULT_MAX_REWORK
        ),
        "max_concurrent_tasks": _clamp_int(
            data.get("max_concurrent_tasks"), MAX_CONCURRENT_MIN, MAX_CONCURRENT_MAX, _DEFAULT_MAX_CONCURRENT
        ),
        "path": str(path),
    }


def write_settings(
    project_root: str | None = None,
    max_rework_rounds: Any = None,
    max_concurrent_tasks: Any = None,
) -> dict[str, Any]:
    current = read_settings(project_root)
    rework = (
        current["max_rework_rounds"] if max_rework_rounds is None
        else _clamp_int(max_rework_rounds, MAX_REWORK_MIN, MAX_REWORK_MAX, current["max_rework_rounds"])
    )
    concurrent = (
        current["max_concurrent_tasks"] if max_concurrent_tasks is None
        else _clamp_int(max_concurrent_tasks, MAX_CONCURRENT_MIN, MAX_CONCURRENT_MAX, current["max_concurrent_tasks"])
    )
    path = _settings_config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"max_rework_rounds": rework, "max_concurrent_tasks": concurrent}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {**data, "path": str(path)}


# Default canvas layout. Nodes carry explicit x/y because the flow is not a
# simple row: product design fans out to two parallel branches (UI design and
# architecture) that merge back into implementation, and review loops back to
# implementation on rework.
_DEFAULT_STEP_MID_Y = 160
_WORKFLOW_STATUS_LABELS = {
    "created": "Todo",
    "assigned": "Assigned",
    "in_progress": "In Progress",
    "blocked": "Blocked",
    "closed": "Done",
}


def default_workflow_statuses() -> list[dict[str, str]]:
    return [
        {"value": value, "label": _WORKFLOW_STATUS_LABELS[value]}
        for value in (
            "created",
            "assigned",
            "in_progress",
            "blocked",
            "closed",
        )
    ]


def default_workflow_steps() -> list[dict[str, Any]]:
    # (id, name, role_id, required, isolate, integrate, decompose, x, y)
    # isolate: run in a per-task git worktree (implement/test/review all share
    # one worktree per task, so review reads exactly what implement produced).
    # integrate: terminal single-assignee gate that merges the task's worktree
    # branch into main, verifies it, and checks the task's acceptance criteria;
    # runs in project_root, serialized by the main tree.
    # decompose: design-first — the goal itself runs intake + the product/UI/
    # architecture design once, then `decompose` (hub) splits it into implementation
    # subtasks partitioned by the architecture's modules. Each subtask starts at
    # `implement`, so the design steps run per goal, not per subtask.
    specs = [
        # Fully linear forward chain (design runs sequentially: UI then arch), so
        # every card sits on one row; rework edges loop back underneath.
        ("intake", "Triage", "hub", True, False, False, False, 40, _DEFAULT_STEP_MID_Y),
        ("product_design", "Product Design", "product_designer", False, False, False, False, 340, _DEFAULT_STEP_MID_Y),
        ("ui_design", "UI Design", "ui_designer", False, False, False, False, 640, _DEFAULT_STEP_MID_Y),
        ("architecture", "Architecture", "architect", False, False, False, False, 940, _DEFAULT_STEP_MID_Y),
        # decompose: the decomposition gate. Architecture feeds it, and hub splits the
        # goal into subtasks that begin at implement.
        ("decompose", "Decompose", "hub", True, False, False, True, 1240, _DEFAULT_STEP_MID_Y),
        ("implement", "Implement", "implementer", True, True, False, False, 1540, _DEFAULT_STEP_MID_Y),
        # review runs before test: a human/agent review first, then test is the
        # mandatory machine-verification gate. Set test's `verify` command (e.g.
        # the project's test suite) so a failing run objectively sends the task
        # back to implement instead of trusting a self-report.
        ("review", "Review", "reviewer", True, True, False, False, 1840, _DEFAULT_STEP_MID_Y),
        ("test", "Test", "tester", False, True, False, False, 2140, _DEFAULT_STEP_MID_Y),
        ("integrate", "Integrate", "integrator", True, False, True, False, 2440, _DEFAULT_STEP_MID_Y),
    ]
    return [
        {
            "id": step_id,
            "name": name,
            "role_id": role_id,
            "required": required,
            "isolate": isolate,
            "integrate": integrate,
            "decompose": decompose,
            "x": x,
            "y": y,
        }
        for step_id, name, role_id, required, isolate, integrate, decompose, x, y in specs
    ]


def default_workflow_edges() -> list[dict[str, str]]:
    # Design-first flow: the goal runs intake + design, then splits at `decompose`.
    #   sequential: product_design -> ui_design -> architecture (UI before arch)
    #   split    : decompose -> implement — subtasks begin here, one per module
    #   loop-back: review / test / integrate return to implement on rework
    return [
        {"from": "intake", "to": "product_design"},
        {"from": "product_design", "to": "ui_design"},      # design chain
        {"from": "ui_design", "to": "architecture"},        # UI first, then arch
        {"from": "architecture", "to": "decompose"},        # feeds the decompose step
        {"from": "decompose", "to": "implement"},           # split: subtasks start here
        {"from": "implement", "to": "review"},              # review first
        {"from": "review", "to": "test"},                   # then the verify gate
        {"from": "review", "to": "implement"},              # loop-back (rework)
        {"from": "test", "to": "integrate"},                # merge worktree branch to main
        {"from": "test", "to": "implement"},                # verify failed -> rework
        {"from": "integrate", "to": "implement"},           # loop-back (merge conflict -> rework)
    ]


def _normalize_workflow_step(
    step: Any,
    index: int,
) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise InvalidInputError("workflow step must be an object")
    step_id = _agent_slug(str(step.get("id", "") or f"step-{index + 1}"))
    name = str(step.get("name", "") or step_id).strip()
    role_id = str(step.get("role_id", "")).strip()
    if not name:
        raise InvalidInputError("workflow step name is required")

    def _coord(key: str, default: float) -> float:
        raw = step.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise InvalidInputError(f"workflow step {key} must be a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidInputError(f"workflow step {key} must be finite")
        return max(0.0, round(value, 2))

    if not _is_valid_role_id(role_id):
        raise InvalidInputError("workflow step role_id is invalid")
    try:
        timeout_minutes = int(step.get("timeout_minutes", 0) or 0)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow step timeout_minutes must be an integer") from None
    if timeout_minutes < 0:
        raise InvalidInputError("workflow step timeout_minutes must be >= 0")
    raw_prompt = step.get("prompt", "")
    if not isinstance(raw_prompt, str):
        raise InvalidInputError("workflow step prompt must be a string")
    # Core-role steps are always required; so is any integrate step — it merges
    # the worktree branch back to main, so skipping it would strand every
    # isolated step's commits on their branch. Lock it by the integrate flag
    # itself, not just its (currently hub) role, so re-assigning the role can't
    # silently make it optional.
    decompose = bool(step.get("decompose", False))
    required_locked = (
        role_id in REQUIRED_TEAM_ROLES
        or bool(step.get("integrate", False))
        or decompose
    )
    required = True if required_locked else bool(step.get("required", False))

    return {
        "id": step_id,
        "name": name,
        "role_id": role_id,
        "required": required,
        "required_locked": required_locked,
        "timeout_minutes": timeout_minutes,
        # isolate: run this step in a per-task git worktree so concurrent
        # implementers of different tasks never share a working tree.
        # integrate: this step merges the task's worktree branch back into the
        # main tree, so it runs in project_root (never isolated).
        # decompose: at this step a root goal splits into business subtasks; it
        # runs at goal level in project_root (never isolated), and the subtasks
        # begin at its forward successors — so goal-level design steps before it
        # happen once, not per subtask.
        "isolate": bool(step.get("isolate", False))
        and not bool(step.get("integrate", False))
        and not decompose,
        "integrate": bool(step.get("integrate", False)),
        "decompose": decompose,
        # User-authored instructions for this step. They refine the generated
        # step contract but never replace the engine-owned output protocol.
        "prompt": raw_prompt.strip(),
        # verify: an objective shell command the engine runs itself after the
        # agent, in the same working tree. Its real exit code overrides the
        # agent's self-reported `done` (a failing gate the agent can't fake).
        "verify": str(step.get("verify", "") or "").strip(),
        "x": _coord("x", 40 + index * 300),
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
        norm_edge = {"from": src, "to": dst}
        if edge.get("rework"):
            # Explicit loop-back marker: lets a rework target sit off the
            # forward path (see _workflow_graph).
            norm_edge["rework"] = True
        normalized.append(norm_edge)
    return normalized


# Structural problems are warnings, not errors: the canvas saves after every
# drag/add, so a half-connected graph is a normal intermediate state.
def _workflow_graph_warnings(
    steps: list[dict[str, Any]], edges: list[dict[str, str]]
) -> list[str]:
    warnings: list[str] = []
    # git prerequisite for isolation/integration. The engine auto-inits a repo at
    # flow start when git is installed (see _ensure_git_repo), so a non-git dir is
    # NOT worth warning about — only a missing git binary is unrecoverable: those
    # steps then run without a worktree and integrate is skipped.
    if any(s.get("isolate") or s.get("integrate") for s in steps) and not _git_available():
        warnings.append(
            "git is not installed: isolate steps will run without a per-task "
            "worktree and the integrate step will be skipped"
        )
    # A decompose step is where a goal splits into subtasks; the subtasks begin at
    # its forward successors, so it must have an outgoing step, and only one such
    # step is used.
    decompose_ids = [s["id"] for s in steps if s.get("decompose")]
    if len(decompose_ids) > 1:
        warnings.append(
            "multiple decompose steps: only the first (" + decompose_ids[0]
            + ") splits the goal"
        )
    if decompose_ids and not any(e["from"] == decompose_ids[0] for e in edges):
        warnings.append(
            f"decompose step '{decompose_ids[0]}' has no outgoing step: its "
            "subtasks would have nowhere to start"
        )
    ids = [step["id"] for step in steps]
    if len(ids) <= 1:
        return warnings
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
        statuses = default_workflow_statuses()
        return {
            "steps": [
                _normalize_workflow_step(step, index)
                for index, step in enumerate(default_workflow_steps())
            ],
            "statuses": statuses,
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
    statuses = default_workflow_statuses()
    normalized = [
        _normalize_workflow_step(step, index)
        for index, step in enumerate(steps)
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
        "statuses": statuses,
        "edges": edges,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, edges),
    }


def _coerce_token_budget(value: Any) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return 0
    return budget if budget > 0 else 0


# Node box the UI paints (matches WF_NODE_W in static/ui.html). Steps closer
# than one box + a gap on the same visual row overlap on the canvas.
_WF_NODE_WIDTH = 200.0
_WF_MIN_STEP_DX = _WF_NODE_WIDTH + 40.0  # min horizontal center gap in a row
_WF_ROW_TOLERANCE = 80.0                 # nodes within this dy count as one row


def _separate_overlapping_steps(steps: list[dict[str, Any]]) -> None:
    """Nudge steps apart in place so no two boxes overlap on the same canvas row.

    The UI's dagre layout already spaces nodes, but a config written another way
    (a hand-edited file, an API POST, a programmatic edit) can place two nodes on
    top of each other — the node then hides the short edge to its neighbour and
    looks disconnected. Only same-row (close dy) nodes that are too close in x are
    pushed right, so intentional parallel stacks (branches at different y) and
    already-spaced layouts are left untouched. Deterministic: nodes are settled
    left-to-right, each shifted just past any earlier node it would overlap."""
    ordered = sorted(
        steps, key=lambda s: (float(s.get("x", 0.0)), float(s.get("y", 0.0)), s["id"])
    )
    placed: list[dict[str, Any]] = []
    for s in ordered:
        # Re-check after each shift: moving right can bring a further node in range.
        for _ in range(len(placed) + 1):
            moved = False
            for p in placed:
                same_row = abs(float(s["y"]) - float(p["y"])) < _WF_ROW_TOLERANCE
                dx = float(s["x"]) - float(p["x"])
                if same_row and 0.0 <= dx < _WF_MIN_STEP_DX:
                    s["x"] = round(float(p["x"]) + _WF_MIN_STEP_DX, 2)
                    moved = True
            if not moved:
                break
        placed.append(s)


def write_workflow_config(
    steps: list[Any],
    project_root: str | None = None,
    edges: Any = None,
) -> dict[str, Any]:
    if not isinstance(steps, list):
        raise InvalidInputError("steps must be a list")
    normalized_statuses = default_workflow_statuses()
    normalized = [
        _normalize_workflow_step(step, index)
        for index, step in enumerate(steps)
    ]
    if not normalized:
        raise InvalidInputError("workflow must include at least one step")
    valid_ids = {step["id"] for step in normalized}
    if len(valid_ids) != len(normalized):
        raise InvalidInputError("workflow step ids must be unique")
    # The UI hides Remove on core-role cards; enforce the same rule here so
    # a raw POST can't save a workflow with the core loop deleted. Reads stay
    # lenient so legacy configs still load.
    missing_core = sorted(
        CORE_WORKFLOW_ROLES - {step["role_id"] for step in normalized}
    )
    if missing_core:
        raise InvalidInputError(
            "workflow must keep steps for core roles: " + ", ".join(missing_core)
        )
    _reject_unknown_roles({step["role_id"] for step in normalized if step["role_id"]}, project_root)
    # Fallback for configs written outside the UI (hand edit / API / script): keep
    # nodes from stacking on the canvas, where a covered edge reads as "not
    # connected". A UI save has already dagre-spaced them, so this is a no-op there.
    _separate_overlapping_steps(normalized)
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
        "statuses": normalized_statuses,
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
    enabled = member.get("enabled", True)
    if isinstance(enabled, bool):
        enabled_value = enabled
    elif isinstance(enabled, str):
        lowered = enabled.strip().lower()
        if lowered not in {"true", "false"}:
            raise InvalidInputError("enabled must be a boolean")
        enabled_value = lowered == "true"
    else:
        raise InvalidInputError("enabled must be a boolean")
    return {
        "agent_name": agent_name,
        "role_id": role_id,
        "enabled": enabled_value,
        "expertise_level": max(1, min(expertise_level, 5)),
        # 0 (or negative) means unlimited concurrency; a positive value is a
        # hard cap with no upper ceiling.
        "max_concurrent_tasks": max(0, max_concurrent_tasks),
        "capabilities": [capability.strip() for capability in capabilities if capability.strip()],
        "notes": str(member.get("notes", "")).strip(),
        # Members with a runner command/default auto-run dispatched steps.
        # Empty means the step waits for a live session/manual completion.
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
            "tokens_total": store.sum_goal_tokens(task["id"]),
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


def team_locked_reason(store: Store) -> str | None:
    """Team config is frozen while any task is actively executing workflow
    steps: reassignment math, role constraints, and running auto-runners all
    read the team live, so edits mid-flight corrupt routing. Blocked tasks
    do NOT lock — fixing the team is the documented way to unblock them."""
    busy = _active_workflow_task_ids(store)
    if not busy:
        return None
    ids = ", ".join(f"#{task_id}" for task_id in busy[:10])
    return (
        f"team config is locked while workflow tasks are running ({ids}); "
        "wait for them to finish, or block/close them first"
    )


def workflow_locked_reason(store: Store) -> str | None:
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
        # max_concurrent <= 0 means unlimited: never hard-exclude on load
        # (load_penalty below still soft-prefers idle members).
        if max_concurrent > 0 and active_count >= max_concurrent:
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
# How many times a loop-back (rework) target may be re-entered before the engine
# stops looping and blocks the task for the hub. Prevents review/implement
# rework from spinning forever when feedback is not being resolved.
MAX_REWORK_ROUNDS = 3


def _workflow_graph(cfg: dict[str, Any]) -> set[tuple[str, str]]:
    """Return the set of loop-back (rework) edges. If any edge is explicitly
    marked `"rework": true`, those are the loop-backs verbatim. Otherwise
    loop-backs are inferred via DFS (an edge into a node still on the DFS stack
    closes a cycle); every other edge is forward flow."""
    explicit = {
        (edge["from"], edge["to"]) for edge in cfg["edges"] if edge.get("rework")
    }
    if explicit:
        return explicit
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
    # An entry step has no forward incoming edge (a back edge into it, e.g. an
    # accept -> intake reopen, still leaves it an entry). But an explicit rework
    # target is never an entry, even though its only incoming edges are loop-backs.
    forward_in = {
        e["to"] for e in cfg["edges"] if (e["from"], e["to"]) not in back
    }
    rework_targets = {e["to"] for e in cfg["edges"] if e.get("rework")}
    return [
        s["id"] for s in cfg["steps"]
        if s["id"] not in forward_in and s["id"] not in rework_targets
    ]


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

    def _reach(seeds: list[str], reverse: bool = False, include_back: bool = False) -> set[str]:
        graph: dict[str, list[str]] = {}
        for edge in cfg["edges"]:
            if not include_back and (edge["from"], edge["to"]) in back:
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
    for step in cfg["steps"]:
        if step.get("decompose") and not _forward_out(cfg, back, step["id"]):
            errors.append(f"decompose step '{step['id']}' has no forward successor")
    main_entry = entries[0] if entries else None
    # Reachability includes rework edges, so an explicitly rework-only step is
    # still reachable, not dead.
    reachable = _reach([main_entry], include_back=True) if main_entry else set()
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


def _main_workflow_reachable_steps(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    entries = _workflow_entry_steps(cfg, back)
    if not entries:
        return []
    graph: dict[str, list[str]] = {}
    for edge in cfg["edges"]:
        if (edge["from"], edge["to"]) in back:
            continue
        graph.setdefault(edge["from"], []).append(edge["to"])
    seen: set[str] = set()
    stack = [entries[0]]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return [step for step in cfg["steps"] if step["id"] in seen]


def _required_workflow_roles(cfg: dict[str, Any], back: set[tuple[str, str]]) -> set[str]:
    return {
        step["role_id"]
        for step in _main_workflow_reachable_steps(cfg, back)
        if step.get("required") and step.get("role_id")
    }


def _team_role_of(members: list[dict[str, Any]], agent: str) -> str | None:
    for member in members:
        if member["agent_name"] == agent and member.get("enabled", True):
            return member["role_id"]
    return None


# Outcomes that close out one dispatch of a step: the agent finished it
# (done/rework) or the engine took it away (reassigned on timeout).
_STEP_FINISHING_OUTCOMES = ("done", "rework", "reassigned")


def _last_dispatch_and_finish(
    transitions: list[dict[str, Any]],
) -> tuple[dict[str, tuple[int, str]], dict[str, int]]:
    """Per step: the most recent dispatch (id, assignee) and the id of the most
    recent finishing transition. A step is active when its latest dispatch has
    no finish after it. Using the LATEST dispatch rather than a dispatch/finish
    count means a runner killed before it finished (a dispatch with no matching
    finish) does not phantom-activate the step forever across restarts."""
    last_dispatch: dict[str, tuple[int, str]] = {}
    last_finish: dict[str, int] = {}
    for t in transitions:
        if t["outcome"] == "dispatched":
            last_dispatch[t["to_step"]] = (t["id"], t.get("note", ""))
        elif t["outcome"] in _STEP_FINISHING_OUTCOMES and t["from_step"]:
            last_finish[t["from_step"]] = t["id"]
    return last_dispatch, last_finish


def _active_steps(transitions: list[dict[str, Any]]) -> list[str]:
    last_dispatch, last_finish = _last_dispatch_and_finish(transitions)
    return [
        step for step, (did, _) in last_dispatch.items()
        if did > last_finish.get(step, 0)
    ]


_WORKFLOW_STATUS_OVERRIDES = {"blocked", "closed"}


def _workflow_derived_task_status(
    task: dict[str, Any],
    transitions: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> str:
    stored = task.get("task_status") or task.get("status") or ""
    if task.get("is_goal"):
        return stored
    if stored in _WORKFLOW_STATUS_OVERRIDES:
        return stored
    return stored


def _project_workflow_task_status(
    store: Store,
    project_root: str | None,
    task: dict[str, Any],
    cfg: dict[str, Any] | None = None,
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


def _active_step_assignees(transitions: list[dict[str, Any]]) -> dict[str, str]:
    last_dispatch, last_finish = _last_dispatch_and_finish(transitions)
    return {
        step: assignee
        for step, (did, assignee) in last_dispatch.items()
        if did > last_finish.get(step, 0)
    }


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
    # A rework target feeds back into a shared step but is not a parallel branch
    # of it, so it must not gate the join.
    rework_targets = {e["to"] for e in cfg["edges"] if e.get("rework")}
    required_preds = [
        e["from"] for e in cfg["edges"]
        if e["to"] == target
        and (e["from"], e["to"]) not in back
        and steps[e["from"]]["required"]
        and e["from"] not in rework_targets
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
    # The UI sends no agent name (older pages sent "ui"); it acts as the
    # team's hub member so the engine's assignee/hub constraint recognizes it.
    agent = (raw_agent or "").strip()
    if agent and agent != "ui":
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
    team = read_team_config(project_root)
    enabled_roles = {
        member["role_id"] for member in team["members"] if member.get("enabled", True)
    }
    missing_roles = sorted(_required_workflow_roles(cfg, back) - enabled_roles)
    if missing_roles:
        raise InvalidInputError(
            "team is missing required roles: "
            + ", ".join(missing_roles)
            + ". Enable a member for each on the Team page."
        )
    members = team["members"]
    probe_task = {
        "id": 0,
        "title": title,
        "content": content,
        "importance": "normal",
        "size": "medium",
        "risk": "medium",
        "required_capabilities": [],
        "exclusive_workspace": True,
    }
    missing: list[str] = []
    for step in _main_workflow_reachable_steps(cfg, back):
        assignee = _pick_assignee(store, probe_task, step, members)
        if assignee is None:
            if step["required"]:
                missing.append(f"{step['id']} ({step['role_id']}): no enabled member")
            continue
        member = _member_named(members, assignee) or {"agent_name": assignee}
        if not _runner_command_for(member):
            missing.append(f"{step['id']} ({assignee}): no runner_command/default")
    if missing:
        raise InvalidInputError(
            "goal cannot auto-run until runner commands are configured: "
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
        created.append(task)
    # 2. Persist each subtask's prerequisite task ids.
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
        members = read_team_config(project_root)["members"]
        _notify_hub(
            store, members,
            f"目标 #{goal_id} 触及 token 硬预算：已用 {total} > 预算 {budget}。"
            "已冻结后续派发，需人工介入（提高预算 / 重新拆分 / 终止目标）。",
        )
    return True


def _detect_goal_verify(root: Path) -> str:
    """Infer a default project test command from build markers in `root` (looked
    at the project root only, never walked up). Empty string when nothing is
    recognized. Used as the goal convergence check when no goal_verify is
    configured, so a goal still gets an objective integration gate out of the
    box. An explicit goal_verify always wins over this guess."""
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        # Only when a `test` script exists — `npm test` errors without one.
        if isinstance(data, dict) and isinstance(data.get("scripts"), dict) \
                and str(data["scripts"].get("test") or "").strip():
            return "npm test"
    makefile = root / "Makefile"
    if makefile.exists():
        try:
            if re.search(r"(?m)^test:", makefile.read_text(encoding="utf-8")):
                return "make test"
        except OSError:
            pass
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() \
            or (root / "setup.cfg").exists():
        for name in ("tests", "test"):
            if (root / name).is_dir():
                return f"python -m unittest discover -s {name}"
    return ""


def _effective_goal_verify(goal: dict[str, Any] | None, project_root: str | None) -> str:
    """The convergence command to run for a goal: the goal's own goal_verify if
    set, otherwise a best-effort default detected from project markers. Empty
    when neither is available."""
    own = str((goal or {}).get("goal_verify") or "").strip()
    if own:
        return own
    return _detect_goal_verify(_project_root(project_root))


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
) -> dict[str, Any]:
    # Record the raw output first so a parse failure (bad JSON) still leaves the
    # decompose step's result inspectable on its card.
    _record_step_result(store, goal, step["id"], result)
    subtasks = _parse_goal_subtasks(result)
    intake_card = store.find_open_step_card(goal["id"], step["id"])
    if intake_card:
        store.update_task_step_details(
            intake_card["id"],
            result_summary=f"Created {len(subtasks)} work item(s)",
        )
    # Re-validate workflow/team/state before dispatching the business subtasks.
    # The goal passed this gate at creation, but team/workflow config can change
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
    if target_steps is None:
        started = _start_goal_business_subtasks(store, project_root, goal, actor, subtasks)
    else:
        started = _start_goal_business_subtasks(
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

def _role_duty_summary(project_root: str | None, role_id: str) -> str:
    """First bullet under a role's 职责 section — a one-line description of
    the work that role performs, used as the step card's work summary."""
    for role in list_agent_roles(_agents_dir(project_root)):
        if role["id"] != role_id:
            continue
        in_duties = False
        for line in role["content"].splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                in_duties = "职责" in stripped
                continue
            if in_duties and stripped.startswith("- "):
                return stripped[2:].strip()
    return ""


def _upsert_step_card(
    store: Store,
    project_root: str | None,
    parent: dict[str, Any],
    step: dict[str, Any],
    assignee: str,
    step_inputs: dict[str, Any],
) -> dict[str, Any]:
    card = store.find_open_step_card(parent["id"], step["id"])
    if card:
        # Redispatch (rework loop / timeout reassign): reuse the open card.
        store.set_task_workflow_state(
            card["id"], task_status="assigned", assignee=assignee
        )
        return store.update_task_step_details(
            card["id"], step_inputs=step_inputs, result_summary="", artifacts=[]
        ) or card
    # Title = step type + what THIS task is actually about (the parent task's
    # title), so each card reflects its own work — not the generic role duty.
    work = (parent.get("title") or "").strip()
    title = f"{step['name']} · {work[:60]}" if work else step["name"]
    return store.create_step_card(
        parent_task_id=parent["id"],
        workflow_step=step["id"],
        title=title,
        content=(
            f"Workflow step '{step['name']}' (role {step['role_id']}) "
            f"of task #{parent['id']}\n\n{parent.get('content', '')}"
        ),
        sender=WORKFLOW_ENGINE_AGENT,
        assignee=assignee,
        status="assigned",
        role_required=step["role_id"],
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
            "role_id": step.get("role_id") or "",
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
    command = _runner_command_for(member)
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
    members: list[dict[str, Any]],
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
            # Optional step with nobody to run it: pass through to successors.
            for nxt in _forward_out(cfg, back, target):
                store.record_task_transition(
                    task_id, target, nxt, WORKFLOW_ENGINE_AGENT, "skipped",
                    f"no team member for optional step {target}",
                )
                queue.append(nxt)
            notices.append(f"optional step {target} skipped (no team member)")
            continue
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
                _member_named(members, assignee) or {"agent_name": assignee},
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
) -> dict[str, Any]:
    """Re-run a blocked (or active) workflow step with a chosen agent.

    Used by the task panel to recover a step that blocked — e.g. the assigned
    CLI hit a rate/session limit — by re-dispatching it to a different agent
    (a different model) without editing the team. Records a fresh dispatch to
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
        members = read_team_config(project_root)["members"]
        base = _member_named(members, agent) or {}
        member = {
            "agent_name": agent,
            "role_id": step_def["role_id"],
            "enabled": True,
            "expertise_level": int(base.get("expertise_level", 3)),
            "max_concurrent_tasks": int(base.get("max_concurrent_tasks", 0)),
            "capabilities": list(base.get("capabilities", [])),
            "notes": base.get("notes", ""),
            "runner_command": base.get("runner_command", ""),
        }
        if not _runner_command_for(member):
            raise InvalidInputError(
                f"agent {agent!r} has no runner command or built-in default, so it "
                "cannot auto-run the step; pick a CLI-backed agent"
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
        job = _dispatch_step(
            store, project_root, task, step_def, member, upstream_result
        )
        return {
            "task_id": task_id,
            "step": step_id,
            "assignee": agent,
            "runner_command": _runner_command_for(member),
            "queued_job_id": job["id"] if job else None,
            "reran": True,
        }


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
    members = read_team_config(project_root)["members"]
    dispatched, notices = _dispatch_targets(
        store, project_root, task, targets, cfg, back, members, upstream_result
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

    if outcome == "blocked":
        store.record_task_transition(task_id, step, step, agent, "blocked", result)
        store.set_task_workflow_state(task_id, task_status="blocked")
        _settle_step_card(store, task, step, "blocked")
        _recompute_parent_goal_status(store, task, project_root)
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
                store, members,
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
        store, project_root, task, targets, cfg, back, members, upstream
    )
    return {
        "task_id": task_id, "step": step, "outcome": outcome,
        "dispatched": dispatched, "notices": notices,
    }


# How often the background watcher scans for timed-out steps.
WORKFLOW_TIMEOUT_POLL_SECONDS = 60
# The scheduler drains finished run jobs; keep latency low so a step's next
# step dispatches promptly after the runner reports.
SCHEDULER_POLL_SECONDS = 1.0
# Per-process name so two UI/scheduler instances hold distinguishable applying
# leases (the finish CAS guards on this exact name).
SCHEDULER_RUNNER_NAME = f"workflow-scheduler-{os.getpid()}"
# Applying a finished job is a few fast DB ops; keep the lease short so a
# crashed scheduler's job is reclaimable quickly.
SCHEDULER_APPLYING_LEASE_SECONDS = 60

# --- Auto-runner -------------------------------------------------------------
# Dispatched steps whose assignee has a runner command spawn a one-shot CLI
# process instead of waiting for a live session to poll the inbox. The command
# receives the prompt on stdin, works in the project root, and its stdout tail
# is submitted via the engine as the step result.
# runner_command on the member overrides the per-tool default.
RUNNER_DEFAULT_TIMEOUT_SECONDS = 1800
# Soft/hard timeouts for a running step. Once a step has produced no stdout or
# stderr for the soft interval, the hub agent is asked to inspect it and decide
# KILL (stuck/errored) or CONTINUE; the hard cap force-kills regardless so
# nothing runs forever.
RUNNER_SOFT_TIMEOUT_SECONDS = 600
RUNNER_HARD_TIMEOUT_SECONDS = 1800
# The hub inspection is itself a CLI call; keep it bounded.
HUB_INSPECT_TIMEOUT_SECONDS = 180
# How often the central hub-inspection sweep scans for stuck runs.
HUB_SWEEP_POLL_SECONDS = 60
# How often a runner re-checks the hard cap and the hub's kill flag while waiting.
RUNNER_CANCEL_POLL_SECONDS = 10
# After a process exits/is killed, how long to wait for the stdout/stderr readers
# to drain before force-closing the pipes — bounds the wedge when an escaped child
# keeps a pipe open so EOF never arrives.
RUNNER_STREAM_DRAIN_SECONDS = 5
# Headless CLIs default to asking for permission on every file write / tool
# call, which no one is there to answer — a runner without full permissions
# can only produce inline text. These defaults grant maximum autonomy; set
# runner_command on the member to run with a tighter policy.
_DEFAULT_RUNNER_COMMANDS = {
    "antigravity": 'agy --dangerously-skip-permissions --print "$(cat)"',
    "claude-code": "claude -p --dangerously-skip-permissions",
    "codex": "codex exec --dangerously-bypass-approvals-and-sandbox -",
    # gemini additionally refuses to start headless in an untrusted dir.
    "gemini": "GEMINI_CLI_TRUST_WORKSPACE=true gemini --yolo",
    # opencode's `run` takes the prompt as a positional arg (not stdin), so
    # $(cat) bridges the piped prompt; --auto auto-approves permissions not
    # explicitly denied (its headless-autonomy flag).
    "opencode": 'opencode run --auto "$(cat)"',
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


_VERDICT_RE = re.compile(
    r"WORKFLOW_OUTCOME\s*[:=]\s*(done|rework|blocked)", re.IGNORECASE
)
# Cap on the stored per-step result summary. It travels in every board-poll
# task row, so a legacy runner's full-output fallback must not bloat payloads
# (200 rows x 20KB was ~4MB per 5s poll).
_STEP_SUMMARY_MAX = 2000

_RESULT_SUMMARY_RE = re.compile(
    r"(?im)^RESULT_SUMMARY\s*:\s*(.+?)\s*$"
)
_ARTIFACTS_RE = re.compile(
    r"(?im)^ARTIFACTS\s*:\s*(\[.*\])\s*$"
)


def _step_can_rework(cfg: dict[str, Any], back: set[tuple[str, str]], step_id: str) -> bool:
    """True when the step has a loop-back edge, i.e. it may send the task back
    for rework (e.g. review -> implement)."""
    return any(
        edge["from"] == step_id and (edge["from"], edge["to"]) in back
        for edge in cfg["edges"]
    )


def _parse_runner_verdict(text: str) -> str | None:
    """Extract a runner's self-reported WORKFLOW_OUTCOME (last one wins), or
    None if it did not emit one."""
    matches = _VERDICT_RE.findall(text or "")
    return matches[-1].lower() if matches else None


def _parse_step_output_metadata(text: str) -> tuple[str, list[str]]:
    """Extract the structured step-result protocol with a legacy fallback.

    New runners emit one-line RESULT_SUMMARY and a JSON ARTIFACTS array. Older
    runners remain useful: their output (minus protocol bookkeeping) becomes the
    summary and their artifact list is empty.
    """
    text = (text or "").strip()
    summaries = _RESULT_SUMMARY_RE.findall(text)
    # Summaries ride along in every /api/tasks row and /api/goals step entry, so
    # keep them small — the full output stays in the run's stdout log.
    summary = summaries[-1].strip()[:_STEP_SUMMARY_MAX] if summaries else ""
    artifacts: list[str] = []
    artifact_matches = _ARTIFACTS_RE.findall(text)
    if artifact_matches:
        try:
            raw = json.loads(artifact_matches[-1])
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, list):
            for item in raw:
                value = str(item).strip() if isinstance(item, str) else ""
                if value and value not in artifacts:
                    artifacts.append(value[:1000])
                if len(artifacts) >= 100:
                    break
    if not summary:
        legacy_lines = [
            line for line in text.splitlines()
            if not re.match(
                r"(?i)^\s*(WORKFLOW_OUTCOME|TOKENS_USED|RESULT_SUMMARY|ARTIFACTS)\s*[:=]",
                line,
            )
        ]
        summary = _tail("\n".join(legacy_lines), _STEP_SUMMARY_MAX)
    return summary, artifacts


def _structured_upstream(result: str) -> str:
    """The upstream context handed to the NEXT step: the completed step's
    summary plus its artifact references, which the next agent reads directly —
    instead of an arbitrarily truncated transcript of the previous runner.

    Protocol runners (RESULT_SUMMARY/ARTIFACTS) collapse to a tight block;
    legacy output degrades to its cleaned tail, which matches what the
    transition note carried before, so older runners behave as they always did.
    """
    summary, artifacts = _parse_step_output_metadata(result)
    lines: list[str] = []
    if summary:
        lines.append(summary)
    if artifacts:
        lines.append("ARTIFACTS:")
        lines.extend(f"- {artifact}" for artifact in artifacts)
        lines.append("（完整细节请直接读取上述产物文件/引用，不要依赖摘要复述。）")
    return "\n".join(lines)


# CLI-native token-usage formats, tried before the self-reported sentinel.
# Accurate where a CLI prints its own usage (e.g. codex "tokens used\n<n>").
_NATIVE_TOKEN_PATTERNS = [
    re.compile(r"tokens used\s*[\r\n]+\s*([\d,]+)", re.IGNORECASE),  # codex
]
# Universal fallback: the prompt asks every runner to print this. Approximate
# (the model estimates its own usage) — only used when no native count exists.
_SELF_REPORT_TOKEN_RE = re.compile(r"TOKENS_USED\s*[:=]\s*([\d,]+)", re.IGNORECASE)


def _parse_run_tokens(stdout: str, stderr: str) -> int | None:
    """Token count for a run: prefer an accurate CLI-native number, fall back to
    the runner's self-reported TOKENS_USED sentinel, else None. Last match wins
    (usage lines are cumulative)."""
    text = f"{stderr or ''}\n{stdout or ''}"
    for pattern in _NATIVE_TOKEN_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            return int(matches[-1].replace(",", ""))
    matches = _SELF_REPORT_TOKEN_RE.findall(text)
    if matches:
        return int(matches[-1].replace(",", ""))
    return None


def _triage_config_snapshot(project_root: str | None) -> str:
    """Return a compact, non-secret view of the effective workflow and team.

    Goal preflight already rejects mechanically unexecutable configurations.
    Triage receives this snapshot to judge whether the remaining role choices,
    optional gates, and capacity are sensible for the requested goal without
    exposing full runner commands in the prompt.
    """
    workflow = read_workflow_config(project_root)
    team = read_team_config(project_root)
    snapshot = {
        "workflow": {
            "steps": [
                {
                    "id": step["id"],
                    "role": step.get("role_id", ""),
                    "required": bool(step.get("required")),
                    "isolate": bool(step.get("isolate")),
                    "integrate": bool(step.get("integrate")),
                    "decompose": bool(step.get("decompose")),
                    "prompt_configured": bool(step.get("prompt")),
                    "verify_configured": bool(step.get("verify")),
                }
                for step in workflow["steps"]
            ],
            "edges": workflow["edges"],
            "warnings": workflow.get("warnings", []),
        },
        "team": {
            "members": [
                {
                    "agent": member.get("agent_name", ""),
                    "role": member.get("role_id", ""),
                    "enabled": bool(member.get("enabled", True)),
                    "runner_ready": bool(_runner_command_for(member)),
                    "max_concurrent_tasks": member.get("max_concurrent_tasks", 1),
                    "capabilities": member.get("capabilities", []),
                }
                for member in team["members"]
            ],
            "missing_core_roles": team.get("missing_roles", []),
        },
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def _build_step_prompt(
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    upstream_result: str,
    can_rework: bool = False,
    isolated: bool = False,
) -> str:
    roles = {
        role["id"]: role["content"]
        for role in list_agent_roles(_agents_dir(project_root))
    }
    role_text = roles.get(step["role_id"], "")
    branch = _worktree_branch(task["id"])
    if step.get("integrate"):
        cwd_line = (
            "当前工作目录是项目主工作树（main）。本任务的实现成果在独立 git 分支 "
            f"`{branch}` 上，你的职责是把它集成回主干。\n"
        )
    elif isolated:
        cwd_line = (
            f"当前工作目录是本任务专属的 git worktree，位于分支 `{branch}`，"
            "与其它任务的工作树完全隔离。直接读写文件完成任务；改动只影响该分支，"
            "不会污染主干，也看不到其它任务的在途改动。完成后请把成果 "
            "`git add -A && git commit` 到当前分支（后续 integrate 步骤据此合并回主干）。\n"
        )
    else:
        cwd_line = "当前工作目录就是项目根目录，直接读写文件完成任务。\n"
    triage_block = ""
    if (
        step.get("id") == "intake"
        and task.get("is_goal")
        and not task.get("parent_task_id")
    ):
        triage_block = (
            "\n## Triage：目标与执行配置体检\n"
            "保持轻量，只做目标归一化、关键阻塞识别，以及 workflow/team 是否适合执行"
            "本 Goal 的检查。下面是引擎解析后的有效配置快照（runner 仅暴露是否可用，不暴露命令）：\n"
            f"```json\n{_triage_config_snapshot(project_root)}\n```\n"
            "检查 workflow 是否有合理的入口、设计/拆解边界、验证与集成终点、返工路径；"
            "检查步骤 Role 是否匹配职责，以及 team 的启用成员、runner、能力和并发是否足以执行。\n"
            "引擎已完成硬性可执行性预检。只有具体问题会使执行失败、不安全或明显不适合本 Goal 时才 `blocked`；"
            "普通优化建议标为 warning，不要阻塞。不要在本步骤做设计、任务拆解或实现。\n"
            "输出简短的 Goal/Scope/Acceptance/Constraints/Open blocker，并附：\n"
            "`CONFIG_CHECK: ok|warning|blocked`\n"
            "`CONFIG_FINDINGS: <简短结论>`\n"
        )
    integrate_block = ""
    if step.get("integrate"):
        integrate_block = (
            "\n## 集成与最终验收（integrate 步骤）\n"
            f"若分支 `{branch}` 存在，把它合并回主干；若项目不是 git 仓库或该分支"
            "不存在，说明任务在主工作树直接执行，跳过合并但仍必须完成后续验收。步骤：\n"
            "1. 有任务分支时，`git status` 确认主工作树干净、当前在集成目标分支；\n"
            f"2. 有任务分支时执行 `git merge --no-ff {branch}`；\n"
            "3. 有冲突：能安全解决就解决后 `git commit`；无法安全解决则 "
            "`git merge --abort` 并裁决 `rework`，在原因里列出冲突文件；\n"
            "4. 对照任务描述和 Acceptance 验收标准检查集成后的实际结果；不满足则 `rework`；\n"
            "5. 在主工作树运行相关测试；测试失败且本步无法修复则 `rework`；\n"
            "6. 合并（如需）、验收和测试全部通过后才裁决 `done`，该任务随后直接关闭。\n"
            "不要手动删除该 worktree 或分支——集成完成后引擎会自动回收。\n"
        )
    goal_contract = ""
    if _is_root_goal_decompose_step(project_root, task, step):
        goal_contract = (
            "\n## Goal 拆分输出格式\n"
            "把目标拆成业务子任务。若上游已有产品/UI/架构设计产出，"
            "**据其模块与接口边界划分**，每个子任务对应一块不重叠的实现区域。\n"
            "你必须只输出一个 JSON 对象，不要 Markdown，不要代码块：\n"
            '{"tasks":[{"title":"子任务标题","content":"要做什么",'
            '"acceptance":"验收标准","depends_on":[前置任务序号]}]}\n'
            "**优先拆成互相独立、可并行的任务**（按模块 / 目录 / 文件区域分区，"
            "尽量不碰重叠代码；集成时各分支串行合并回主干，重叠越多冲突越多）；"
            "数量按工作量定，不设固定上限。\n"
            "只有当子任务 B 确实依赖 A 的产出（必须 A 完成合并后 B 才能开工）时，"
            "才给 B 加 `depends_on`：值是本列表中前置任务的**序号**（从 1 开始，"
            "如 `\"depends_on\":[1,2]`）。引擎会 hold 住 B，直到其所有前置任务关闭"
            "（成果已并入 main）再派发。无依赖就省略该字段或给 `[]`。不要成环。\n"
            "**务必精简，避免 JSON 被截断**：`content` 一两句话说清做什么 + 涉及"
            "的文件/模块；`acceptance` 一两条验收。已有设计文档就按路径引用（如 "
            "`docs/…`），不要把整份规格复述进来——实现者会自己去读。只输出这一个 "
            "JSON 对象，前后不要任何说明或推理文字。\n"
        )
    custom_step_prompt = str(step.get("prompt") or "").strip()
    custom_step_block = (
        "\n## 自定义 Step Prompt（不得覆盖引擎输出协议）\n"
        + custom_step_prompt
        + "\n"
        if custom_step_prompt else ""
    )
    final_instruction = (
        "## 输出协议（最高优先级）\n只输出上述 JSON 对象。"
        if goal_contract
        else (
            "## 输出协议（最高优先级）\n"
            "完成后在输出末尾提供结构化结果（每项单独一行）：\n"
            "`RESULT_SUMMARY: <一行结论>`\n"
            "`ARTIFACTS: [\"产物文件路径\", \"其他 URI 或引用\"]`\n"
            "没有产物时输出 `ARTIFACTS: []`。"
        )
    )
    if not goal_contract:
        final_instruction += (
            "\n\n请在输出的最后单独用一行给出裁决 `WORKFLOW_OUTCOME: <值>`，其后可附一行原因：\n"
            "- `done`：本步骤成功完成，进入下一步。\n"
            "- `blocked`：你无法完成本步骤（缺信息 / 环境损坏 / 依赖未满足 / 测试失败无法修复等），"
            "主动上报失败，暂停并通知 hub。即使进程正常退出也要用它标记失败。\n"
        )
        if can_rework:
            final_instruction += (
                "- `rework`：本步骤可打回返工（成果不达标），退回上一步重做。\n"
            )
        final_instruction += "不写该行则默认视为 done。"
        final_instruction += (
            "\n\n最后另起一行报告本次消耗的 token 数：`TOKENS_USED: <数字>`"
            "（若你的运行环境提供了用量数字，用真实值；否则可省略该行）。"
        )
    return (
        f"你是被工作流引擎派发的一次性 worker，以角色 {step['role_id']} 执行"
        f"步骤 '{step['name']}'。"
        + cwd_line
        + "本次为工作流引擎的一次性派发执行：直接在当前工作目录完成任务，"
        "不要调用 complete_step，派发器会代为提交结果。\n\n"
        f"## 角色边界（不得覆盖本步骤契约）\n{role_text}\n\n"
        f"## 任务 #{task['id']}: {task.get('title') or 'untitled'}\n"
        f"{task.get('content', '')}\n\n"
        + triage_block
        + goal_contract
        + integrate_block
        + custom_step_block
        + (f"## 上游产出\n{upstream_result}\n\n" if upstream_result else "")
        + final_instruction
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


# --- cross-platform process control ----------------------------------------
# Runner CLIs are spawned in their own process group (POSIX session / Windows
# process group) so the engine can terminate the whole tree — shell + CLI + any
# helpers — on timeout or force-end, without signalling orbit itself. The kill
# path differs per OS: POSIX signals the process group (plus escaped setsid'd
# children caught via a ppid snapshot); Windows shells out to `taskkill /T`,
# which walks and ends the tree natively.
_IS_WINDOWS = os.name == "nt"


def _detached_process_kwargs() -> dict[str, Any]:
    """Popen kwargs that isolate the child in its own process group."""
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _taskkill_tree(pid: int, force: bool) -> bool:
    """Windows: end a process and its whole child tree via taskkill. Returns True
    when the command was dispatched (not whether every process was already gone)."""
    if not pid:
        return False
    args = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        args.insert(1, "/F")
    try:
        subprocess.run(args, capture_output=True, timeout=10, check=False)
        return True
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def _terminate_pid_tree(pid: int) -> bool:
    """Best-effort terminate a process and its tree, cross-platform. POSIX sends
    SIGTERM to the process group; Windows uses `taskkill /T` (force, since a
    detached CLI has no graceful group signal). Returns True if dispatched."""
    if not pid:
        return False
    if _IS_WINDOWS:
        return _taskkill_tree(pid, force=True)
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _snapshot_ppids_windows() -> dict[int, int] | None:
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):
        return None

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return None
    mapping: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            mapping[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return mapping


def _snapshot_ppids_ps() -> dict[int, int] | None:
    if _IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    mapping: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        mapping[pid] = ppid
    return mapping


def _snapshot_ppids_libproc() -> dict[int, int] | None:
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util
    except ImportError:
        return None
    lib_path = ctypes.util.find_library("proc")
    if not lib_path:
        return None
    libproc = ctypes.CDLL(lib_path, use_errno=True)
    PROC_ALL_PIDS = ctypes.c_uint32(1)
    PROC_PIDTBSDINFO = ctypes.c_int(3)
    libproc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    item_size = ctypes.sizeof(ctypes.c_int)
    capacity = 4096
    while True:
        buf_type = ctypes.c_int * (capacity // item_size)
        buf = buf_type()
        bytes_used = libproc.proc_listpids(PROC_ALL_PIDS, ctypes.c_uint32(0), buf, ctypes.sizeof(buf))
        if bytes_used <= 0:
            if ctypes.get_errno():
                return None
            return {}
        if bytes_used < ctypes.sizeof(buf):
            count = bytes_used // item_size
            pids = buf[:count]
            break
        capacity *= 2
    info_buf = (ctypes.c_ubyte * 1024)()
    mapping: dict[int, int] = {}
    # proc_pidinfo returns 0 for processes this uid can't introspect; those are
    # skipped, so this snapshot can under-count system-wide pids. Fine here: it
    # is a fallback behind `ps`, and a runner's own descendants (the only ones
    # we kill) are always readable. Don't promote it to primary expecting full
    # coverage.
    for pid in pids:
        if pid <= 0:
            continue
        written = libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, ctypes.c_uint64(0), info_buf, ctypes.sizeof(info_buf))
        if written < 20:
            continue
        data = bytes(info_buf[:written])
        fields = struct.unpack_from("=5I", data)
        real_pid = int(fields[3])
        ppid = int(fields[4])
        if real_pid:
            mapping[real_pid] = ppid
    return mapping


def _snapshot_ppids_procfs() -> dict[int, int] | None:
    if not sys.platform.startswith("linux"):
        return None
    root = Path("/proc")
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return None
    mapping: dict[int, int] = {}
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        status_path = entry / "status"
        try:
            with status_path.open("r", encoding="utf-8", errors="replace") as handle:
                pid_val: int | None = None
                ppid_val: int | None = None
                for line in handle:
                    if line.startswith("Pid:"):
                        try:
                            pid_val = int(line.split()[1])
                        except (IndexError, ValueError):
                            pid_val = None
                    elif line.startswith("PPid:"):
                        try:
                            ppid_val = int(line.split()[1])
                        except (IndexError, ValueError):
                            ppid_val = None
                        if pid_val is not None and ppid_val is not None:
                            mapping[pid_val] = ppid_val
                        break
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return mapping


def _snapshot_ppids() -> dict[int, int]:
    for getter in (
        _snapshot_ppids_windows,
        _snapshot_ppids_ps,
        _snapshot_ppids_libproc,
        _snapshot_ppids_procfs,
    ):
        mapping = getter()
        if mapping is None:
            continue
        if mapping:
            return mapping
    return {}


def _descendant_pids(root_pid: int) -> list[int]:
    mapping = _snapshot_ppids()
    if not mapping:
        return []
    children: dict[int, list[int]] = {}
    for pid, ppid in mapping.items():
        children.setdefault(ppid, []).append(pid)
    seen: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid == root_pid or pid in seen:
            continue
        seen.append(pid)
        stack.extend(children.get(pid, []))
    return seen


def _kill_process_group(proc: "subprocess.Popen[bytes]") -> None:
    """Force-kill the runner's whole process tree (shell + CLI + helpers). On
    Windows, `taskkill /F /T` walks and ends the tree natively. On POSIX, SIGKILL
    the process group, then reap any setsid'd child that escaped it — snapshotted
    before the kill, since once the parent dies its children reparent to init and
    the tree link is lost."""
    if not proc.pid:
        return
    if _IS_WINDOWS:
        _taskkill_tree(proc.pid, force=True)
        try:
            proc.kill()  # backstop if taskkill is unavailable
        except OSError:
            pass
        return
    descendants = _descendant_pids(proc.pid)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# hub_inspect_sweep bookkeeping: run_id -> {"size": last output byte count seen,
# "inspected": monotonic time of the last hub inspection of this run}.
_HUB_SWEEP_STATE: dict[int, dict[str, float]] = {}


def _run_last_output_at(log_dir: str | None, started_at: datetime) -> datetime:
    """Best-effort timestamp of the latest actual stdout/stderr byte.

    Empty log files mean the process has produced no output, so silence is
    measured from the run start instead of the file creation timestamp.
    """
    latest = started_at
    if not log_dir:
        return latest
    for name in ("stdout.log", "stderr.log"):
        try:
            stat = (Path(log_dir) / name).stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if mtime > latest:
            latest = mtime
    return latest


def _read_run_output_tail(log_dir: str | None, tail_bytes: int = 4000) -> str:
    """Last tail_bytes of each log file — a long-running step's logs can be huge,
    and the hub prompt only needs the recent tail. Seeks instead of reading all."""
    if not log_dir:
        return ""
    parts = []
    for name in ("stdout.log", "stderr.log"):
        path = Path(log_dir) / name
        try:
            with path.open("rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - tail_bytes))
                chunk = fh.read().decode("utf-8", errors="replace")
            if chunk.strip():
                parts.append(chunk)
        except OSError:
            pass
    return "\n".join(parts)


def _hub_inspect_batch(
    store: Store, project_root: str | None, candidates: list[dict[str, Any]]
) -> dict[int, str]:
    """One hub-agent call to judge several still-running, silent steps at once.
    Returns {run_id: "kill"|"continue"}; anything not clearly marked KILL by the
    hub stays "continue" so a healthy step is never killed on doubt."""
    decisions = {c["run_id"]: "continue" for c in candidates}
    try:
        members = read_team_config(project_root)["members"]
    except Exception:
        return decisions
    hub = next(
        (m for m in members
         if m.get("role_id") == "hub" and m.get("enabled", True) and _runner_command_for(m)),
        None,
    )
    if not hub:
        return decisions
    command = _runner_command_for(hub)
    lines = []
    for i, c in enumerate(candidates, 1):
        out = _tail(c.get("output") or "", 500) or "(长时间无输出)"
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
            cwd=str(_project_root(project_root)), **_detached_process_kwargs(),
        )
        try:
            out, _ = proc.communicate(prompt.encode("utf-8"), timeout=HUB_INSPECT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
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
    store: Store, project_root: str | None, now: float | None = None
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
        prev = _HUB_SWEEP_STATE.get(rid)
        try:
            started_at = datetime.fromisoformat(run["started_at"])
        except (TypeError, ValueError):
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (now_dt - started_at).total_seconds()
        last_output_at = _run_last_output_at(run.get("log_dir"), started_at)
        silent_for = (now_dt - last_output_at).total_seconds()
        _HUB_SWEEP_STATE[rid] = {
            "inspected": (prev or {}).get("inspected", 0.0),
            "last_output": last_output_at.timestamp(),
        }
        # filter A: a run still producing output (silent for less than the soft
        # interval) is working — skip it; only inspect genuinely silent runs.
        if silent_for < RUNNER_SOFT_TIMEOUT_SECONDS:
            continue
        # cadence: inspect a given run at most once per soft interval
        if now - _HUB_SWEEP_STATE[rid]["inspected"] < RUNNER_SOFT_TIMEOUT_SECONDS:
            continue
        task = store.get_task(int(run["task_id"]))
        candidates.append({
            "run_id": rid, "task_id": run["task_id"],
            "title": task.get("title") if task else "",
            "step": run.get("workflow_step") or (task.get("workflow_step") if task else ""),
            "assignee": run.get("worker"), "elapsed": elapsed,
            "silent_for": silent_for,
            "output": _read_run_output_tail(run.get("log_dir")),
        })
    for gone in [k for k in _HUB_SWEEP_STATE if k not in live_ids]:
        _HUB_SWEEP_STATE.pop(gone, None)
    if not candidates:
        return []
    decisions = _hub_inspect_batch(store, project_root, candidates)
    cfg = read_workflow_config(project_root)
    steps = {s["id"]: s for s in cfg["steps"]}
    flagged: list[int] = []
    for c in candidates:
        _HUB_SWEEP_STATE[c["run_id"]]["inspected"] = now
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
        wf_task_id = _workflow_task_id_for_run(store, int(c["task_id"]))
        try:
            apply_run_outcome(
                store, project_root, wf_task_id, step, c["assignee"],
                "blocked", reason, status="failed",
            )
        except Exception:
            # One task's block failure must never abort the whole sweep.
            pass
    return flagged


def _workflow_task_id_for_run(store: Store, run_task_id: int) -> int:
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


def _task_blocked_reason(store: Store, task: dict[str, Any]) -> str | None:
    """Why a blocked task is blocked: the note of its most recent 'blocked'
    transition, for the detail view. Step cards carry no transitions of their
    own — the block is recorded on the parent workflow task — so fall back to
    the parent. Returns None for tasks that are not blocked."""
    if task.get("task_status") != "blocked":
        return None

    def _latest(tid: int) -> str | None:
        for t in reversed(store.list_task_transitions(tid)):
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

WORKTREE_LOCK_RETRIES = 5


def _task_workflow_finished(task: dict[str, Any] | None) -> bool:
    """True when a task has left the workflow for good: it is gone, 'closed', or
    — for a goal — 'accepted' (goals use a separate lifecycle vocabulary)."""
    if task is None:
        return True
    status = task.get("task_status")
    if status == "closed":
        return True
    return status == "accepted" and bool(task.get("is_goal"))


def _git(root: Path, *args: str, timeout: float = 30.0) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _is_git_repo(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--git-dir", timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _worktree_branch(task_id: int) -> str:
    return f"orbit/task-{task_id}"


def _task_worktree_dir(project_root: str | None, task_id: int) -> Path:
    return project_state_dir(_project_root(project_root)) / "worktrees" / f"task-{task_id}"


def _worktree_base_ref(root: Path) -> str | None:
    # New worktrees branch off the main tree's current commit. An unborn HEAD
    # (repo with no commits) can't seed a worktree -> caller skips isolation.
    try:
        if _git(root, "rev-parse", "--verify", "-q", "HEAD", timeout=10).returncode == 0:
            return "HEAD"
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _git_available() -> bool:
    """Whether the git binary can be invoked at all. When git is absent the
    engine cannot isolate or integrate, and silently degrades to project_root."""
    import shutil

    return shutil.which("git") is not None


def _workflow_needs_git(cfg: dict[str, Any]) -> bool:
    """A workflow needs a git repo only if some step runs isolated (per-task
    worktree) or integrates (merge the task branch back to main)."""
    return any(s.get("isolate") or s.get("integrate") for s in cfg["steps"])


def _ensure_state_dir_gitignored(root: Path) -> None:
    """Keep orbit's runtime dirs (per-task worktrees, task logs) out of the repo
    so `git status` stays clean for the integrate step and the worktrees are
    never committed into the tree they branch from."""
    state = project_state_dir(root)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    present = set(existing.splitlines())
    wanted = [f"{state.name}/tasks/", f"{state.name}/worktrees/"]
    missing = [line for line in wanted if line not in present]
    if not missing:
        return
    joiner = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(
        existing + joiner + "".join(f"{line}\n" for line in missing),
        encoding="utf-8",
    )


def _ensure_git_repo(project_root: str | None) -> bool:
    """Guarantee a git repo with at least one commit before the workflow starts,
    so isolate/integrate steps have a base ref to branch from and merge into.

    - git binary missing -> return False; steps degrade to project_root without
      isolation and integrate no-ops (see run_step_worker).
    - not a repo         -> `git init`, gitignore the runtime dirs, and make one
                            initial commit of the working tree as the base.
    - repo, unborn HEAD  -> same initial commit.
    - repo with commits  -> left untouched (never auto-commit in-progress work).

    Returns True when a usable repo with a base ref exists afterwards."""
    if not _git_available():
        print(
            "git: not installed; workflow steps run in project root without "
            "worktree isolation (integrate is skipped)", flush=True,
        )
        return False
    root = _project_root(project_root)
    if not _is_git_repo(root):
        try:
            cp = _git(root, "init")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"git: init failed in {root}: {exc!r}", flush=True)
            return False
        if cp.returncode != 0:
            print(f"git: init failed in {root}: {(cp.stderr or cp.stdout).strip()}", flush=True)
            return False
        print(f"git: initialized empty repository in {root}", flush=True)
    if _worktree_base_ref(root) is not None:
        return True  # already has a commit to branch from
    # Unborn HEAD: create the isolation base. Ignore orbit's runtime dirs first
    # so the worktrees/logs are never committed into the base tree. Pass an inline
    # identity so the commit never fails on a machine with no global git identity.
    _ensure_state_dir_gitignored(root)
    ident = ("-c", "user.name=orbit", "-c", "user.email=orbit@localhost")
    msg = "orbit: initialize repository for worktree isolation"
    try:
        _git(root, "add", "-A")
        cp = _git(root, *ident, "commit", "-m", msg)
        if cp.returncode != 0:
            # Empty project (nothing staged): seed an empty root commit so a base
            # ref still exists for worktrees to branch off.
            _git(root, *ident, "commit", "--allow-empty", "-m", msg)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"git: initial commit failed in {root}: {exc!r}", flush=True)
        return False
    if _worktree_base_ref(root) is None:
        print(f"git: could not create an initial commit in {root}", flush=True)
        return False
    print(f"git: created initial commit as the worktree base in {root}", flush=True)
    return True


def _branch_exists(root: Path, branch: str) -> bool:
    try:
        return _git(
            root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", timeout=10
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _worktree_registered(root: Path, wt_dir: Path) -> bool:
    try:
        out = _git(root, "worktree", "list", "--porcelain", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    target = str(wt_dir.resolve())
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            try:
                if str(Path(path).resolve()) == target:
                    return True
            except OSError:
                continue
    return False


def _ensure_task_worktree(project_root: str | None, task_id: int) -> Path | None:
    """Idempotently return a per-task git worktree, creating it if needed.

    Returns None (and logs) when the project isn't a git repo or has no commits,
    so the caller falls back to running in project_root. Idempotent because the
    engine re-runs a step after its lease expires: an already-present worktree /
    branch is reattached instead of recreated, a stale registration left by a
    SIGKILLed run is pruned first, and concurrent adds for different tasks that
    briefly contend on the repo lock are retried."""
    import shutil

    root = _project_root(project_root)
    if not _is_git_repo(root):
        print(
            f"worktree: {root} is not a git repo; step runs in project root "
            "without isolation", flush=True,
        )
        return None
    base = _worktree_base_ref(root)
    if base is None:
        print(
            f"worktree: {root} has no commits yet; step runs without isolation",
            flush=True,
        )
        return None
    wt_dir = _task_worktree_dir(project_root, task_id)
    branch = _worktree_branch(task_id)
    if wt_dir.exists() and _worktree_registered(root, wt_dir):
        return wt_dir
    try:
        _git(root, "worktree", "prune")  # drop stale registrations from killed runs
    except (OSError, subprocess.SubprocessError):
        pass
    # A leftover unregistered directory (e.g. a failed force-remove) would make
    # `worktree add` fail with "already exists"; clear it before adding.
    if wt_dir.exists() and not _worktree_registered(root, wt_dir):
        shutil.rmtree(wt_dir, ignore_errors=True)
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for attempt in range(WORKTREE_LOCK_RETRIES):
        try:
            if _branch_exists(root, branch):
                cp = _git(root, "worktree", "add", str(wt_dir), branch)
            else:
                cp = _git(root, "worktree", "add", "-b", branch, str(wt_dir), base)
        except (OSError, subprocess.SubprocessError) as exc:
            last_err = repr(exc)
            cp = None
        if cp is not None and cp.returncode == 0:
            return wt_dir
        if cp is not None:
            last_err = (cp.stderr or cp.stdout).strip()
        # A racing worker for the same task may have won the add; accept it.
        if _worktree_registered(root, wt_dir):
            return wt_dir
        time.sleep(0.2 * (attempt + 1))
    print(f"worktree: failed to create {wt_dir}: {last_err}", flush=True)
    return None


def _remove_task_worktree(project_root: str | None, task_id: int) -> None:
    root = _project_root(project_root)
    wt_dir = _task_worktree_dir(project_root, task_id)
    for args in (
        ("worktree", "remove", "--force", str(wt_dir)),
        ("worktree", "prune"),
        ("branch", "-D", _worktree_branch(task_id)),
    ):
        try:
            _git(root, *args)
        except (OSError, subprocess.SubprocessError):
            pass


def _sweep_task_worktrees(store: Store, project_root: str | None) -> None:
    """Reap per-task worktrees whose task has finished the workflow (see
    _task_workflow_finished) or is gone. Runs on the timeout watcher so
    SIGKILLed runs that never cleaned up (their trap can't fire on SIGKILL)
    don't leak worktrees and branches indefinitely."""
    root = _project_root(project_root)
    wt_root = project_state_dir(root) / "worktrees"
    if not wt_root.exists() or not _is_git_repo(root):
        return
    try:
        _git(root, "worktree", "prune")
    except (OSError, subprocess.SubprocessError):
        pass
    for child in sorted(wt_root.iterdir()):
        if not child.is_dir() or not child.name.startswith("task-"):
            continue
        try:
            task_id = int(child.name[len("task-"):])
        except ValueError:
            continue
        task = store.get_task(task_id)
        if not _task_workflow_finished(task):
            continue
        _remove_task_worktree(project_root, task_id)


# --- machine verification gate ---------------------------------------------
# The agent self-reports its outcome; a step's `verify` command lets the engine
# objectively check the work (tests/build) and override an over-optimistic
# `done`. Runs in the step's working tree so it sees exactly what the agent
# produced (the per-task worktree for isolated steps).

VERIFY_HARD_TIMEOUT_SECONDS = 900.0


def _run_step_verify(command: str, cwd: Path) -> tuple[int, str]:
    """Run a step's verify command in cwd; return (exit_code, combined_output).
    A timeout or spawn failure counts as a failing gate (nonzero)."""
    try:
        cp = subprocess.run(
            command, shell=True, cwd=str(cwd),
            capture_output=True, text=True,
            timeout=VERIFY_HARD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, f"verify timed out after {int(VERIFY_HARD_TIMEOUT_SECONDS)}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"verify failed to run: {exc!r}"
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


# --- goal convergence gate --------------------------------------------------
# Subtasks pass their own isolated tests, but the integrated main can still
# fail. When all of a goal's subtasks close, a queued goal_verify action runs
# the acceptance suite on main; this sweep applies the real exit code.

GOAL_VERIFY_POLL_SECONDS = 30
# Reclaim a goal_verify action stuck in 'running' by a crashed daemon.
GOAL_VERIFY_STALE_SECONDS = VERIFY_HARD_TIMEOUT_SECONDS + 300.0


def _iso_age_seconds(ts: str) -> float:
    """Seconds since an ISO-8601 timestamp; inf if unparseable."""
    try:
        parsed = datetime.fromisoformat((ts or "").strip())
    except (ValueError, TypeError):
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def goal_verify_sweep(store: Store, project_root: str | None) -> list[dict[str, Any]]:
    """Run queued goal-convergence verifications on the integrated main tree and
    accept or stall each goal by the real exit code. Single-owner (one daemon):
    claims pending goal_verify actions plus any left 'running' by a crashed
    process. Runs the suite synchronously on its own thread, so goals verify one
    at a time on main — no concurrent-suite thrash, no merge races. Each goal
    runs its own goal_verify command (falling back to auto-detect)."""
    pending = [
        a for a in store.list_workflow_actions("pending", limit=100)
        if a["action_type"] == "goal_verify"
    ]
    stale = [
        a for a in store.list_workflow_actions("running", limit=100)
        if a["action_type"] == "goal_verify"
        and _iso_age_seconds(a.get("updated_at", "")) > GOAL_VERIFY_STALE_SECONDS
    ]
    members = read_team_config(project_root)["members"]
    processed: list[dict[str, Any]] = []
    for action in pending + stale:
        goal_id = int(action["task_id"])
        goal = store.get_task(goal_id)
        if not goal or not goal.get("is_goal") or goal.get("task_status") == "closed":
            store.finish_workflow_action(action["id"], "done", "goal gone/closed")
            continue
        command = _effective_goal_verify(goal, project_root)
        if not command:
            # No command to run (goal set none and nothing auto-detects): accept
            # the goal on aggregation rather than leaving the action stuck.
            store.set_task_workflow_state(goal_id, task_status="accepted")
            store.finish_workflow_action(action["id"], "done", "no goal_verify command")
            processed.append({"goal_id": goal_id, "exit_code": 0})
            continue
        # Mark running so a restart mid-verify can tell in-flight from queued.
        store.finish_workflow_action(action["id"], "running", "verifying integrated main")
        run = store.create_task_run(
            goal_id, worker="goal-verify", command=command, workflow_step="goal_verify"
        )
        if run:
            log_dir = _task_run_dir(project_root, goal_id, int(run["attempt"]))
            run = store.update_task_run_log_dir(run["id"], str(log_dir)) or run
        code, out = _run_step_verify(command, _project_root(project_root))
        if run:
            try:
                _write_run_file(run, "verify", f"$ {command}  (exit {code})\n\n{out}")
                store.finish_task_run(
                    run["id"], "succeeded" if code == 0 else "failed", code
                )
            except (InvalidInputError, OSError):
                pass
        if code == 0:
            store.set_task_workflow_state(goal_id, task_status="accepted")
            store.finish_workflow_action(action["id"], "done", "goal verified on main")
        else:
            store.set_task_workflow_state(goal_id, task_status="stalled")
            store.finish_workflow_action(
                action["id"], "failed", f"goal verify failed (exit {code})"
            )
            _notify_hub(
                store, members,
                f"目标 #{goal_id} 收敛验证失败：`{command}` 退出码 {code}"
                "（引擎在集成后的 main 上判定，非 agent 自报）。"
                "各子任务在各自 worktree 里通过，但合并后失败——需人工介入或补修子任务。\n"
                f"{_tail(out, 2000)}",
            )
        processed.append({"goal_id": goal_id, "exit_code": code})
    return processed


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
    """Execute one dispatched step via the member's CLI and record the run.

    Exit 0 -> done (stdout tail as result); nonzero/timeout/missing command ->
    blocked. With advance=True the outcome is applied through the engine inline
    (legacy path); with advance=False the run is only executed and recorded and
    the parsed outcome/result is returned for the scheduler to apply."""
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
    command = _runner_command_for(member)
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
    # Defaults so the post-run verify gate can reference these even on the
    # no-command path (where they are never assigned in the else branch).
    exec_dir = _project_root(project_root)
    isolated = False
    can_rework = False
    if not command:
        result = (
            f"no runner command for agent {assignee}; set runner_command on "
            "the team member"
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
        prompt = _build_step_prompt(
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
                elif run and store.run_cancel_requested(run["id"]):
                    kill_reason = f"hub inspection killed the step after {int(elapsed)}s (stuck/errored)"
                if kill_reason is not None:
                    _kill_process_group(proc)
                    status = "timeout"
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
            stdout_thread.join(timeout=RUNNER_STREAM_DRAIN_SECONDS)
            stderr_thread.join(timeout=RUNNER_STREAM_DRAIN_SECONDS)
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
            if status == "timeout":
                pass
            elif exit_code == 0:
                result = _tail(stdout, 4000) or "runner finished with no output"
                # A clean exit defaults to done, but the runner can override via
                # a `WORKFLOW_OUTCOME:` line: any step may self-report `blocked`
                # (it ran but the work failed / is stuck); a rework-capable step
                # (e.g. review) may send the task back with `rework`.
                verdict = _parse_runner_verdict(stdout)
                if verdict == "blocked":
                    outcome, status = "blocked", "failed"
                elif verdict == "rework" and can_rework:
                    outcome, status = "rework", "succeeded"
                elif not stdout.strip():
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
                result = f"runner stdin failed: {stdin_errors[-1]}"
            else:
                result = f"runner exited {exit_code}: {_tail(stderr or stdout, 2000)}"
        except OSError as exc:
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
    goal_intake = _is_root_goal_decompose_step(project_root, task, step)
    if outcome == "done" and goal_intake:
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
    if outcome == "done" and not goal_intake and verify_cmd:
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
    tokens = _parse_run_tokens(stdout, stderr)
    if run:
        try:
            _write_run_file(run, "stdout", stdout)
            _write_run_file(run, "stderr", stderr)
            if outcome == "done":
                _write_run_file(run, "result", result)
            _append_run_event(
                run,
                {
                    "type": "runner_finished",
                    "workflow_task_id": task_id,
                    "workflow_step": step["id"],
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
        steps = {s["id"]: s for s in cfg["steps"]}
        step = steps.get(job["step"])
        if not step:
            store.finish_run_job(
                job["id"], "failed", f"unknown step {job['step']}",
                runner_name=runner_name, current_status="running",
            )
            return {"job_id": job["id"], "status": "failed", "error": "step missing"}
        members = read_team_config(project_root)["members"]
        base = _member_named(members, job["assignee"]) or {}
        member = {
            **base,
            "agent_name": job["assignee"],
            "role_id": step["role_id"],
            "enabled": True,
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
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)
        # Hand the parsed outcome/result to the scheduler via the job row. The
        # runner does not advance the workflow itself.
        outcome = report.get("outcome") or "blocked"
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


def _steps_for_roles(
    project_root: str | None, roles: list[str] | None
) -> list[str] | None:
    """Resolve role names to the workflow step ids that use them, so a runner
    scoped to --roles claims only those steps' jobs. None means no role filter."""
    roles = [r.strip() for r in (roles or []) if r.strip()]
    if not roles:
        return None
    wanted = set(roles)
    cfg = read_workflow_config(project_root)
    return [s["id"] for s in cfg["steps"] if s.get("role_id") in wanted]


def runner_loop(
    store: Store,
    project_root: str | None,
    runner_name: str,
    agents: list[str] | None = None,
    poll_seconds: float = 2.0,
    once: bool = False,
    roles: list[str] | None = None,
    max_concurrency: int = 5,
) -> None:
    """Poll and execute queued run_jobs until interrupted. `agents`/`roles`
    scope which jobs are claimed; max_concurrency runs that many jobs in
    parallel (each parallel worker leases under a distinct name)."""
    steps = _steps_for_roles(project_root, roles)
    # A role filter that matches no step would silently claim nothing; treat an
    # empty resolved set as a hard stop rather than "all steps".
    if roles and not steps:
        print(f"runner {runner_name}: no steps match roles {roles}; nothing to do", flush=True)
        return

    def _worker(name: str) -> None:
        while True:
            result = run_queued_job(
                store,
                project_root,
                runner_name=name,
                agents=agents,
                steps=steps,
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
    processed: list[dict[str, Any]] = []
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
            report = apply_run_outcome(
                store,
                project_root,
                int(job["task_id"]),
                step,
                job["assignee"],
                job.get("outcome") or "blocked",
                job.get("result") or "",
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
    for task in store.list_active_workflow_tasks():
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


# Runs left in these states mean a runner is gone, not working.
_RUN_DEAD_STATUSES = ("orphaned", "failed")
_PENDING_WORKFLOW_ACTION_STALE_SECONDS = 30


def _latest_run_for_step(
    store: Store, task: dict[str, Any], step_id: str
) -> dict[str, Any] | None:
    """Newest run for a step, read from its run holder (the goal's step card
    when materialized, else the task itself — same target run_step_worker
    records on)."""
    holder_id = task["id"]
    if _materializes_step_cards(task):
        card = store.find_open_step_card(task["id"], step_id)
        if card:
            holder_id = card["id"]
    runs = store.list_task_runs(holder_id, limit=1)
    return runs[0] if runs else None


def _run_is_after(run: dict[str, Any], transition: dict[str, Any]) -> bool:
    """Whether a run started at or after a transition — used to confirm a dead
    run belongs to the current dispatch rather than a superseded one."""
    try:
        return datetime.fromisoformat(run["started_at"]) >= datetime.fromisoformat(
            transition["created_at"]
        )
    except (ValueError, TypeError, KeyError):
        return True


def _task_has_running_run(store: Store, task_id: int) -> bool:
    holders = [task_id] + [c["id"] for c in store.list_tasks_by_parent(task_id)]
    for holder in holders:
        if any(r["status"] == "running" for r in store.list_task_runs(holder, limit=5)):
            return True
    return False


def _is_stale_timestamp(value: str, now: datetime, seconds: int) -> bool:
    try:
        created_at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    return (now - created_at).total_seconds() >= seconds


def check_task_health(
    store: Store, project_root: str | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    with _WORKFLOW_ENGINE_LOCK:
        return _check_task_health_locked(store, project_root, now)


def _check_task_health_locked(
    store: Store, project_root: str | None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Bottom-line watchdog for the otherwise purely event-driven engine: scan
    non-terminal tasks and notify the hub about stuck states nothing else
    recovers — (A) a step whose runner died (orphaned/failed run, none running),
    (B) a rework transition that never dispatched its target, and (C) a task or
    directly-executing goal with no active step and no run in progress. Alerts
    are deduped via a `health_alert` transition so the hub is not re-pinged
    every cycle for the same unchanged problem."""
    members = read_team_config(project_root)["members"]
    cfg = read_workflow_config(project_root)
    back = _workflow_graph(cfg)
    steps = {s["id"]: s for s in cfg["steps"]}
    alerts: list[dict[str, Any]] = []
    now = now or datetime.now(timezone.utc)
    for action in store.list_workflow_actions(status="pending", limit=500):
        if action.get("action_type") != "dispatch_step":
            continue
        if not _is_stale_timestamp(
            action.get("created_at", ""), now, _PENDING_WORKFLOW_ACTION_STALE_SECONDS
        ):
            continue
        task = store.get_task(action["task_id"])
        if not task or task.get("task_status") in {"closed", "accepted", "blocked"}:
            store.finish_workflow_action(action["id"], "done")
            continue
        notice = _notify_hub(
            store,
            members,
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
        transitions = store.list_task_transitions(task_id)
        active = _active_step_assignees(transitions)

        # A. A step is active but its runner is dead and nothing is running.
        for step_id, assignee in active.items():
            if step_id not in steps:
                continue
            run = _latest_run_for_step(store, task, step_id)
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
            if not _run_is_after(run, last_dispatch):
                continue
            if any(
                t["outcome"] == "health_alert" and t["to_step"] == step_id
                and t["id"] > last_dispatch["id"]
                for t in transitions
            ):
                continue  # already alerted for this dispatch
            store.record_task_transition(
                task_id, step_id, step_id, WORKFLOW_ENGINE_AGENT, "health_alert",
                f"step '{step_id}' stalled: {assignee} runner is dead, nothing running",
            )
            notice = _notify_hub(
                store, members,
                f"Task #{task_id} step '{step_id}' looks stuck: the {assignee} runner "
                "died and nothing is running. Re-run it (pick an agent) or "
                "complete_step as hub.",
            )
            alerts.append({"task_id": task_id, "step": step_id,
                           "problem": "dead runner", "notice": notice})

        # B. advance_workflow_task records rework before dispatching the target.
        # If the server dies in that small window, the old step is settled but
        # the target never becomes active.
        undispatched_rework = False
        if transitions and not active and not _task_has_running_run(store, task_id):
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
                alerted = any(
                    t["id"] > last_rework["id"]
                    and t["outcome"] == "health_alert"
                    and t["from_step"] == last_rework["from_step"]
                    and t["to_step"] == target
                    for t in transitions
                )
                undispatched_rework = not dispatched
                if not dispatched and not alerted:
                    store.record_task_transition(
                        task_id,
                        last_rework["from_step"],
                        target,
                        WORKFLOW_ENGINE_AGENT,
                        "health_alert",
                        f"rework to '{target}' was recorded but never dispatched",
                    )
                    notice = _notify_hub(
                        store, members,
                        f"Task #{task_id} recorded rework from "
                        f"'{last_rework['from_step']}' to '{target}', but the "
                        "target step was never dispatched and nothing is running. "
                        "Re-run it or complete_step as hub.",
                    )
                    alerts.append({
                        "task_id": task_id,
                        "step": target,
                        "problem": "undispatched rework",
                        "notice": notice,
                    })

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
                _root_goal_decompose_step_id(cfg, back) is None
                or not any(
                    child.get("source_message_id") is not None
                    for child in _business_subtasks_for_goal(store, task_id)
                )
            )
        )
        if (
            (not task.get("is_goal") or direct_goal)
            and task.get("task_status")
            in {"in_progress", "new", "running", "decomposing"}
            and transitions
            and not active
            and not _task_has_running_run(store, task_id)
        ):
            last_settle = max(
                (t for t in transitions if t["outcome"] in ("done", "rework")),
                key=lambda t: t["id"], default=None,
            )
            stalled_step = (last_settle or {}).get("to_step", "")
            if stalled_step and stalled_step in steps:
                store.record_task_transition(
                    task_id, "", stalled_step, WORKFLOW_ENGINE_AGENT, "blocked",
                    "orphaned: advance interrupted before dispatching this step",
                )
                store.set_task_workflow_state(
                    task_id,
                    workflow_step=stalled_step,
                    task_status="stalled" if direct_goal else "blocked",
                )
                notice = _notify_hub(
                    store, members,
                    f"Task #{task_id} was orphaned (advance interrupted at "
                    f"'{stalled_step}'); marked blocked. Re-run it or close it.",
                )
                alerts.append({"task_id": task_id, "step": stalled_step,
                               "problem": "orphaned -> blocked", "notice": notice})
            else:
                # No forward target left — the task really finished.
                if direct_goal:
                    _finish_goal_workflow(store, project_root, task)
                else:
                    store.set_task_workflow_state(
                        task_id, workflow_step="", task_status="closed"
                    )
                    _recompute_parent_goal_status(store, task, project_root)
                alerts.append({"task_id": task_id, "step": None,
                               "problem": (
                                   "orphaned -> accepted" if direct_goal
                                   else "orphaned -> closed"
                               ), "notice": ""})
    return alerts


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


def _task_runs_root(project_root: str | None) -> Path:
    return project_state_dir(_project_root(project_root)) / "tasks"


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


_FILE_APPEND_LOCK = threading.Lock()


def _append_run_file(run: dict[str, Any], file_key: str, content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        raise InvalidInputError("content must be a string")
    file_path = _task_run_file(run, file_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_APPEND_LOCK:
        with file_path.open("a", encoding="utf-8") as file:
            file.write(content)
    return {"file": file_key, "path": str(file_path), "bytes": len(content.encode("utf-8"))}


def _append_run_event(run: dict[str, Any], event: dict[str, Any]) -> None:
    record = {
        "run_id": run.get("id"),
        "task_id": run.get("task_id"),
        "attempt": run.get("attempt"),
        "worker": run.get("worker", ""),
        **event,
    }
    if "created_at" not in record:
        record["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _append_run_file(run, "events", json.dumps(record, ensure_ascii=False) + "\n")


def _write_process_stdin(
    proc: subprocess.Popen[bytes],
    payload: bytes,
    errors: list[str],
) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(payload)
    except BrokenPipeError:
        pass
    except OSError as exc:
        errors.append(str(exc))
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _stream_process_output(
    run: dict[str, Any] | None,
    proc: subprocess.Popen[bytes],
    stream_name: str,
    chunks: list[str],
) -> None:
    stream = proc.stdout if stream_name == "stdout" else proc.stderr
    if stream is None:
        return
    try:
        while True:
            try:
                raw_chunk = os.read(stream.fileno(), 4096)
            except (OSError, ValueError):
                # Our read end was closed under us (kill path unblocking a reader
                # wedged on a pipe an escaped child still holds open). Stop.
                break
            if not raw_chunk:
                break
            chunk = raw_chunk.decode("utf-8", errors="replace")
            if not chunk:
                break
            chunks.append(chunk)
            if run:
                try:
                    _append_run_file(run, stream_name, chunk)
                except (InvalidInputError, OSError):
                    pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


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
                        "command": f"hermes --profile {shlex.quote(profile_name)}",
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
    return Path(str(resources.files("orbit") / "role_templates"))


def _agents_dir(project_root: str | None) -> Path:
    # Prefer the project's own roles, then the server cwd's agents/, and as
    # a last resort the templates bundled in the package — so a fresh project
    # that never ran `orbit config` still gets the default role set.
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
    run_worker: bool = True,
    worker_concurrency: int = 5,
) -> Starlette:
    store = Store(db_path)
    # uvicorn.run() blocks until the process dies (Ctrl-C included), so a plain
    # atexit hook is the reliable place to checkpoint the WAL and close the
    # connection cleanly.
    atexit.register(store.close)

    # Embedded-runner mode: this process is the only runner, so any task_run
    # still 'running' at startup is a leftover from a crashed prior daemon.
    # Orphan them, else they count against their worker's max_concurrent_tasks
    # forever and starve assignment. Skip in decoupled mode (run_worker=False):
    # standalone runners may legitimately be mid-run when the daemon restarts.
    if run_worker:
        reaped = store.reap_stale_runs()
        if reaped:
            print(f"reaped {reaped} stale running task_runs at startup", flush=True)

    # Step-timeout watchdog: the engine is otherwise purely event-driven, so
    # a dead assignee would leave its step active forever. Daemon thread dies
    # with the process; check errors must never kill the watcher.
    def _timeout_watcher() -> None:
        while True:
            time.sleep(WORKFLOW_TIMEOUT_POLL_SECONDS)
            root = current_project.get("project_root")
            for check in (
                check_workflow_step_timeouts,
                check_task_health,
                _sweep_task_worktrees,
            ):
                try:
                    check(store, root)
                except Exception:
                    pass

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

    def _scheduler() -> None:
        while True:
            time.sleep(SCHEDULER_POLL_SECONDS)
            root = current_project.get("project_root")
            try:
                scheduler_tick(store, root)
            except Exception:
                pass

    def _hub_sweep() -> None:
        # Separate thread: a hub inspection can take up to HUB_INSPECT_TIMEOUT, so
        # it must not block the health watcher or the scheduler.
        while True:
            time.sleep(HUB_SWEEP_POLL_SECONDS)
            try:
                hub_inspect_sweep(store, current_project.get("project_root"))
            except Exception:
                pass

    def _goal_verify_sweep() -> None:
        # Separate thread: a goal-verify suite can run for minutes, so it must
        # not block the scheduler, health watcher, or hub sweep. Goals verify
        # one at a time here (serialized on main), which is what we want.
        while True:
            time.sleep(GOAL_VERIFY_POLL_SECONDS)
            try:
                goal_verify_sweep(store, current_project.get("project_root"))
            except Exception:
                pass

    def _embedded_runner() -> None:
        # Convenience: run a worker in-process so `orbit serve` executes goals
        # end-to-end without a separate `orbit runner`. For a decoupled /
        # multi-host / restart-safe setup, start serve with run_worker=False and
        # run standalone runners instead.
        runner_loop(
            store,
            current_project.get("project_root"),
            runner_name="serve-embedded",
            poll_seconds=2.0,
            max_concurrency=worker_concurrency,
        )

    threading.Thread(
        target=_timeout_watcher, name="workflow-timeout-watcher", daemon=True
    ).start()
    threading.Thread(
        target=_scheduler, name="workflow-scheduler", daemon=True
    ).start()
    threading.Thread(
        target=_hub_sweep, name="hub-inspect-sweep", daemon=True
    ).start()
    threading.Thread(
        target=_goal_verify_sweep, name="goal-verify-sweep", daemon=True
    ).start()
    if run_worker:
        threading.Thread(
            target=_embedded_runner, name="embedded-runner", daemon=True
        ).start()

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

    def _engine_force_close(task_id: int) -> dict:
        return force_close_goal(
            store, current_project.get("project_root"), task_id
        )

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
            },
        )

    @route("/api/projects", methods=["GET"])
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
        return _json(request, {"tools": tools})

    @route("/api/agent-roles", methods=["GET"])
    async def api_agent_roles(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents_dir = _agents_dir(current_project.get("project_root"))
        roles = await _to_thread(list_agent_roles, agents_dir)
        return _json(request, {"roles": roles})

    @route("/api/agent-roles/{role_id}", methods=["POST"])
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

    @route("/api/team", methods=["GET"])
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

    @route("/api/team", methods=["POST"])
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
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"success": True, **workflow})

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
                rows = [
                    _project_workflow_task_status(
                        store, current_project.get("project_root"), row, cfg
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
                        row["blocked_reason"] = _task_blocked_reason(store, row)
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
                data.get("role_required"),
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

    @route("/api/tasks/{task_id:int}/assignment-candidates", methods=["GET"])
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

    return Starlette(routes=routes)
