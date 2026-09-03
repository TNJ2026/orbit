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
import time
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
class FileChange:
    """One path the run changed, relative to what was there before it."""

    path: str
    status: str  # added | modified | deleted | renamed | typechanged

    @staticmethod
    def _statuses() -> Mapping[str, str]:
        return {
            "A": "added", "M": "modified", "D": "deleted",
            "R": "renamed", "T": "typechanged", "C": "copied",
        }


@dataclass(frozen=True)
class ChangeSummary:
    """What a run did to the project, as far as git can independently say.

    Three separate answers, because collapsing them loses the difference
    (§5). An Agent may commit its work, switch branch, or stage things; the
    file content the project now carries is a different question from what
    HEAD points at, and reporting the second in place of the first is how a
    run that committed everything comes to look like a run that changed
    nothing.

    `scope` is `run_cumulative`: this is everything since the recovery point,
    not the work of one node. Attributing it to a node would need a tree
    saved at every node boundary, which is not done by default — so it is
    named here rather than left for a reader to assume.
    """

    kind: str
    scope: str
    content: tuple[FileChange, ...] = ()
    staged: tuple[FileChange, ...] = ()
    head_before: str | None = None
    head_after: str | None = None
    branch_after: str | None = None
    uncovered: tuple[str, ...] = ()

    @property
    def head_moved(self) -> bool:
        return self.head_before != self.head_after

    def to_primitive(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "content": [
                {"path": item.path, "status": item.status} for item in self.content
            ],
            "staged": [
                {"path": item.path, "status": item.status} for item in self.staged
            ],
            "head_before": self.head_before,
            "head_after": self.head_after,
            "branch_after": self.branch_after,
            "head_moved": self.head_moved,
            "uncovered": list(self.uncovered),
        }


def _parse_name_status(text: str) -> tuple[FileChange, ...]:
    statuses = FileChange._statuses()  # noqa: SLF001
    changes: list[FileChange] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0][:1]
        path = parts[-1]
        changes.append(FileChange(path, statuses.get(code, code.lower())))
    return tuple(sorted(changes, key=lambda item: item.path))


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

    def load(self, run_id: str) -> RecoveryPoint | None:
        """Rebuild a run's recovery point from the ref it left behind.

        So a summary or a restore is still answerable after the Runtime that
        made the point has gone: the ref is durable, the in-memory claim is
        not, and "what did that run change" outlives the process that ran it.
        """

        ref = self.ref_for(run_id)
        commit = _run_git(self.project_root, "rev-parse", "--verify", "-q", ref)
        if commit.returncode != 0:
            return None
        tree = _run_git(self.project_root, "rev-parse", f"{ref}^{{tree}}")
        if tree.returncode != 0:
            return None
        parent = _run_git(self.project_root, "rev-parse", "--verify", "-q", f"{ref}^")
        return RecoveryPoint(
            run_id=run_id,
            project_root=self.project_root,
            kind="git",
            created_at="",
            head=parent.stdout.strip() if parent.returncode == 0 else None,
            worktree_tree=tree.stdout.strip(),
            ref=ref,
            uncovered=self._uncovered(),
        )

    def summarize(self, point: RecoveryPoint) -> ChangeSummary:
        """What the run changed, measured against the point it started from.

        Not cheap: it scans the working tree and writes a tree object, so
        callers ask for it when something needs it — acceptance, a page being
        read, a run settling — rather than after every node. §5.

        The current side is built through its own index for the same reason
        the baseline was: a plain worktree diff cannot see files that are new
        *and* untracked, which is most of what an Agent creates.
        """

        if point.worktree_tree is None:
            raise RecoveryPointError("recovery point carries no baseline tree")
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "orbit-summary-index"
            _checked(
                _run_git(self.project_root, "add", "-A", index=index),
                "staging the working tree for a change summary",
            )
            current_tree = _checked(
                _run_git(self.project_root, "write-tree", index=index),
                "writing the current tree",
            )
        content = _parse_name_status(_checked(
            _run_git(
                self.project_root, "diff", "--name-status",
                point.worktree_tree, current_tree,
            ),
            "comparing content against the recovery point",
        ))
        staged: tuple[FileChange, ...] = ()
        if point.index_tree is not None:
            current_index = _run_git(self.project_root, "write-tree")
            if current_index.returncode == 0:
                staged = _parse_name_status(_checked(
                    _run_git(
                        self.project_root, "diff", "--name-status",
                        point.index_tree, current_index.stdout.strip(),
                    ),
                    "comparing the staged state against the recovery point",
                ))
        branch = _run_git(self.project_root, "rev-parse", "--abbrev-ref", "HEAD")
        return ChangeSummary(
            kind="git",
            # Named, not assumed: without a tree per node boundary there is
            # nothing that could attribute this to one step.
            scope="run_cumulative",
            content=content,
            staged=staged,
            head_before=point.head,
            head_after=self._head(),
            branch_after=(
                branch.stdout.strip() if branch.returncode == 0 else None
            ),
            uncovered=point.uncovered,
        )

    def forget(self, point: RecoveryPoint) -> None:
        """Drop the ref once nothing needs the way back any more."""

        if point.ref:
            _run_git(self.project_root, "update-ref", "-d", point.ref)

    def points(self) -> tuple[tuple[str, str, float], ...]:
        """Every recovery ref here: name, run id, and when it was written."""

        listing = _run_git(
            self.project_root, "for-each-ref", RECOVERY_REF_PREFIX,
            "--format=%(refname)%09%(committerdate:unix)",
        )
        if listing.returncode != 0:
            return ()
        found: list[tuple[str, str, float]] = []
        for line in listing.stdout.splitlines():
            if "\t" not in line:
                continue
            ref, _, stamp = line.partition("\t")
            try:
                written = float(stamp)
            except ValueError:
                continue
            found.append((ref, ref[len(RECOVERY_REF_PREFIX) + 1:], written))
        return tuple(found)

    def sweep(
        self, live_run_ids: Iterable[str], *, older_than_seconds: float,
        now: float | None = None,
    ) -> tuple[str, ...]:
        """Reclaim the ways back nobody can still need.

        Two conditions, both required (§6.3). The run must be over — a live
        run's way back is the whole point of having one — and the point must
        have outlived the retention period, because "the run finished" is not
        the moment somebody stops wanting to undo it. Between them they mean
        this only ever removes Orbit's own recovery data for settled runs; it
        never touches project files.

        These refs pin a tree of the whole project, so leaving them forever
        is real growth inside somebody's repository, not just bookkeeping.
        """

        moment = time.time() if now is None else now
        live = {str(item) for item in live_run_ids}
        # Ref names are sanitised run ids, so a live run is matched by the
        # name its point would have rather than by string equality.
        live_refs = {self.ref_for(item) for item in live}
        reclaimed: list[str] = []
        for ref, _run_id, written in self.points():
            if ref in live_refs:
                continue
            if moment - written < older_than_seconds:
                continue
            result = _run_git(self.project_root, "update-ref", "-d", ref)
            if result.returncode == 0:
                reclaimed.append(ref)
        return tuple(reclaimed)
