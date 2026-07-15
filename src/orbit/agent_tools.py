"""Agent CLI catalog, command resolution, and local installation detection."""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable


AGENT_TOOL_CANDIDATES = [
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

AGENT_RUNNER_COMMANDS = {
    "claude-code": "claude -p --output-format stream-json --verbose --dangerously-skip-permissions",
    "codex": 'codex exec --dangerously-bypass-approvals-and-sandbox "$(cat)"',
    "gemini": 'gemini -o json --yolo -p "$(cat)"',
    "antigravity": 'agy --dangerously-skip-permissions --print "$(cat)"',
    "hermes": 'hermes --yolo -z "$(cat)"',
    "opencode": 'opencode run --auto "$(cat)"',
}


def agent_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug or "profile"


def command_for_agent(agent: str | None) -> str:
    agent = (agent or "").strip()
    if not agent:
        return ""
    if agent in AGENT_RUNNER_COMMANDS:
        return AGENT_RUNNER_COMMANDS[agent]
    if agent.startswith("hermes-"):
        profile = agent[len("hermes-") :]
        return f'hermes --profile {shlex.quote(profile)} --yolo -z "$(cat)"'
    return ""


def detect_hermes_profiles(
    profile_root: Path | None = None,
) -> list[dict[str, str]]:
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


def detect_agent_tools(
    *,
    which: Callable[[str], str | None] = shutil.which,
    profile_loader: Callable[[], list[dict[str, str]]] = detect_hermes_profiles,
) -> list[dict[str, Any]]:
    """Detect installed CLIs, expanding every Hermes profile into an Agent."""
    tools: list[dict[str, Any]] = []
    for candidate in AGENT_TOOL_CANDIDATES:
        path = which(candidate["command"])
        tools.append({**candidate, "installed": path is not None, "path": path})
        if candidate["id"] != "hermes":
            continue
        used_ids = {"hermes"}
        for profile in profile_loader():
            profile_name = profile["name"]
            base_id = f"hermes-{agent_slug(profile_name)}"
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
