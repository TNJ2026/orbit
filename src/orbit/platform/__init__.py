"""Host-facing concerns that are independent of any workflow engine.

Modules here own project discovery, filesystem layout and process control.
They must not import the workflow domain, a persistence adapter, or the legacy
engine — the dependency arrow points from the engine to the platform, never
back.
"""

from .projects import (
    DEFAULT_PROJECT_INDEX_PATH,
    DEFAULT_STATE_ROOT,
    RUNTIME_DB_NAME,
    legacy_database_candidates,
    list_projects,
    project_db_path,
    project_id,
    project_state_dir,
    resolve_project_root,
    server_url,
    upsert_project,
    warn_about_legacy_database,
)

__all__ = [
    "DEFAULT_PROJECT_INDEX_PATH",
    "DEFAULT_STATE_ROOT",
    "RUNTIME_DB_NAME",
    "legacy_database_candidates",
    "list_projects",
    "project_db_path",
    "project_id",
    "project_state_dir",
    "resolve_project_root",
    "server_url",
    "upsert_project",
    "warn_about_legacy_database",
]
