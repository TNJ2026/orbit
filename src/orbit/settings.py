"""Per-project runtime settings persisted under the Orbit state directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import project_state_dir


MAX_REWORK_MIN, MAX_REWORK_MAX, DEFAULT_MAX_REWORK = 2, 5, 3
MAX_CONCURRENT_MIN, MAX_CONCURRENT_MAX, DEFAULT_MAX_CONCURRENT = 1, 6, 5


def settings_config_path(project_root: str | None) -> Path:
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    return project_state_dir(root) / "settings.json"


def clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def read_settings(project_root: str | None = None) -> dict[str, Any]:
    path = settings_config_path(project_root)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "max_rework_rounds": clamp_int(
            data.get("max_rework_rounds"),
            MAX_REWORK_MIN,
            MAX_REWORK_MAX,
            DEFAULT_MAX_REWORK,
        ),
        "max_concurrent_tasks": clamp_int(
            data.get("max_concurrent_tasks"),
            MAX_CONCURRENT_MIN,
            MAX_CONCURRENT_MAX,
            DEFAULT_MAX_CONCURRENT,
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
        current["max_rework_rounds"]
        if max_rework_rounds is None
        else clamp_int(
            max_rework_rounds,
            MAX_REWORK_MIN,
            MAX_REWORK_MAX,
            current["max_rework_rounds"],
        )
    )
    concurrent = (
        current["max_concurrent_tasks"]
        if max_concurrent_tasks is None
        else clamp_int(
            max_concurrent_tasks,
            MAX_CONCURRENT_MIN,
            MAX_CONCURRENT_MAX,
            current["max_concurrent_tasks"],
        )
    )
    path = settings_config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "max_rework_rounds": rework,
        "max_concurrent_tasks": concurrent,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {**data, "path": str(path)}
