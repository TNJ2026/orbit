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

import codecs
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
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


def kill_pid_tree(pid: int, *, known_descendants: Sequence[int] = ()) -> bool:
    """Force-kill a process tree, including children that escaped the group.

    ``known_descendants`` is a snapshot the caller took *earlier*, before it
    sent any signal. Taking one here is too late for a caller that already
    terminated the group: the parent is gone, its escapees have reparented to
    init, and no walk of the current tree can reach them any more.
    """

    if not pid:
        return False
    if IS_WINDOWS:
        return _taskkill_tree(pid, force=True)
    escaped = list(dict.fromkeys([*known_descendants, *descendant_pids(pid)]))
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


def stop_pid_tree(
    pid: int,
    *,
    grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
    wait_for: Callable[[float], object] | None = None,
) -> bool:
    """TERM a process tree, then KILL whatever outlived the grace period.

    The descendant snapshot is taken before the first signal, which is the
    only moment it is still complete: a child that called ``setsid`` never
    receives the group signal, and once its parent dies it is no longer a
    descendant of anything this caller knows about. Everything after that
    point works from the snapshot.

    ``wait_for`` is the caller's own way of waiting on the main process — for
    a ``Popen`` that is ``process.wait``. Without it the grace period is spent
    sleeping instead of watching, which is correct but slower.
    """

    if not pid:
        return False
    escaped = descendant_pids(pid)
    dispatched = terminate_pid_tree(pid)
    # An escapee is outside the group, so the group TERM never reached it.
    for child in escaped:
        try:
            os.kill(child, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    exited = _await_exit(wait_for, grace_seconds)
    if exited and not any(_pid_alive(child) for child in escaped):
        return dispatched
    dispatched = kill_pid_tree(pid, known_descendants=escaped) or dispatched
    _await_exit(wait_for, grace_seconds)
    return dispatched


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or IS_WINDOWS:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _await_exit(wait_for: Callable[[float], object] | None, seconds: float) -> bool:
    """Give the main process ``seconds`` to exit; True when it did."""

    if wait_for is None:
        time.sleep(seconds)
        return False
    try:
        wait_for(seconds)
        return True
    except subprocess.TimeoutExpired:
        return False
    except (OSError, ValueError):
        return True


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
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    reached_eof = False
    try:
        while True:
            try:
                raw = os.read(stream.fileno(), read_size)
            except (OSError, ValueError):
                break
            if not raw:
                reached_eof = True
                break
            # A UTF-8 code point may straddle two os.read() calls.  Decoding
            # each block independently turns that valid character into U+FFFD;
            # the incremental decoder retains an incomplete suffix until the
            # next block arrives.
            chunk = decoder.decode(raw, final=False)
            if not chunk:
                continue
            kept = buffer.append(chunk)
            if kept and on_chunk is not None:
                try:
                    on_chunk(kept)
                except Exception:
                    # A failing sink must not kill the drain loop; the process
                    # still needs to be reaped.
                    pass
        if reached_eof:
            tail = decoder.decode(b"", final=True)
            if tail:
                kept = buffer.append(tail)
                if kept and on_chunk is not None:
                    try:
                        on_chunk(kept)
                    except Exception:
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
    # What first asked this process to stop. `cancelled` and `timed_out` say
    # *that* it was stopped; a caller deciding whether an attempt may be
    # retried needs to know *why*, and the two booleans cannot express a race
    # between them — whichever arrived first is the reason that counts.
    termination_reason: str | None = None
    # Drain threads still alive when the bounded join gave up. They hold a
    # pipe an escaped descendant keeps open, and nothing can safely kill a
    # Python thread — so the only honest thing is to count them. Silently
    # returning here is how a leak becomes invisible.
    leaked_drain_threads: int = 0


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
        self._termination_reason: str | None = None
        self._threads: list[threading.Thread] = []
        self._drain_errors: list[BaseException] = []

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
        def drain(stream, buffer, sink) -> None:
            try:
                stream_output(stream, buffer, on_chunk=sink)
            except BaseException as exc:  # surfaced by wait() on the owner thread
                with self._lock:
                    self._drain_errors.append(exc)

        for stream, buffer, sink, name in (
            (self._process.stdout, self.stdout, self._on_stdout, "stdout"),
            (self._process.stderr, self.stderr, self._on_stderr, "stderr"),
        ):
            thread = threading.Thread(
                target=drain,
                args=(stream, buffer, sink),
                name=f"process-{name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def wait(
        self, timeout: float | None = None,
        *, kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
        completion_predicate: Callable[[str], bool] | None = None,
    ) -> ProcessResult:
        """Run to completion, killing the tree on timeout."""

        self._start_drains()
        timed_out = False
        completed_output = False
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = (
                None if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            poll_timeout = remaining
            if completion_predicate is not None:
                poll_timeout = (
                    0.1 if remaining is None else min(0.1, remaining)
                )
            try:
                self._process.wait(timeout=poll_timeout)
                # The process can exit in the same polling slice that emitted
                # the marker. Check once more after wait() succeeds: otherwise
                # this race skips the marker branch and descendants that
                # inherited the pipes can keep the drains alive.
                if (
                    completion_predicate is not None
                    and completion_predicate(self.stdout.text)
                ):
                    completed_output = True
                    self._note_reason("completed_output")
                    kill_pid_tree(self._process.pid)
                    self._unwedge_readers()
                break
            except subprocess.TimeoutExpired:
                if (
                    completion_predicate is not None
                    and completion_predicate(self.stdout.text)
                ):
                    completed_output = True
                    self._note_reason("completed_output")
                elif deadline is None or time.monotonic() < deadline:
                    continue
                else:
                    timed_out = True
                    self._note_reason("timeout")
            # A terminal output marker is as authoritative as process exit for
            # a prompt CLI. Stop descendants now instead of waiting for a
            # gateway/tool child that kept the parent or its pipes alive.
            # Snapshot descendants before the first signal. A child may have
            # called setsid() and escaped the process group; killing the group
            # first reparents that child and makes it undiscoverable.
            stop_pid_tree(
                self._process.pid,
                grace_seconds=kill_grace_seconds,
                wait_for=self._process.wait,
            )
            self._unwedge_readers()
            break
        leaked = 0
        for thread in self._threads:
            thread.join(timeout=kill_grace_seconds)
            # A thread still alive here is parked in read() on a pipe some
            # escaped descendant still holds open. It cannot be killed, and it
            # will not be waited on again — so it is counted, and the count is
            # what lets a caller notice the leak instead of inferring it from
            # a process that mysteriously never frees its slot.
            if thread.is_alive():
                leaked += 1
        # Timeout/cancellation deliberately closes pipes and can leave a final
        # partial byte sequence; preserve their unknown-result semantics.  A
        # normally completed process, however, must emit valid UTF-8 rather
        # than having corrupt bytes silently replaced and persisted.
        if (
            self._drain_errors and not timed_out and not self.cancelled
            and not completed_output
        ):
            raise self._drain_errors[0]
        with self._lock:
            reason = self._termination_reason
        return ProcessResult(
            returncode=self._process.returncode,
            stdout=self.stdout.text,
            stderr=self.stderr.text,
            stdout_truncated=self.stdout.truncated,
            stderr_truncated=self.stderr.truncated,
            cancelled=self.cancelled,
            timed_out=timed_out,
            termination_reason=reason,
            leaked_drain_threads=leaked,
        )

    def _note_reason(self, reason: str) -> None:
        """Record why this process was stopped, first writer wins.

        A cancel arriving while the timeout kill is already underway must not
        rewrite history: the caller needs the reason that actually initiated
        the stop, not the last one to reach the lock.
        """

        with self._lock:
            if self._termination_reason is None:
                self._termination_reason = reason

    def terminate(self) -> bool:
        return terminate_pid_tree(self._process.pid)

    def kill(self) -> bool:
        """Force-kill the tree and unblock any reader wedged on a held pipe."""

        dispatched = kill_pid_tree(self._process.pid)
        try:
            self._process.kill()
        except OSError:
            pass
        self._unwedge_readers()
        return dispatched

    def _unwedge_readers(self) -> None:
        """Close our read ends so a drain thread cannot wait on a dead writer.

        An escaped child can keep the write end open, leaving the drain thread
        blocked on read forever. Closing our side is what ends that read.
        """

        for stream in (self._process.stdout, self._process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def cancel(
        self, *, grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
        reason: str = "cancelled",
    ) -> bool:
        """Cooperative stop: terminate, then force-kill if it does not exit."""

        with self._lock:
            self._cancelled = True
        self._note_reason(reason)
        # Through stop_pid_tree rather than terminate()-then-kill(): the
        # escapee snapshot has to be taken before the first signal, and a
        # kill() reached only after terminate() has already killed the parent
        # can no longer find what escaped.
        stop_pid_tree(
            self._process.pid, grace_seconds=grace_seconds,
            wait_for=self._process.wait,
        )
        self._unwedge_readers()
        return self._process.poll() is not None


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
    on_start: Callable[[ProcessHandle], None] | None = None,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> ProcessResult:
    """Spawn, stream and reap a child process in one call.

    ``on_start`` hands the live handle to the caller before the wait begins,
    which is the only way another thread can stop this child: without it a
    cancelled job would keep an Agent CLI running to completion and simply
    throw the answer away.

    ``kill_grace_seconds`` is how long a killed tree gets to actually die and
    its drain threads to unblock. Two seconds suits a quick command; an Agent
    CLI killed mid-write to a worktree deserves longer, which is why callers
    can raise it.
    """

    handle = ProcessHandle(
        argv, cwd=cwd, env=env, stdin_text=stdin_text,
        max_output_bytes=max_output_bytes, redactor=redactor,
        on_stdout=on_stdout, on_stderr=on_stderr,
    )
    if on_start is not None:
        on_start(handle)
    return handle.wait(timeout=timeout, kill_grace_seconds=kill_grace_seconds)
