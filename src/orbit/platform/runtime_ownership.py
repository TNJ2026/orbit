"""Cross-process ownership for one writable Orbit Runtime database.

The lock doubles as the discovery record. A client that wants to talk to a
Runtime it did not start — a plugin host, an editor, a second terminal —
needs two facts nobody else can supply truthfully: that a Runtime is alive,
and where it answers. The owner is the only process entitled to write the
second, and the lock it already holds is the only honest source of the
first.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Iterator, Mapping, TextIO


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
        self._file = handle
        self._write({})
        return self

    def _write(self, facts: Mapping[str, object]) -> None:
        handle = self._file
        assert handle is not None
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(), "db_path": str(self.db_path), **facts,
        }, sort_keys=True))
        handle.flush()

    def publish(self, **facts: object) -> None:
        """Record where this Runtime answers, for clients that must find it.

        Only the owner may write here, and only while it still owns the
        database — which is what makes a published endpoint trustworthy rather
        than a note anyone could have left. Replaces any previously published
        facts, so a caller publishes the whole set each time.
        """

        if self._file is None:
            raise RuntimeOwnershipError(
                "cannot publish runtime facts without holding the lock"
            )
        self._write(facts)

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


DEFAULT_RUNTIME_ROOT = Path.home() / ".orbit"
_LOCK_SUFFIX = ".owner.lock"


@dataclass(frozen=True)
class DiscoveredRuntime:
    """One Runtime that is alive right now, and how to reach it."""

    lock_path: Path
    facts: Mapping[str, object]

    @property
    def db_path(self) -> str:
        return str(self.facts.get("db_path", ""))

    @property
    def pid(self) -> int | None:
        value = self.facts.get("pid")
        return value if isinstance(value, int) else None

    @property
    def base_url(self) -> str | None:
        """Where this Runtime answers, or None if it has not published yet.

        A Runtime is discoverable from the moment it takes the lock, which is
        before it binds a port. A client that finds one without an endpoint has
        found a Runtime still starting up, not a broken one.
        """

        value = self.facts.get("base_url")
        return value if isinstance(value, str) and value else None


def _owner_is_live(lock_path: Path) -> bool:
    """Whether somebody still owns this database, asked of the lock itself.

    Taking the lock proves nobody holds it — which is exactly what a crashed
    owner leaves behind, since the kernel drops the lock but not the file. The
    recorded pid cannot answer this: it may name a zombie, or a number the OS
    has already handed to something unrelated.
    """

    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except (BlockingIOError, OSError):
        return True
    finally:
        handle.close()


def discover_runtimes(
    root: Path | str | None = None,
) -> tuple[DiscoveredRuntime, ...]:
    """Every live Runtime under `root`, in stable path order.

    This is the supported way for a client that did not start a Runtime to find
    one — a plugin host, an editor, a second terminal. It reports only what an
    owner published about itself while holding its lock, so a stale record from
    a crashed process is never mistaken for a live endpoint.
    """

    base = Path(root).expanduser() if root is not None else DEFAULT_RUNTIME_ROOT
    if not base.is_dir():
        return ()
    found: list[DiscoveredRuntime] = []
    for lock_path in sorted(base.rglob(f"*{_LOCK_SUFFIX}")):
        if not _owner_is_live(lock_path):
            continue
        try:
            facts = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(facts, dict):
            found.append(DiscoveredRuntime(lock_path, facts))
    return tuple(found)
