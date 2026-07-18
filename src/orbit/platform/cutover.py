"""The one-time acknowledgement that pre-migration data is being abandoned.

Before the cutover, a project could hold a legacy database containing both
old-engine records and Runtime data written through the transitional path.
The paths themselves live in `projects.legacy_database_candidates()`, the only
place in the package allowed to name them. The new Runtime reads none of it.
Rather than importing (which would resurrect the
dual-state problem the cutover exists to end) or deleting (which would destroy
data the user may still want), `orbit serve` refuses to start until the user
says, once, that they know.

Three rules shape this module:

* **Fail closed, and only when there is something to fail about.** No legacy
  file, no prompt — a fresh install never sees this.
* **Never open the legacy file.** Its path is stat-ed and printed. It is not
  read, copied, connected to or deleted. The marker records the path, not the
  contents.
* **The acknowledgement is auditable.** A `0600` marker records what was
  acknowledged and when, so a later "why is my old data gone?" has an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .projects import legacy_database_candidates, project_db_dir


MARKER_NAME = "cutover-acknowledged.json"
MARKER_VERSION = 1
ACKNOWLEDGE_FLAG = "--acknowledge-discard-legacy-data"

# A distinct code so a supervisor can tell "needs a human decision" from a
# crash or a port clash.
EXIT_NEEDS_ACKNOWLEDGEMENT = 3


class CutoverRequired(RuntimeError):
    """Legacy data exists and has not been acknowledged."""

    exit_code = EXIT_NEEDS_ACKNOWLEDGEMENT


@dataclass(frozen=True)
class CutoverMarker:
    version: int
    acknowledged_at: str
    acknowledged_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_paths": list(self.acknowledged_paths),
        }


def marker_path(project_dir: Path | str | None = None, base_dir=None) -> Path:
    return project_db_dir(project_dir, base_dir) / MARKER_NAME


def read_marker(project_dir=None, base_dir=None) -> CutoverMarker | None:
    path = marker_path(project_dir, base_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != MARKER_VERSION:
        return None
    return CutoverMarker(
        MARKER_VERSION,
        str(payload.get("acknowledged_at", "")),
        tuple(str(item) for item in payload.get("acknowledged_paths", ())),
    )


def write_marker(
    paths: tuple[Path, ...], *, project_dir=None, base_dir=None, now=None
) -> CutoverMarker:
    """Record the acknowledgement. Only paths and a timestamp are stored."""

    marker = CutoverMarker(
        MARKER_VERSION,
        (now or datetime.now(timezone.utc)).isoformat(),
        tuple(str(path) for path in paths),
    )
    target = marker_path(project_dir, base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600 through the descriptor rather than chmod-ed afterwards, so
    # the file is never briefly world-readable.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(marker.to_dict(), handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    return marker


def refusal_message(paths: tuple[Path, ...]) -> str:
    listed = "\n".join(f"  {path}" for path in paths)
    return (
        "Refusing to start: this project has pre-migration data from the "
        "legacy engine.\n"
        f"{listed}\n\n"
        "orbit no longer reads these files. Their contents — including any "
        "Runtime data written before the cutover — are abandoned, not "
        "migrated. orbit will not open, copy or delete them; that is your "
        "call.\n\n"
        f"To continue and accept that, start once with {ACKNOWLEDGE_FLAG}."
    )


def ensure_cutover_acknowledged(
    *, acknowledged: bool = False, project_dir=None, base_dir=None, now=None
) -> CutoverMarker | None:
    """Gate startup on the acknowledgement. Returns the marker, or None.

    Called before the database is opened, so a refusal cannot be preceded by a
    partial start.
    """

    existing = read_marker(project_dir, base_dir)
    if existing is not None:
        return existing

    paths = legacy_database_candidates(project_dir, base_dir)
    if not paths:
        # Nothing to abandon: a fresh project is never asked to acknowledge
        # anything, and no marker is written.
        return None

    if not acknowledged:
        raise CutoverRequired(refusal_message(paths))
    return write_marker(paths, project_dir=project_dir, base_dir=base_dir, now=now)
