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
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Iterable, Mapping, Sequence

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

    @classmethod
    def from_primitive(cls, value: Mapping) -> "RecoveryPoint":
        return cls(
            run_id=value["run_id"], project_root=Path(value["project_root"]),
            kind=value["kind"], created_at=value["created_at"],
            head=value.get("head"), worktree_tree=value.get("worktree_tree"),
            index_tree=value.get("index_tree"), ref=value.get("ref"),
            covered=tuple(value.get("covered", ())),
            uncovered=tuple(value.get("uncovered", ())),
        )


def _write_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _RecoveryHistory:
    """Durable point metadata and the immutable end-of-run observation."""

    def _history_path(self, run_id: str) -> Path:
        raise NotImplementedError

    def _history(self, run_id: str) -> dict:
        try:
            value = json.loads(self._history_path(run_id).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("expected recovery metadata object")
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise RecoveryPointError(f"cannot read recovery metadata: {exc}") from exc

    def finalize(self, run_id: str) -> None:
        history = self._history(run_id)
        if "summary" in history:
            return
        point = self.load(run_id)
        if point is None:
            raise RecoveryPointError(f"recovery point missing for {run_id}")
        try:
            history["summary"] = self.summarize(point).to_primitive()
        except (RecoveryPointError, OSError) as exc:
            # Failure to compare is a final observation too, never permission
            # to attribute a later run's work to this one on the next GET.
            history["summary"] = {
                "kind": "unavailable", "scope": "run_cumulative",
                "content": [], "staged": [], "error": str(exc),
                "uncovered": list(point.uncovered),
            }
        history.setdefault("point", point.to_primitive())
        _write_json(self._history_path(run_id), history)

    def final_summary(self, run_id: str) -> Mapping | None:
        return self._history(run_id).get("summary")


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
    parts = iter(text.rstrip("\0").split("\0") if text else ())
    for status in parts:
        code = status[:1]
        path = next(parts)
        if code in {"R", "C"}:
            path = next(parts)  # destination of a rename/copy
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


def _run_git(root: Path, *args: str, index: Path | None = None, binary: bool = False):
    """git, optionally against an index file that is not the user's."""

    environment = dict(os.environ)
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, check=False,
            timeout=GIT_TIMEOUT_SECONDS, env=environment,
        )
        if not binary:
            # TextIOWrapper's universal-newline conversion also corrupts
            # literal CR/CRLF in NUL-delimited filenames.
            result.stdout = result.stdout.decode("utf-8", errors="surrogateescape")
            result.stderr = result.stderr.decode("utf-8", errors="surrogateescape")
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryPointError(f"git failed: {' '.join(args)}: {exc}") from exc


