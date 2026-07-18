"""Canonical JSON encoding for versioned workflow contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any

from .ids import EntityId
from .versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion


JSON_SCALARS = (str, int, float, bool, type(None))


def freeze_json(value: Any) -> Any:
    """Validate JSON data and return an immutable representation."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def to_primitive(value: Any) -> Any:
    """Convert a contract value to a canonical JSON-compatible structure."""

    if isinstance(value, EntityId):
        return str(value)
    if isinstance(value, (AggregateVersion, Revision, SchemaVersion, DefinitionHash)):
        return value.value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetimes must be timezone-aware")
        utc = value.astimezone(timezone.utc)
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = to_primitive(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_primitive(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def definition_hash(value: Any) -> DefinitionHash:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return DefinitionHash(f"sha256:{digest}")
