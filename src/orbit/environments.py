"""Execution-environment provider registry.

Design §3.1: an environment provider is not a node handler. The engine and
runner obtain a step's execution root through this seam; the per-task git
worktree is one provider among others, not a core-engine concern. The registry
is the extension point for future providers (temp dir, container, remote).
"""

from __future__ import annotations

from typing import Any

from .worktrees import (
    ensure_task_worktree as _ensure_task_worktree,
    project_root as _project_root,
    remove_task_worktree as _remove_task_worktree,
)


class EnvironmentProvider:
    """Creates, reuses, and cleans up execution environments (design §5.2/§5.3).

    `acquire(context)` returns {"root": Path, "meta": {...}}; `context` carries
    at least project_root, task_id, and the step. `release(context, policy)`
    applies the declared cleanup policy for one environment instance."""

    id = ""
    description = ""
    default_scope = "workflow_run"
    default_cleanup = "manual"

    def acquire(self, context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def release(self, context: dict[str, Any], policy: str) -> None:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "default_scope": self.default_scope,
            "default_cleanup": self.default_cleanup,
        }


class ProjectRootEnvironment(EnvironmentProvider):
    id = "project_root"
    description = "Shared project root; no provisioning, no isolation."

    def acquire(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "root": _project_root(context.get("project_root")),
            "meta": {"isolated": False},
        }

    def release(self, context: dict[str, Any], policy: str) -> None:
        # The project root is never provisioned, so never reclaimed.
        return None


class GitWorktreeEnvironment(EnvironmentProvider):
    """Per-task git worktree wrapping worktrees.py (unchanged internals)."""

    id = "git.worktree"
    description = (
        "Per-task git worktree on branch orbit/task-<id>; steps of the same "
        "task share one instance."
    )
    default_scope = "workflow_item"
    default_cleanup = "on_terminal"

    def acquire(self, context: dict[str, Any]) -> dict[str, Any]:
        # Falls back to the project root (no isolation) when the worktree
        # cannot be provisioned — non-git project, no base commit, git absent.
        project_root_value = context.get("project_root")
        worktree = _ensure_task_worktree(project_root_value, int(context["task_id"]))
        if worktree is None:
            return {
                "root": _project_root(project_root_value),
                "meta": {"isolated": False},
            }
        return {"root": worktree, "meta": {"isolated": True}}

    def release(self, context: dict[str, Any], policy: str) -> None:
        if policy == "manual":
            return None
        _remove_task_worktree(context.get("project_root"), int(context["task_id"]))


PROJECT_ROOT = ProjectRootEnvironment()
GIT_WORKTREE = GitWorktreeEnvironment()

# Registry extension point: future providers (tmp dir, container, remote)
# register here without touching graph routing or the runner.
ENVIRONMENT_PROVIDERS: dict[str, EnvironmentProvider] = {
    PROJECT_ROOT.id: PROJECT_ROOT,
    GIT_WORKTREE.id: GIT_WORKTREE,
}


def environment_provider_schema() -> list[dict[str, Any]]:
    """Provider descriptors exposed to the workflow editor via the node schema."""
    return [provider.describe() for provider in ENVIRONMENT_PROVIDERS.values()]


def resolve_environment(step: dict[str, Any]) -> tuple[EnvironmentProvider, str, str]:
    """Map one step to (provider, scope, cleanup).

    An explicit environment.type wins; a step without one derives it from the
    legacy isolate boolean (isolate -> git.worktree, otherwise project_root).
    Structural steps (integrate/decompose/approval) never isolate even when a
    git.worktree environment is declared — mirroring the isolate derivation in
    _normalize_workflow_step, so a normalized step resolves to git.worktree
    exactly when its `isolate` field is true."""
    raw = step.get("environment")
    env = raw if isinstance(raw, dict) else {}
    env_type = str(env.get("type") or "").strip()
    if not env_type:
        env_type = "git.worktree" if step.get("isolate") else "project_root"
    if env_type == "git.worktree" and (
        step.get("integrate")
        or step.get("decompose")
        or step.get("approval")
        or str(step.get("type") or "") == "approval"
    ):
        env_type = "project_root"
    provider = ENVIRONMENT_PROVIDERS.get(env_type)
    if provider is None:
        raise ValueError(f"unknown workflow environment provider: {env_type}")
    scope = str(env.get("scope") or provider.default_scope)
    cleanup = str(env.get("cleanup") or provider.default_cleanup)
    return provider, scope, cleanup
