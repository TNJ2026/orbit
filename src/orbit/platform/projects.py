"""Project discovery, runtime database paths and the local project index.

This module is deliberately free of engine imports: it answers "which project
am I in and where does its database live", nothing else.  The legacy engine's
`.dev_loop` state directory and `messages.db` database are *not* supported
here.  They survive only as the sentinel in :func:`legacy_database_candidates`,
which exists so `orbit` can warn once that a pre-migration file is being
abandoned — the paths are stat'ed, never opened.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen


# The new Runtime keeps one database per project.  The name is deliberately
# different from the legacy `messages.db`: a file called messages.db always
# belongs to the old engine, so the two can never be confused.
RUNTIME_DB_NAME = "runtime.db"

STATE_DIR_NAME = ".orbit"

# Home-level root holding per-project databases and the project index.
DEFAULT_STATE_ROOT = Path.home() / STATE_DIR_NAME / "projects"
DEFAULT_PROJECT_INDEX_PATH = DEFAULT_STATE_ROOT / "index.json"

PROJECT_MARKERS = (".git", "pyproject.toml")

READY_PATH = "/health/ready"


def resolve_project_root(project_dir: Path | str | None = None) -> Path:
    """Nearest ancestor holding a project marker, so running from a
    subdirectory resolves to the same project as running from the root."""

    start = (
        Path.cwd().resolve()
        if project_dir is None
        else Path(project_dir).expanduser().resolve()
    )
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return start


def project_state_dir(project_root: Path | str) -> Path:
    """Per-project state directory. Always `.orbit`; the legacy `.dev_loop`
    fallback is gone with the legacy engine."""

    return Path(project_root) / STATE_DIR_NAME


def project_id(project_root: Path | str) -> str:
    root = Path(project_root).expanduser().resolve()
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def project_slug(project_root: Path | str) -> str:
    name = Path(project_root).name
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-._")
    return slug or "project"


def project_db_dir(
    project_dir: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Per-project directory under the state root.

    The short digest keeps two projects with the same leaf name apart.
    """

    project_path = resolve_project_root(project_dir)
    root = Path(base_dir or DEFAULT_STATE_ROOT).expanduser()
    return root / f"{project_slug(project_path)}-{project_id(project_path)}"


def project_db_path(
    project_dir: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Default runtime database path for a project."""

    return project_db_dir(project_dir, base_dir) / RUNTIME_DB_NAME


# --- legacy sentinel -------------------------------------------------------
#
# The two literals below are the only place in production code allowed to name
# the legacy layout.  Everything here treats them as *paths to stat*, never as
# databases to open: the migration abandons their contents on purpose.

def legacy_database_candidates(
    project_dir: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> tuple[Path, ...]:
    """Pre-migration database locations for this project, if any exist.

    Only `Path.exists()` is consulted.  Callers must not open, copy, import or
    hand these paths to a database driver.
    """

    project_path = resolve_project_root(project_dir)
    root = Path(base_dir or DEFAULT_STATE_ROOT).expanduser()
    slug = project_slug(project_path)
    digest = project_id(project_path)

    candidates = [
        root / f"{slug}-{digest}" / "messages.db",
        Path.home() / ".dev_loop" / "projects" / f"{slug}-{digest}" / "messages.db",
    ]
    return tuple(path for path in candidates if path.exists())


def legacy_engine_db_path(
    project_dir: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Database the *legacy* commands write to during the transition.

    `orbit serve` runs the new Runtime against `runtime.db`, which refuses to
    start on a file containing legacy tables. Keeping the legacy engine on its
    own filename means running `orbit up` cannot poison the new database. Both
    this function and the legacy commands disappear in M6.
    """

    return project_db_dir(project_dir, base_dir) / "messages.db"


def legacy_database_warning(paths: tuple[Path, ...]) -> str | None:
    """One-shot message for abandoned pre-migration data.

    Deliberately offers no import, copy or compatibility path: the migration
    drops legacy content, and a half-supported import would resurrect the very
    dual-state problem the cutover removes.
    """

    if not paths:
        return None
    listed = "\n".join(f"  {path}" for path in paths)
    return (
        "Found a pre-migration database from the legacy engine:\n"
        f"{listed}\n"
        "It is NOT used and NOT imported. It may hold both legacy engine data "
        "and Runtime data written before the cutover; all of it is abandoned. "
        f"This project now uses {RUNTIME_DB_NAME}. Delete the file above once "
        "you no longer need it for reference."
    )


def warn_about_legacy_database(
    project_dir: Path | str | None = None,
    base_dir: Path | str | None = None,
    emit: Callable[[str], None] | None = None,
) -> bool:
    """Warn once if a legacy database exists. Returns True when it warned."""

    message = legacy_database_warning(legacy_database_candidates(project_dir, base_dir))
    if message is None:
        return False
    (emit or _default_emit)(message)
    return True


def _default_emit(message: str) -> None:
    import sys

    print(message, file=sys.stderr)


# --- project index ---------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def browser_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


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
            # No fcntl (Windows) or unsupported filesystem: degrade to no
            # cross-process lock rather than failing the write.
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
    identifier = project_id(root)
    entry = {
        "id": identifier,
        "name": root.name or str(root),
        "project_root": str(root),
        "db_path": str(Path(db_path).expanduser()) if db_path is not None else "",
        "server_url": server_url(host, port),
        "host": host,
        "port": port,
        "last_seen": _now(),
    }
    path = Path(index_path or DEFAULT_PROJECT_INDEX_PATH).expanduser()
    with _index_write_lock(path):
        projects = [
            project for project in _read_index(path)
            if project.get("id") != identifier
        ]
        projects.append(entry)
        projects.sort(key=lambda project: project.get("last_seen", ""), reverse=True)
        _write_index(path, projects)
    return entry


def is_project_online(project: dict[str, Any], timeout: float = 0.25) -> bool:
    """Liveness probe against the new Runtime readiness endpoint."""

    url = str(project.get("server_url", "")).rstrip("/")
    if not url:
        return False
    try:
        with urlopen(f"{url}{READY_PATH}", timeout=timeout) as response:
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
