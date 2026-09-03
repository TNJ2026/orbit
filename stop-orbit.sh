#!/usr/bin/env bash
# Stop the Orbit Hub and every discovered workspace Runtime without restarting.
# The shared restart implementation performs PID identity checks before every
# signal, waits for graceful shutdown, and only then force-stops survivors.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

exec "$ROOT_DIR/restart-orbit.sh" --stop-only "$@"
