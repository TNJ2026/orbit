"""Cross-process ownership for one writable Orbit Runtime database."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO


class RuntimeOwnershipError(RuntimeError):
    pass


class RuntimeOwnership:
    """Hold an OS lock for as long as one process may drive a Runtime DB.

    The kernel releases the lock when the process exits, including crashes.
    Metadata is diagnostic only; it is never used to steal a live lock.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".owner.lock")
        self._file: TextIO | None = None

    def acquire(self) -> "RuntimeOwnership":
        if self._file is not None:
            return self
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == "":
                    handle.seek(0); handle.write("0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeOwnershipError(
                f"Runtime database is already owned: {self.db_path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(), "db_path": str(self.db_path),
        }, sort_keys=True))
        handle.flush()
        self._file = handle
        return self

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "RuntimeOwnership":
        return self.acquire()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()
