"""Safe default capability adapters for HandlerContext."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
from collections.abc import Sequence


class SecretAccessError(PermissionError):
    pass


class SecretValue:
    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("secret value cannot be empty")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue(**redacted**)"

    __str__ = __repr__


class ScopedSecretResolver:
    def __init__(self, allowed: tuple[str, ...], values: Mapping[str, str]) -> None:
        self._allowed = frozenset(allowed)
        self._values = MappingProxyType(dict(values))

    def resolve(self, name: str) -> SecretValue:
        if name not in self._allowed:
            raise SecretAccessError(f"secret was not declared by Handler Manifest: {name}")
        value = self._values.get(name)
        if value is None:
            raise SecretAccessError(f"declared secret is unavailable: {name}")
        return SecretValue(value)

    def redact(self, text: str) -> str:
        result = text
        for value in self._values.values():
            if value:
                result = result.replace(value, "**redacted**")
        return result

    def redact_data(self, value):
        """Deep-redact JSON error details without mutating the source value."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {key: self.redact_data(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact_data(item) for item in value]
        return value


class RejectingArtifactWriter:
    def write(self, *, name: str, content: bytes, content_type: str):
        raise RuntimeError("ARTIFACT_NOT_AVAILABLE: Artifact persistence starts in Step 7")
    def read(self, artifact_id, *, max_size_bytes=None):
        raise RuntimeError("ARTIFACT_NOT_AVAILABLE: no Artifact was authorized")
    def open(self, artifact_id):
        raise RuntimeError("ARTIFACT_NOT_AVAILABLE: no Artifact was authorized")
    def open_writer(self, *, name, content_type):
        raise RuntimeError("ARTIFACT_NOT_AVAILABLE: Artifact persistence is not configured")


class NullTracer:
    def record(self, name, fields) -> None:
        return None


class DiscardedAttemptOutput:
    """Drops what a Handler prints.

    Output is a convenience for the operator, never a result: a deployment
    without an output store still runs Handlers, it just cannot show their
    console. Failing here would turn a missing convenience into a failed run.
    """

    def emit(self, stream: str, text: str) -> None:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
