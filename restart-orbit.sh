#!/usr/bin/env bash
# Stop the Orbit Hub and the Runtimes it manages, then start it again the way
# it is normally started.
#
# The start half is deliberately not implemented here. `agent-app.json` already
# declares the command, the ready URL and how long a cold start may take, and
# `orbit agent-app ensure` — what `scripts/start-orbit.sh` runs — owns the
# `pid.json` that records which process holds the port. A restart that starts
# the Hub itself leaves that file naming a process it killed, and the next
# `ensure` then refuses to run at all: the port answers, the recorded PID is
# gone, and it will not adopt a Hub it does not own. So this script stops, and
# hands starting back.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STATE_ROOT="${AGENT_APP_STATE_DIR:-${HOME}/.local/state/agent-apps}"
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

temporary="$(mktemp -d "${TMPDIR:-/tmp}/orbit-restart.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT
pids="$temporary/pids"

# Every PID this script may signal, each already checked against the process
# it belongs to. A PID from a record is a number that *was* a process: the OS
# reuses them, and a stale entry naming a PID something else now holds is how
# a restart script destroys an unrelated program. `ps` is the only thing that
# can say what a number is now, so nothing is signalled without asking it.
"${ORBIT[@]}" runtimes --json > "$temporary/runtimes.json"
python3 - "$temporary/runtimes.json" "$STATE_ROOT" "$ROOT_DIR/agent-app.json" > "$pids" <<'PYTHON'
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

runtimes_path, state_root, manifest_path = (Path(part) for part in sys.argv[1:4])


def command_of(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        return ""


candidates: list[tuple[int, str, str]] = []
# The Hub, as the Agent App host records it — not as whatever holds the port,
# which is a different question with a worse answer when they disagree.
for pid_file in sorted(state_root.glob("orbit/*/pid.json")):
    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
        candidates.append((int(payload["pid"]), "Orbit Hub", "orbit hub serve"))
    except (OSError, ValueError, KeyError, TypeError):
        continue

# And whatever is actually holding the ready port, which is not always the
# process the host recorded — a Hub started by hand leaves that record naming
# something dead while the port answers, and that is the state `ensure` will
# not start from. Identity-checked like everything else, so an unrelated
# program on the port is reported and left alone.
try:
    ready_url = json.loads(manifest_path.read_text(encoding="utf-8"))["service"]["ready_url"]
    port = urlparse(ready_url).port or 80
except (OSError, ValueError, KeyError, TypeError):
    port = None
if port is not None and shutil.which("lsof"):
    listeners = subprocess.run(
        ["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    for value in listeners:
        try:
            candidates.append((int(value), "Orbit Hub", "orbit hub serve"))
        except ValueError:
            continue

# Parsed, not scraped: `--json` is a document, and a pattern that expects one
# field per line finds nothing at all the day the output is compact.
try:
    listed = json.loads(runtimes_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    listed = []
if isinstance(listed, dict):
    listed = listed.get("runtimes", [])
for entry in listed if isinstance(listed, list) else []:
    if not isinstance(entry, dict):
        continue
    try:
        candidates.append((int(entry["pid"]), "Orbit Runtime", "orbit serve"))
    except (KeyError, TypeError, ValueError):
        continue

seen: set[int] = set()
for pid, label, expected in candidates:
    if pid <= 1 or pid in seen:
        continue
    seen.add(pid)
    command = command_of(pid)
    if not command:
        continue          # Already gone; nothing to stop and nothing to warn about.
    if expected not in command:
        print(
            f"skipping PID {pid}: recorded as {label} but now runs {command}",
            file=sys.stderr,
        )
        continue
    print(f"{pid}\t{label}")
PYTHON

if [ ! -s "$pids" ]; then
  echo "Nothing of Orbit's is running."
else
  # The Hub first — "Orbit Hub" sorts before "Orbit Runtime" — because a
  # connected Agent App can ask it to launch a replacement Runtime while this
  # is still stopping the old ones. The separator is explicit: the default
  # splits on blanks, so `-k2,2` would compare the word "Orbit" on every line
  # and fall back to sorting by PID.
  while IFS=$'\t' read -r pid label; do
    [ -n "$pid" ] || continue
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "Would stop $label (PID $pid)."
      continue
    fi
    echo "Stopping $label (PID $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
  done < <(sort -t"$(printf '\t')" -k2,2 "$pids")
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run complete; nothing was stopped or started."
  exit 0
fi

# Graceful shutdown settles workers and in-flight Runs. What is still up after
# that is forced down: this script is the operator's full-restart escape hatch,
# and every PID in the list has been confirmed to be what it claims.
deadline=$((SECONDS + 45))
while [ "$SECONDS" -lt "$deadline" ]; do
  alive=0
  while IFS=$'\t' read -r pid _label; do
    [ -n "$pid" ] || continue
    if kill -0 "$pid" 2>/dev/null; then alive=1; break; fi
  done <"$pids"
  [ "$alive" -eq 0 ] && break
  sleep 1
done
while IFS=$'\t' read -r pid label; do
  [ -n "$pid" ] || continue
  if kill -0 "$pid" 2>/dev/null; then
    echo "Force stopping $label (PID $pid)..."
    kill -KILL "$pid" 2>/dev/null || true
  fi
done <"$pids"

echo "Starting Orbit through the Agent App host..."
exec "$ROOT_DIR/scripts/start-orbit.sh"
