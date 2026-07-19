"""Read-only versioned port Schema Catalog."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from ..domain.serialization import canonical_json, freeze_json


class InMemorySchemaCatalog:
    def __init__(self, schemas: Mapping[str, Mapping[str, Any]]) -> None:
        self._schemas = {key: freeze_json(value) for key, value in sorted(schemas.items())}
        self._fingerprint = "sha256:" + hashlib.sha256(
            canonical_json(self._schemas).encode()
        ).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def get(self, schema_id: str) -> Mapping[str, Any] | None:
        return self._schemas.get(schema_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._schemas)

    def compatible(self, source_schema_id: str, target_schema_id: str) -> bool:
        return source_schema_id == target_schema_id and source_schema_id in self._schemas
