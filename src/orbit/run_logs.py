"""Filesystem persistence and stream helpers for task-run logs."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import InvalidInputError, project_state_dir


TASK_RUN_FILES = {
    "events": "events.jsonl",
    "prompt": "prompt.txt",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "result": "result.md",
    "diff": "diff.patch",
}

_FILE_APPEND_LOCK = threading.Lock()


def task_runs_root(project_root: str | None) -> Path:
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    return project_state_dir(root) / "tasks"


def task_run_dir(
    project_root: str | None, task_id: int, attempt: int
) -> Path:
    return task_runs_root(project_root) / str(task_id) / f"run-{attempt:03d}"


def task_run_file(run: dict[str, Any], file_key: str) -> Path:
    if file_key not in TASK_RUN_FILES:
        raise InvalidInputError("unknown run file")
    raw_log_dir = str(run.get("log_dir") or "")
    if not raw_log_dir:
        raise InvalidInputError("run log directory is missing")
    log_dir = Path(raw_log_dir).resolve()
    file_path = (log_dir / TASK_RUN_FILES[file_key]).resolve()
    if log_dir not in (file_path, *file_path.parents):
        raise InvalidInputError("invalid run file path")
    return file_path


def append_run_file(
    run: dict[str, Any], file_key: str, content: str
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise InvalidInputError("content must be a string")
    file_path = task_run_file(run, file_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_APPEND_LOCK:
        with file_path.open("a", encoding="utf-8") as file:
            file.write(content)
    return {
        "file": file_key,
        "path": str(file_path),
        "bytes": len(content.encode("utf-8")),
    }


def append_run_event(run: dict[str, Any], event: dict[str, Any]) -> None:
    record = {
        "run_id": run.get("id"),
        "task_id": run.get("task_id"),
        "attempt": run.get("attempt"),
        "worker": run.get("worker", ""),
        **event,
    }
    if "created_at" not in record:
        record["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_run_file(run, "events", json.dumps(record, ensure_ascii=False) + "\n")


def write_process_stdin(
    proc: subprocess.Popen[bytes], payload: bytes, errors: list[str]
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


def stream_process_output(
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
                break
            if not raw_chunk:
                break
            chunk = raw_chunk.decode("utf-8", errors="replace")
            if not chunk:
                break
            chunks.append(chunk)
            if run:
                try:
                    append_run_file(run, stream_name, chunk)
                except (InvalidInputError, OSError):
                    pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def write_run_file(
    run: dict[str, Any], file_key: str, content: str
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise InvalidInputError("content must be a string")
    file_path = task_run_file(run, file_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {
        "file": file_key,
        "path": str(file_path),
        "bytes": len(content.encode("utf-8")),
    }


def read_run_file(
    run: dict[str, Any], file_key: str, tail: int = 65536
) -> dict[str, Any]:
    file_path = task_run_file(run, file_key)
    if not file_path.exists():
        return {"file": file_key, "path": str(file_path), "content": "", "bytes": 0}
    tail = max(1, min(int(tail), 1024 * 1024))
    file_size = file_path.stat().st_size
    truncated = file_size > tail
    if truncated:
        with file_path.open("rb") as file:
            file.seek(-tail, 2)
            chunk = file.read()
    else:
        chunk = file_path.read_bytes()
    return {
        "file": file_key,
        "path": str(file_path),
        "content": chunk.decode("utf-8", errors="replace"),
        "bytes": file_size,
        "truncated": truncated,
    }


def run_last_output_at(log_dir: str | None, started_at: datetime) -> datetime:
    """Timestamp of the latest non-empty stdout/stderr log entry."""
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
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if modified > latest:
            latest = modified
    return latest


def read_run_output_tail(log_dir: str | None, tail_bytes: int = 4000) -> str:
    """Read only the recent tail of stdout and stderr for Hub inspection."""
    if not log_dir:
        return ""
    parts: list[str] = []
    for name in ("stdout.log", "stderr.log"):
        path = Path(log_dir) / name
        try:
            with path.open("rb") as file:
                size = file.seek(0, os.SEEK_END)
                file.seek(max(0, size - tail_bytes))
                chunk = file.read().decode("utf-8", errors="replace")
            if chunk.strip():
                parts.append(chunk)
        except OSError:
            pass
    return "\n".join(parts)
