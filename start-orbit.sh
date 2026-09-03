#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [ -n "${ORBIT_CLI:-}" ]; then
  [ -x "$ORBIT_CLI" ] || { echo "ORBIT_CLI is not executable: $ORBIT_CLI" >&2; exit 127; }
  ORBIT=("$ORBIT_CLI")
elif [ -x "$ROOT_DIR/.venv/bin/orbit" ]; then
  ORBIT=("$ROOT_DIR/.venv/bin/orbit")
elif [ -x "$ROOT_DIR/.venv/Scripts/orbit.exe" ]; then
  ORBIT=("$ROOT_DIR/.venv/Scripts/orbit.exe")
elif command -v uv >/dev/null 2>&1; then
  ORBIT=(uv run --project "$ROOT_DIR" orbit)
else
  echo "Orbit cannot start: no project virtualenv or uv executable was found." >&2
  exit 127
fi

# Internal modes let the Agent App manifest and MCP configuration use this
# same portable launcher without exposing more user-facing scripts.
if [ "${1:-}" = "--hub-service" ]; then
  shift
  exec "${ORBIT[@]}" hub serve "$@"
fi
if [ "${1:-}" = "--mcp-proxy" ]; then
  shift
  if [ -n "${ORBIT_AGENT_APP_WORKSPACE:-}" ]; then
    workspace="$(cd "$ORBIT_AGENT_APP_WORKSPACE" && pwd -P)"
    exec "${ORBIT[@]}" agent-app mcp-proxy "$ROOT_DIR/agent-app.json" --workspace "$workspace" "$@"
  fi
  exec "${ORBIT[@]}" agent-app mcp-proxy "$ROOT_DIR/agent-app.json" "$@"
fi

if [ "$#" -gt 1 ]; then
  echo "usage: ./start-orbit.sh [PROJECT_PATH]" >&2
  exit 2
fi
workspace_input="${1:-$PWD}"
if [ ! -d "$workspace_input" ]; then
  echo "Orbit project path is not a directory: $workspace_input" >&2
  exit 2
fi
workspace="$(cd "$workspace_input" && pwd -P)"
"${ORBIT[@]}" agent-app ensure "$ROOT_DIR/agent-app.json" >/dev/null
exec "${ORBIT[@]}" hub register "$workspace"
