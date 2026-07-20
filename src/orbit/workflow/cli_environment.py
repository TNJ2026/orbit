"""Minimal, identity-aware environment for trusted local Agent CLIs."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Mapping


def trusted_cli_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Keep shell secrets out while preserving OS credential-store identity.

    Claude Code on macOS needs ``USER`` to resolve its Keychain-backed login.
    ``HOME`` alone finds the config directory but reports the CLI as logged
    out. ``LOGNAME`` is carried as the matching POSIX identity; neither value
    is a credential. Provider/API tokens remain deliberately excluded.
    """

    values = os.environ if source is None else source
    user = values.get("USER") or values.get("LOGNAME") or getpass.getuser()
    return {
        "PATH": values.get("PATH", ""),
        "HOME": values.get("HOME") or str(Path.home()),
        "USER": user,
        "LOGNAME": values.get("LOGNAME") or user,
    }
