"""Which run holds which project directory, machine-wide.

`workspace_access` with `isolation: none` hands an Agent the developer's real
project directory. Two runs in there at once would be two sets of CLIs with
full permissions editing the same files, so a project admits one run at a
time and the rest queue. This is the record and the mutual exclusion behind
that sentence. Design: docs/project-file-access-design.md §4.

Two locks, and always in this order:

    registration lock  →  check overlap  →  write record  →  release
                       →  then the project's own ownership lock, for the run

The registration lock is machine-wide and held for microseconds, only long
enough to make "is anything overlapping already claimed, and if not, claim
it" one atomic step. The ownership lock is per project and held for the whole
run. Taking them the other way round — asking to register while holding a
project — is what two runs would need to do to deadlock each other, so no
path here does it, including recovery and takeover.

The lock cannot be the whole story, because the kernel drops it when a
process dies and a crashed Runtime's Agent subprocesses can outlive it. So
the *record* is what blocks: it survives the lock, and a claim whose owner is
gone becomes something a person or a recovery path has to resolve, not
something the next run silently steps over. Acquiring the lock is where a
takeover check starts, never proof the project is free.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import tempfile
from typing import Iterable, TextIO
import weakref


DEFAULT_OCCUPANCY_ROOT = Path.home() / ".orbit" / "project-occupancy"
REGISTRY_LOCK_NAME = "registry.lock"


class ProjectOccupancyError(RuntimeError):
    """A project could not be claimed."""


class ProjectBusy(ProjectOccupancyError):
    """Another live run holds this project, or one that overlaps it."""


class ProjectNeedsRecovery(ProjectOccupancyError):
    """A claim outlived the Runtime that made it.

    Distinct from `ProjectBusy` on purpose: nobody is working, but nobody can
    prove the last worker's Agent subprocesses are gone either. Queueing
    behind this would wait forever; stepping over it would let a new run write
    the same files as a process that may still be writing them.
    """


def _digest(path: Path) -> str:
    """What generation of a record this is, readable or not."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _write_record(path: Path, value: dict) -> None:
    """Publish a complete record without truncating the last good copy."""

    descriptor, temporary = tempfile.mkstemp(prefix=".claim-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# A fork handler cannot be unregistered, so one is installed per process and
# it walks the locks still alive — the same reasoning as
# `runtime_ownership`, and for the same reason: `--execution-workers` forks,
# a flock belongs to the inherited open-file description, and a worker
# holding the descriptor after its Runtime exits would keep a dead run's
# claim looking live forever.
_LIVE_LOCKS: "weakref.WeakSet[_FileLock]" = weakref.WeakSet()
_FORK_HOOK_LOCK = threading.Lock()
_FORK_HOOK_REGISTERED = False


def _drop_inherited_locks() -> None:
    for lock in list(_LIVE_LOCKS):
        lock.drop_in_forked_child()


def _register_fork_hook() -> None:
    global _FORK_HOOK_REGISTERED
    if os.name == "nt" or not hasattr(os, "register_at_fork"):
        return
    with _FORK_HOOK_LOCK:
        if _FORK_HOOK_REGISTERED:
            return
        os.register_at_fork(after_in_child=_drop_inherited_locks)
        _FORK_HOOK_REGISTERED = True


def _lock_handle(handle: TextIO, *, blocking: bool) -> bool:
    """Take an exclusive OS lock on an open file. False when already held."""

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == "":
                handle.seek(0)
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(
                handle.fileno(),
                msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,
                1,
            )
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), flags)
    except (BlockingIOError, OSError):
        return False
    return True


