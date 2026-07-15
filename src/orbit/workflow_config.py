"""Workflow schema, normalization, persistence, and layout helpers."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .agent_tools import agent_slug as _agent_slug
from .node_handlers import get_node_handler
from .store import InvalidInputError, project_state_dir
from .worktrees import git_available as _git_available
from .workflow_graph import workflow_graph as _workflow_graph

_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}


def _config_stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size

def _project_root(project_root: str | None) -> Path:
    return Path(project_root).resolve() if project_root else Path.cwd().resolve()


def _workflow_config_path(project_root: str | None) -> Path:
    return project_state_dir(_project_root(project_root)) / "workflow.json"


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


DEFAULT_STEP_PROMPTS = {
    "intake": (
        "Normalize the Goal into scope, acceptance criteria, constraints, and only genuinely "
        "blocking ambiguities. Review the supplied effective workflow snapshot for gate and "
        "rework-path problems. Keep this pass brief; do not design, decompose, or implement."
    ),
    "product_design": (
        "Define the target users, core scenarios, scope, priorities, constraints, non-goals, and "
        "verifiable acceptance criteria. Reuse the Triage brief and keep the product definition "
        "implementation-neutral. Write the result to `docs/product.md` — downstream steps and the "
        "isolated implementers read it from there, so it must live on disk, not only in your reply."
    ),
    "ui_design": (
        "Turn the product definition into an implementable UI specification covering layout, "
        "components, states, interactions, responsive behavior, accessibility, and concrete "
        "visual tokens. Follow existing interface patterns. Write the spec to `docs/ui.md` — "
        "downstream steps and the isolated implementers read it from there, so it must live on disk."
    ),
    "architecture": (
        "Define the simplest sound architecture for the approved product and UI design. Specify "
        "module boundaries, interfaces, data models, migrations, reuse decisions, risks, and "
        "verification strategy without implementing the solution. Write it to `docs/architecture.md` "
        "— downstream steps and the isolated implementers read it from there, so it must live on disk."
    ),
    "approval": "Wait for the human owner to approve the design before decomposition.",
    "decompose": (
        "Use the design docs in `docs/` (product, UI, architecture) to split the Goal into focused, "
        "independently executable tasks aligned with ownership and module boundaries. In each task, "
        "reference the relevant `docs/…` paths instead of restating the spec — the implementer reads "
        "them from its worktree. Add dependencies only when one task truly requires another task's "
        "merged result."
    ),
    "implement": (
        "Implement the assigned task against its acceptance criteria using existing project patterns. "
        "Keep the change scoped, run relevant checks, and leave the task branch complete and reviewable. "
        "Surface approval-requiring design choices instead of expanding scope."
    ),
    "review": (
        "Independently review the submitted work for correctness, regressions, concurrency, security, "
        "maintainability, and test coverage. Do not modify the work. Report actionable findings with "
        "severity, evidence, and precise locations; request rework for material issues."
    ),
    "test": (
        "Run risk-based unit, integration, regression, and manual checks appropriate to the task. "
        "Record reproducible commands, inputs, expected and actual results, coverage boundaries, and "
        "remaining risk. Do not modify production code."
    ),
    "integrate": (
        "Integrate the task conservatively, verify the resulting main tree, and check every acceptance "
        "criterion before closing the task. Resolve only straightforward integration-local issues; "
        "request rework when conflicts, failures, or unmet criteria require implementation changes."
    ),
}

# Non-editable safety boundaries for sensitive workflow steps. Workflow
# authors may refine a step's editable prompt, but cannot turn an independent
# review into an implementation pass or let a test pass mutate production code.
ENGINE_STEP_CONTRACTS = {
    "implement": (
        "Implement only the approved task scope. Do not bypass acceptance criteria, "
        "weaken checks, or claim verification that was not actually run."
    ),
    "review": (
        "This is an independent, read-only review. Do not modify files, commit, "
        "or implement fixes; return actionable findings or a clean verdict."
    ),
    "test": (
        "Treat production code as read-only. You may create ephemeral test output, "
        "but do not change or commit production files to make checks pass."
    ),
}


def default_workflow_steps() -> list[dict[str, Any]]:
    # (id, name, required, isolate, integrate, decompose, approval, x, y)
    # isolate: run in a per-task git worktree (implement/test/review all share
    # one worktree per task, so review reads exactly what implement produced).
    # integrate: terminal single-assignee gate that merges the task's worktree
    # branch into main, verifies it, and checks the task's acceptance criteria;
    # runs in project_root, serialized by the main tree.
    # decompose: design-first — the goal itself runs intake + the product/UI/
    # architecture design once, then `decompose` splits it into implementation
    # subtasks partitioned by the architecture's modules. Each subtask starts at
    # `implement`, so the design steps run per goal, not per subtask.
    specs = [
        # Fully linear forward chain (design runs sequentially: UI then arch), so
        # every card sits on one row; rework edges loop back underneath.
        ("intake", "Triage", True, False, False, False, False, 40, _DEFAULT_STEP_MID_Y),
        ("product_design", "Product Design", False, False, False, False, False, 340, _DEFAULT_STEP_MID_Y),
        ("ui_design", "UI Design", False, False, False, False, False, 640, _DEFAULT_STEP_MID_Y),
        ("architecture", "Architecture", False, False, False, False, False, 940, _DEFAULT_STEP_MID_Y),
        # decompose: the decomposition gate. Architecture feeds it, and this step
        # splits the goal into subtasks that begin at implement.
        ("decompose", "Decompose", True, False, False, True, False, 1240, _DEFAULT_STEP_MID_Y),
        ("implement", "Implement", True, True, False, False, False, 1540, _DEFAULT_STEP_MID_Y),
        # review runs before test: a human/agent review first, then test is the
        # mandatory machine-verification gate. Set test's `verify` command (e.g.
        # the project's test suite) so a failing run objectively sends the task
        # back to implement instead of trusting a self-report.
        ("review", "Review", True, True, False, False, False, 1840, _DEFAULT_STEP_MID_Y),
        ("test", "Test", False, True, False, False, False, 2140, _DEFAULT_STEP_MID_Y),
        ("integrate", "Integrate", True, False, True, False, False, 2440, _DEFAULT_STEP_MID_Y),
    ]
    return [
        {
            "id": step_id,
            "name": name,
            "required": required,
            "isolate": isolate,
            "integrate": integrate,
            "decompose": decompose,
            "approval": approval,
            # No default Agent: every step starts unassigned. The goal-start
            # preflight (_validate_goal_auto_runners) blocks until the user picks
            # an Agent per step, so nothing silently runs the wrong CLI.
            "agents": [],
            "prompt": DEFAULT_STEP_PROMPTS[step_id],
            "x": x,
            "y": y,
        }
        for step_id, name, required, isolate, integrate, decompose, approval, x, y in specs
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
        {"from": "architecture", "to": "decompose"},        # design -> split work
        {"from": "decompose", "to": "implement"},           # split: subtasks start here
        {"from": "implement", "to": "review"},              # review first
        {"from": "review", "to": "test"},                   # then the verify gate
        {"from": "review", "to": "implement", "rework": True},
        {"from": "test", "to": "integrate"},                # merge worktree branch to main
        {"from": "test", "to": "implement", "rework": True},
        {"from": "integrate", "to": "implement", "rework": True},
    ]


# Legacy configs did not declare executor capabilities. Keep their historical
# defaults while making the normalized/runtime schema capability-driven.
_MULTI_AGENT_STEP_IDS = {"implement", "review", "test"}
_LEGACY_NON_REMOVABLE_STEP_IDS = {"intake", "implement", "review"}


def _port_slug(value: Any) -> str:
    raw = str(value or "").strip()
    return _agent_slug(raw).lower() if raw else ""


def _normalize_agents(step: dict[str, Any]) -> list[str]:
    """A step's ordered, deduplicated round-robin Agent list, capped at 3.
    Migrates a legacy single `agent` string into the list so hand-edited configs
    keep their binding instead of silently dropping to no-agent."""
    raw = step.get("agents")
    if isinstance(raw, list):
        vals = [str(a).strip() for a in raw if str(a).strip()]
    else:
        single = str(step.get("agent") or "").strip()
        vals = [single] if single else []
    out: list[str] = []
    for agent in vals:
        if agent not in out:
            out.append(agent)
        if len(out) >= 3:
            break
    return out


def _normalize_step_agent_commands(
    value: Any, agents: list[str], legacy_command: Any = None
) -> dict[str, str]:
    """{agent: shell command} for one step, keyed only to its current agents.
    Empty commands are dropped; a legacy step-level `command` fills any agent
    without its own entry so pre-per-agent configs keep running."""
    mapping = value if isinstance(value, dict) else {}
    legacy = str(legacy_command or "").strip()
    out: dict[str, str] = {}
    for agent in agents:
        cmd = str(mapping.get(agent) or "").strip() or legacy
        if cmd:
            out[agent] = cmd
    return out


def _normalize_workflow_step(
    step: Any,
    index: int,
) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise InvalidInputError("workflow step must be an object")
    step_id = _agent_slug(str(step.get("id", "") or f"step-{index + 1}"))
    name = str(step.get("name", "") or step_id).strip()
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

    try:
        timeout_minutes = int(step.get("timeout_minutes", 0) or 0)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow step timeout_minutes must be an integer") from None
    if timeout_minutes < 0:
        raise InvalidInputError("workflow step timeout_minutes must be >= 0")
    # Known standard steps receive their editable default only when the field is
    # absent. An explicit empty string means the user intentionally cleared it.
    raw_prompt = (
        step["prompt"] if "prompt" in step
        else DEFAULT_STEP_PROMPTS.get(step_id, "")
    )
    if not isinstance(raw_prompt, str):
        raise InvalidInputError("workflow step prompt must be a string")
    # An integrate step merges the worktree branch back to main, so skipping it
    # would strand every isolated step's commits on their branch; a decompose
    # step splits a goal into subtasks. Both are structural, so always required.
    # The Triage entry step (`intake`) is the goal's front door — always required
    # and locked so it can't be unchecked or removed.
    explicit_type = str(step.get("type", "") or "").strip().lower()
    legacy_approval = bool(step.get("approval", False))
    node_type = "approval" if legacy_approval else (explicit_type or "action")
    if node_type not in {"action", "approval", "end"}:
        raise InvalidInputError(f"unsupported workflow node type: {node_type}")
    raw_handler = str(step.get("handler", "") or "").strip()
    if legacy_approval and raw_handler in {"", "agent"}:
        raw_handler = "human"
    decompose = bool(step.get("decompose", False)) or raw_handler == "legacy.decompose"
    approval = node_type == "approval"
    integrate = bool(step.get("integrate", False))
    required_locked = (
        step_id == "intake"
        or integrate
        or decompose
        or approval
        or node_type == "end"
    )
    required = True if required_locked else bool(step.get("required", False))

    raw_executor = step.get("executor")
    executor = raw_executor if isinstance(raw_executor, dict) else {}
    agent_source = dict(step)
    if "agents" in executor:
        agent_source["agents"] = executor.get("agents")
    _agents = _normalize_agents(agent_source)
    default_max_agents = 3 if step_id in _MULTI_AGENT_STEP_IDS else 1
    try:
        max_agents = int(executor.get("max_agents", default_max_agents) or 1)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow executor.max_agents must be an integer") from None
    max_agents = max(1, min(3, max_agents))
    _agents = _agents[:max_agents]
    strategy = str(executor.get("strategy", "") or "").strip()
    if strategy not in {"single", "round_robin"}:
        strategy = "round_robin" if max_agents > 1 else "single"
    if strategy == "single":
        max_agents = 1
        _agents = _agents[:1]
    command_source = executor.get("command_overrides", step.get("agent_commands"))
    commands = _normalize_step_agent_commands(command_source, _agents, step.get("command"))

    raw_environment = step.get("environment")
    if isinstance(raw_environment, dict):
        environment = dict(raw_environment)
        environment_type = str(environment.get("type", "project_root") or "project_root")
    else:
        environment_type = "git.worktree" if step.get("isolate") else "project_root"
        environment = {"type": environment_type}
    if environment_type not in {"project_root", "git.worktree"}:
        raise InvalidInputError(f"unsupported workflow environment: {environment_type}")
    environment["type"] = environment_type
    if environment_type == "git.worktree":
        environment.setdefault("scope", "workflow_item")
        environment.setdefault("cleanup", "on_terminal")
    isolate = environment_type == "git.worktree" and not integrate and not decompose and not approval

    raw_ports = step.get("ports")
    if raw_ports is None:
        ports = (
            ["approved", "changes_requested", "cancelled"]
            if node_type == "approval" and not legacy_approval
            else ["success"] if node_type == "end"
            else ["success", "rework"]
        )
    elif not isinstance(raw_ports, list):
        raise InvalidInputError("workflow step ports must be a list")
    else:
        ports = []
        for raw_port in raw_ports:
            port = _port_slug(raw_port)
            if port and port not in ports:
                ports.append(port)
        if node_type == "action" and "success" not in ports:
            ports.insert(0, "success")
    implicit_default_port = (
        "approved" if node_type == "approval" and not legacy_approval else "success"
    )
    default_port = _port_slug(step.get("default_port", implicit_default_port)) or implicit_default_port
    if default_port not in ports:
        raise InvalidInputError("workflow step default_port must be declared in ports")
    raw_unrouted = step.get("unrouted", "allowed")
    if isinstance(raw_unrouted, bool):
        unrouted = "allowed" if raw_unrouted else "blocked"
    else:
        unrouted = str(raw_unrouted or "blocked").strip().lower()
    if unrouted not in {"allowed", "blocked"}:
        raise InvalidInputError("workflow step unrouted must be 'allowed' or 'blocked'")

    handler = raw_handler or (
        "legacy.decompose" if decompose
        else "human" if node_type == "approval"
        else "end" if node_type == "end"
        else "agent"
    )
    try:
        handler_spec = get_node_handler({"type": node_type, "handler": handler})
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    if not handler_spec.requires_agent:
        _agents = []
        commands = {}
        strategy = "single"
        max_agents = 1
    contract = step.get("contract", ENGINE_STEP_CONTRACTS.get(step_id, ""))
    if not isinstance(contract, str):
        raise InvalidInputError("workflow step contract must be a string")
    removable = bool(step.get("removable", step_id not in _LEGACY_NON_REMOVABLE_STEP_IDS))

    normalized_step = {
        "id": step_id,
        "name": name,
        "type": node_type,
        "handler": handler,
        "enabled": bool(step.get("enabled", True)),
        "removable": removable,
        "skippable": bool(step.get("skippable", not required)),
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
        "isolate": isolate,
        "integrate": integrate,
        "decompose": decompose,
        "approval": approval,
        "approval_required": (
            bool(step.get("approval_required", False)) if node_type == "action" else False
        ),
        # User-authored instructions for this step. They refine the generated
        # step contract but never replace the engine-owned output protocol.
        "prompt": raw_prompt.strip(),
        # verify: an objective shell command the engine runs itself after the
        # agent, in the same working tree. Its real exit code overrides the
        # agent's self-reported `done` (a failing gate the agent can't fake).
        "verify": str(step.get("verify", "") or "").strip(),
        # agents: implement/review/test allow 1-3 (round-robin); every other step
        # takes exactly one. Each dispatch advances the step's persistent cursor.
        "agents": _agents,
        # agent_commands: {agent: shell command} per this step. Blank for an
        # agent uses its built-in CLI. A legacy step-level `command` migrates
        # onto every agent so existing configs keep running.
        "agent_commands": commands,
        "executor": {
            "strategy": strategy,
            "max_agents": max_agents,
            "agents": _agents,
            "command_overrides": commands,
            "rework_affinity": str(executor.get("rework_affinity", "same_executor") or "same_executor"),
        },
        "environment": environment,
        "exclusive": str(step.get("exclusive", "project_root" if integrate else "") or "").strip(),
        "contract": contract.strip(),
        "ports": ports,
        "default_port": default_port,
        "unrouted": unrouted,
        "terminal": node_type == "end" or bool(step.get("terminal", False)),
        "x": _coord("x", 40 + index * 300),
        "y": _coord("y", _DEFAULT_STEP_MID_Y),
    }
    if handler == "command":
        normalized_step["command"] = str(step.get("command", "") or "").strip()
    return normalized_step


def _normalize_workflow_edges(
    edges: Any,
    valid_ids: set[str],
    ports_by_step: dict[str, set[str]] | None = None,
) -> list[dict[str, str]]:
    if edges is None:
        return []
    if not isinstance(edges, list):
        raise InvalidInputError("workflow edges must be a list")
    seen: set[tuple[str, str, str]] = set()
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
        port = _port_slug(edge.get("port"))
        if not port:
            port = "rework" if edge.get("rework") else "success"
        if ports_by_step is not None and port not in ports_by_step.get(src, set()):
            raise InvalidInputError(
                f"workflow edge port {port!r} is not declared by step {src!r}"
            )
        key = (src, dst, port)
        if key in seen:
            continue
        seen.add(key)
        norm_edge = {"from": src, "to": dst}
        # Keep legacy success/rework edge JSON byte-compatible when no explicit
        # port was authored; the runtime derives success/rework exactly as before.
        if str(edge.get("port", "") or "").strip():
            norm_edge["port"] = port
        if "max_iterations" in edge:
            try:
                max_iterations = int(edge.get("max_iterations"))
            except (TypeError, ValueError):
                raise InvalidInputError("workflow edge max_iterations must be an integer") from None
            if max_iterations < 1:
                raise InvalidInputError("workflow edge max_iterations must be >= 1")
            norm_edge["max_iterations"] = max_iterations
        if edge.get("rework") or port == "rework":
            # Explicit loop-back marker: lets a rework target sit off the
            # forward path (see _workflow_graph).
            norm_edge["rework"] = True
        normalized.append(norm_edge)
    return normalized


# Structural problems are warnings, not errors: the canvas saves after every
# drag/add, so a half-connected graph is a normal intermediate state.
def _workflow_graph_warnings(
    steps: list[dict[str, Any]],
    edges: list[dict[str, str]],
    git_available: Callable[[], bool] | None = None,
) -> list[str]:
    warnings: list[str] = []
    git_available = git_available or _git_available
    # git prerequisite for isolation/integration. The engine auto-inits a repo at
    # flow start when git is installed (see _ensure_git_repo), so a non-git dir is
    # NOT worth warning about — only a missing git binary is unrecoverable: those
    # isolate steps then run without a worktree; integrate still performs its
    # acceptance checks, but has no branch to merge.
    if any(s.get("isolate") or s.get("integrate") for s in steps) and not git_available():
        warnings.append(
            "git is not installed: isolate steps will run without a per-task "
            "worktree; integrate cannot merge a branch but will still run acceptance checks"
        )
    ids = [step["id"] for step in steps]
    if len(ids) <= 1:
        return warnings

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

    # Loop-back edges are conditional rework paths, not ordinary flow. They must
    # not make an otherwise terminal step look non-terminal. For legacy configs
    # with no explicit marker, retain only inferred loop-backs that belong to the
    # component reachable from a genuine raw entry. A disconnected cycle is a
    # malformed subgraph, not a valid rework path, and should still be warned.
    raw_adj: dict[str, list[str]] = {}
    raw_incoming: set[str] = set()
    for edge in edges:
        raw_adj.setdefault(edge["from"], []).append(edge["to"])
        raw_incoming.add(edge["to"])
    entries = [step_id for step_id in ids if step_id not in raw_incoming]
    explicit_rework = any(edge.get("rework") for edge in edges)
    back = _workflow_graph({"steps": steps, "edges": edges})
    if not explicit_rework:
        raw_reachable = _reach(entries, raw_adj)
        back = {edge for edge in back if edge[0] in raw_reachable}
    forward_edges = [edge for edge in edges if (edge["from"], edge["to"]) not in back]

    # A decompose step is where a goal splits into subtasks; the subtasks begin at
    # its forward successors, so it must have an outgoing step, and only one such
    # step is used.
    decompose_ids = [s["id"] for s in steps if s.get("decompose")]
    if len(decompose_ids) > 1:
        warnings.append(
            "multiple decompose steps: only the first (" + decompose_ids[0]
            + ") splits the goal"
        )
    if decompose_ids and not any(
        e["from"] == decompose_ids[0] for e in forward_edges
    ):
        warnings.append(
            f"decompose step '{decompose_ids[0]}' has no outgoing step: its "
            "subtasks would have nowhere to start"
        )
    adj: dict[str, list[str]] = {}
    radj: dict[str, list[str]] = {}
    for edge in forward_edges:
        adj.setdefault(edge["from"], []).append(edge["to"])
        radj.setdefault(edge["to"], []).append(edge["from"])
    terminals = [i for i in ids if i not in adj]

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
    cache_key = str(path.resolve())
    stamp = _config_stamp(path)
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get(cache_key)
        if cached is not None and cached[0] == stamp:
            return deepcopy(cached[1])
    if not path.exists():
        # Normalize the defaults too so unsaved workflows carry the same
        # derived fields (required_locked, timeout_minutes) as saved ones.
        statuses = default_workflow_statuses()
        default_steps = [
            _normalize_workflow_step(step, index)
            for index, step in enumerate(default_workflow_steps())
        ]
        result = {
            "steps": default_steps,
            "statuses": statuses,
            "edges": _normalize_workflow_edges(
                default_workflow_edges(),
                {step["id"] for step in default_steps},
                {step["id"]: set(step["ports"]) for step in default_steps},
            ),
            "path": str(path),
            "warnings": [],
        }
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[cache_key] = (stamp, result)
        return deepcopy(result)
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
        edges = _normalize_workflow_edges(
            raw_edges,
            valid_ids,
            {step["id"]: set(step["ports"]) for step in normalized},
        )
    result = {
        "steps": normalized,
        "statuses": statuses,
        "edges": edges,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, edges),
    }
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[cache_key] = (stamp, result)
    return deepcopy(result)
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
    # A step may be saved with no Agent (steps default empty). The agent gate is
    # deferred to goal start (_validate_goal_auto_runners), so authoring a
    # workflow and assigning Agents can happen in either order.
    # Fallback for configs written outside the UI (hand edit / API / script): keep
    # nodes from stacking on the canvas, where a covered edge reads as "not
    # connected". A UI save has already dagre-spaced them, so this is a no-op there.
    _separate_overlapping_steps(normalized)
    normalized_edges = _normalize_workflow_edges(
        edges,
        valid_ids,
        {step["id"]: set(step["ports"]) for step in normalized},
    )
    path = _workflow_config_path(project_root)
    project_root_path = _project_root(project_root)
    resolved_path = path.resolve()
    if project_root_path not in (resolved_path, *resolved_path.parents):
        raise InvalidInputError("workflow config path escapes project root")
    path.parent.mkdir(parents=True, exist_ok=True)
    # required_locked is derived from engine step flags on every read; keep it out of
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
    result = {
        "steps": normalized,
        "statuses": normalized_statuses,
        "edges": normalized_edges,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, normalized_edges),
    }
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[str(path.resolve())] = (_config_stamp(path), result)
    return deepcopy(result)
