"""Git repository provisioning and per-task worktree lifecycle."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .store import Store, project_state_dir


WORKTREE_LOCK_RETRIES = 5


def project_root(project_root: str | None) -> Path:
    return Path(project_root).resolve() if project_root else Path.cwd().resolve()


def task_workflow_finished(task: dict[str, Any] | None) -> bool:
    if task is None:
        return True
    status = task.get("task_status")
    if status == "closed":
        return True
    return status == "accepted" and bool(task.get("is_goal"))


def git(
    root: Path, *args: str, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_git_repo(root: Path) -> bool:
    try:
        return git(root, "rev-parse", "--git-dir", timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def worktree_branch(task_id: int) -> str:
    return f"orbit/task-{task_id}"


def task_worktree_dir(project_root_value: str | None, task_id: int) -> Path:
    return (
        project_state_dir(project_root(project_root_value))
        / "worktrees"
        / f"task-{task_id}"
    )


def worktree_base_ref(root: Path) -> str | None:
    try:
        if git(root, "rev-parse", "--verify", "-q", "HEAD", timeout=10).returncode == 0:
            return "HEAD"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def git_available() -> bool:
    return shutil.which("git") is not None


def workflow_needs_git(cfg: dict[str, Any]) -> bool:
    # A workflow needs git when any step resolves to the git.worktree
    # environment provider, or an integrate step must merge a task branch in
    # the main tree. Late import: environments.py wraps this module.
    from .environments import GIT_WORKTREE, resolve_environment

    for step in cfg["steps"]:
        provider, _scope, _cleanup = resolve_environment(step)
        if provider is GIT_WORKTREE or step.get("integrate"):
            return True
    return False


def ensure_state_dir_gitignored(root: Path) -> None:
    state = project_state_dir(root)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    present = set(existing.splitlines())
    wanted = [f"{state.name}/tasks/", f"{state.name}/worktrees/"]
    missing = [line for line in wanted if line not in present]
    if not missing:
        return
    joiner = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(
        existing + joiner + "".join(f"{line}\n" for line in missing),
        encoding="utf-8",
    )


def ensure_git_repo(project_root_value: str | None) -> bool:
    """Guarantee a Git repository with a commit usable as a worktree base."""
    if not git_available():
        print(
            "git: not installed; workflow steps run in project root without "
            "worktree isolation (integrate is skipped)",
            flush=True,
        )
        return False
    root = project_root(project_root_value)
    if not is_git_repo(root):
        try:
            result = git(root, "init")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"git: init failed in {root}: {exc!r}", flush=True)
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            print(f"git: init failed in {root}: {detail}", flush=True)
            return False
        print(f"git: initialized empty repository in {root}", flush=True)
    if worktree_base_ref(root) is not None:
        return True
    ensure_state_dir_gitignored(root)
    identity = ("-c", "user.name=orbit", "-c", "user.email=orbit@localhost")
    message = "orbit: initialize repository for worktree isolation"
    try:
        git(root, "add", "-A")
        result = git(root, *identity, "commit", "-m", message)
        if result.returncode != 0:
            git(root, *identity, "commit", "--allow-empty", "-m", message)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"git: initial commit failed in {root}: {exc!r}", flush=True)
        return False
    if worktree_base_ref(root) is None:
        print(f"git: could not create an initial commit in {root}", flush=True)
        return False
    print(f"git: created initial commit as the worktree base in {root}", flush=True)
    return True


def branch_exists(root: Path, branch: str) -> bool:
    try:
        return git(
            root,
            "rev-parse",
            "--verify",
            "-q",
            f"refs/heads/{branch}",
            timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def worktree_registered(root: Path, worktree_dir: Path) -> bool:
    try:
        output = git(root, "worktree", "list", "--porcelain", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    target = str(worktree_dir.resolve())
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            if str(Path(line[len("worktree ") :].strip()).resolve()) == target:
                return True
        except OSError:
            continue
    return False


def commit_goal_design_artifacts(project_root_value: str | None) -> bool:
    """Commit only docs/ so newly created task worktrees receive design files."""
    if not git_available():
        return False
    root = project_root(project_root_value)
    if not (root / "docs").exists() or worktree_base_ref(root) is None:
        return False
    try:
        git(root, "add", "docs")
        if git(root, "diff", "--cached", "--quiet", "--", "docs").returncode == 0:
            return False
        identity = ["-c", "user.name=orbit", "-c", "user.email=orbit@localhost"]
        result = git(root, *identity, "commit", "-m", "orbit: goal design artifacts")
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_task_worktree(
    project_root_value: str | None, task_id: int
) -> Path | None:
    """Idempotently create or reattach a per-task Git worktree."""
    root = project_root(project_root_value)
    if not is_git_repo(root):
        print(
            f"worktree: {root} is not a git repo; step runs in project root "
            "without isolation",
            flush=True,
        )
        return None
    base = worktree_base_ref(root)
    if base is None:
        print(
            f"worktree: {root} has no commits yet; step runs without isolation",
            flush=True,
        )
        return None
    worktree_dir = task_worktree_dir(project_root_value, task_id)
    branch = worktree_branch(task_id)
    if worktree_dir.exists() and worktree_registered(root, worktree_dir):
        return worktree_dir
    try:
        git(root, "worktree", "prune")
    except (OSError, subprocess.SubprocessError):
        pass
    if worktree_dir.exists() and not worktree_registered(root, worktree_dir):
        shutil.rmtree(worktree_dir, ignore_errors=True)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(WORKTREE_LOCK_RETRIES):
        try:
            if branch_exists(root, branch):
                result = git(root, "worktree", "add", str(worktree_dir), branch)
            else:
                result = git(
                    root, "worktree", "add", "-b", branch, str(worktree_dir), base
                )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = repr(exc)
            result = None
        if result is not None and result.returncode == 0:
            return worktree_dir
        if result is not None:
            last_error = (result.stderr or result.stdout).strip()
        if worktree_registered(root, worktree_dir):
            return worktree_dir
        time.sleep(0.2 * (attempt + 1))
    print(f"worktree: failed to create {worktree_dir}: {last_error}", flush=True)
    return None


def remove_task_worktree(project_root_value: str | None, task_id: int) -> None:
    root = project_root(project_root_value)
    worktree_dir = task_worktree_dir(project_root_value, task_id)
    for args in (
        ("worktree", "remove", "--force", str(worktree_dir)),
        ("worktree", "prune"),
        ("branch", "-D", worktree_branch(task_id)),
    ):
        try:
            git(root, *args)
        except (OSError, subprocess.SubprocessError):
            pass


def sweep_task_worktrees(store: Store, project_root_value: str | None) -> None:
    root = project_root(project_root_value)
    worktree_root = project_state_dir(root) / "worktrees"
    if not worktree_root.exists() or not is_git_repo(root):
        return
    try:
        git(root, "worktree", "prune")
    except (OSError, subprocess.SubprocessError):
        pass
    for child in sorted(worktree_root.iterdir()):
        if not child.is_dir() or not child.name.startswith("task-"):
            continue
        try:
            task_id = int(child.name[len("task-") :])
        except ValueError:
            continue
        if task_workflow_finished(store.get_task(task_id)):
            remove_task_worktree(project_root_value, task_id)
