"""Isolated working copies for steps that need one.

A workspace is addressed by an opaque ``workspace_ref`` supplied by the caller
(a run id, a node-run id, or anything else stable).  Nothing here knows about
tasks, runs or the workflow domain.
"""

from .git import (
    GitWorkspaceProvider,
    WorkspaceError,
    WorkspaceLease,
    WorkspaceUnavailable,
    git_available,
    is_git_repo,
)

__all__ = [
    "GitWorkspaceProvider",
    "WorkspaceError",
    "WorkspaceLease",
    "WorkspaceUnavailable",
    "git_available",
    "is_git_repo",
]
