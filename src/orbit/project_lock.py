"""Single-project daemon ownership lock."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from .store import project_state_dir


class ProjectAlreadyRunningError(RuntimeError):
    pass


class ProjectProcessLock:
    def __init__(self, project_root: Path):
        self.path = project_state_dir(project_root) / "serve.lock"
        self._file: IO[str] | None = None

    def __enter__(self) -> "ProjectProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.write("0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            lock_file.close()
            raise ProjectAlreadyRunningError(
                f"orbit serve is already running for this project ({self.path})"
            ) from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        self._file = lock_file
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
