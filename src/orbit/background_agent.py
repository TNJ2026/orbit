"""Machine-wide unattended Agent worker.

The worker talks only to the stable Hub. Each claimed item is executed by one
child command using a deliberately small JSON stdin/stdout contract: stdin is
the delegation request and stdout must end with a JSON object result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import time
import uuid
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BackgroundAgentError(RuntimeError):
    pass


def _post(base_url: str, operation: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}/internal/v1/background-delegations/{operation}",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read()
    except HTTPError as exc:
        raise BackgroundAgentError(exc.read().decode("utf-8", "replace")) from exc
    except (OSError, URLError) as exc:
        raise BackgroundAgentError(f"Orbit Hub is unavailable: {exc}") from exc
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackgroundAgentError("Orbit Hub returned invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise BackgroundAgentError("Orbit Hub returned a non-object response")
    return decoded


class BackgroundAgentWorker:
    def __init__(
        self, command: str, *, hub_url: str = "http://127.0.0.1:8848",
        pools: tuple[str, ...] = ("default",), lease_seconds: int = 30,
        poll_seconds: float = 1.0, worker_id: str | None = None,
        parent_pid: int | None = None,
    ) -> None:
        argv = tuple(shlex.split(command))
        if not argv:
            raise ValueError("background Agent command is required")
        if not 10 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 10 and 300")
        self.argv = argv
        self.hub_url = hub_url
        self.pools = pools
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.parent_pid = parent_pid
        self.worker_id = worker_id or (
            f"background:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )

    def _lease_body(self, workspace_id: str, delegation_id: str) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id, "delegation_id": delegation_id,
            "worker_id": self.worker_id, "lease_seconds": self.lease_seconds,
        }

    def run_once(self) -> bool:
        claimed = _post(self.hub_url, "claim", {
            "worker_id": self.worker_id, "pools": list(self.pools),
            "lease_seconds": self.lease_seconds,
        })
        item = claimed.get("delegation")
        if not isinstance(item, Mapping):
            return False
        workspace_id = str(claimed["workspace_id"])
        delegation_id = str(item["delegation_id"])
        workspace = Path(str(claimed["workspace_path"]))
        with tempfile.TemporaryFile(mode="w+t") as output, \
                tempfile.TemporaryFile(mode="w+t") as errors:
            process = subprocess.Popen(
                self.argv, cwd=workspace, stdin=subprocess.PIPE, stdout=output,
                stderr=errors, text=True, start_new_session=os.name != "nt",
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(item["request"], sort_keys=True))
            process.stdin.close()
            cancelled = False
            next_renewal = time.monotonic() + self.lease_seconds / 3
            try:
                while process.poll() is None:
                    time.sleep(min(0.5, max(0.05, next_renewal - time.monotonic())))
                    if time.monotonic() < next_renewal:
                        continue
                    renewed = _post(
                        self.hub_url, "renew",
                        self._lease_body(workspace_id, delegation_id),
                    ).get("delegation")
                    if isinstance(renewed, Mapping) and renewed.get("cancel_requested"):
                        process.terminate()
                        cancelled = True
                    next_renewal = time.monotonic() + self.lease_seconds / 3
            except BaseException:
                # Losing the Hub means losing the authority to keep working.
                # Stop the child and leave the lease to become unknown; never
                # claim success for an execution whose ownership was lost.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise
            output.seek(0)
            errors.seek(0)
            stdout, stderr = output.read(), errors.read()
        completion = self._lease_body(workspace_id, delegation_id)
        if process.returncode == 0 and not cancelled:
            try:
                result = json.loads(stdout)
                if not isinstance(result, Mapping):
                    raise ValueError("result is not an object")
            except (json.JSONDecodeError, ValueError) as exc:
                completion["error"] = f"Agent returned invalid JSON: {exc}"
            else:
                completion["result"] = dict(result)
        else:
            reason = "Agent was cancelled" if cancelled else (
                stderr.strip() or f"Agent exited with status {process.returncode}"
            )
            completion["error"] = reason[-4000:]
        _post(self.hub_url, "complete", completion)
        return True

    def serve_forever(self) -> None:
        while True:
            if self.parent_pid is not None:
                try:
                    os.kill(self.parent_pid, 0)
                except OSError:
                    return
            try:
                worked = self.run_once()
            except BackgroundAgentError as exc:
                print(f"background Agent: {exc}", flush=True)
                worked = False
            if not worked:
                time.sleep(self.poll_seconds)
