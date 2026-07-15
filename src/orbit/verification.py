"""Step-level machine gates and integrated Goal convergence verification."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .run_logs import task_run_dir, write_run_file
from .runner_protocol import tail
from .store import InvalidInputError, Store, UnknownAgentError


VERIFY_HARD_TIMEOUT_SECONDS = 900.0
GOAL_VERIFY_POLL_SECONDS = 30
GOAL_VERIFY_STALE_SECONDS = VERIFY_HARD_TIMEOUT_SECONDS + 300.0

WORKFLOW_ENGINE_AGENT = "workflow"
HUB_NOTIFY_AGENT = "hub"


def project_root(project_root_value: str | None) -> Path:
    return (
        Path(project_root_value).resolve()
        if project_root_value
        else Path.cwd().resolve()
    )


def detect_goal_verify(root: Path) -> str:
    """Infer a conservative project test command from root-level markers."""
    package = root / "package.json"
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if (
            isinstance(data, dict)
            and isinstance(data.get("scripts"), dict)
            and str(data["scripts"].get("test") or "").strip()
        ):
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
    if any((root / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
        for name in ("tests", "test"):
            if (root / name).is_dir():
                return f"python -m unittest discover -s {name}"
    return ""


def effective_goal_verify(
    goal: dict[str, Any] | None, project_root_value: str | None
) -> str:
    own = str((goal or {}).get("goal_verify") or "").strip()
    return own or detect_goal_verify(project_root(project_root_value))


def run_step_verify(command: str, cwd: Path) -> tuple[int, str]:
    """Run a verification command, mapping timeout and spawn errors to failure."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=VERIFY_HARD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, f"verify timed out after {int(VERIFY_HARD_TIMEOUT_SECONDS)}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"verify failed to run: {exc!r}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def iso_age_seconds(timestamp: str) -> float:
    try:
        parsed = datetime.fromisoformat((timestamp or "").strip())
    except (ValueError, TypeError):
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _notify_hub(store: Store, text: str) -> str:
    try:
        if not store.agent_exists(HUB_NOTIFY_AGENT):
            return f"hub agent {HUB_NOTIFY_AGENT!r} not registered; notice dropped"
        if not store.agent_exists(WORKFLOW_ENGINE_AGENT):
            store.register_agent(
                WORKFLOW_ENGINE_AGENT,
                "workflow engine: routes tasks along the configured workflow",
            )
        store.send_message(WORKFLOW_ENGINE_AGENT, HUB_NOTIFY_AGENT, text)
        return f"notified {HUB_NOTIFY_AGENT}"
    except (UnknownAgentError, InvalidInputError) as exc:
        return f"hub notification failed: {exc}"


def goal_verify_sweep(
    store: Store, project_root_value: str | None
) -> list[dict[str, Any]]:
    """Apply queued Goal verification actions serially on integrated main."""
    pending = [
        action
        for action in store.list_workflow_actions("pending", limit=100)
        if action["action_type"] == "goal_verify"
    ]
    stale = [
        action
        for action in store.list_workflow_actions("running", limit=100)
        if action["action_type"] == "goal_verify"
        and iso_age_seconds(action.get("updated_at", "")) > GOAL_VERIFY_STALE_SECONDS
    ]
    processed: list[dict[str, Any]] = []
    for action in pending + stale:
        goal_id = int(action["task_id"])
        goal = store.get_task(goal_id)
        if not goal or not goal.get("is_goal") or goal.get("task_status") == "closed":
            store.finish_workflow_action(action["id"], "done", "goal gone/closed")
            continue
        command = effective_goal_verify(goal, project_root_value)
        if not command:
            store.set_task_workflow_state(goal_id, task_status="accepted")
            store.finish_workflow_action(
                action["id"], "done", "no goal_verify command"
            )
            processed.append({"goal_id": goal_id, "exit_code": 0})
            continue
        store.finish_workflow_action(
            action["id"], "running", "verifying integrated main"
        )
        run = store.create_task_run(
            goal_id,
            worker="goal-verify",
            command=command,
            workflow_step="goal_verify",
        )
        if run:
            log_dir = task_run_dir(project_root_value, goal_id, int(run["attempt"]))
            run = store.update_task_run_log_dir(run["id"], str(log_dir)) or run
        code, output = run_step_verify(command, project_root(project_root_value))
        if run:
            try:
                write_run_file(
                    run, "verify", f"$ {command}  (exit {code})\n\n{output}"
                )
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
                store,
                f"目标 #{goal_id} 收敛验证失败：`{command}` 退出码 {code}"
                "（引擎在集成后的 main 上判定，非 agent 自报）。"
                "各子任务在各自 worktree 里通过，但合并后失败——需人工介入或补修子任务。\n"
                f"{tail(output, 2000)}",
            )
        processed.append({"goal_id": goal_id, "exit_code": code})
    return processed
