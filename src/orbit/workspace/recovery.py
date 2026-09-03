"""A way back, taken before an Agent is let into the real project directory.

`isolation: none` hands a CLI with full permissions the developer's actual
working tree. Nothing here makes that safe — an Agent can still do the wrong
thing thoroughly — but it does make the state before it recoverable, which is
the difference between a risk and an accident. Design:
docs/project-file-access-design.md §6.

What is covered, and what deliberately is not:

* tracked files, committed or not — content and staged state both
* untracked files that git is not ignoring
* NOT ignored files. `node_modules`, `.venv`, build output: writing those into
  the object store would make the space cost of a recovery point unbounded,
  and they are not project content to compare against anyway.
* NOT submodule working trees. `git stash create` cannot protect them either;
  this is the same gap, stated rather than papered over.

The baseline is written through a *separate index file*, never the user's
own. `git stash create` is not used for the same reason it is not enough: it
does not cover plain untracked files, and §5's change summary needs a
baseline that does, or every untracked file that was already there is
reported as something this run added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable, Mapping

from .git import GIT_TIMEOUT_SECONDS, WorkspaceError, _git, is_git_repo


# Objects a recovery point needs must survive `git gc`, so they hang off a
# ref of Orbit's own rather than being written and orphaned.
RECOVERY_REF_PREFIX = "refs/orbit/recovery"
# Room the object store is left after a recovery point is written. A baseline
# that fills the disk has taken the project down to save it.
DEFAULT_MIN_FREE_BYTES = 1 * 1024**3


class RecoveryPointError(WorkspaceError):
    """A recovery point could not be established."""


class RecoveryUnavailable(RecoveryPointError):
    """This project cannot have the recovery point it was asked for.

    Never a reason to proceed without one: §6.2 is explicit that a workflow
    needing recovery it cannot have is refused, not quietly run unprotected.
    """


@dataclass(frozen=True)
class RecoveryPoint:
    """Where the project was before a run touched it."""

    run_id: str
    project_root: Path
    kind: str
    created_at: str
    head: str | None = None
    worktree_tree: str | None = None
    index_tree: str | None = None
    ref: str | None = None
    covered: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    def to_primitive(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "project_root": str(self.project_root),
            "kind": self.kind,
            "created_at": self.created_at,
            "head": self.head,
            "worktree_tree": self.worktree_tree,
            "index_tree": self.index_tree,
            "ref": self.ref,
            "covered": list(self.covered),
            "uncovered": list(self.uncovered),
        }


@dataclass(frozen=True)
class RestoreEntry:
    """One path a restore would touch, and what it would do to it."""

    path: str
    action: str  # "restore" | "keep" | "conflict"
    detail: str = ""


@dataclass(frozen=True)
class RestorePlan:
    """What a restore would do, for a person to read before it happens.

    Deliberately a plan and not an action: §6.3 requires the paths to be
    shown and confirmed, and requires that a restore never resolves a
    file/directory type conflict by deleting recursively on its own.
    """

    point: RecoveryPoint
    entries: tuple[RestoreEntry, ...] = ()

    @property
    def restores(self) -> tuple[RestoreEntry, ...]:
        return tuple(item for item in self.entries if item.action == "restore")

    @property
    def keeps(self) -> tuple[RestoreEntry, ...]:
        return tuple(item for item in self.entries if item.action == "keep")

    @property
    def conflicts(self) -> tuple[RestoreEntry, ...]:
        return tuple(item for item in self.entries if item.action == "conflict")

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)


def _run_git(root: Path, *args: str, index: Path | None = None):
    """git, optionally against an index file that is not the user's."""

    environment = dict(os.environ)
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=False,
            timeout=GIT_TIMEOUT_SECONDS, env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryPointError(f"git failed: {' '.join(args)}: {exc}") from exc