def _unlock_handle(handle: TextIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class _FileLock:
    """One exclusive OS lock, released by the kernel if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file: TextIO | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if not _lock_handle(handle, blocking=blocking):
            handle.close()
            return False
        _register_fork_hook()
        _LIVE_LOCKS.add(self)
        self._file = handle
        return True

    def drop_in_forked_child(self) -> None:
        """Close this process's duplicate without unlocking the original.

        `LOCK_UN` here would release the parent's lock too: after a fork both
        descriptors name one open-file description, and the lock lives on that
        description rather than on either descriptor.
        """

        handle, self._file = self._file, None
        if handle is not None:
            handle.close()

    def release(self) -> None:
        _LIVE_LOCKS.discard(self)
        handle, self._file = self._file, None
        if handle is None:
            return
        _unlock_handle(handle)
        handle.close()

    def __enter__(self) -> "_FileLock":
        if not self.acquire(blocking=True):
            raise ProjectOccupancyError(f"could not take lock {self.path}")
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


@dataclass(frozen=True)
class ProjectIdentity:
    """What makes two paths the same project, or overlapping ones.

    Path strings are not enough: `/tmp` and `/private/tmp`, a
    case-insensitive filesystem, and a bind mount all give one directory more
    than one name. Device and inode identify it regardless — where the
    filesystem reports them usefully, which is not everywhere, so the resolved
    path is kept as the fallback and as the only way to see nesting.
    """

    real_path: Path
    device: int | None = None
    inode: int | None = None

    @classmethod
    def of(cls, path: Path | str) -> "ProjectIdentity":
        resolved = Path(path).expanduser().resolve()
        try:
            status = resolved.stat()
        except OSError as exc:
            raise ProjectOccupancyError(
                f"project directory is unusable: {resolved}: {exc}"
            ) from exc
        if not resolved.is_dir():
            raise ProjectOccupancyError(
                f"project path is not a directory: {resolved}"
            )
        device = getattr(status, "st_dev", 0) or None
        inode = getattr(status, "st_ino", 0) or None
        return cls(resolved, device, inode)

    @property
    def key(self) -> str:
        """A filesystem-safe name for this project's own lock and record."""

        if self.device is not None and self.inode is not None:
            seed = f"dev:{self.device}:ino:{self.inode}"
        else:
            seed = f"path:{self.real_path}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def overlaps(self, other: "ProjectIdentity") -> bool:
        """Whether one run in each of these two would share files.

        Not just "the same directory": `/project` and `/project/sub` have
        different inodes and overlap completely, and a run in each would write
        the same files while both believed they held something of their own.
        """

        if (
            self.device is not None and other.device is not None
            and self.device == other.device and self.inode == other.inode
        ):
            return True
        mine, theirs = self.real_path, other.real_path
        return mine == theirs or mine in theirs.parents or theirs in mine.parents

    def to_primitive(self) -> dict[str, object]:
        return {
            "real_path": str(self.real_path),
            "device": self.device,
            "inode": self.inode,
        }

    @classmethod
    def from_primitive(cls, value: dict) -> "ProjectIdentity":
        return cls(
            Path(str(value.get("real_path", ""))),
            value.get("device"),
            value.get("inode"),
        )


@dataclass(frozen=True)
class Occupancy:
    """One registered claim, as it survives on disk."""

    run_id: str
    identity: ProjectIdentity
    runtime_pid: int
    claimed_at: str
    state: str = "active"
    # Where this run's way back is, what it covers, and what it does not.
    # Kept here rather than with the run because this is the record designed
    # to outlive a crashed Runtime — and a crash is when somebody most needs
    # to be told what can be restored before they resolve the claim.
    recovery: dict | None = None
    # Minted per claim, never derived from the run. One run can hold this
    # project more than once — it is resolved, it recovers, it claims again —
    # and the second hold is a different thing to confirm stopped than the
    # first. Without this, two generations of one run are the same record to
    # anybody looking at it. See `claim_token`.
    claim_id: str = ""

    def to_primitive(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "identity": self.identity.to_primitive(),
            "runtime_pid": self.runtime_pid,
            "claimed_at": self.claimed_at,
            "state": self.state,
            "recovery": self.recovery,
            "claim_id": self.claim_id,
        }

    @classmethod
    def from_primitive(cls, value: dict) -> "Occupancy":
        recovery = value.get("recovery")
        return cls(
            str(value.get("run_id", "")),
            ProjectIdentity.from_primitive(value.get("identity") or {}),
            int(value.get("runtime_pid") or 0),
            str(value.get("claimed_at", "")),
            str(value.get("state", "active")),
            recovery if isinstance(recovery, dict) else None,
            str(value.get("claim_id", "")),
        )


class ProjectClaim:
    """A held project. Release it, or the project stays claimed."""

    def __init__(
        self, registry: "ProjectOccupancyRegistry", occupancy: Occupancy,
        lock: _FileLock,
    ) -> None:
        self.registry = registry
        self.occupancy = occupancy
        self._lock = lock

    @property
    def path(self) -> Path:
        return self.occupancy.identity.real_path

    @property
    def run_id(self) -> str:
        return self.occupancy.run_id

    def record_recovery(self, facts: dict) -> None:
        """Note this run's way back on the claim that outlives it."""

        self.occupancy = replace(self.occupancy, recovery=dict(facts))
        self.registry._rewrite(self.occupancy)  # noqa: SLF001

    def release(self) -> None:
        """Give the project back, in the one order that cannot deadlock.

        Ownership lock first, registration lock second — the same direction
        `claim` takes them, and never the reverse.
        """

        self._lock.release()
        self.registry._forget(self.occupancy)

    def __enter__(self) -> "ProjectClaim":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class ProjectOccupancyRegistry:
    """The machine's record of claimed project directories."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or DEFAULT_OCCUPANCY_ROOT).expanduser()

    # -- reading ----------------------------------------------------------

    def _record_path(self, occupancy: Occupancy) -> Path:
        safe = hashlib.sha256(occupancy.run_id.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{occupancy.identity.key}.{safe}.json"

    def _project_lock(self, identity: ProjectIdentity) -> _FileLock:
        return _FileLock(self.root / f"{identity.key}.owner.lock")

    def _records(
        self, *, errors: list | None = None,
    ) -> tuple[tuple[Path, Occupancy, str], ...]:
        """Every claim on record, with the file it was actually read from.

        Removal goes by the path found here rather than by recomputing a name
        from the run id: the record is the thing that blocks, so whatever is
        on disk is what has to come off it, whoever wrote it and by whatever
        name.

        Each comes with its `claim_token` — a digest of the bytes on disk, so
        an unreadable record has one too. It is what a caller that looked at a
        claim quotes back to say *that* is the claim it means.
        """

        if not self.root.is_dir():
            return ()
        found: list[tuple[Path, Occupancy, str]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = path.read_bytes()
                token = hashlib.sha256(raw).hexdigest()[:16]
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict) or not value.get("run_id"):
                    raise ValueError("missing run identity")
                occupancy = Occupancy.from_primitive(value)
                if not occupancy.identity.real_path.is_absolute():
                    raise ValueError("missing absolute project path")
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                if errors is not None:
                    errors.append({
                        "record_id": path.name, "state": "corrupt",
                        "error": str(exc),
                        # A record nobody can parse still has bytes, and those
                        # are as much a generation as a parsed claim_id is.
                        "claim_token": _digest(path),
                    })
                    continue
                raise ProjectNeedsRecovery(
                    f"cannot safely read project occupancy {path}: {exc}; "
                    "refusing new claims until the record is repaired"
                ) from exc
            found.append((path, occupancy, token))
        return tuple(found)

    def occupancies(self) -> tuple[Occupancy, ...]:
        """Every claim on record, live or abandoned."""

        return tuple(occupancy for _path, occupancy, _token in self._records())

    def holder_is_live(self, occupancy: Occupancy) -> bool:
        """Whether a Runtime still holds this claim's project lock.

        Asked of the lock, never of the recorded pid: a pid names a process
        that *was*, and the OS reuses the numbers. Taking the lock proves
        nobody holds it, which is exactly what a crash leaves behind — so a
        false answer here means "the claim is abandoned", not "the project is
        free". `ProjectNeedsRecovery` is what that distinction is for.
        """

        lock = self._project_lock(occupancy.identity)
        if not lock.acquire(blocking=False):
            return True
        lock.release()
        return False

    # -- claiming ---------------------------------------------------------

    def claim(
        self, path: Path | str, *, run_id: str, now: str | None = None,
    ) -> ProjectClaim:
        """Take a project for one run, or say precisely why it cannot be had.

        The overlap check and the record that answers it happen under the
        registration lock together, so two Runtimes asking at once cannot both
        be told yes. The project's own lock is taken after that lock is back
        down — never while holding it.
        """

        identity = ProjectIdentity.of(path)
        stamp = now or datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        occupancy = Occupancy(
            run_id, identity, os.getpid(), stamp,
            claim_id=secrets.token_hex(8),
        )

        self.root.mkdir(parents=True, exist_ok=True)
        lock = self._project_lock(identity)
        with _FileLock(self.root / REGISTRY_LOCK_NAME):
            for existing in self.occupancies():
                # Only the coordinator already holding a claim can reuse it.
                # A matching run id on disk is not evidence its old Agent died.
                if not existing.identity.overlaps(identity):
                    continue
                if existing.state != "active" or not self.holder_is_live(existing):
                    raise ProjectNeedsRecovery(
                        f"{identity.real_path} overlaps a claim left by run "
                        f"{existing.run_id!r} whose Runtime is gone; its Agent "
                        "processes must be confirmed stopped and the claim "
                        "resolved before another run may write here"
                    )
                raise ProjectBusy(
                    f"{identity.real_path} overlaps the project held by run "
                    f"{existing.run_id!r} ({existing.identity.real_path})"
                )
            # Taken before the registration lock goes down, not after. In
            # between, the record would name a claim whose lock nobody holds —
            # indistinguishable, to the next Runtime asking, from one whose
            # Runtime crashed, and it would be told this project needs
            # recovery when in fact a run was two instructions from starting.
            # Registration → ownership is also the one order this module ever
            # takes them in; the reverse is what would deadlock.
            if not lock.acquire(blocking=False):
                raise ProjectBusy(
                    f"{identity.real_path} is locked by a Runtime that left no "
                    "claim on record; refusing rather than writing beside it"
                )
            try:
                _write_record(self._record_path(occupancy), occupancy.to_primitive())
            except OSError:
                lock.release()
                raise
        return ProjectClaim(self, occupancy, lock)

    def _rewrite(self, occupancy: Occupancy) -> None:
        with _FileLock(self.root / REGISTRY_LOCK_NAME):
            for path, found, _token in self._records():
                if found.run_id == occupancy.run_id:
                    _write_record(path, occupancy.to_primitive())

    def _forget(self, occupancy: Occupancy) -> None:
        with _FileLock(self.root / REGISTRY_LOCK_NAME):
            # An unrelated broken record must not prevent a known owner
            # releasing its own claim. Broken records remain quarantined by
            # the strict claim path until explicitly resolved.
            for path, found, _token in self._records(errors=[]):
                if found.run_id == occupancy.run_id:
                    path.unlink(missing_ok=True)

    def resolve(
        self, run_id: str, *, expected_claim: str,
        processes_stopped: bool = False,
    ) -> tuple[str, ...]:
        """Clear an abandoned claim, once its processes are known to be gone.

        Deliberately explicit and deliberately not called by `claim`: proving
        an Agent subprocess died is not something this module can do, and
        clearing the record is the step that lets another run write the files
        that process may still be holding.

        `expected_claim` is the `claim_token` the caller saw, checked here
        under the registration lock. A confirmation is about the claim
        somebody looked at, and one run can hold this project more than once:
        resolved, recovered, claimed again. Between reading a page and
        answering it, the record may have become a *later* generation of the
        same run — whose Agent processes nobody has checked at all. Without
        this the stale confirmation would clear it, which is the one thing §4
        exists to prevent.

        `processes_stopped` is asked for only where a record cannot be read at
        all. Naming the run proves nothing extra about such a record — the
        match is on the file name either way — so this door and
        `resolve_record` ask the caller for the same promise.
        """

        cleared: list[str] = []
        with _FileLock(self.root / REGISTRY_LOCK_NAME):
            errors: list = []
            found = False
            for path, occupancy, token in self._records(errors=errors):
                if occupancy.run_id != run_id:
                    continue
                found = True
                if self.holder_is_live(occupancy):
                    raise ProjectBusy(f"run {run_id!r} still holds the project")
                self._check_generation(
                    expected_claim, token, f"run {run_id!r}",
                )
                path.unlink(missing_ok=True)
                cleared.append(occupancy.run_id)
            suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
            for item in errors:
                if item["record_id"].endswith(f".{suffix}.json"):
                    found = True
                    self._check_generation(
                        expected_claim, item["claim_token"], f"run {run_id!r}",
                    )
                    self._quarantine_record(
                        item["record_id"], processes_stopped=processes_stopped,
                    )
                    cleared.append(run_id)
            if not found:
                # A caller reached here having looked at a claim. Nothing to
                # clear is news, not success: reporting an empty tuple reads
                # as "cleared", and what happened is that the claim they were
                # looking at is not the one on disk any more.
                raise ProjectNeedsRecovery(
                    f"run {run_id!r} has no claim on record here; the one you "
                    "were looking at is already gone"
                )
        return tuple(cleared)

    @staticmethod
    def _check_generation(expected: str, actual: str, subject: str) -> None:
        if expected != actual:
            raise ProjectNeedsRecovery(
                f"the claim on {subject} changed since it was inspected "
                f"({expected!r} is now {actual!r}); confirm the Agent "
                "processes of the claim that is there now, not of the one "
                "that was"
            )

    def _quarantine_record(
        self, record_id: str, *, processes_stopped: bool,
    ) -> None:
        # Only names generated by _record_path can identify an ownership lock.
        parts = record_id.split(".")
        if (len(parts) != 3 or parts[2] != "json"
                or any(len(part) != 16 or any(c not in "0123456789abcdef" for c in part)
                       for part in parts[:2])):
            raise ProjectNeedsRecovery("record has no trustworthy lock identity")
        lock = _FileLock(self.root / f"{parts[0]}.owner.lock")
        if not lock.acquire(blocking=False):
            raise ProjectBusy(f"record {record_id!r} still has a live owner")
        try:
            # Taken after the lock, so "somebody still holds this" is the
            # answer a caller gets first. A free lock is not a stopped Agent
            # either — `abandon` releases it and leaves the record on purpose
            # — and a record nobody can read is the case that knows least, so
            # this is where the caller has to say it checked.
            if not processes_stopped:
                raise ProjectNeedsRecovery(
                    "confirm Agent processes are stopped before repair"
                )
            directory = self.root / "quarantine"
            directory.mkdir(exist_ok=True)
            fd, destination = tempfile.mkstemp(prefix=record_id + ".", dir=directory)
            os.close(fd)
            os.replace(self.root / record_id, destination)
        finally:
            lock.release()

    def resolve_record(
        self, record_id: str, *, expected_claim: str,
        processes_stopped: bool = False,
    ) -> None:
        """Repair one corrupt record, for a caller that cannot name its run.

        A record's file name is its project and its run, so a later generation
        of the same run lands on the same name. `expected_claim` is what
        separates them here, as it does in `resolve`.
        """

        with _FileLock(self.root / REGISTRY_LOCK_NAME):
            errors: list = []
            self._records(errors=errors)
            broken = {item["record_id"]: item for item in errors}
            if record_id not in broken:
                raise ProjectNeedsRecovery("record is not a known corrupt record")
            self._check_generation(
                expected_claim, broken[record_id]["claim_token"],
                f"record {record_id!r}",
            )
            self._quarantine_record(
                record_id, processes_stopped=processes_stopped,
            )

    def inspect(
        self, path: Path | str,
    ) -> tuple[tuple[tuple[Occupancy, str], ...], tuple[dict, ...]]:
        """Diagnostic reads report corruption without opening the claim gate.

        Each claim comes with its `claim_token`, because a page that shows a
        claim is where somebody decides to clear one, and `resolve` will ask
        which claim they meant.

        Identity is established the way `blocked_by` establishes it — device
        and inode, not the path as spelled. A page that answered "nobody holds
        this" about the very claim that just refused a run would be worse than
        no page: on a case-insensitive volume, a bind mount, or a second mount
        of the same filesystem, the two spellings are one directory.

        Where the directory itself is gone — which is one of the ways a run
        gets stuck holding it — there is no device and inode to ask for, and
        the resolved path is what is left. A diagnostic read answers with what
        it can see rather than refusing, because refusing here is refusing the
        one page that would explain the situation.
        """

        errors: list = []
        records = self._records(errors=errors)
        try:
            identity = ProjectIdentity.of(path)
        except ProjectOccupancyError:
            identity = ProjectIdentity(Path(path).expanduser().resolve())
        return (
            tuple(
                (item, token) for _path, item, token in records
                if item.identity.overlaps(identity)
            ),
            tuple(errors),
        )

    def blocked_by(self, path: Path | str) -> tuple[Occupancy, ...]:
        """Claims that would refuse a run here, for a caller that wants to say so."""

        identity = ProjectIdentity.of(path)
        return tuple(
            item for item in self.occupancies()
            if item.identity.overlaps(identity)
        )
