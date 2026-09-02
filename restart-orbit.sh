#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HUB_HOST="${ORBIT_HUB_HOST:-127.0.0.1}"
HUB_PORT="${ORBIT_HUB_PORT:-8848}"
STATE_DIR="${ORBIT_HUB_STATE_DIR:-${HOME}/.orbit/hub}"
LOG_FILE="${ORBIT_HUB_LOG:-${STATE_DIR}/hub.log}"
PID_FILE="${STATE_DIR}/hub.pid"
DRY_RUN=0

if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: ./restart-orbit.sh [--dry-run]" >&2
  exit 2
fi

if [ -x "$ROOT_DIR/.venv/bin/orbit" ]; then
  ORBIT=("$ROOT_DIR/.venv/bin/orbit")
elif command -v uv >/dev/null 2>&1; then
  ORBIT=(uv run --project "$ROOT_DIR" orbit)
else
  echo "Orbit CLI not found; create .venv or install uv first." >&2
  exit 127
fi

if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof is required to identify the Hub listening on port $HUB_PORT." >&2
  exit 127
fi

temporary="$(mktemp -d "${TMPDIR:-/tmp}/orbit-restart.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT
runtime_json="$temporary/runtimes.json"
runtime_pids="$temporary/runtime-pids"
hub_pids="$temporary/hub-pids"

"${ORBIT[@]}" runtimes --json >"$runtime_json"
sed -n \
  's/^[[:space:]]*"pid":[[:space:]]*\([0-9][0-9]*\),\{0,1\}[[:space:]]*$/\1/p' \
  "$runtime_json" | sort -u >"$runtime_pids"
lsof -t -nP -iTCP:"$HUB_PORT" -sTCP:LISTEN 2>/dev/null \
  | sort -u >"$hub_pids" || true

while IFS= read -r pid; do
  [ -n "$pid" ] || continue
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$command_line" in
    *"orbit hub serve"*) ;;
    *)
      echo "Refusing to stop PID $pid on port $HUB_PORT; it is not an Orbit Hub:" >&2
      echo "  $command_line" >&2
      exit 1
      ;;
  esac
done <"$hub_pids"

stop_pid() {
  pid="$1"
  label="$2"
  if ! kill -0 "$pid" 2>/dev/null; then
    return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "Would stop $label (PID $pid)."
    return
  fi
  echo "Stopping $label (PID $pid)..."
  kill -TERM "$pid" 2>/dev/null || true
}

# Stop the router first. Otherwise a connected Agent App can ask it to launch
# a replacement Runtime while this script is still stopping the old ones.
while IFS= read -r pid; do
  [ -n "$pid" ] && stop_pid "$pid" "Orbit Hub"
done <"$hub_pids"
while IFS= read -r pid; do
  [ -n "$pid" ] && stop_pid "$pid" "Orbit Runtime"
done <"$runtime_pids"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run complete; nothing was stopped or started."
  exit 0
fi

# Give graceful shutdown enough time to settle workers and active Runs. A
# Runtime that still exists afterwards is explicitly forced down: this script
# is the operator's full-restart escape hatch.
deadline=$((SECONDS + 45))
while [ "$SECONDS" -lt "$deadline" ]; do
  alive=0
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    if kill -0 "$pid" 2>/dev/null; then alive=1; break; fi
  done <"$hub_pids"
  if [ "$alive" -eq 0 ]; then
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      if kill -0 "$pid" 2>/dev/null; then alive=1; break; fi
    done <"$runtime_pids"
  fi
  [ "$alive" -eq 0 ] && break
  sleep 1
done

while IFS= read -r pid; do
  [ -n "$pid" ] || continue
  if kill -0 "$pid" 2>/dev/null; then
    echo "Force stopping stale process (PID $pid)..."
    kill -KILL "$pid" 2>/dev/null || true
  fi
done <"$hub_pids"
while IFS= read -r pid; do
  [ -n "$pid" ] || continue
  if kill -0 "$pid" 2>/dev/null; then
    echo "Force stopping stale Runtime (PID $pid)..."
    kill -KILL "$pid" 2>/dev/null || true
  fi
done <"$runtime_pids"

mkdir -p "$STATE_DIR"
echo "Starting Orbit Hub on http://$HUB_HOST:$HUB_PORT ..."
nohup "${ORBIT[@]}" hub serve --host "$HUB_HOST" --port "$HUB_PORT" \
  >>"$LOG_FILE" 2>&1 &
new_hub_pid=$!
printf '%s\n' "$new_hub_pid" >"$PID_FILE"

ready=0
for _attempt in $(seq 1 50); do
  if curl -fsS "http://$HUB_HOST:$HUB_PORT/health/ready" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$new_hub_pid" 2>/dev/null; then break; fi
  sleep 0.2
done

if [ "$ready" -ne 1 ]; then
  echo "Orbit Hub failed to become ready; recent log output:" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  exit 1
fi

echo "Orbit Hub restarted (PID $new_hub_pid)."
echo "Log: $LOG_FILE"
echo "Agent Apps may start new workspace Runtimes as soon as they reconnect."
