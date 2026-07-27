"""JSON Schema 2020-12 definition of Workflow DSL Core 1.2."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from ..domain.serialization import freeze_json


DSL_SCHEMA_ID = "orbit://workflow/dsl/1.3"
# Public because a generator that asks a model for an id has to state the
# grammar it will be validated against; two copies of it would drift.
ID_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$"


def _array_of(ref: str) -> dict[str, Any]:
    return {"type": "array", "items": {"$ref": ref}, "default": []}


_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": DSL_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": ["dsl_version", "metadata", "nodes", "edges", "entry", "terminals"],
    "properties": {
        "dsl_version": {"enum": ["1.0", "1.2", "1.3"]},
        "metadata": {"$ref": "#/$defs/metadata"},
        "inputs": _array_of("#/$defs/port"),
        "outputs": _array_of("#/$defs/port"),
        "nodes": _array_of("#/$defs/node"),
        "edges": _array_of("#/$defs/edge"),
        "entry": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/id"}},
        "terminals": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/id"},
        },
        "result": {"$ref": "#/$defs/endpoint"},
        "policies": _array_of("#/$defs/policy"),
        "extensions": _array_of("#/$defs/extension"),
    },
    "$defs": {
        "id": {"type": "string", "pattern": ID_PATTERN},
        "metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "name"],
            "properties": {
                "id": {"$ref": "#/$defs/id"},
                "slug": {"$ref": "#/$defs/id"},
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string", "default": ""},
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                },
            },
        },
        "port": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "schema_id"],
            "properties": {
                "id": {"$ref": "#/$defs/id"},
                "schema_id": {"type": "string", "minLength": 1},
                "required": {"type": "boolean", "default": True},
                "default": {},
                "description": {"type": "string", "default": ""},
                "transport": {
                    "enum": ["inline", "artifact_ref", "secret_ref"],
                    "default": "inline",
                },
                "max_size_bytes": {"type": "integer", "minimum": 0},
                "content_types": {
                    "type": "array", "items": {"type": "string"},
                    "default": [],
                },
                "visibility": {
                    "enum": ["node", "run", "subflow", "workflow"],
                },
            },
        },
        "handler": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "version"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "version": {"type": "string", "minLength": 1},
            },
        },
        "node": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "kind"],
            "properties": {
                "id": {"$ref": "#/$defs/id"},
                "kind": {"enum": ["action", "human", "agentic", "foreach", "subflow", "decision", "join", "terminal", "extension"]},
                "inputs": _array_of("#/$defs/port"),
                "outputs": _array_of("#/$defs/port"),
                "handler": {"$ref": "#/$defs/handler"},
                # What a reader calls this step. It sits beside `config`, not
                # inside it: `config` belongs to the Handler, and a Handler
                # with a closed config schema rejects a field meant for people.
                # `\S` rejects a blank label here, so an author sees a
                # diagnostic rather than a bare domain error later.
                "label": {
                    "type": "string", "minLength": 1, "maxLength": 80,
                    "pattern": r"\S",
                },
                "config": {"type": "object", "default": {}},
                "policies": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/id"},
                    "default": [],
                },
                "extension": {"$ref": "#/$defs/extension"},
                "route_mode": {"enum": ["exclusive", "parallel"]},
            },
        },
        "endpoint": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node", "port"],
            "properties": {
                "node": {"$ref": "#/$defs/id"},
                "port": {"$ref": "#/$defs/id"},
            },
        },
        "edge": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "from", "to"],
            "properties": {
                "id": {"$ref": "#/$defs/id"},
                "from": {"$ref": "#/$defs/endpoint"},
                "to": {"$ref": "#/$defs/endpoint"},
                "condition": {"type": ["string", "object", "boolean"]},
                "mapping": {"type": "object"},
                "route": {"enum": ["success", "error", "timeout", "cancel"], "default": "success"},
                "priority": {"type": "integer", "minimum": 0, "default": 0},
                "back_edge": {"type": "boolean", "default": False},
                "policy": {"$ref": "#/$defs/id"},
            },
        },
        "policy": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "kind", "config"],
            "properties": {
                "id": {"$ref": "#/$defs/id"},
                "kind": {"type": "string", "minLength": 1},
                "config": {"type": "object"},
            },
        },
        "extension": {
            "type": "object",
            "additionalProperties": False,
            "required": ["extension_id", "extension_version", "config"],
            "properties": {
                "extension_id": {"type": "string", "minLength": 1},
                "extension_version": {"type": "string", "minLength": 1},
                "config": {"type": "object"},
            },
        },
    },
}

WORKFLOW_DSL_SCHEMA: Mapping[str, Any] = MappingProxyType(freeze_json(_SCHEMA))
