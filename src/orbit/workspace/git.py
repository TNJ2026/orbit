"""Per-workspace git worktrees.

Behaviour contract: docs/migration/m1b-behaviour-inventory.md §2.

Workspaces are keyed by an opaque ``workspace_ref`` chosen by the caller, not
by an engine-specific integer id.  The ref is sanitised into a filesystem slug
and every resulting path is checked to still live under the worktree root, so a
hostile ref can neither traverse out with ``..`` nor escape through a symlink.

Acquire is idempotent: the runtime re-runs a step after a lease expires, and an
already-present worktree must be re-attached rather than rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import time


GIT_TIMEOUT_SECONDS = 30.0
LOCK_RETRIES = 5
BRANCH_PREFIX = "orbit/ws-"
WORKTREES_DIRNAME = "worktrees"

_SAFE_SLUG = re.compile(r"[^A-Za-z0-9_.-]+")


class WorkspaceError(RuntimeError):
    """A workspace operation failed in a way the caller must handle."""


class WorkspaceUnavailable(WorkspaceError):
    """Isolation is impossible here; the caller should fall back to the root.

    Raised for the two benign cases the legacy engine logged and skipped: the
    project is not a git repository, or it has no commit to branch from.
    """


@dataclass(frozen=True)
class WorkspaceLease:
    """A checked-out workspace. ``path`` is guaranteed inside the root."""

    workspace_ref: str
    path: Path
    branch: str
    base_ref: str


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def git_available() -> bool:
    return shutil.which("git") is not None


def is_git_repo(root: Path | str) -> bool:
    try:
        return _git(Path(root), "rev-parse", "--git-dir", timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def workspace_slug(workspace_ref: str) -> str:
    """Filesystem-safe, collision-resistant slug for an arbitrary ref.

    The digest keeps two refs that sanitise to the same characters apart, so
    ``run:a/b`` and ``run:a-b`` never share a directory.
    """

    ref = str(workspace_ref).strip()
    if not ref:
        raise ValueError("workspace_ref must not be empty")
    cleaned = _SAFE_SLUG.sub("-", ref).strip("-._")[:48] or "ws"
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned}-{digest}"


class GitWorkspaceProvider:
    """Provisions and reclaims git worktrees under a project's state dir."""

    def __init__(self, project_root: Path | str, state_dir: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_dir = Path(state_dir)
        self.worktrees_root = self.state_dir / WORKTREES_DIRNAME

    # -- paths ------------------------------------------------------------

    def _checked_root(self) -> Path:
        """The worktrees root, proven not to be a symlink out of the state dir.

        A hostile or stale checkout can leave `.orbit/worktrees` as a symlink
        pointing anywhere; writing through it would put worktrees outside the
        area orbit owns. Resolving both sides and requiring containment catches
        that. Traversal via the ref itself is impossible by construction:
        :func:`workspace_slug` strips path separators and dots.
        """

        root = self.worktrees_root
        if not root.exists():
            return root
        resolved = root.resolve()
        state = self.state_dir.resolve()
        if resolved != state / WORKTREES_DIRNAME and state not in resolved.parents:
            raise WorkspaceError(
                f"worktrees root resolves outside the state directory: {resolved}"
            )
        return resolved

    def _resolved_path(self, workspace_ref: str) -> Path:
        """Directory for a ref — always a direct child of the worktrees root."""

        return self._checked_root() / workspace_slug(workspace_ref)

    def branch_name(self, workspace_ref: str) -> str:
        return f"{BRANCH_PREFIX}{workspace_slug(workspace_ref)}"

    # -- git state --------------------------------------------------------

    def _base_ref(self) -> str | None:
        """Commit new worktrees branch from; None when HEAD is unborn."""

        try:
            found = _git(
                self.project_root, "rev-parse", "--verify", "-q", "HEAD", timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return "HEAD" if found.returncode == 0 else None

    def _branch_exists(self, branch: str) -> bool:
        try:
            return _git(
                self.project_root, "rev-parse", "--verify", "-q", branch, timeout=10
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _registered(self, path: Path) -> bool:
        try:
            listing = _git(
                self.project_root, "worktree", "list", "--porcelain", timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        target = str(path.resolve()) if path.exists() else str(path)
        for line in listing.splitlines():
            if not line.startswith("worktree "):
                continue
            registered = line[len("worktree "):].strip()
            try:
                if str(Path(registered).resolve()) == target:
                    return True
            except OSError:
                continue
        return False

    def is_dirty(self, workspace_ref: str) -> bool:
        """Whether the workspace has uncommitted changes."""

        path = self._resolved_path(workspace_ref)
        if not path.exists():
            return False
        try:
            status = _git(path, "status", "--porcelain", timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return bool(status.stdout.strip())

    # -- lifecycle --------------------------------------------------------

    def acquire(self, workspace_ref: str) -> WorkspaceLease:
        """Idempotently provision a worktree for ``workspace_ref``.

        Raises :class:`WorkspaceUnavailable` when isolation is impossible, so
        the caller can fall back to running in the project root.
        """

        if not git_available():
            raise WorkspaceUnavailable("git is not installed")
        if not is_git_repo(self.project_root):
            raise WorkspaceUnavailable(f"{self.project_root} is not a git repository")
        base = self._base_ref()
        if base is None:
            raise WorkspaceUnavailable(
                f"{self.project_root} has no commit to branch a workspace from"
            )

        path = self._resolved_path(workspace_ref)
        branch = self.branch_name(workspace_ref)
        if path.exists() and self._registered(path):
            return WorkspaceLease(workspace_ref, path, branch, base)

        # Stale registrations from a SIGKILLed run block a fresh add.
        try:
            _git(self.project_root, "worktree", "prune")
        except (OSError, subprocess.SubprocessError):
            pass
        # An unregistered leftover directory makes `worktree add` fail with
        # "already exists"; clear it first.
        if path.exists() and not self._registered(path):
            shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)

        last_error = ""
        for attempt in range(LOCK_RETRIES):
            try:
                if self._branch_exists(branch):
                    result = _git(
                        self.project_root, "worktree", "add", str(path), branch
                    )
                else:
                    result = _git(
                        self.project_root, "worktree", "add", "-b", branch,
                        str(path), base,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = repr(exc)
                result = None
            if result is not None and result.returncode == 0:
                return WorkspaceLease(workspace_ref, path, branch, base)
            if result is not None:
                last_error = (result.stderr or result.stdout).strip()
            # A concurrent acquire for the same ref may have won the add.
            if self._registered(path):
                return WorkspaceLease(workspace_ref, path, branch, base)
            time.sleep(0.2 * (attempt + 1))
        raise WorkspaceError(f"failed to create workspace {workspace_ref!r}: {last_error}")

    def release(self, workspace_ref: str, *, delete_branch: bool = True) -> None:
        """Remove a workspace. Idempotent — releasing twice is not an error."""

        path = self._resolved_path(workspace_ref)
        commands = [
            ("worktree", "remove", "--force", str(path)),
            ("worktree", "prune"),
        ]
        if delete_branch:
            commands.append(("branch", "-D", self.branch_name(workspace_ref)))
        for args in commands:
            try:
                _git(self.project_root, *args)
            except (OSError, subprocess.SubprocessError):
                pass

    def list_workspaces(self) -> tuple[str, ...]:
        """Slugs of every directory currently under the worktrees root."""

        root = self._checked_root()
        if not root.exists():
            return ()
        return tuple(sorted(item.name for item in root.iterdir() if item.is_dir()))

    def sweep(self, live_refs: set[str] | frozenset[str]) -> tuple[str, ...]:
        """Reclaim workspaces whose ref is no longer live.

        Takes the live set directly instead of querying a store: reclamation is
        a filesystem concern, and coupling it to engine tables is what made the
        legacy sweep untestable in isolation.  Returns the reclaimed slugs.
        """

        root = self._checked_root()
        if not root.exists() or not is_git_repo(self.project_root):
            return ()
        live_slugs = {workspace_slug(ref) for ref in live_refs}
        reclaimed: list[str] = []
        for slug in self.list_workspaces():
            if slug in live_slugs:
                continue
            path = root / slug
            for args in (
                ("worktree", "remove", "--force", str(path)),
                ("worktree", "prune"),
                ("branch", "-D", f"{BRANCH_PREFIX}{slug}"),
            ):
                try:
                    _git(self.project_root, *args)
                except (OSError, subprocess.SubprocessError):
                    pass
            shutil.rmtree(path, ignore_errors=True)
            reclaimed.append(slug)
        return tuple(reclaimed)

    def ensure_state_dir_ignored(self) -> None:
        """Keep worktrees out of the repository they branch from.

        Without this the integrate step sees its own scratch directories as
        untracked changes and `git status` is never clean.
        """

        gitignore = self.project_root / ".gitignore"
        state_dir = self.state_dir
        try:
            rel = state_dir.relative_to(self.project_root)
        except ValueError:
            return  # state dir lives outside the repo; nothing to ignore
        entry = f"{rel.as_posix()}/"
        existing = ""
        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8")
            if entry in existing.split():
                return
        joiner = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(f"{existing}{joiner}{entry}\n", encoding="utf-8")
