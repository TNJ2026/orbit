"""Deprecated shim: the project index now lives in `orbit.platform.projects`.

Re-exported rather than duplicated so the legacy engine and the new platform
module can never disagree about the index format or the online probe. Deleted
in M6 together with the legacy server.
"""

from __future__ import annotations

from .platform.projects import (
    DEFAULT_PROJECT_INDEX_PATH,
    browser_host,
    is_project_online,
    list_projects,
    project_id,
    server_url,
    upsert_project,
)

__all__ = [
    "DEFAULT_PROJECT_INDEX_PATH",
    "browser_host",
    "is_project_online",
    "list_projects",
    "project_id",
    "server_url",
    "upsert_project",
]
