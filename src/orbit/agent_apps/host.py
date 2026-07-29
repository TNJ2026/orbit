"""Single-instance lifecycle management for manifest-declared local Agent Apps."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..platform.process import (
    descendant_pids,
    detached_process_kwargs,
    kill_pid_tree,
    terminate_pid_tree,
)
from .manifest import AgentAppManifest, load_manifest


class AgentAppHostError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnsuredApp:
    manifest: AgentAppManifest
    workspace: Path | None
    state_dir: Path
    started: bool


def default_state_root() -> Path:
    configured = os.environ.get("AGENT_APP_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "agent-apps"


def _scope_key(manifest: AgentAppManifest, workspace: Path | None) -> str:
    if manifest.scope == "global":
        return "global"
    if workspace is None:
        raise AgentAppHostError("workspace-scoped app requires a workspace path")
    return hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]


def _health_check(url: str, timeout: float = 1.0) -> bool:
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, URLError):
        return False


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@contextmanager
def _startup_lock(path: Path) -> Iterator[None]:
    """An advisory, process-wide lock; callers still use health as truth."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            lock_file.write("0")
            lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class AgentAppHost:
    """Starts a declared App once per scope and waits for its real ready URL."""

    def __init__(
        self,
        *,
        state_root: Path | str | None = None,
        health_check: Callable[[str], bool] | None = None,
        launcher: Callable[[AgentAppManifest, Path, Path | None], subprocess.Popen] | None = None,
        process_exists: Callable[[int], bool] = _process_exists,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state_root = Path(state_root or default_state_root()).expanduser()
        self.health_check = health_check or _health_check
        self.launcher = launcher or self._launch
        self.process_exists = process_exists
        self.clock = clock
        self.sleep = sleep

    def ensure(self, manifest_path: Path | str, *, workspace: Path | str | None = None) -> EnsuredApp:
        manifest = load_manifest(manifest_path)
        resolved_workspace = (
            Path(workspace).expanduser().resolve() if workspace is not None else None
        )
        scope_key = _scope_key(manifest, resolved_workspace)
        state_dir = self.state_root / manifest.app_id / scope_key
        endpoint_key = hashlib.sha256(
            manifest.service.ready_url.encode("utf-8")
        ).hexdigest()[:16]
        with _startup_lock(self.state_root / "_endpoints" / f"{endpoint_key}.lock"):
            with _startup_lock(state_dir / "lock"):
                if self.health_check(manifest.service.ready_url):
                    if not self._owns_ready_endpoint(
                        state_dir, manifest, resolved_workspace
                    ):
                        raise AgentAppHostError(
                            f"{manifest.service.ready_url} is already served by an "
                            "unowned or different Agent App scope"
                        )
                    return EnsuredApp(
                        manifest, resolved_workspace, state_dir, started=False
                    )
                process = self.launcher(manifest, state_dir, resolved_workspace)
                self._record_process(
                    state_dir, manifest, process, resolved_workspace
                )
                deadline = self.clock() + manifest.service.timeout_seconds
                while self.clock() < deadline:
                    if self.health_check(manifest.service.ready_url):
                        return EnsuredApp(
                            manifest, resolved_workspace, state_dir, started=True
                        )
                    if process.poll() is not None:
                        raise AgentAppHostError(
                            f"{manifest.app_id} exited with code {process.returncode}; "
                            f"see {state_dir / 'service.stderr.log'}"
                        )
                    self.sleep(0.1)
                self._stop_process(process)
                raise AgentAppHostError(
                    f"{manifest.app_id} did not become ready within "
                    f"{manifest.service.timeout_seconds:g}s; "
                    f"see {state_dir / 'service.stderr.log'}"
                )

    def active_workspace(self, manifest_path: Path | str) -> Path | None:
        """Return the only live managed workspace for an App, if unambiguous."""

        manifest = load_manifest(manifest_path)
        if manifest.scope == "global":
            return None
        candidates: set[Path] = set()
        for pid_file in (self.state_root / manifest.app_id).glob("*/pid.json"):
            try:
                payload = json.loads(pid_file.read_text(encoding="utf-8"))
                pid = int(payload["pid"])
                workspace = Path(payload["workspace"]).expanduser().resolve()
            except (
                OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
            ):
                continue
            if (
                payload.get("app_id") == manifest.app_id
                and payload.get("ready_url") == manifest.service.ready_url
                and workspace.is_dir()
                and self.process_exists(pid)
            ):
                candidates.add(workspace)
        if not candidates:
            raise AgentAppHostError(
                f"no active managed workspace found for {manifest.app_id}; "
                "open the App first or set --workspace"
            )
        if len(candidates) > 1:
            raise AgentAppHostError(
                f"multiple active workspaces found for {manifest.app_id}; set --workspace"
            )
        return next(iter(candidates))

    def _launch(
        self, manifest: AgentAppManifest, state_dir: Path, workspace: Path | None,
    ) -> subprocess.Popen:
        state_dir.mkdir(parents=True, exist_ok=True)
        environment = {"PATH": os.environ.get("PATH", "")}
        for name in manifest.service.environment:
            if name in os.environ:
                environment[name] = os.environ[name]
        cwd = workspace if manifest.scope == "workspace" else manifest.service.cwd
        if cwd is None:
            cwd = manifest.service.cwd
        substitutions = {"{manifest_dir}": str(manifest.path.parent)}
        if workspace is not None:
            substitutions["{workspace}"] = str(workspace)
        command = []
        for argument in manifest.service.command:
            if "{workspace}" in argument and workspace is None:
                raise AgentAppHostError("global App command cannot use {workspace}")
            for marker, value in substitutions.items():
                argument = argument.replace(marker, value)
            command.append(argument)
        stdout = (state_dir / "service.stdout.log").open("ab")
        stderr = (state_dir / "service.stderr.log").open("ab")
        try:
            try:
                return subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                    **detached_process_kwargs(),
                )
            except OSError as exc:
                raise AgentAppHostError(
                    f"cannot start {manifest.app_id}: {exc}"
                ) from exc
        finally:
            stdout.close()
            stderr.close()

    @staticmethod
    def _record_process(
        state_dir: Path,
        manifest: AgentAppManifest,
        process: subprocess.Popen,
        workspace: Path | None,
    ) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pid.json").write_text(json.dumps({
            "pid": process.pid,
            "app_id": manifest.app_id,
            "ready_url": manifest.service.ready_url,
            "workspace": None if workspace is None else str(workspace),
        }, sort_keys=True), encoding="utf-8")

    def _owns_ready_endpoint(
        self,
        state_dir: Path,
        manifest: AgentAppManifest,
        workspace: Path | None,
    ) -> bool:
        try:
            payload = json.loads(
                (state_dir / "pid.json").read_text(encoding="utf-8")
            )
            pid = int(payload["pid"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
        return (
            payload.get("app_id") == manifest.app_id
            and payload.get("ready_url") == manifest.service.ready_url
            and payload.get("workspace")
            == (None if workspace is None else str(workspace))
            and self.process_exists(pid)
        )

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        descendants = descendant_pids(process.pid)
        terminate_pid_tree(process.pid)
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        kill_pid_tree(process.pid, known_descendants=descendants)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
