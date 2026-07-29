#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ -n "${ORBIT_AGENT_APP_WORKSPACE:-}" ]; then
  WORKSPACE_DIR="$(cd "$ORBIT_AGENT_APP_WORKSPACE" && pwd -P)"
  exec uv run --project "$ROOT_DIR" orbit agent-app mcp-proxy \
    "$ROOT_DIR/agent-app.json" --workspace "$WORKSPACE_DIR"
fi

exec uv run --project "$ROOT_DIR" orbit agent-app mcp-proxy \
  "$ROOT_DIR/agent-app.json"
