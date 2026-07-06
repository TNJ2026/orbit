"""Persistent index of project daemons for the local Web UI."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from .store import DEFAULT_DB_ROOT, resolve_project_root

DEFAULT_PROJECT_INDEX_PATH = DEFAULT_DB_ROOT / "index.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_id(project_root: Path | str) -> str:
    root = Path(project_root).expanduser().resolve()
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def browser_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def server_url(host: str, port: int) -> str:
    return f"http://{browser_host(host)}:{port}"


def _read_index(index_path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    projects = data.get("projects", data if isinstance(data, list) else [])
    return [project for project in projects if isinstance(project, dict)]


def _write_index(index_path: Path, projects: list[dict[str, Any]]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: servers for different projects share this index, and
    # two starting at once must not race on the same temp file.
    tmp_path = index_path.with_suffix(f".{os.getpid()}.tmp")
    tmp_path.write_text(
        json.dumps({"projects": projects}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(index_path)


@contextmanager
def _index_write_lock(index_path: Path):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = index_path.with_suffix(index_path.suffix + ".lock")
    lock_file = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            # No fcntl (e.g. Windows) or locking unsupported on this filesystem:
            # degrade to no cross-process lock rather than failing the write.
            locked = False
        yield
    finally:
        try:
            if locked:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        finally:
            lock_file.close()


def upsert_project(
    project_root: Path | str | None = None,
    db_path: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8848,
    index_path: Path | str | None = None,
) -> dict[str, Any]:
    root = resolve_project_root(project_root)
    pid = project_id(root)
    now = _now()
    entry = {
        "id": pid,
        "name": root.name or str(root),
        "project_root": str(root),
        "db_path": str(Path(db_path).expanduser()) if db_path is not None else "",
        "server_url": server_url(host, port),
        "host": host,
        "port": port,
        "last_seen": now,
    }
    path = Path(index_path or DEFAULT_PROJECT_INDEX_PATH).expanduser()
    with _index_write_lock(path):
        projects = [
            project for project in _read_index(path)
            if project.get("id") != pid
        ]
        projects.append(entry)
        projects.sort(key=lambda project: project.get("last_seen", ""), reverse=True)
        _write_index(path, projects)
    return entry


def is_project_online(project: dict[str, Any], timeout: float = 0.25) -> bool:
    url = str(project.get("server_url", "")).rstrip("/")
    if not url:
        return False
    try:
        with urlopen(f"{url}/api/status", timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, URLError, ValueError):
        return False


def list_projects(
    current_project_id: str | None = None,
    index_path: Path | str | None = None,
    online_checker: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    path = Path(index_path or DEFAULT_PROJECT_INDEX_PATH).expanduser()
    checker = online_checker or is_project_online
    projects = []
    for project in _read_index(path):
        item = dict(project)
        item["current"] = item.get("id") == current_project_id
        item["online"] = True if item["current"] else checker(item)
        projects.append(item)
    projects.sort(key=lambda project: (not project["current"], project.get("name", "")))
    return projects