def _checked(result, what: str, *, strip: bool = True):
    if result.returncode != 0:
        raise RecoveryPointError(
            f"{what} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip() if strip else result.stdout


def _restore_target(root: Path, relative: str) -> Path:
    """Never follow a changed parent symlink out of the recovery scope."""

    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RecoveryPointError(f"invalid recovery path: {relative!r}")
    parent = root
    for part in path.parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise RecoveryPointError(f"unsafe recovery parent: {parent}")
    target = root / path
    if target.is_dir() and not target.is_symlink():
        raise RecoveryPointError(f"directory conflicts with recovery file: {target}")
    return target


def _restore_bytes(root: Path, relative: str, data: bytes, mode: str) -> None:
    target = _restore_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".orbit-restore-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if mode != "120000":
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        if mode == "120000":
            Path(temporary).unlink()
            os.symlink(os.fsdecode(data), temporary)
        else:
            os.chmod(temporary, 0o755 if mode == "100755" else 0o644)
        _restore_target(root, relative)  # recheck immediately before replacement
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class GitRecoveryPoints(_RecoveryHistory):
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

    def _history_path(self, run_id: str) -> Path:
        directory = _checked(
            _run_git(self.project_root, "rev-parse", "--git-path", "orbit-recovery"),
            "locating recovery metadata",
        )
        return self.project_root / directory / (self.ref_for(run_id).rsplit("/", 1)[-1] + ".json")

    def preflight(self, *, protect: Sequence[str] = ()) -> None:
        """Whether a point could be made here, asked before a Run exists."""

        if not self.available():
            raise RecoveryUnavailable(
                f"{self.project_root} is not a git repository; a git recovery "
                "point cannot be established here"
            )
        self._check_space()

    def create(
        self, run_id: str, *, protect: Sequence[str] = (),
    ) -> RecoveryPoint:
        """Record where the project is, before anything is allowed to write.

        Taken after the project lock and before the first node runs, so what
        it captures is the state the operator would expect to come back to.

        `protect` is accepted and ignored, so a caller does not have to know
        which strategy it is talking to. git's baseline is the whole project
        already; there is nothing for a workflow to enumerate, and a list that
        narrowed it would only describe less than what is covered.
        """

        if not self.available():
            raise RecoveryUnavailable(
                f"{self.project_root} is not a git repository; a git recovery "
                "point cannot be established here"
            )
        existing = self.load(run_id)
        if existing is not None:
            return existing
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
        self._anchor(ref, worktree_tree, head, run_id, index_tree=index_tree)
        point = RecoveryPoint(
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
        _write_json(self._history_path(run_id), {"point": point.to_primitive()})
        return point

    def _head(self) -> str | None:
        result = _run_git(self.project_root, "rev-parse", "--verify", "-q", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    def _anchor(
        self, ref: str, tree: str, head: str | None, run_id: str,
        *, index_tree: str | None = None,
    ) -> None:
        """Point an Orbit ref at a commit for the baseline tree.

        Without a ref the tree is unreachable and `git gc` is entitled to
        delete it — a recovery point that evaporates on a housekeeping run is
        worse than none, because it was counted on.
        """

        args = ["commit-tree", tree, "-m", f"orbit recovery point for {run_id}"]
        if head is not None:
            args.extend(["-p", head])
        if index_tree is not None:
            index_commit = _checked(
                _run_git(self.project_root, "commit-tree", index_tree, "-m", "Orbit index baseline"),
                "anchoring staged baseline",
            )
            args.extend(["-p", index_commit])
        commit = _checked(
            _run_git(self.project_root, *args), "anchoring the recovery point",
        )
        _checked(
            _run_git(self.project_root, "update-ref", ref, commit, ""),
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
        covered = self._tree_entries(point)
        entries: list[RestoreEntry] = []
        for relative in covered:
            try:
                _restore_target(self.project_root, relative)
            except RecoveryPointError as exc:
                entries.append(RestoreEntry(
                    relative, "conflict", str(exc),
                ))
            else:
                entries.append(RestoreEntry(relative, "restore"))
        for relative in self._added_since(point):
            entries.append(RestoreEntry(
                relative, "keep", "added during the run; not deleted",
            ))
        return RestorePlan(point, tuple(entries))

    def _tree_entries(self, point: RecoveryPoint) -> dict[str, tuple[str, str]]:
        listing = _checked(_run_git(
            self.project_root, "ls-tree", "-rz", "--full-tree", point.worktree_tree,
        ), "reading baseline entries", strip=False)
        result = {}
        for entry in listing.split("\0"):
            if not entry:
                continue
            header, relative = entry.split("\t", 1)
            mode, kind, oid = header.split()
            if kind == "blob":
                result[relative] = (mode, oid)
        return result

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
                self.project_root, "diff", "--name-only", "-z", "--diff-filter=A",
                point.worktree_tree, current,
            ),
            "comparing against the recovery point", strip=False,
        )
        return tuple(line for line in listing.split("\0") if line)

    def restore(self, point: RecoveryPoint, plan: RestorePlan) -> tuple[str, ...]:
        """Put the covered paths back. Refuses while the plan is blocked."""

        if plan.blocked:
            raise RecoveryPointError(
                "restore is blocked by path type conflicts: "
                + ", ".join(item.path for item in plan.conflicts)
            )
        restored: list[str] = []
        entries = self._tree_entries(point)
        if plan.point != point:
            raise RecoveryPointError("restore plan belongs to another point")
        for entry in plan.restores:
            if entry.path not in entries:
                raise RecoveryPointError("restore path is not in baseline")
            _restore_target(self.project_root, entry.path)
        for entry in plan.restores:
            mode, oid = entries[entry.path]
            blob = _checked(_run_git(
                self.project_root, "cat-file", "blob",
                oid, binary=True,
            ), "reading baseline blob", strip=False)
            _restore_bytes(self.project_root, entry.path, blob, mode)
            restored.append(entry.path)
        return tuple(restored)

    def load(self, run_id: str) -> RecoveryPoint | None:
        """Rebuild a run's recovery point from the ref it left behind.

        So a summary or a restore is still answerable after the Runtime that
        made the point has gone: the ref is durable, the in-memory claim is
        not, and "what did that run change" outlives the process that ran it.
        """

        ref = self.ref_for(run_id)
        history = self._history(run_id)
        commit = _run_git(self.project_root, "rev-parse", "--verify", "-q", ref)
        if commit.returncode != 0:
            if history:
                raise RecoveryPointError("recovery metadata exists but its ref is missing")
            return None
        tree = _run_git(self.project_root, "rev-parse", f"{ref}^{{tree}}")
        if tree.returncode != 0:
            raise RecoveryPointError("cannot read baseline tree")
        if "point" in history:
            point = RecoveryPoint.from_primitive(history["point"])
            if point.worktree_tree != tree.stdout.strip():
                raise RecoveryPointError("baseline ref and metadata disagree")
            return point
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
                self.project_root, "diff", "--name-status", "-z",
                point.worktree_tree, current_tree,
            ),
            "comparing content against the recovery point", strip=False,
        ))
        staged: tuple[FileChange, ...] = ()
        if point.index_tree is not None:
            current_index = _run_git(self.project_root, "write-tree")
            if current_index.returncode == 0:
                staged = _parse_name_status(_checked(
                    _run_git(
                        self.project_root, "diff", "--name-status", "-z",
                        point.index_tree, current_index.stdout.strip(),
                    ),
                    "comparing the staged state against the recovery point", strip=False,
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
            self._history_path(point.run_id).unlink(missing_ok=True)

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
                self._history_path(_run_id).unlink(missing_ok=True)
                reclaimed.append(ref)
        return tuple(reclaimed)


class FileBackupRecoveryPoints(_RecoveryHistory):
    """A way back for a project git cannot give one for (§6.2).

    Outside git there is no "everything tracked" to lean on, and copying the
    whole directory is what this design refuses to do. So the workflow names
    the files whose loss would matter — `workspace_access.protect` — and those
    are copied before the run.

    The coverage is partial by construction, and that is the point to be
    honest about rather than to smooth over: an Agent CLI writes through the
    filesystem, not through anything this process can intercept, so a file
    nobody named is a file with no way back. `uncovered` says so, and §6.2
    requires an operator to be shown it before the run rather than after.

    Filesystem snapshots and copy-on-write (§6.2's first choice) are not
    implemented: whether a given platform, volume and path support them is
    not something this can establish portably, and claiming a snapshot that
    silently was not one would be worse than not offering it.
    """

    def __init__(
        self, project_root: Path | str, state_dir: Path | str, *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.state_dir = Path(state_dir)
        self.min_free_bytes = min_free_bytes
        self._root = self.state_dir / "recovery-points"

    def available(self) -> bool:
        return self.project_root.is_dir()

    def preflight(self, *, protect: Sequence[str] = ()) -> None:
        """Whether a point could be made here, asked before a Run exists.

        The same refusal `create` would give, arriving early enough that a
        workflow which can never run here leaves no Run behind to explain it.
        """

        if not protect:
            raise RecoveryUnavailable(
                f"{self.project_root} is not a git repository, so a recovery "
                "point can only cover files the workflow names; declare "
                "workspace_access.protect or run somewhere git can answer"
            )
        matches, _unmatched = self._matches(protect)
        self._check_space(sum(source.stat().st_size for source, _ in matches))

    def _home(self, run_id: str) -> Path:
        safe = "".join(
            character if character.isalnum() or character in "-_." else "-"
            for character in run_id
        ).strip("-.") or "run"
        return self._root / safe

    def _history_path(self, run_id: str) -> Path:
        return self._home(run_id) / "recovery.json"

    def create(
        self, run_id: str, *, protect: Sequence[str] = (),
    ) -> RecoveryPoint:
        existing = self.load(run_id)
        if existing is not None:
            return existing
        matches, unmatched = self._matches(protect)
        self._check_space(sum(source.stat().st_size for source, _ in matches))
        home = self._home(run_id)
        point = RecoveryPoint(
            run_id=run_id, project_root=self.project_root, kind="file_backup",
            created_at=_stamp(), ref=str(home),
            covered=tuple(str(relative) for _, relative in matches),
            uncovered=(
                "every file the workflow did not name in workspace_access.protect",
                *(
                    f"workspace_access.protect {pattern!r} matched no file, so "
                    "nothing was copied for it"
                    for pattern in unmatched
                ),
            ),
        )
        self._root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.state_dir, prefix="recovery-staging-") as staging:
            staged = Path(staging) / "point"
            for source, relative in matches:
                target = staged / "files" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            _write_json(staged / "recovery.json", {"point": point.to_primitive()})
            os.rename(staged, home)
        return point

    def _matches(
        self, protect: Sequence[str],
    ) -> tuple[list[tuple[Path, Path]], tuple[str, ...]]:
        """The files `protect` names, and the patterns that named none.

        A pattern matching nothing is not a refusal on its own: naming a file
        the run is about to write is a reasonable thing for a workflow to do,
        and there is nothing to copy for it *yet*. What it is, is a gap in the
        way back — so it goes where every other gap goes, into `uncovered`,
        which §6.2 already requires an operator to be shown before the run.

        Matching nothing at all is a refusal, because then the point covers
        nothing and there is no way back to establish.
        """

        if not protect:
            raise RecoveryUnavailable(
                f"{self.project_root} is not a git repository, so a recovery "
                "point can only cover files the workflow names; declare "
                "workspace_access.protect or run somewhere git can answer"
            )
        matches: list[tuple[Path, Path]] = []
        unmatched: list[str] = []
        for pattern in protect:
            matched = False
            for candidate in sorted(self.project_root.glob(pattern)):
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(self.project_root)
                except ValueError:
                    raise RecoveryPointError(
                        f"{pattern!r} matched {candidate}, which resolves "
                        f"outside {self.project_root}"
                    ) from None
                if self._root.resolve() in resolved.parents:
                    continue
                matched = True
                matches.append((resolved, relative))
            if not matched and pattern not in unmatched:
                unmatched.append(pattern)
        if not matches:
            raise RecoveryUnavailable(
                "workspace_access.protect matched no files at all "
                f"({', '.join(repr(item) for item in protect)}), so this run "
                "would have no way back"
            )
        return sorted(set(matches)), tuple(unmatched)

    def _check_space(self, needed: int) -> None:
        try:
            usage = shutil.disk_usage(
                self.state_dir if self.state_dir.exists()
                else self.state_dir.parent
            )
        except OSError as exc:
            raise RecoveryPointError(f"could not read free space: {exc}") from exc
        if usage.free - needed < self.min_free_bytes:
            raise RecoveryUnavailable(
                f"copying {needed} bytes would leave less than "
                f"{self.min_free_bytes} bytes free; refusing rather than "
                "running unprotected"
            )

    def load(self, run_id: str) -> RecoveryPoint | None:
        home = self._home(run_id)
        if not home.is_dir():
            return None
        history = self._history(run_id)
        if "point" not in history:
            raise RecoveryPointError("backup has no complete recovery metadata")
        point = RecoveryPoint.from_primitive(history["point"])
        for relative in point.covered:
            source = _restore_target(home / "files", relative)
            if not source.is_file():
                raise RecoveryPointError(f"backup file missing: {relative}")
        return point

    def plan_restore(self, point: RecoveryPoint) -> RestorePlan:
        home = Path(point.ref or "")
        entries: list[RestoreEntry] = []
        for relative in point.covered:
            try:
                _restore_target(self.project_root, relative)
            except RecoveryPointError as exc:
                entries.append(RestoreEntry(
                    relative, "conflict", str(exc),
                ))
            else:
                entries.append(RestoreEntry(relative, "restore"))
        entries.append(RestoreEntry(
            "(everything not named in protect)", "keep",
            "never copied, so never restored — see uncovered",
        ))
        return RestorePlan(point, tuple(entries))

    def restore(self, point: RecoveryPoint, plan: RestorePlan) -> tuple[str, ...]:
        if plan.blocked:
            raise RecoveryPointError(
                "restore is blocked by path type conflicts: "
                + ", ".join(item.path for item in plan.conflicts)
            )
        home = Path(point.ref or "")
        restored: list[str] = []
        if plan.point != point:
            raise RecoveryPointError("restore plan belongs to another point")
        for entry in plan.restores:
            if entry.path not in point.covered:
                raise RecoveryPointError("restore path is not in baseline")
            _restore_target(self.project_root, entry.path)
        for entry in plan.restores:
            source = home / "files" / entry.path
            if not source.is_file():
                raise RecoveryPointError(f"backup file missing: {entry.path}")
            _restore_bytes(self.project_root, entry.path, source.read_bytes(),
                           "100755" if source.stat().st_mode & 0o111 else "100644")
            restored.append(entry.path)
        return tuple(restored)

    def summarize(self, point: RecoveryPoint) -> ChangeSummary:
        """There is no independent comparison to make here.

        §5 is explicit that outside git the change record is the Agent's own
        account plus whatever the workflow's acceptance could verify. Saying
        that plainly beats returning an empty diff that reads like "nothing
        changed".
        """

        return ChangeSummary(
            kind="agent_report_only",
            scope="run_cumulative",
            uncovered=(
                "this project is not a git repository; Orbit made no "
                "independent comparison, so the change record is the Agent's "
                "own account plus the workflow's acceptance",
            ),
        )

    def sweep(
        self, live_run_ids: Iterable[str], *, older_than_seconds: float,
        now: float | None = None,
    ) -> tuple[str, ...]:
        if not self._root.is_dir():
            return ()
        moment = time.time() if now is None else now
        live = {self._home(str(item)).name for item in live_run_ids}
        reclaimed: list[str] = []
        for child in sorted(self._root.iterdir()):
            if child.name in live or not child.is_dir():
                continue
            try:
                age = moment - child.stat().st_mtime
            except OSError:
                continue
            if age < older_than_seconds:
                continue
            shutil.rmtree(child, ignore_errors=True)
            reclaimed.append(child.name)
        return tuple(reclaimed)

    def forget(self, point: RecoveryPoint) -> None:
        if point.ref:
            shutil.rmtree(Path(point.ref), ignore_errors=True)
