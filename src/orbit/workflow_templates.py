"""Built-in workflow templates: steps, edges, default prompts, and contracts.

Development-specific defaults (software prompts, git isolation flags, engine
step contracts) live here, outside the core schema/normalization in
workflow_config.py, so the engine core stays domain-neutral.
"""

from __future__ import annotations

from typing import Any

from .store import InvalidInputError


# Default canvas layout. Nodes carry explicit x/y because the flow is not a
# simple row: product design fans out to two parallel branches (UI design and
# architecture) that merge back into implementation, and review loops back to
# implementation on rework.
_DEFAULT_STEP_MID_Y = 160


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
