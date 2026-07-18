"""Stable identifier contracts used by workflow domain objects."""

from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import uuid4


_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, order=True)
class EntityId:
    """A namespaced, JSON-friendly entity identifier."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if not _KIND_RE.fullmatch(self.kind):
            raise ValueError(f"invalid identifier kind: {self.kind!r}")
        if not _VALUE_RE.fullmatch(self.value):
            raise ValueError(f"invalid identifier value: {self.value!r}")

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"

    @classmethod
    def parse(cls, raw: str) -> EntityId:
        kind, separator, value = raw.partition(":")
        if not separator:
            raise ValueError(f"identifier has no kind prefix: {raw!r}")
        return cls(kind=kind, value=value)


def new_id(kind: str) -> EntityId:
    """Create a locally unique identifier with a stable kind prefix."""

    return EntityId(kind=kind, value=uuid4().hex)
