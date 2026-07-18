"""Concurrency-safe child process control.

Behaviour contract: docs/migration/m1b-behaviour-inventory.md §1.

Every child runs in its own process group, so terminating one never signals
orbit itself.  The descendant snapshot is taken *before* the kill: once a parent
dies its children reparent to init and the tree link is gone, so a `setsid`
child that escaped the group could not be found afterwards.

This module owns no engine state.  It does not know what a task, a run or a
node is — callers pass a process spec and get a handle back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import Callable, Mapping, Sequence


IS_WINDOWS = os.name == "nt"

DEFAULT_READ_SIZE = 4096
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_KILL_GRACE_SECONDS = 2.0

Redactor = Callable[[str], str]


def detached_process_kwargs() -> dict[str, object]:
    """Popen kwargs that put the child in its own process group."""

    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# --- process tree discovery ------------------------------------------------


def _ppids_windows() -> dict[int, int] | None:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):
        return None

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return None
    mapping: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            mapping[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return mapping


def _ppids_ps() -> dict[int, int] | None:
    if IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    mapping: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            mapping[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return mapping


def _ppids_procfs() -> dict[int, int] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    mapping: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # comm may contain spaces and parentheses; ppid is the field after the
        # closing paren and the state character.
        close = stat_text.rfind(")")
        if close == -1:
            continue
        fields = stat_text[close + 2:].split()
        if len(fields) < 2:
            continue
        try:
            mapping[int(entry.name)] = int(fields[1])
        except ValueError:
            continue
    return mapping or None


def snapshot_ppids() -> dict[int, int]:
    """pid -> ppid for every visible process, via the first backend that works."""

    for backend in (_ppids_windows, _ppids_ps, _ppids_procfs):
        mapping = backend()
        if mapping:
            return mapping
    return {}


def descendant_pids(root_pid: int) -> list[int]:
    """Every descendant of ``root_pid`` at snapshot time (root excluded).

    Non-positive pids return nothing: on macOS pid 0 is the parent of launchd,
    so treating it as a root would claim every process on the machine as a
    descendant — a footgun for any caller that then kills the result.
    """

    if root_pid <= 0:
        return []
    mapping = snapshot_ppids()
    if not mapping:
        return []
    children: dict[int, list[int]] = {}
    for pid, ppid in mapping.items():
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid == root_pid or pid in found:
            continue
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def _taskkill_tree(pid: int, force: bool) -> bool:
    """Windows: end a process and its whole tree. True when dispatched."""

    if not pid:
        return False
    args = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        args.insert(1, "/F")
    try:
        subprocess.run(args, capture_output=True, timeout=10, check=False)
        return True
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def terminate_pid_tree(pid: int) -> bool:
    """Best-effort graceful stop of a process tree. True when dispatched.

    Windows has no graceful group signal for a detached CLI, so a forced
    taskkill *is* the graceful path there.
    """

    if not pid:
        return False
    if IS_WINDOWS:
        return _taskkill_tree(pid, force=True)
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def kill_pid_tree(pid: int) -> bool:
    """Force-kill a process tree, including children that escaped the group."""

    if not pid:
        return False
    if IS_WINDOWS:
        return _taskkill_tree(pid, force=True)
    # Snapshot first: after the group dies, escaped children reparent to init
    # and are no longer reachable from this pid.
    escaped = descendant_pids(pid)
    dispatched = False
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        dispatched = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    for child in escaped:
        try:
            os.kill(child, signal.SIGKILL)
            dispatched = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return dispatched


# --- streaming -------------------------------------------------------------


@dataclass
class OutputBuffer:
    """Bounded, optionally redacted capture of one stream."""

    limit_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    redactor: Redactor | None = None
    chunks: list[str] = field(default_factory=list)
    byte_count: int = 0
    truncated: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, chunk: str) -> str:
        """Record a chunk; returns what was actually kept (possibly clipped)."""

        if not chunk:
            return ""
        if self.redactor is not None:
            chunk = self.redactor(chunk)
        encoded = len(chunk.encode("utf-8", errors="replace"))
        with self._lock:
            if self.truncated:
                return ""
            room = self.limit_bytes - self.byte_count
            if room <= 0:
                self.truncated = True
                return ""
            if encoded > room:
                # Clip on a character boundary rather than mid-codepoint.
                kept = chunk.encode("utf-8", errors="replace")[:room].decode(
                    "utf-8", errors="ignore"
                )
                self.truncated = True
            else:
                kept = chunk
            self.chunks.append(kept)
            self.byte_count += len(kept.encode("utf-8", errors="replace"))
            return kept

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self.chunks)


def stream_output(
    stream,
    buffer: OutputBuffer,
    *,
    on_chunk: Callable[[str], None] | None = None,
    read_size: int = DEFAULT_READ_SIZE,
) -> None:
    """Drain ``stream`` into ``buffer`` until EOF or the read end is closed.

    Reads with ``os.read`` rather than buffered iteration so output is visible
    while the child is still running.  A closed read end is a normal stop
    condition: the kill path closes it deliberately to unwedge a reader blocked
    on a pipe that an escaped child still holds open.
    """

    if stream is None:
        return
    try:
        while True:
            try:
                raw = os.read(stream.fileno(), read_size)
            except (OSError, ValueError):
                break
            if not raw:
                break
            chunk = raw.decode("utf-8", errors="replace")
            if not chunk:
                break
            kept = buffer.append(chunk)
            if kept and on_chunk is not None:
                try:
                    on_chunk(kept)
                except Exception:
                    # A failing sink must not kill the drain loop; the process
                    # still needs to be reaped.
                    pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


# --- handle ----------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cancelled: bool = False
    timed_out: bool = False


class ProcessHandle:
    """A running child process and its captured output.

    Thread-safe: ``cancel()`` may be called from another thread while
    ``wait()`` is draining the pipes.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        redactor: Redactor | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        if not argv or not all(argv):
            raise ValueError("argv must be a non-empty sequence of non-empty strings")
        self.argv = tuple(argv)
        self.stdout = OutputBuffer(max_output_bytes, redactor)
        self.stderr = OutputBuffer(max_output_bytes, redactor)
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._lock = threading.Lock()
        self._cancelled = False
        self._threads: list[threading.Thread] = []

        self._process = subprocess.Popen(
            self.argv,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **detached_process_kwargs(),
        )
        if stdin_text is not None:
            self._write_stdin(stdin_text)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _write_stdin(self, text: str) -> None:
        # Write on a thread: a child that never reads stdin would otherwise
        # deadlock the caller on a full pipe.
        def writer() -> None:
            try:
                assert self._process.stdin is not None
                self._process.stdin.write(text.encode("utf-8"))
                self._process.stdin.flush()
            except (OSError, ValueError, AssertionError):
                pass
            finally:
                try:
                    if self._process.stdin is not None:
                        self._process.stdin.close()
                except OSError:
                    pass

        thread = threading.Thread(target=writer, name="process-stdin", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _start_drains(self) -> None:
        for stream, buffer, sink, name in (
            (self._process.stdout, self.stdout, self._on_stdout, "stdout"),
            (self._process.stderr, self.stderr, self._on_stderr, "stderr"),
        ):
            thread = threading.Thread(
                target=stream_output,
                args=(stream, buffer),
                kwargs={"on_chunk": sink},
                name=f"process-{name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def wait(self, timeout: float | None = None) -> ProcessResult:
        """Run to completion, killing the tree on timeout."""

        self._start_drains()
        timed_out = False
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill()
            try:
                self._process.wait(timeout=DEFAULT_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        for thread in self._threads:
            thread.join(timeout=DEFAULT_KILL_GRACE_SECONDS)
        return ProcessResult(
            returncode=self._process.returncode,
            stdout=self.stdout.text,
            stderr=self.stderr.text,
            stdout_truncated=self.stdout.truncated,
            stderr_truncated=self.stderr.truncated,
            cancelled=self.cancelled,
            timed_out=timed_out,
        )

    def terminate(self) -> bool:
        return terminate_pid_tree(self._process.pid)

    def kill(self) -> bool:
        """Force-kill the tree and unblock any reader wedged on a held pipe."""

        dispatched = kill_pid_tree(self._process.pid)
        try:
            self._process.kill()
        except OSError:
            pass
        # An escaped child can keep the write end open, leaving our drain thread
        # blocked on read forever. Closing our read end is what unwedges it.
        for stream in (self._process.stdout, self._process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        return dispatched

    def cancel(self, *, grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS) -> bool:
        """Cooperative stop: terminate, then force-kill if it does not exit."""

        with self._lock:
            self._cancelled = True
        self.terminate()
        try:
            self._process.wait(timeout=grace_seconds)
            return True
        except subprocess.TimeoutExpired:
            self.kill()
            return False


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    timeout: float | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    redactor: Redactor | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> ProcessResult:
    """Spawn, stream and reap a child process in one call."""

    handle = ProcessHandle(
        argv, cwd=cwd, env=env, stdin_text=stdin_text,
        max_output_bytes=max_output_bytes, redactor=redactor,
        on_stdout=on_stdout, on_stderr=on_stderr,
    )
    return handle.wait(timeout=timeout)