def _checked(result, what: str) -> str:
    if result.returncode != 0:
        raise RecoveryPointError(
            f"{what} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class GitRecoveryPoints:
    """Recovery points for a git project, kept on an Orbit-owned ref."""

    def __init__(
        self, project_root: Path | str, *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.min_free_bytes = min_free_bytes

    def ref_for(self, run_id: str) -> str:
        safe = "".join(
            character if character.isalnum() or character in "-_." else "-"
            for character in run_id
        ).strip("-.") or "run"
        return f"{RECOVERY_REF_PREFIX}/{safe}"

    def available(self) -> bool:
        return is_git_repo(self.project_root)

    def create(self, run_id: str) -> RecoveryPoint:
        """Record where the project is, before anything is allowed to write.

        Taken after the project lock and before the first node runs, so what
        it captures is the state the operator would expect to come back to.
        """

        if not self.available():
            raise RecoveryUnavailable(
                f"{self.project_root} is not a git repository; a git recovery "
                "point cannot be established here"
            )
        self._check_space()
        head = self._head()
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "orbit-recovery-index"
            # `add -A` against an index of our own: untracked files come in,
            # ignored files stay out, and the user's staged state is not
            # disturbed by any of it.
            _checked(
                _run_git(self.project_root, "add", "-A", index=index),
                "staging the working tree for a recovery point",
            )
            worktree_tree = _checked(
                _run_git(self.project_root, "write-tree", index=index),
                "writing the working-tree baseline",
            )
        # The user's own index, read without writing to it.
        index_tree = _checked(
            _run_git(self.project_root, "write-tree"),
            "writing the staged baseline",
        ) if head is not None else None
        ref = self.ref_for(run_id)
        self._anchor(ref, worktree_tree, head, run_id)
        return RecoveryPoint(
            run_id=run_id,
            project_root=self.project_root,
            kind="git",
            created_at=_stamp(),
            head=head,
            worktree_tree=worktree_tree,
            index_tree=index_tree,
            ref=ref,
            covered=(
                "tracked files (content and staged state)",
                "untracked files git is not ignoring",
            ),
            uncovered=self._uncovered(),
        )

    def _head(self) -> str | None:
        result = _run_git(self.project_root, "rev-parse", "--verify", "-q", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    def _anchor(
        self, ref: str, tree: str, head: str | None, run_id: str,
    ) -> None:
        """Point an Orbit ref at a commit for the baseline tree.

        Without a ref the tree is unreachable and `git gc` is entitled to
        delete it — a recovery point that evaporates on a housekeeping run is
        worse than none, because it was counted on.
        """

        args = ["commit-tree", tree, "-m", f"orbit recovery point for {run_id}"]
        if head is not None:
            args.extend(["-p", head])
        commit = _checked(
            _run_git(self.project_root, *args), "anchoring the recovery point",
        )
        _checked(
            _run_git(self.project_root, "update-ref", ref, commit),
            "recording the recovery point ref",
        )

    def _uncovered(self) -> tuple[str, ...]:
        uncovered = ["files git is ignoring (build output, dependencies, caches)"]
        modules = _run_git(
            self.project_root, "submodule", "status", "--recursive",
        )
        if modules.returncode == 0 and modules.stdout.strip():
            uncovered.append("submodule working trees")
        return tuple(uncovered)

    def _check_space(self) -> None:
        try:
            usage = shutil.disk_usage(self.project_root)
        except OSError as exc:
            raise RecoveryPointError(
                f"could not read free space for {self.project_root}: {exc}"
            ) from exc
        if usage.free < self.min_free_bytes:
            raise RecoveryUnavailable(
                f"{usage.free} bytes free on {self.project_root} is under the "
                f"{self.min_free_bytes} byte floor a recovery point needs; "
                "refusing rather than running unprotected"
            )

    # -- coming back ------------------------------------------------------

    def plan_restore(self, point: RecoveryPoint) -> RestorePlan:
        """What coming back would do, path by path, without doing it.

        Conservative by §6.3: covered paths are restored, anything the run
        added is kept rather than deleted, and a path whose type changed
        (a file where a directory now stands, or the reverse) stops the
        restore instead of being cleared recursively.
        """

        if point.worktree_tree is None:
            raise RecoveryPointError("recovery point carries no baseline tree")
        listing = _checked(
            _run_git(
                self.project_root, "ls-tree", "-r", "--name-only",
                point.worktree_tree,
            ),
            "reading the recovery point",
        )
        covered = [line for line in listing.splitlines() if line]
        entries: list[RestoreEntry] = []
        for relative in covered:
            target = self.project_root / relative
            if target.is_dir() and not target.is_symlink():
                entries.append(RestoreEntry(
                    relative, "conflict",
                    "a directory now stands where this file was",
                ))
            else:
                entries.append(RestoreEntry(relative, "restore"))
        for relative in self._added_since(point):
            entries.append(RestoreEntry(
                relative, "keep", "added during the run; not deleted",
            ))
        return RestorePlan(point, tuple(entries))

    def _added_since(self, point: RecoveryPoint) -> tuple[str, ...]:
        """Paths present now that the baseline does not carry."""

        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "orbit-compare-index"
            _checked(
                _run_git(self.project_root, "add", "-A", index=index),
                "staging the working tree for comparison",
            )
            current = _checked(
                _run_git(self.project_root, "write-tree", index=index),
                "writing the current tree",
            )
        listing = _checked(
            _run_git(
                self.project_root, "diff", "--name-only", "--diff-filter=A",
                point.worktree_tree, current,
            ),
            "comparing against the recovery point",
        )
        return tuple(line for line in listing.splitlines() if line)

    def restore(self, point: RecoveryPoint, plan: RestorePlan) -> tuple[str, ...]:
        """Put the covered paths back. Refuses while the plan is blocked."""

        if plan.blocked:
            raise RecoveryPointError(
                "restore is blocked by path type conflicts: "
                + ", ".join(item.path for item in plan.conflicts)
            )
        restored: list[str] = []
        for entry in plan.restores:
            blob = _run_git(
                self.project_root, "cat-file", "blob",
                f"{point.worktree_tree}:{entry.path}",
            )
            if blob.returncode != 0:
                continue
            target = self.project_root / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(blob.stdout, encoding="utf-8")
            restored.append(entry.path)
        return tuple(restored)

    def forget(self, point: RecoveryPoint) -> None:
        """Drop the ref once nothing needs the way back any more."""

        if point.ref:
            _run_git(self.project_root, "update-ref", "-d", point.ref)
