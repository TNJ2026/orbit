"""Run-scoped Git worktrees granted by ``--agent-project-access``.

Non-Git projects use their real project root directly and therefore need no
workspace-copy implementation here. The grant stays lock-free and pickleable:
execution workers receive it through multiprocessing, while cleanup is safely
guarded by the provider's age-based sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .git import GitWorkspaceProvider, WorkspaceError


DEFAULT_MIN_AGE_SECONDS = 600.0


@dataclass
class GitWorktreeGrant:
    """Whole-project access through one isolated Git worktree per Run."""

    provider: GitWorkspaceProvider
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS

    def acquire(self, ref: str, *, files: Sequence[str] | None = None) -> Path:
        # ``files`` remains accepted because Handler callers use one interface;
        # a Git worktree always exposes the complete committed tree.
        dirty = self.provider.project_status_porcelain()
        if dirty:
            preview = "\n".join(f"  {line}" for line in dirty[:10])
            remainder = (
                "" if len(dirty) <= 10
                else f"\n  ... and {len(dirty) - 10} more"
            )
            raise WorkspaceError(
                f"source checkout {self.provider.project_root} has uncommitted "
                "or untracked changes; commit or stash them before starting a "
                f"workflow that needs project access:\n{preview}{remainder}"
            )
        return self.provider.acquire(ref).path

    def sweep(self, live_refs: Iterable[str]) -> tuple[str, ...]:
        return self.provider.sweep(
            frozenset(live_refs), min_age_seconds=self.min_age_seconds,
        )
