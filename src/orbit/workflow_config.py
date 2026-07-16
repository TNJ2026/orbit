"""Workflow schema, normalization, persistence, and layout helpers."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .agent_tools import agent_slug as _agent_slug
from .node_handlers import get_node_handler, workflow_node_schema
from .store import InvalidInputError, project_state_dir
from .workflow_data import resolve_path, validate_jsonlogic
from .worktrees import git_available as _git_available
from .workflow_graph import (
    workflow_execution_errors as _workflow_execution_errors,
    workflow_graph as _workflow_graph,
)

_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}
WORKFLOW_EXPRESSION_LANGUAGE = {
    "name": "jsonlogic", "version": "1", "profile": "orbit-safe-v1"
}


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
            "type": "action",
            "handler": "legacy.decompose" if decompose else "agent",
            "executor": {
                "strategy": "round_robin" if step_id in {"implement", "review", "test"} else "single",
                "max_agents": 3 if step_id in {"implement", "review", "test"} else 1,
                "rework_affinity": "same_executor",
            },
            "environment": {
                "type": "git.worktree" if isolate else "project_root",
                **({"scope": "workflow_item", "cleanup": "on_terminal"} if isolate else {}),
            },
            "enabled": True,
            "removable": step_id not in {"intake", "implement", "review"},
            "skippable": step_id not in {"intake", "decompose", "integrate"},
            "required_locked": step_id in {"intake", "decompose", "integrate"},
            "workspace_access": "read_only" if step_id in {"review", "test"} else "read_write",
            "inject_workflow_snapshot": step_id == "intake",
            "contract": ENGINE_STEP_CONTRACTS.get(step_id, ""),
            "exclusive": "project_root" if integrate else "",
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


def workflow_template_summaries() -> list[dict[str, str]]:
    return [
        {"id": "software", "name": "Software development"},
        {"id": "content", "name": "Content publishing"},
        {"id": "data", "name": "Data processing"},
    ]


def _template_action(
    step_id: str,
    name: str,
    prompt: str,
    x: float,
    *,
    max_agents: int = 1,
    workspace_access: str = "read_write",
    contract: str = "",
) -> dict[str, Any]:
    return {
        "id": step_id,
        "name": name,
        "type": "action",
        "handler": "agent",
        "agents": [],
        "executor": {
            "strategy": "round_robin" if max_agents > 1 else "single",
            "max_agents": max_agents,
            "rework_affinity": "same_executor",
        },
        "environment": {"type": "project_root"},
        "enabled": True,
        "removable": True,
        "skippable": True,
        "required": True,
        "required_locked": False,
        "workspace_access": workspace_access,
        "inject_workflow_snapshot": False,
        "contract": contract,
        "prompt": prompt,
        "ports": ["success", "rework"],
        "default_port": "success",
        "unrouted": "allowed",
        "x": x,
        "y": _DEFAULT_STEP_MID_Y,
    }


def _content_workflow_template() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    steps = [
        _template_action(
            "brief", "Brief",
            "Clarify the audience, purpose, channel, constraints, facts to preserve, and acceptance criteria for the content.",
            40,
        ),
        _template_action(
            "draft", "Draft",
            "Create the content from the approved brief. Keep claims sourced, match the intended channel, and return the draft as an artifact.",
            340,
            max_agents=3,
        ),
        _template_action(
            "editorial_review", "Editorial Review",
            "Review accuracy, structure, tone, accessibility, policy compliance, and channel fit. Request rework for material issues.",
            640,
            workspace_access="read_only",
            contract="Review independently. Do not rewrite or publish the content; report actionable findings or approve it.",
        ),
        {
            "id": "owner_approval",
            "name": "Owner Approval",
            "type": "approval",
            "handler": "human",
            "ports": ["approved", "changes_requested"],
            "default_port": "approved",
            "unrouted": "blocked",
            "required": True,
            "removable": True,
            "skippable": False,
            "prompt": "Approve the reviewed content or request specific changes.",
            "x": 940,
            "y": _DEFAULT_STEP_MID_Y,
        },
        _template_action(
            "publish", "Publish",
            "Publish or package the approved content for its target channel. Record the final location and publication evidence as artifacts.",
            1240,
            contract="Publish only the explicitly approved revision and preserve an auditable artifact reference.",
        ),
        {
            "id": "complete", "name": "Complete", "type": "end", "handler": "end",
            "ports": ["success"], "default_port": "success", "unrouted": "allowed",
            "required": True, "removable": True, "skippable": False,
            "x": 1540, "y": _DEFAULT_STEP_MID_Y,
        },
    ]
    edges = [
        {"from": "brief", "to": "draft"},
        {"from": "draft", "to": "editorial_review"},
        {"from": "editorial_review", "to": "owner_approval"},
        {"from": "editorial_review", "to": "draft", "port": "rework", "rework": True, "max_iterations": 3},
        {"from": "owner_approval", "to": "publish", "port": "approved"},
        {"from": "owner_approval", "to": "draft", "port": "changes_requested", "rework": True, "max_iterations": 3},
        {"from": "publish", "to": "complete"},
    ]
    return steps, edges


def _data_workflow_template() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    steps = [
        _template_action(
            "source", "Source",
            "Identify and inspect the input datasets, access constraints, expected grain, freshness, and required output contract.",
            40,
        ),
        _template_action(
            "transform", "Transform",
            "Clean, normalize, join, and transform the source data reproducibly. Preserve lineage and record generated datasets as artifacts.",
            340,
            max_agents=3,
        ),
        _template_action(
            "validate", "Validate",
            "Validate schema, completeness, uniqueness, ranges, reconciliation totals, and representative samples. Request rework on any material failure.",
            640,
            workspace_access="read_only",
            contract="Validate independently against the declared data contract. Do not alter source or transformed datasets to make checks pass.",
        ),
        _template_action(
            "deliver", "Deliver",
            "Package the validated output, documentation, lineage, and reproducible commands. Record every deliverable location as an artifact.",
            940,
        ),
        {
            "id": "complete", "name": "Complete", "type": "end", "handler": "end",
            "ports": ["success"], "default_port": "success", "unrouted": "allowed",
            "required": True, "removable": True, "skippable": False,
            "x": 1240, "y": _DEFAULT_STEP_MID_Y,
        },
    ]
    edges = [
        {"from": "source", "to": "transform"},
        {"from": "transform", "to": "validate"},
        {"from": "validate", "to": "deliver"},
        {"from": "validate", "to": "transform", "port": "rework", "rework": True, "max_iterations": 3},
        {"from": "deliver", "to": "complete"},
    ]
    return steps, edges


def workflow_template_definition(
    template_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    template_id = str(template_id or "software").strip().lower()
    if template_id == "software":
        return default_workflow_steps(), default_workflow_edges()
    if template_id == "content":
        return _content_workflow_template()
    if template_id == "data":
        return _data_workflow_template()
    raise InvalidInputError(f"unknown workflow template: {template_id}")


# Legacy configs did not declare executor capabilities. Keep their historical
# defaults while making the normalized/runtime schema capability-driven.
_LEGACY_STEP_CAPABILITIES = {
    "intake": {"required_locked": True, "removable": False, "skippable": False,
               "inject_workflow_snapshot": True},
    "implement": {"max_agents": 3, "removable": False, "skippable": True},
    "review": {"max_agents": 3, "removable": False, "skippable": True,
               "workspace_access": "read_only"},
    "test": {"max_agents": 3, "skippable": True, "workspace_access": "read_only"},
}


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
    if node_type not in {"action", "approval", "decision", "join", "foreach", "subflow", "end"}:
        raise InvalidInputError(f"unsupported workflow node type: {node_type}")
    raw_handler = str(step.get("handler", "") or "").strip()
    is_legacy_step = not any(
        key in step
        for key in (
            "type", "handler", "executor", "environment", "enabled",
            "removable", "skippable", "contract", "workspace_access",
            "inject_workflow_snapshot",
        )
    )
    legacy_capabilities = _LEGACY_STEP_CAPABILITIES.get(step_id, {}) if is_legacy_step else {}
    if legacy_approval and raw_handler in {"", "agent"}:
        raw_handler = "human"
    decompose = bool(step.get("decompose", False)) or raw_handler == "legacy.decompose"
    approval = node_type == "approval"
    integrate = bool(step.get("integrate", False))
    required_locked = (
        bool(step.get("required_locked", legacy_capabilities.get("required_locked", False)))
        or integrate
        or decompose
        or approval
        or node_type == "join"
        or node_type == "foreach"
        or node_type == "subflow"
        or node_type == "end"
    )
    required = True if required_locked else bool(step.get("required", False))

    raw_executor = step.get("executor")
    executor = raw_executor if isinstance(raw_executor, dict) else {}
    agent_source = dict(step)
    if "agents" in executor:
        agent_source["agents"] = executor.get("agents")
    _agents = _normalize_agents(agent_source)
    default_max_agents = int(legacy_capabilities.get("max_agents", 1))
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
    if node_type == "foreach" and environment_type == "git.worktree":
        raise InvalidInputError(
            "workflow foreach currently requires project_root environment; "
            "item-scoped git worktrees are not supported"
        )

    raw_ports = step.get("ports")
    if raw_ports is None:
        ports = (
            ["approved", "changes_requested", "cancelled"]
            if node_type == "approval" and not legacy_approval
            else ["matched", "default"] if node_type == "decision"
            else ["success"] if node_type in {"join", "foreach", "subflow", "end"}
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
    if node_type == "decision":
        implicit_default_port = "default"
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
        else "decision" if node_type == "decision"
        else "join" if node_type == "join"
        else "foreach" if node_type == "foreach"
        else "subflow" if node_type == "subflow"
        else "end" if node_type == "end"
        else "agent"
    )
    try:
        handler_spec = get_node_handler({"type": node_type, "handler": handler})
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    max_agents = min(max_agents, handler_spec.max_agents)
    _agents = _agents[:max_agents]
    if not handler_spec.requires_agent:
        _agents = []
        commands = {}
        strategy = "single"
        max_agents = 1
    contract = step.get("contract", ENGINE_STEP_CONTRACTS.get(step_id, ""))
    if not isinstance(contract, str):
        raise InvalidInputError("workflow step contract must be a string")
    removable = bool(step.get("removable", legacy_capabilities.get("removable", True)))
    workspace_access = str(
        step.get("workspace_access", legacy_capabilities.get("workspace_access", "read_write"))
        or "read_write"
    ).strip().lower()
    if workspace_access not in {"read_write", "read_only"}:
        raise InvalidInputError("workflow step workspace_access must be 'read_write' or 'read_only'")
    input_schema = step.get("input_schema") or {}
    output_schema = step.get("output_schema") or {}
    for field_name, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
        if not isinstance(schema, dict):
            raise InvalidInputError(f"workflow step {field_name} must be an object")
        if schema and schema.get("type", "object") != "object":
            raise InvalidInputError(f"workflow step {field_name} root type must be 'object'")
        try:
            json.dumps(schema)
        except (TypeError, ValueError):
            raise InvalidInputError(f"workflow step {field_name} must be JSON-serializable") from None
    raw_retry = step.get("retry") or {}
    if not isinstance(raw_retry, dict):
        raise InvalidInputError("workflow step retry must be an object")
    try:
        normalization_retries = int(raw_retry.get("normalization", 0) or 0)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow step retry.normalization must be an integer") from None
    if normalization_retries < 0 or normalization_retries > 5:
        raise InvalidInputError("workflow step retry.normalization must be between 0 and 5")
    raw_rules = step.get("rules") or []
    if not isinstance(raw_rules, list):
        raise InvalidInputError("workflow decision rules must be a list")
    rules: list[dict[str, Any]] = []
    if node_type == "decision":
        for rule_index, rule in enumerate(raw_rules):
            if not isinstance(rule, dict) or "when" not in rule:
                raise InvalidInputError(
                    f"workflow decision rule {rule_index} must contain 'when'"
                )
            rule_port = _port_slug(rule.get("port"))
            if rule_port not in ports:
                raise InvalidInputError(
                    f"workflow decision rule {rule_index} port must be declared by the node"
                )
            errors = validate_jsonlogic(rule["when"], f"rules[{rule_index}].when")
            if errors:
                raise InvalidInputError("; ".join(errors))
            rules.append({"when": deepcopy(rule["when"]), "port": rule_port})
    elif raw_rules:
        raise InvalidInputError("workflow rules are only valid on decision nodes")
    join_policy = str(
        step.get("join_policy", "all_activated") or "all_activated"
    ).strip()
    if join_policy == "all":
        join_policy = "all_activated"
    aggregation = str(step.get("aggregation", "list") or "list").strip()
    try:
        join_threshold = int(step.get("join_threshold", 0) or 0)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow join_threshold must be an integer") from None
    join_remaining = str(
        step.get("join_remaining", "continue") or "continue"
    ).strip().lower()
    if node_type == "join":
        if join_policy not in {"all_activated", "any", "quorum", "count", "all_successful"}:
            raise InvalidInputError(
                "workflow join_policy must be one of all_activated, any, "
                "quorum, count, all_successful"
            )
        if aggregation not in {"list", "object_by_source", "first"}:
            raise InvalidInputError(
                "workflow join aggregation must be list, object_by_source, or first"
            )
        if join_policy in {"quorum", "count"}:
            if join_threshold < 1:
                raise InvalidInputError(
                    "workflow join_threshold must be a positive integer "
                    "for quorum/count joins"
                )
        else:
            join_threshold = 0
        if join_remaining not in {"continue", "cancel"}:
            raise InvalidInputError(
                "workflow join_remaining must be 'continue' or 'cancel'"
            )
    items_path = str(step.get("items", "$.input.items") or "").strip()
    item_key_path = str(step.get("item_key", "") or "").strip()
    item_depends_on_path = str(
        step.get("item_depends_on", "$.depends_on") or ""
    ).strip()
    try:
        foreach_concurrency = int(step.get("max_concurrency", 1) or 1)
    except (TypeError, ValueError):
        raise InvalidInputError("workflow foreach max_concurrency must be an integer") from None
    item_output_schema = step.get("item_output_schema") or {}
    if node_type == "foreach":
        if not items_path.startswith("$"):
            raise InvalidInputError("workflow foreach items must be a '$'-rooted path")
        for field_name, path in (
            ("items", items_path),
            ("item_key", item_key_path),
            ("item_depends_on", item_depends_on_path),
        ):
            if path and not path.startswith("$"):
                raise InvalidInputError(
                    f"workflow foreach {field_name} must be a '$'-rooted path"
                )
            if path:
                try:
                    resolve_path({}, path)
                except KeyError:
                    pass
                except ValueError as exc:
                    raise InvalidInputError(
                        f"workflow foreach {field_name} path is invalid: {exc}"
                    ) from None
        if foreach_concurrency < 1 or foreach_concurrency > 100:
            raise InvalidInputError(
                "workflow foreach max_concurrency must be between 1 and 100"
            )
        if not isinstance(item_output_schema, dict):
            raise InvalidInputError("workflow foreach item_output_schema must be an object")
        if item_output_schema and item_output_schema.get("type", "object") != "object":
            raise InvalidInputError(
                "workflow foreach item_output_schema root type must be 'object'"
            )
        try:
            json.dumps(item_output_schema)
        except (TypeError, ValueError):
            raise InvalidInputError(
                "workflow foreach item_output_schema must be JSON-serializable"
            ) from None

    raw_subflow_ref = str(step.get("subflow", "") or "").strip()
    subflow_ref = _agent_slug(raw_subflow_ref) if raw_subflow_ref else ""
    if node_type == "subflow" and not subflow_ref:
        raise InvalidInputError(
            f"workflow subflow node {step_id!r} requires a 'subflow' name"
        )

    normalized_step = {
        "id": step_id,
        "name": name,
        "type": node_type,
        "handler": handler,
        "enabled": bool(step.get("enabled", True)),
        "removable": removable,
        "skippable": bool(step.get("skippable", legacy_capabilities.get("skippable", not required))),
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
        "workspace_access": workspace_access,
        "inject_workflow_snapshot": bool(
            step.get(
                "inject_workflow_snapshot",
                legacy_capabilities.get("inject_workflow_snapshot", False),
            )
        ),
        "input_schema": deepcopy(input_schema),
        "output_schema": deepcopy(output_schema),
        "retry": {"normalization": normalization_retries},
        "rules": rules,
        "join_policy": join_policy if node_type == "join" else "",
        "join_threshold": join_threshold if node_type == "join" else 0,
        "join_remaining": join_remaining if node_type == "join" else "",
        "aggregation": aggregation if node_type == "join" else "",
        "items": items_path if node_type == "foreach" else "",
        "item_key": item_key_path if node_type == "foreach" else "",
        "item_depends_on": item_depends_on_path if node_type == "foreach" else "",
        "item_output_schema": deepcopy(item_output_schema) if node_type == "foreach" else {},
        "max_concurrency": foreach_concurrency if node_type == "foreach" else 1,
        "subflow": subflow_ref if node_type == "subflow" else "",
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
        if "mapping" in edge:
            mapping = edge.get("mapping")
            if not isinstance(mapping, dict):
                raise InvalidInputError("workflow edge mapping must be an object")
            for target, rule in mapping.items():
                if not str(target).strip():
                    raise InvalidInputError("workflow edge mapping target must not be empty")
                if isinstance(rule, str):
                    source = rule
                elif isinstance(rule, dict):
                    source = str(rule.get("from") or "")
                else:
                    raise InvalidInputError(
                        "workflow edge mapping values must be path strings or objects"
                    )
                if source != "$" and not source.startswith("$."):
                    raise InvalidInputError("workflow edge mapping source must start with '$.'")
            norm_edge["mapping"] = deepcopy(mapping)
        if "condition" in edge:
            condition = edge.get("condition")
            errors = validate_jsonlogic(condition, "workflow edge condition")
            if errors:
                raise InvalidInputError("; ".join(errors))
            norm_edge["condition"] = deepcopy(condition)
        if "priority" in edge:
            if "condition" not in edge:
                raise InvalidInputError(
                    "workflow edge priority requires a condition"
                )
            try:
                priority = int(edge.get("priority"))
            except (TypeError, ValueError):
                raise InvalidInputError("workflow edge priority must be an integer") from None
            norm_edge["priority"] = priority
        if edge.get("rework") or port == "rework":
            # Explicit loop-back marker: lets a rework target sit off the
            # forward path (see _workflow_graph).
            norm_edge["rework"] = True
        normalized.append(norm_edge)
    return normalized


def _normalize_workflow_subflows(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize the top-level `subflows` map: {name: {steps, edges}}.

    Each subflow is an independently executable graph reusing the main-graph
    step/edge normalization. Unlike the main canvas (which saves half-connected
    intermediate states), subflows are authored as complete units, so
    structural problems are hard errors. First version keeps recursion out:
    no decompose steps and no nested subflow nodes inside a subflow."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidInputError(
            "workflow subflows must be an object mapping name to definition"
        )
    subflows: dict[str, dict[str, Any]] = {}
    for raw_name, definition in raw.items():
        stripped = str(raw_name or "").strip()
        if not stripped:
            raise InvalidInputError("workflow subflow name must not be empty")
        name = _agent_slug(stripped)
        if name in subflows:
            raise InvalidInputError(f"duplicate workflow subflow name: {name!r}")
        if not isinstance(definition, dict):
            raise InvalidInputError(f"workflow subflow {name!r} must be an object")
        raw_steps = definition.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise InvalidInputError(
                f"workflow subflow {name!r} must include a non-empty steps list"
            )
        steps = [
            _normalize_workflow_step(step, index)
            for index, step in enumerate(raw_steps)
        ]
        step_ids = {step["id"] for step in steps}
        if len(step_ids) != len(steps):
            raise InvalidInputError(
                f"workflow subflow {name!r} step ids must be unique"
            )
        for step in steps:
            if step.get("decompose"):
                raise InvalidInputError(
                    f"workflow subflow {name!r} must not contain a decompose "
                    f"step ({step['id']!r})"
                )
            if step.get("type") == "subflow":
                raise InvalidInputError(
                    f"workflow subflow {name!r} must not contain a nested "
                    f"subflow node ({step['id']!r})"
                )
        edges = _normalize_workflow_edges(
            definition.get("edges"),
            step_ids,
            {step["id"]: set(step["ports"]) for step in steps},
        )
        sub_cfg = {"steps": steps, "edges": edges}
        errors = _workflow_execution_errors(sub_cfg, _workflow_graph(sub_cfg))
        if errors:
            raise InvalidInputError(
                f"workflow subflow {name!r} is not executable: " + "; ".join(errors)
            )
        subflows[name] = sub_cfg
    return subflows


def _normalize_workflow_supervisor(raw: Any) -> dict[str, str]:
    """Normalize the top-level `supervisor` config: {agent, command}.

    The workflow-level supervisor runs hub supervision (stuck-run inspection)
    explicitly, decoupled from any node (design §11). Both fields are optional
    strings; an empty command falls back to the legacy implicit anchor (the
    Decompose step's first Agent)."""
    if raw is None:
        return {"agent": "", "command": ""}
    if not isinstance(raw, dict):
        raise InvalidInputError("workflow supervisor must be an object")
    normalized: dict[str, str] = {}
    for key in ("agent", "command"):
        value = raw.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise InvalidInputError(f"workflow supervisor {key} must be a string")
        normalized[key] = value.strip()
    return normalized


def _validate_subflow_references(
    steps: list[dict[str, Any]], subflows: dict[str, dict[str, Any]]
) -> None:
    for step in steps:
        if step.get("type") != "subflow":
            continue
        name = str(step.get("subflow") or "")
        if name not in subflows:
            raise InvalidInputError(
                f"workflow step {step['id']!r} references unknown subflow: {name!r}"
            )


def workflow_config_for_task(
    project_root: str | None, task: dict[str, Any] | None
) -> dict[str, Any]:
    """The workflow configuration the engine should route `task` by.

    A task with an empty `workflow_ref` traverses the main graph; a subflow
    child task traverses the named subflow, wrapped in a cfg dict of the same
    shape as the main one so every engine path works unchanged."""
    cfg = read_workflow_config(project_root)
    ref = str((task or {}).get("workflow_ref") or "").strip()
    if not ref:
        return cfg
    sub = (cfg.get("subflows") or {}).get(ref)
    if sub is None:
        raise InvalidInputError(f"unknown workflow subflow: {ref!r}")
    return {
        **cfg,
        "steps": sub["steps"],
        "edges": sub["edges"],
        # A subflow may not contain subflow nodes, so the nested map is empty.
        "subflows": {},
        "subflow": ref,
        "warnings": [],
    }


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

    # A quorum/count join whose threshold exceeds its inbound branches can never
    # reach it at runtime; that is an authoring mistake worth flagging, but the
    # graph is still executable (a smaller run may route fewer branches).
    for step in steps:
        threshold = int(step.get("join_threshold") or 0)
        if step.get("type") == "join" and threshold:
            inbound = len({e["from"] for e in forward_edges if e["to"] == step["id"]})
            if threshold > inbound:
                warnings.append(
                    f"join '{step['id']}' threshold {threshold} exceeds its "
                    f"{inbound} incoming branch(es)"
                )

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
            "subflows": {},
            "supervisor": _normalize_workflow_supervisor(None),
            "path": str(path),
            "warnings": [],
            "schema": workflow_node_schema(),
            "templates": workflow_template_summaries(),
            "expression_language": deepcopy(WORKFLOW_EXPRESSION_LANGUAGE),
        }
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[cache_key] = (stamp, result)
        return deepcopy(result)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidInputError(f"invalid workflow config: {exc}") from exc
    steps = data.get("steps", []) if isinstance(data, dict) else []
    expression_language = data.get("expression_language") if isinstance(data, dict) else None
    if expression_language is not None and expression_language != WORKFLOW_EXPRESSION_LANGUAGE:
        raise InvalidInputError(
            "unsupported workflow expression_language; expected "
            "jsonlogic v1 profile orbit-safe-v1"
        )
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
    subflows = _normalize_workflow_subflows(
        data.get("subflows") if isinstance(data, dict) else None
    )
    _validate_subflow_references(normalized, subflows)
    supervisor = _normalize_workflow_supervisor(
        data.get("supervisor") if isinstance(data, dict) else None
    )
    result = {
        "steps": normalized,
        "statuses": statuses,
        "edges": edges,
        "subflows": subflows,
        "supervisor": supervisor,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, edges),
        "schema": workflow_node_schema(),
        "templates": workflow_template_summaries(),
        "expression_language": deepcopy(WORKFLOW_EXPRESSION_LANGUAGE),
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
    subflows: Any = None,
    supervisor: Any = None,
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
    # subflows/supervisor=None means "leave them as they are": the workflow
    # canvas only edits the main graph, and its save must not drop authored
    # subflows or the workflow-level supervisor.
    if (subflows is None or supervisor is None) and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            if subflows is None:
                subflows = existing.get("subflows")
            if supervisor is None:
                supervisor = existing.get("supervisor")
    normalized_subflows = _normalize_workflow_subflows(subflows)
    normalized_supervisor = _normalize_workflow_supervisor(supervisor)
    _validate_subflow_references(normalized, normalized_subflows)
    project_root_path = _project_root(project_root)
    resolved_path = path.resolve()
    if project_root_path not in (resolved_path, *resolved_path.parents):
        raise InvalidInputError("workflow config path escapes project root")
    path.parent.mkdir(parents=True, exist_ok=True)
    persisted = normalized
    data = {
        "steps": persisted,
        "edges": normalized_edges,
        "expression_language": deepcopy(WORKFLOW_EXPRESSION_LANGUAGE),
    }
    if normalized_subflows:
        data["subflows"] = normalized_subflows
    if normalized_supervisor["agent"] or normalized_supervisor["command"]:
        data["supervisor"] = normalized_supervisor
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result = {
        "steps": normalized,
        "statuses": normalized_statuses,
        "edges": normalized_edges,
        "subflows": normalized_subflows,
        "supervisor": normalized_supervisor,
        "path": str(path),
        "warnings": _workflow_graph_warnings(normalized, normalized_edges),
        "schema": workflow_node_schema(),
        "templates": workflow_template_summaries(),
        "expression_language": deepcopy(WORKFLOW_EXPRESSION_LANGUAGE),
    }
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[str(path.resolve())] = (_config_stamp(path), result)
    return deepcopy(result)
