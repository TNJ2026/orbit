"""OS-backed local CLI sandbox with explicit fail-closed capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import resource
import selectors
import shutil
import subprocess
import time


@dataclass(frozen=True)
class SandboxPolicy:
    root: Path
    allowed_executables: tuple[str, ...]
    network_allowed: bool = False
    timeout_seconds: float = 30
    max_output_bytes: int = 1024 * 1024
    max_memory_bytes: int = 256 * 1024 * 1024
    max_processes: int = 1
    cpu_seconds: int = 30
    trusted_first_party: bool = False
    require_memory_enforcement: bool = True

    def __post_init__(self) -> None:
        if not self.allowed_executables:
            raise ValueError("sandbox executable allowlist is required")
        if any(
            value <= 0
            for value in (
                self.timeout_seconds,
                self.max_output_bytes,
                self.max_memory_bytes,
                self.max_processes,
                self.cpu_seconds,
            )
        ):
            raise ValueError("sandbox limits must be positive")


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    backend: str


class SandboxUnavailable(PermissionError):
    pass


def _darwin_profile(policy: SandboxPolicy, root: Path) -> str:
    escaped = str(root).replace('"', '\\"')
    rules = ["(version 1)", "(allow default)", "(deny file-write*)"]
    rules.append(f'(allow file-write* (subpath "{escaped}"))')
    if not policy.network_allowed:
        rules.append("(deny network*)")
    if policy.max_processes == 1:
        rules.append("(deny process-fork)")
    return " ".join(rules)


def _wrap_with_os_sandbox(
    argv: tuple[str, ...], policy: SandboxPolicy, root: Path
) -> tuple[tuple[str, ...], str]:
    system = platform.system()
    if policy.trusted_first_party:
        return argv, "trusted-first-party"
    if system == "Darwin":
        backend = shutil.which("sandbox-exec")
        if backend is None:
            raise SandboxUnavailable("macOS sandbox-exec is unavailable")
        if policy.require_memory_enforcement:
            raise SandboxUnavailable(
                "macOS backend cannot enforce a hard address-space limit; "
                "use an external container or explicitly disable this requirement"
            )
        return (
            backend,
            "-p",
            _darwin_profile(policy, root),
            "--",
            *argv,
        ), "macos-sandbox-exec"
    if system == "Linux":
        backend = shutil.which("bwrap")
        if backend is None:
            raise SandboxUnavailable("Linux bubblewrap is unavailable")
        network = () if policy.network_allowed else ("--unshare-net",)
        wrapped = (
            backend,
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(root),
            str(root),
            "--chdir",
            str(root),
            *network,
            "--",
            *argv,
        )
        return wrapped, "linux-bubblewrap"
    raise SandboxUnavailable(f"no supported OS sandbox for {system}")


def run_sandboxed(
    argv,
    policy: SandboxPolicy,
    *,
    cwd: Path | None = None,
    env=None,
) -> SandboxResult:
    if not argv:
        raise ValueError("sandbox command is required")
    executable = Path(argv[0]).name
    if executable not in policy.allowed_executables:
        raise PermissionError("executable not allowed")
    resolved = shutil.which(argv[0])
    if resolved is None:
        raise ValueError("sandbox executable was not found")

    root = policy.root.resolve(strict=True)
    work = (cwd or root).resolve(strict=True)
    if work != root and root not in work.parents:
        raise PermissionError("working directory escapes sandbox")
    command, backend = _wrap_with_os_sandbox(
        (resolved, *(str(item) for item in argv[1:])), policy, root
    )

    def limits() -> None:
        resource.setrlimit(
            resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds)
        )
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (policy.max_output_bytes, policy.max_output_bytes),
        )
        if platform.system() == "Linux":
            resource.setrlimit(
                resource.RLIMIT_NPROC,
                (policy.max_processes, policy.max_processes),
            )
            resource.setrlimit(
                resource.RLIMIT_AS,
                (policy.max_memory_bytes, policy.max_memory_bytes),
            )
        os.setsid()

    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if env:
        safe_env.update(
            {
                key: value
                for key, value in env.items()
                if key in {"LANG", "LC_ALL", "TZ"}
            }
        )
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=work,
        env=safe_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        preexec_fn=limits,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        while selector.get_map():
            if time.monotonic() - started > policy.timeout_seconds:
                process.kill()
                process.wait()
                raise TimeoutError("sandbox timeout")
            for key, _ in selector.select(timeout=0.05):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                used = sum(len(value) for value in buffers.values())
                remaining = policy.max_output_bytes - used
                if remaining <= 0 or len(chunk) > remaining:
                    process.kill()
                    process.wait()
                    raise ValueError("sandbox output limit exceeded")
                buffers[key.data].extend(chunk)
        process.wait(timeout=1)
        return SandboxResult(
            process.returncode,
            bytes(buffers["stdout"]),
            bytes(buffers["stderr"]),
            time.monotonic() - started,
            backend,
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
