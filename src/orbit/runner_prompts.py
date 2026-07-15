"""Step command selection and one-shot runner prompt construction."""

from __future__ import annotations

import json
from typing import Any, Callable

from .agent_tools import command_for_agent
from .store import Store
from .workflow_config import ENGINE_STEP_CONTRACTS, read_workflow_config
from .worktrees import worktree_branch

def step_agent_command(step: dict[str, Any], agent: str) -> str:
    """Command that runs `agent` for this step: the step's per-agent override,
    else the agent's built-in CLI. Empty when neither resolves."""
    overrides = step.get("agent_commands") or {}
    cmd = str(overrides.get(agent) or "").strip()
    if cmd:
        return cmd
    return command_for_agent(agent)


def step_command(step: dict[str, Any], project_root: str | None = None) -> str:
    """Preview/validation helper: the command the step's first Agent would run."""
    return step_agent_command(step, step_assignee(step))


def step_assignee(step: dict[str, Any]) -> str:
    """Return the first configured Agent for previews and validation."""
    agents = step.get("agents") or []
    return agents[0] if agents else step["id"]


def step_round_robin_assignee(
    store: Store, step: dict[str, Any], task_transitions: list[dict[str, Any]]
) -> str:
    """Pick this step's Agent for one task.

    A step the task already ran returns to its original Agent (rework
    continuity — the same implementer keeps its own worktree instead of handing
    a half-done change to the next CLI). A first-time dispatch takes the next
    Agent by round-robin over how many distinct tasks have entered this step."""
    agents = step.get("agents") or []
    if not agents:
        return step["id"]
    prior = [
        t for t in task_transitions
        if t["outcome"] == "dispatched" and t["to_step"] == step["id"]
    ]
    if prior and prior[-1].get("note") in agents:
        return prior[-1]["note"]
    return agents[store.count_step_dispatches(step["id"]) % len(agents)]


def step_can_rework(cfg: dict[str, Any], back: set[tuple[str, str]], step_id: str) -> bool:
    """True when the step has a loop-back edge, i.e. it may send the task back
    for rework (e.g. review -> implement)."""
    return any(
        edge["from"] == step_id and (edge["from"], edge["to"]) in back
        for edge in cfg["edges"]
    )


