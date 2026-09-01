#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Desktop Agent Apps commonly start MCP servers with a minimal PATH that does
# not include Homebrew or user-local binaries. Resolve uv before starting the
# proxy, then export its directory so the proxy can also launch the Runtime
# command declared in agent-app.json (which intentionally remains portable as
# the bare command "uv"). ORBIT_UV_BIN is an escape hatch for other layouts.
UV_BIN="${ORBIT_UV_BIN:-}"
if [ -z "$UV_BIN" ]; then
  UV_BIN="$(command -v uv 2>/dev/null || true)"
fi
if [ -z "$UV_BIN" ]; then
  for candidate in \
    /opt/homebrew/bin/uv \
    /usr/local/bin/uv \
    "${HOME:-}/.local/bin/uv" \
    "${HOME:-}/.cargo/bin/uv"
  do
    if [ -x "$candidate" ]; then
      UV_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
  echo "Orbit MCP proxy could not find uv." >&2
  echo "Install uv, add it to PATH, or set ORBIT_UV_BIN to its absolute path." >&2
  exit 127
fi

UV_DIR="$(cd "$(dirname "$UV_BIN")" && pwd -P)"
UV_BIN="$UV_DIR/$(basename "$UV_BIN")"
export PATH="$UV_DIR:$PATH"

if [ -n "${ORBIT_AGENT_APP_WORKSPACE:-}" ]; then
  WORKSPACE_DIR="$(cd "$ORBIT_AGENT_APP_WORKSPACE" && pwd -P)"
  exec "$UV_BIN" run --project "$ROOT_DIR" orbit agent-app mcp-proxy \
    "$ROOT_DIR/agent-app.json" --workspace "$WORKSPACE_DIR"
fi

exec "$UV_BIN" run --project "$ROOT_DIR" orbit agent-app mcp-proxy \
  "$ROOT_DIR/agent-app.json"
