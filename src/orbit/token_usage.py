"""Recover token usage from local stores maintained by agent CLIs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


# Local-store timestamps can differ slightly from the runner's subprocess clock.
LOCAL_STORE_CORRELATION_BUFFER_MS = 10_000
AGY_COMMAND_RE = re.compile(r"(?:^|[\s/;&|(])agy(?:\s|$)")
HERMES_COMMAND_RE = re.compile(r"(?:^|[\s/;&|(])hermes(?:\s|$)")
OPENCODE_COMMAND_RE = re.compile(r"(?:^|[\s/;&|(])opencode(?:\s|$)")


def sqlite_ro_uri(db_path: Path) -> str:
    """Return a cross-platform read-only SQLite URI for ``db_path``."""
    return f"{db_path.as_uri()}?mode=ro"


def path_match_keys(exec_dir: str | Path) -> set[str]:
    """Return normalized forms used to correlate a stored cwd to a run."""
    raw = str(exec_dir)
    try:
        real = os.path.realpath(raw)
    except OSError:
        real = raw
    return {os.path.normcase(real), os.path.normcase(raw)}


def pb_read_varint(buf: bytes, pos: int) -> tuple[int, int | None]:
    result = shift = 0
    n = len(buf)
    while pos < n:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            break
    return 0, None


def pb_iter_fields(buf: bytes):
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = pb_read_varint(buf, pos)
        if pos is None:
            return
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, pos = pb_read_varint(buf, pos)
            if pos is None:
                return
            yield field, wire, val
        elif wire == 2:
            length, pos = pb_read_varint(buf, pos)
            if pos is None or pos + length > n:
                return
            yield field, wire, buf[pos:pos + length]
            pos += length
        elif wire == 1:
            if pos + 8 > n:
                return
            yield field, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == 5:
            if pos + 4 > n:
                return
            yield field, wire, buf[pos:pos + 4]
            pos += 4
        else:
            return


def pb_message_field(buf: bytes, field_num: int) -> bytes | None:
    for field, wire, val in pb_iter_fields(buf):
        if field == field_num and wire == 2:
            return val
    return None


def pb_varint_field(buf: bytes, field_num: int) -> int | None:
    for field, wire, val in pb_iter_fields(buf):
        if field == field_num and wire == 0:
            return val
    return None


def pb_string_field(buf: bytes, field_num: int) -> str | None:
    val = pb_message_field(buf, field_num)
    return val.decode("utf-8", "replace") if val is not None else None


def pb_timestamp_ms(ts: bytes) -> int | None:
    seconds = pb_varint_field(ts, 1)
    if seconds is None:
        return None
    nanos = pb_varint_field(ts, 2) or 0
    if not 0 <= nanos <= 999_999_999:
        return None
    return int(seconds) * 1000 + nanos // 1_000_000


def antigravity_gen_tokens(blob: bytes) -> tuple[str | None, int] | None:
    """Return ``(response_id, total_tokens)`` for one generation blob."""
    chat = pb_message_field(blob, 1)
    if chat is None:
        return None
    usage = pb_message_field(chat, 4)
    if usage is None:
        return None
    total = sum(max(0, pb_varint_field(usage, field) or 0) for field in (1, 2, 5, 9, 10))
    if total <= 0:
        return None
    response_id = pb_string_field(usage, 11)
    return (response_id or None), total


def antigravity_conversations_dir() -> Path:
    home = os.environ.get("GEMINI_CLI_HOME") or os.path.join(
        os.path.expanduser("~"), ".gemini"
    )
    return Path(home) / "antigravity-cli" / "conversations"


def antigravity_conversation_usage(
    db_path: Path, workspace_keys: set[str] | None
) -> tuple[int | None, list[tuple[str | None, int]], bool]:
    """Read creation time, generation usage, and workspace match from one DB."""
    try:
        conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True, timeout=1.0)
    except sqlite3.Error:
        return None, [], False
    try:
        created_ms = None
        row = conn.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
        if row and row[0]:
            timestamp = pb_message_field(row[0], 2)
            if timestamp is not None:
                created_ms = pb_timestamp_ms(timestamp)
        rows: list[tuple[str | None, int]] = []
        for (data,) in conn.execute("SELECT data FROM gen_metadata ORDER BY idx"):
            if not data:
                continue
            parsed = antigravity_gen_tokens(data)
            if parsed is not None:
                rows.append(parsed)
        mentions = True
        if workspace_keys is not None:
            mentions = False
            for step_payload, metadata in conn.execute(
                "SELECT step_payload, metadata FROM steps"
            ):
                for blob in (step_payload, metadata):
                    if not blob:
                        continue
                    haystack = os.path.normcase(blob.decode("utf-8", "replace"))
                    if any(key in haystack for key in workspace_keys):
                        mentions = True
                        break
                if mentions:
                    break
        return created_ms, rows, mentions
    except sqlite3.Error:
        return None, [], False
    finally:
        conn.close()


def antigravity_run_tokens(
    exec_dir: str | Path, start_ms: int, end_ms: int
) -> int | None:
    """Sum usage for matching Antigravity conversations in the run window."""
    conversations = antigravity_conversations_dir()
    if not conversations.is_dir():
        return None
    workspace_keys = path_match_keys(exec_dir)
    lo = start_ms - LOCAL_STORE_CORRELATION_BUFFER_MS
    hi = end_ms + LOCAL_STORE_CORRELATION_BUFFER_MS
    total, seen, matched = 0, set(), False
    for db_path in conversations.glob("*.db"):
        try:
            mtime_ms = int(db_path.stat().st_mtime * 1000)
        except OSError:
            continue
        if mtime_ms < lo:
            continue
        try:
            created_ms, rows, mentions = antigravity_conversation_usage(
                db_path, workspace_keys
            )
        except Exception:
            continue
        if not mentions or not rows:
            continue
        stamp = created_ms if created_ms is not None else mtime_ms
        if not lo <= stamp <= hi:
            continue
        matched = True
        for response_id, tokens in rows:
            if response_id is not None:
                if response_id in seen:
                    continue
                seen.add(response_id)
            total += tokens
    return total if matched else None


HERMES_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def hermes_state_db() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(
        os.path.expanduser("~"), ".hermes"
    )
    return Path(home) / "state.db"


def hermes_run_tokens(exec_dir: str | Path, start_ms: int, end_ms: int) -> int | None:
    """Sum usage for matching Hermes sessions in the run window."""
    db_path = hermes_state_db()
    if not db_path.is_file():
        return None
    keys = path_match_keys(exec_dir)
    lo = (start_ms - LOCAL_STORE_CORRELATION_BUFFER_MS) / 1000.0
    hi = (end_ms + LOCAL_STORE_CORRELATION_BUFFER_MS) / 1000.0
    columns = ", ".join(f"COALESCE({column}, 0)" for column in HERMES_TOKEN_COLUMNS)
    try:
        conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        total = 0
        for row in conn.execute(
            f"SELECT started_at, cwd, {columns} FROM sessions "
            "WHERE cwd IS NOT NULL AND cwd != ''"
        ):
            started_at, cwd = row[0], row[1]
            if not isinstance(started_at, (int, float)):
                continue
            seconds = started_at / 1000.0 if started_at > 1e12 else started_at
            if not lo <= seconds <= hi:
                continue
            if os.path.normcase(str(cwd)) not in keys:
                continue
            total += sum(max(0, int(value or 0)) for value in row[2:])
        return total if total > 0 else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def opencode_db() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(data_home) / "opencode" / "opencode.db"


def opencode_message_tokens(tokens: Any) -> int:
    if not isinstance(tokens, dict):
        return 0
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    fields = (
        tokens.get("input"),
        tokens.get("output"),
        tokens.get("reasoning"),
        cache.get("read"),
        cache.get("write"),
    )
    return sum(max(0, int(value)) for value in fields if isinstance(value, (int, float)))


def opencode_run_tokens(exec_dir: str | Path, start_ms: int, end_ms: int) -> int | None:
    """Sum usage for matching OpenCode assistant messages in the run window."""
    db_path = opencode_db()
    if not db_path.is_file():
        return None
    keys = path_match_keys(exec_dir)
    lo = start_ms - LOCAL_STORE_CORRELATION_BUFFER_MS
    hi = end_ms + LOCAL_STORE_CORRELATION_BUFFER_MS
    try:
        conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        total, seen = 0, set()
        for message_id, data_json in conn.execute(
            "SELECT id, data FROM message WHERE time_created BETWEEN ? AND ?",
            (lo, hi),
        ):
            try:
                data = json.loads(data_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            path = data.get("path") if isinstance(data.get("path"), dict) else {}
            if not any(
                isinstance(candidate, str) and os.path.normcase(candidate) in keys
                for candidate in (path.get("cwd"), path.get("root"))
            ):
                continue
            created = (data.get("time") or {}).get("created")
            if not isinstance(created, (int, float)) or not lo <= created <= hi:
                continue
            key = data.get("id") or message_id
            if key in seen:
                continue
            seen.add(key)
            total += opencode_message_tokens(data.get("tokens"))
        return total if total > 0 else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# Readers are selected by command, then correlate by workspace and wall clock.
LOCAL_STORE_TOKEN_READERS = (
    (AGY_COMMAND_RE, antigravity_run_tokens),
    (HERMES_COMMAND_RE, hermes_run_tokens),
    (OPENCODE_COMMAND_RE, opencode_run_tokens),
)