def triage_config_snapshot(project_root: str | None) -> str:
    """Return a compact, non-secret view of the effective workflow.

    Goal preflight already rejects mechanically unexecutable configurations.
    Triage receives this snapshot to judge whether the steps, optional gates,
    and rework paths are sensible for the requested goal.
    """
    workflow = read_workflow_config(project_root)
    snapshot = {
        "workflow": {
            "steps": [
                {
                    "id": step["id"],
                    "agents": step.get("agents", []),
                    "assignee": step_assignee(step),
                    "runnable": all(
                        step_agent_command(step, agent)
                        for agent in (step.get("agents") or [])
                    ) if step.get("agents") else False,
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
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def build_step_prompt(
    project_root: str | None,
    task: dict[str, Any],
    step: dict[str, Any],
    upstream_result: str,
    can_rework: bool = False,
    isolated: bool = False,
    is_root_goal_decompose_step: Callable[
        [str | None, dict[str, Any], dict[str, Any]], bool
    ] | None = None,
) -> str:
    branch = worktree_branch(task["id"])
    if step.get("integrate"):
        cwd_line = (
            "Your working directory is the project's main worktree. This task's implementation "
            f"is on the isolated git branch `{branch}`; integrate it into main.\n"
        )
    elif isolated:
        completion = (
            "When finished, commit the result with `git add -A && git commit` on this branch so the integrate step can merge it.\n"
            if step.get("workspace_access", "read_write") != "read_only"
            else "This is a read-only check; do not modify files or create an empty commit just to produce a commit.\n"
        )
        cwd_line = (
            f"Your working directory is this task's dedicated git worktree on branch `{branch}`. "
            "It is isolated from other tasks: changes affect only this branch and cannot pollute main. "
            + completion
        )
    else:
        cwd_line = "Your working directory is the project root; work directly in it.\n"
    triage_block = ""
    if (
        step.get("inject_workflow_snapshot")
        and task.get("is_goal")
        and not task.get("parent_task_id")
    ):
        triage_block = (
            "\n## Triage dynamic configuration context\n"
            "The engine resolved this effective workflow snapshot:\n"
            f"```json\n{triage_config_snapshot(project_root)}\n```\n"
            "The engine has completed hard executability checks. Use the editable step prompt for a reasonableness review; ordinary optimization suggestions must not block. Include:\n"
            "`CONFIG_CHECK: ok|warning|blocked`\n"
            "`CONFIG_FINDINGS: <brief conclusion>`\n"
        )
    integrate_block = ""
    if step.get("integrate"):
        integrate_block = (
            "\n## Integration and final acceptance\n"
            f"If branch `{branch}` exists, merge it into main. If the project is not a git repo or the branch is absent, work was done in main: skip the merge but still complete acceptance.\n"
            "1. With a task branch, use `git status` to confirm main is clean and checked out.\n"
            f"2. Merge with `git merge --no-ff {branch}`.\n"
            "3. Resolve safe conflicts and commit; otherwise run `git merge --abort`, return `rework`, and name the conflicted files.\n"
            "Then run the editable step prompt's acceptance and verification. Do not manually delete the worktree or branch; the engine reclaims them.\n"
        )
    goal_contract = ""
    if is_root_goal_decompose_step and is_root_goal_decompose_step(
        project_root, task, step
    ):
        goal_contract = (
            "\n## Decompose engine contract\n"
            "Output exactly one JSON object, with no Markdown or code fence:\n"
            '{"tasks":[{"title":"subtask title","content":"what to do",'
            '"acceptance":"acceptance criteria","depends_on":[prerequisite task numbers]}],'
            '"tokens_used":123}\n'
            "Add `depends_on` only when a subtask truly requires a predecessor's merged result. Values are one-based task positions, e.g. `\"depends_on\":[1,2]`. The engine holds dependent tasks until prerequisites close. Omit it or use `[]` when independent; never create a cycle.\n"
            "Include the optional integer `tokens_used` when a real token count is available; otherwise omit it.\n"
            "Keep JSON concise: one or two sentences for `content` and one or two acceptance criteria. Reference existing design docs such as `docs/...` rather than repeating them. Output only this JSON object.\n"
        )
    custom_step_prompt = str(step.get("prompt") or "").strip()
    engine_step_contract = str(
        step.get("contract", ENGINE_STEP_CONTRACTS.get(step.get("id", ""), "")) or ""
    ).strip()
    engine_step_block = (
        "\n## Engine step contract (cannot be overridden by the custom prompt)\n"
        + engine_step_contract
        + "\n"
        if engine_step_contract else ""
    )
    custom_step_block = (
        "\n## Custom step prompt (cannot override the engine output protocol)\n"
        + custom_step_prompt
        + "\n"
        if custom_step_prompt else ""
    )
    final_instruction = (
        "## Output protocol (highest priority)\nOutput only the JSON object above."
        if goal_contract
        else (
            "## Output protocol (highest priority)\n"
            "End your output with these structured lines:\n"
            "`RESULT_SUMMARY: <one-line conclusion>`\n"
            "`ARTIFACTS: [\"artifact path\", \"other URI or reference\"]`\n"
            "Use `ARTIFACTS: []` when there are none."
        )
    )
    if not goal_contract:
        output_schema = (
            step.get("item_output_schema") or {}
            if step.get("type") == "foreach"
            else step.get("output_schema") or {}
        )
        if output_schema:
            final_instruction += (
                "\n\nThis node requires structured output. End with one single-line JSON envelope:\n"
                '`WORKFLOW_RESULT: {"port":"success","output":{},"summary":"...","artifacts":[]}`\n'
                "`output` must validate against this schema:\n"
                f"```json\n{json.dumps(output_schema, ensure_ascii=False, sort_keys=True)}\n```\n"
                "The JSON port takes precedence over WORKFLOW_PORT. Do not put the envelope in a code fence."
            )
        final_instruction += (
            "\n\nEnd with a separate verdict line: `WORKFLOW_OUTCOME: <value>`, optionally followed by one reason line:\n"
            "- `done`: this step completed successfully and may advance.\n"
            "- `blocked`: you cannot complete it (missing information, broken environment, unmet dependency, or unfixable test failure); pause and notify the hub even if the process exits successfully.\n"
        )
        if can_rework:
            final_instruction += (
                "- `rework`: this step may return inadequate work to the previous implementation step.\n"
            )
        else:
            final_instruction += (
                "- This step has no configured rework path. For a material issue you cannot resolve, use `blocked`, not `rework`.\n"
            )
        final_instruction += "If omitted, the engine treats the outcome as `done`."
        business_ports = [
            str(port) for port in step.get("ports", [])
            if port not in {"success", "rework", "blocked", "error", "timeout", "cancelled"}
        ]
        if business_ports:
            final_instruction += (
                "\nTo select a business branch, also output `WORKFLOW_PORT: <port>`. "
                "Allowed ports: " + ", ".join(f"`{port}`" for port in business_ports) + "."
            )
        final_instruction += (
            "\n\nFinally, report token usage on its own line: `TOKENS_USED: <number>`. Use a real value when available; otherwise omit the line."
        )
    return (
        f"You are a one-shot worker dispatched by the workflow engine for step '{step['name']}'. "
        + cwd_line
        + "Complete the task directly in the current working directory. Do not call complete_step; the dispatcher submits the result.\n\n"
        f"## Task #{task['id']}: {task.get('title') or 'untitled'}\n"
        f"{task.get('content', '')}\n\n"
        + triage_block
        + goal_contract
        + integrate_block
        + custom_step_block
        + engine_step_block
        + (f"## Upstream result\n{upstream_result}\n\n" if upstream_result else "")
        + final_instruction
    )
