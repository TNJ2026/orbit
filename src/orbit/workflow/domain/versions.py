"""Version and content-hash value objects."""

from __future__ import annotations

from dataclasses import dataclass
import re


_SCHEMA_VERSION_RE = re.compile(r"^[1-9][0-9]*\.[0-9]+$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class Revision:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise ValueError("revision must be a positive integer")

    def next(self) -> Revision:
        return Revision(self.value + 1)


@dataclass(frozen=True, order=True)
class AggregateVersion:
    """Optimistic-lock version; zero means the aggregate does not exist yet."""

    value: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("aggregate version must be a non-negative integer")

    def next(self) -> AggregateVersion:
        return AggregateVersion(self.value + 1)


@dataclass(frozen=True, order=True)
class SchemaVersion:
    value: str = "1.0"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _SCHEMA_VERSION_RE.fullmatch(self.value):
            raise ValueError(f"invalid schema version: {self.value!r}")


@dataclass(frozen=True, order=True)
class DefinitionHash:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _HASH_RE.fullmatch(self.value):
            raise ValueError(f"invalid definition hash: {self.value!r}")
