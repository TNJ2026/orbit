#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

if [ "$#" -gt 1 ]; then
  echo "usage: start-orbit.sh [PROJECT_PATH]" >&2
  exit 2
fi

WORKSPACE_INPUT="${1:-${ORBIT_AGENT_APP_WORKSPACE:-}}"
if [ -z "$WORKSPACE_INPUT" ]; then
  echo "Orbit needs a project path. Open or select a project directory, then try again." >&2
  echo "Pass it as start-orbit.sh /absolute/project/path or set ORBIT_AGENT_APP_WORKSPACE." >&2
  exit 2
fi

if [ ! -d "$WORKSPACE_INPUT" ]; then
  echo "Orbit project path is not a directory: $WORKSPACE_INPUT" >&2
  echo "Select an existing project directory, then try again." >&2
  exit 2
fi

WORKSPACE_DIR="$(cd "$WORKSPACE_INPUT" && pwd -P)"

exec uv run --project "$ROOT_DIR" orbit agent-app ensure \
  "$ROOT_DIR/agent-app.json" --workspace "$WORKSPACE_DIR"
