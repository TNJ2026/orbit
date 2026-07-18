"""Fail-closed scanning that prevents resolved Secret values entering data stores."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Iterable


class SecretLeakDetected(ValueError):
    code = "artifact_access_denied"

    def __init__(self, path: tuple[str | int, ...]) -> None:
        self.path = path
        self.json_path = "$" + "".join(
            f"[{item}]" if isinstance(item, int) else f".{item}" for item in path
        )
        super().__init__(f"resolved Secret value detected at {self.json_path}")


def assert_no_secret_values(value: Any, secret_values: Iterable[str]) -> None:
    """Reject exact or embedded non-empty Secret strings without exposing them."""

    secrets = tuple(item for item in secret_values if item)
    if not secrets:
        return
    stack: list[tuple[tuple[str | int, ...], Any]] = [((), value)]
    while stack:
        path, current = stack.pop()
        if isinstance(current, str):
            if any(secret in current for secret in secrets):
                raise SecretLeakDetected(path)
        elif isinstance(current, (bytes, bytearray, memoryview)):
            raw = bytes(current)
            if any(secret.encode("utf-8") in raw for secret in secrets):
                raise SecretLeakDetected(path)
        elif isinstance(current, Mapping):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise TypeError("data object keys must be strings")
                stack.append((path + (key,), item))
        elif isinstance(current, Sequence):
            for index, item in enumerate(current):
                stack.append((path + (index,), item))
