"""Cross-platform process-group spawning and termination primitives."""

from __future__ import annotations

import os
import signal
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


IS_WINDOWS = os.name == "nt"


def detached_process_kwargs() -> dict[str, Any]:
    """Popen kwargs that isolate a child in its own process group."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def taskkill_tree(pid: int, force: bool) -> bool:
    """Ask Windows taskkill to terminate a process and all descendants."""
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
    """Best-effort graceful termination of a detached process tree."""
    if not pid:
        return False
    if IS_WINDOWS:
        return taskkill_tree(pid, force=True)
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def snapshot_ppids_windows() -> dict[int, int] | None:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):
        return None

    class ProcessEntry32(ctypes.Structure):
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
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snap or snap == invalid_handle:
        return None
    mapping: dict[int, int] = {}
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            mapping[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return mapping


def snapshot_ppids_ps() -> dict[int, int] | None:
    if IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
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
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        mapping[pid] = ppid
    return mapping


def snapshot_ppids_libproc() -> dict[int, int] | None:
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util
    except ImportError:
        return None
    library_path = ctypes.util.find_library("proc")
    if not library_path:
        return None
    libproc = ctypes.CDLL(library_path, use_errno=True)
    libproc.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    item_size = ctypes.sizeof(ctypes.c_int)
    capacity = 4096
    while True:
        buffer_type = ctypes.c_int * (capacity // item_size)
        buffer = buffer_type()
        bytes_used = libproc.proc_listpids(
            ctypes.c_uint32(1),
            ctypes.c_uint32(0),
            buffer,
            ctypes.sizeof(buffer),
        )
        if bytes_used <= 0:
            return None if ctypes.get_errno() else {}
        if bytes_used < ctypes.sizeof(buffer):
            pids = buffer[: bytes_used // item_size]
            break
        capacity *= 2
    info_buffer = (ctypes.c_ubyte * 1024)()
    mapping: dict[int, int] = {}
    for pid in pids:
        if pid <= 0:
            continue
        written = libproc.proc_pidinfo(
            pid,
            ctypes.c_int(3),
            ctypes.c_uint64(0),
            info_buffer,
            ctypes.sizeof(info_buffer),
        )
        if written < 20:
            continue
        fields = struct.unpack_from("=5I", bytes(info_buffer[:written]))
        real_pid, ppid = int(fields[3]), int(fields[4])
        if real_pid:
            mapping[real_pid] = ppid
    return mapping


def snapshot_ppids_procfs() -> dict[int, int] | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        entries = list(Path("/proc").iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return None
    mapping: dict[int, int] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid_value: int | None = None
            ppid_value: int | None = None
            with (entry / "status").open(
                "r", encoding="utf-8", errors="replace"
            ) as handle:
                for line in handle:
                    if line.startswith("Pid:"):
                        try:
                            pid_value = int(line.split()[1])
                        except (IndexError, ValueError):
                            pid_value = None
                    elif line.startswith("PPid:"):
                        try:
                            ppid_value = int(line.split()[1])
                        except (IndexError, ValueError):
                            ppid_value = None
                        if pid_value is not None and ppid_value is not None:
                            mapping[pid_value] = ppid_value
                        break
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return mapping


def snapshot_ppids() -> dict[int, int]:
    for getter in (
        snapshot_ppids_windows,
        snapshot_ppids_ps,
        snapshot_ppids_libproc,
        snapshot_ppids_procfs,
    ):
        mapping = getter()
        if mapping:
            return mapping
    return {}


def descendant_pids(root_pid: int) -> list[int]:
    mapping = snapshot_ppids()
    if not mapping:
        return []
    children: dict[int, list[int]] = {}
    for pid, ppid in mapping.items():
        children.setdefault(ppid, []).append(pid)
    seen: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid == root_pid or pid in seen:
            continue
        seen.append(pid)
        stack.extend(children.get(pid, []))
    return seen


def kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Force-kill a detached runner and descendants that escaped its group."""
    if not proc.pid:
        return
    if IS_WINDOWS:
        taskkill_tree(proc.pid, force=True)
        try:
            proc.kill()
        except OSError:
            pass
        return
    descendants = descendant_pids(proc.pid)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
