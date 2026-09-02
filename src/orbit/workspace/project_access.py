"""Explicit, opt-in delivery of real project files into an Agent's own
working directory — what `orbit serve --agent-project-access` grants.

Two shapes, one narrow interface (`acquire(ref, *, files=None) -> Path`),
chosen once at Runtime startup based on whether the Workspace is a usable git
repository:

* `GitWorktreeGrant` — the whole tracked tree, via an isolated `git worktree`
  (`GitWorkspaceProvider`, already the one place in the runtime that touches a
  real checkout). Cheap and safe because git's own object store makes "the
  whole tree" cheap, and the worktree is disposable and never merged back.
* `FileAllowlistGrant` — for anything that is not a usable git repository.
  There is no equivalent "only the tracked files" boundary outside git, so
  this never copies more than a caller-named allowlist of relative paths —
  never a denylist over "everything except". A caller wanting the whole
  directory says so explicitly (`files=["**/*"]`), still bounded by the same
  size and disk-headroom limits as any narrower request.

Neither grant is confinement. Nothing here stops a CLI running inside a
granted directory from writing files of its own; what both guarantee is that
those writes land in a disposable copy — a throwaway git branch, or a plain
directory nothing reads from again — never the developer's real working tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import shutil
import threading
from typing import Callable, Iterable, Sequence

from .git import GitWorkspaceProvider, WorkspaceError, workspace_slug


# A quota this generous still bounds a run-away grant; it is not meant to be
# the right number for every deployment; `orbit serve` accepts CLI flags to
# change it (see `--agent-project-access-max-bytes`).
DEFAULT_MAX_BYTES = 2 * 1024**3
# Two ways to describe "leave the disk alone": an absolute floor and a
# fraction of the volume, whichever is larger — a tiny disk keeps its
# absolute floor, a huge one is not allowed to fill to within a few bytes of
# empty just because 10 GiB is a small fraction of it.
DEFAULT_MIN_FREE_BYTES = 10 * 1024**3
DEFAULT_MIN_FREE_FRACTION = 0.10


class QuotaExceeded(RuntimeError):
    """A grant would exceed the configured size or disk-headroom limit.

    Deliberately its own type, not `WorkspaceError`: a quota is a policy this
    deployment chose, not evidence that the mechanism itself is broken, and a
    caller may reasonably want to tell the two apart.
    """


@dataclass
class GitWorktreeGrant:
    """Whole-tree access to a git Workspace, via an isolated worktree."""

    provider: GitWorkspaceProvider
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False,
    )

    def acquire(self, ref: str, *, files: Sequence[str] | None = None) -> Path:
        # `files` is accepted and ignored: a worktree already isolates the
        # whole tracked tree cheaply and safely, and narrowing it further on
        # top of that isolation would only add complexity for no real gain.
        # `files` matters to `FileAllowlistGrant`, the other implementation of
        # this same interface — a caller that does not know which one it is
        # talking to should never have to care.
        with self._lock:
            return self.provider.acquire(ref).path

    def sweep(self, live_refs: Iterable[str]) -> tuple[str, ...]:
        with self._lock:
            return self.provider.sweep(frozenset(live_refs))

    def sweep_live(
        self, live_refs: Callable[[], Iterable[str]],
    ) -> tuple[str, ...]:
        """Resolve liveness and reclaim under the same lock as acquire()."""

        with self._lock:
            return self.provider.sweep(frozenset(live_refs()))


@dataclass
class FileAllowlistGrant:
    """Access to a caller-named allowlist of files, copied into a disposable
    directory — the answer for a Workspace that is not a usable git repository.
    """

    project_root: Path
    state_dir: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    min_free_fraction: float = DEFAULT_MIN_FREE_FRACTION
    _root: Path = field(init=False, repr=False)
    _staging_root: Path = field(init=False, repr=False)
    _markers_root: Path = field(init=False, repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        self.state_dir = Path(self.state_dir)
        self._root = self.state_dir / "project-files"
        # A sibling of `_root`, not a child of it: `sweep()` only ever
        # iterates `_root`, so a copy still being staged here is invisible to
        # it — the same directory a still-executing node is reading from
        # would otherwise look, to a name-based sweep, exactly like an
        # abandoned one. Kept on the same filesystem as `_root` (both direct
        # children of `state_dir`) so the rename below is atomic.
        self._staging_root = self.state_dir / "project-files-staging"
        # Keep implementation metadata outside the directory handed to the
        # Agent: an allowlist must not grow an undeclared marker file.
        self._markers_root = self.state_dir / "project-files-complete"

    def _destination(self, ref: str) -> Path:
        return self._root / workspace_slug(ref)

    def _marker(self, ref: str) -> Path:
        return self._markers_root / workspace_slug(ref)

    def acquire(self, ref: str, *, files: Sequence[str] | None = None) -> Path:
        with self._lock:
            return self._acquire_locked(ref, files=files)

    def _acquire_locked(
        self, ref: str, *, files: Sequence[str] | None = None,
    ) -> Path:
        if not files:
            raise WorkspaceError(
                "FileAllowlistGrant.acquire requires a non-empty files allowlist"
            )
        destination = self._destination(ref)
        marker = self._marker(ref)
        # Idempotent reattach, the same guarantee `GitWorkspaceProvider.acquire`
        # already gives: a retry of one node is meant to see what an earlier
        # attempt left, not a freshly re-copied directory. A destination is
        # reusable only when its out-of-band completion marker exists; older
        # versions created this directory directly and could leave a partial
        # copy behind after failure.
        if destination.is_dir() and marker.is_file():
            return destination

        # Versions before the completion marker could leave a non-empty,
        # partially copied destination behind. Never trust that legacy state.
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        marker.unlink(missing_ok=True)

        staging = self._staging_root / f"{workspace_slug(ref)}.{secrets.token_hex(8)}"
        try:
            matches = self._resolve(files)
            if not matches:
                raise WorkspaceError(
                    f"workspace access for {ref!r} matched no files under "
                    f"{self.project_root} for {list(files)!r}"
                )
            total_bytes = sum(size for _source, _relative, size in matches)
            self._check_quota(total_bytes, ref)
            staging.mkdir(parents=True, exist_ok=True)
            for source, relative, _size in matches:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, destination)
            except OSError:
                # A concurrent acquire() for the same ref finished first.
                # Rename onto an existing non-empty directory always fails,
                # so this is expected, not a real failure — reuse the winner
                # and drop our own now-redundant copy.
                if destination.is_dir() and marker.is_file():
                    shutil.rmtree(staging, ignore_errors=True)
                    return destination
                raise
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("complete\n", encoding="utf-8")
        except QuotaExceeded:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except WorkspaceError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise WorkspaceError(
                f"could not provision workspace access for {ref!r}: {exc}"
            ) from exc
        return destination

    def _resolve(self, patterns: Sequence[str]) -> list[tuple[Path, Path, int]]:
        """Every file a glob pattern names, checked to still be inside the project.

        A symlink resolving outside `project_root` is refused outright, not
        silently skipped — the whole point of an allowlist is that every file
        it names either lands in the copy or the request fails loudly; a
        pattern that quietly matched fewer files than the author expected is
        the same shape of surprise this feature exists to end.
        """

        matches: dict[Path, Path] = {}
        for pattern in patterns:
            for candidate in sorted(self.project_root.glob(pattern)):
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(self.project_root)
                except ValueError:
                    raise WorkspaceError(
                        f"{pattern!r} matched {candidate} which resolves outside "
                        f"{self.project_root} (a symlink pointing out of the "
                        "project); refusing rather than copying it"
                    ) from None
                matches[resolved] = relative
        return [
            (source, relative, source.stat().st_size)
            for source, relative in matches.items()
        ]

    def _check_quota(self, total_bytes: int, ref: str) -> None:
        if total_bytes > self.max_bytes:
            raise QuotaExceeded(
                f"workspace access for {ref!r} would copy {total_bytes} bytes, "
                f"over the {self.max_bytes} byte limit"
            )
        usage_root = self.state_dir if self.state_dir.exists() else self.state_dir.parent
        usage = shutil.disk_usage(usage_root)
        headroom = max(
            self.min_free_bytes, int(usage.total * self.min_free_fraction)
        )
        if usage.free - total_bytes < headroom:
            raise QuotaExceeded(
                f"workspace access for {ref!r} would leave less than "
                f"{headroom} bytes free on {usage_root}"
            )

    def sweep(self, live_refs: Iterable[str]) -> tuple[str, ...]:
        with self._lock:
            return self._sweep_locked(live_refs)

    def sweep_live(
        self, live_refs: Callable[[], Iterable[str]],
    ) -> tuple[str, ...]:
        """Resolve liveness and reclaim under the same lock as acquire()."""

        with self._lock:
            return self._sweep_locked(live_refs())

    def _sweep_locked(self, live_refs: Iterable[str]) -> tuple[str, ...]:
        # With the acquire lock held, no staging directory can belong to a
        # live copy in this Runtime. Anything here survived a killed process
        # (or an earlier failed cleanup) and is safe to reclaim now.
        if self._staging_root.exists():
            for child in self._staging_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        if not self._root.exists():
            return ()
        live_slugs = {workspace_slug(ref) for ref in live_refs}
        reclaimed = []
        for child in sorted(self._root.iterdir()):
            if child.name in live_slugs:
                continue
            shutil.rmtree(child, ignore_errors=True)
            (self._markers_root / child.name).unlink(missing_ok=True)
            reclaimed.append(child.name)
        return tuple(reclaimed)
