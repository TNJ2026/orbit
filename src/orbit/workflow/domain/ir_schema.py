"""WorkflowIR 1.1/1.2 Schema, validation, and lossless deserialization."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from .data import ArtifactVisibility, PortDataPolicy, PortTransport
from .definitions import IREdge, IRExtension, IRHandlerRef, IRNode, IRPolicy, IRPort, WorkflowIR
from .schemas import SchemaValidationError
from .serialization import freeze_json, to_primitive


IR_SCHEMA_ID = "orbit://workflow/ir/1.2"


def _array(ref: str) -> dict[str, Any]:
    return {"type": "array", "items": {"$ref": ref}}


_IR_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": IR_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ir_version", "workflow_id", "name", "description", "labels", "inputs",
        "outputs", "nodes", "edges", "entry", "terminals", "policies", "extensions", "indexes",
    ],
    "properties": {
        "ir_version": {"enum": ["1.1", "1.2"]},
        "workflow_id": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "labels": {"type": "object", "additionalProperties": {"type": "string"}},
        "inputs": _array("#/$defs/port"),
        "outputs": _array("#/$defs/port"),
        "nodes": _array("#/$defs/node"),
        "edges": _array("#/$defs/edge"),
        "entry": {"type": "array", "items": {"type": "string"}},
        "terminals": {"type": "array", "items": {"type": "string"}},
        "policies": _array("#/$defs/policy"),
        "extensions": _array("#/$defs/extension"),
        "indexes": {"type": "object"},
    },
    "$defs": {
        "port": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "schema_id", "required", "has_default", "default", "description", "data_policy"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "schema_id": {"type": "string", "minLength": 1},
                "required": {"type": "boolean"},
                "has_default": {"type": "boolean"},
                "default": {},
                "description": {"type": "string"},
                "data_policy": {
                    "type": "object", "additionalProperties": False,
                    "required": ["transport", "max_size_bytes", "content_types", "visibility"],
                    "properties": {
                        "transport": {"enum": ["inline", "artifact_ref", "secret_ref"]},
                        "max_size_bytes": {"type": "integer", "minimum": 0},
                        "content_types": {"type": "array", "items": {"type": "string"}},
                        "visibility": {
                            "type": ["string", "null"],
                            "enum": ["node", "run", "subflow", "workflow", None],
                        },
                    },
                },
            },
        },
        "handler": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "version", "manifest_fingerprint"],
            "properties": {
                "name": {"type": "string"}, "version": {"type": "string"},
                "manifest_fingerprint": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            },
        },
        "extension": {
            "type": "object", "additionalProperties": False,
            "required": ["extension_id", "extension_version", "config"],
            "properties": {
                "extension_id": {"type": "string"},
                "extension_version": {"type": "string"},
                "config": {"type": "object"},
            },
        },
        "node": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "kind", "inputs", "outputs", "handler", "config", "policies", "extension"],
            "properties": {
                "id": {"type": "string"},
                "kind": {"enum": ["action", "human", "decision", "join", "terminal", "extension"]},
                "inputs": _array("#/$defs/port"),
                "outputs": _array("#/$defs/port"),
                "handler": {"oneOf": [{"$ref": "#/$defs/handler"}, {"type": "null"}]},
                "config": {"type": "object"},
                "policies": {"type": "array", "items": {"type": "string"}},
                "extension": {"oneOf": [{"$ref": "#/$defs/extension"}, {"type": "null"}]},
                "route_mode": {"type": ["string", "null"], "enum": ["exclusive", "parallel", None]},
            },
        },
        "edge": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "source_node", "source_port", "target_node", "target_port", "route", "condition", "mapping"],
            "properties": {
                "id": {"type": "string"}, "source_node": {"type": "string"},
                "source_port": {"type": "string"}, "target_node": {"type": "string"},
                "target_port": {"type": "string"}, "route": {"enum": ["success", "error", "timeout", "cancel"]},
                "condition": {"type": "object"}, "mapping": {"type": "object"},
                "priority": {"type": "integer", "minimum": 0},
                "back_edge": {"type": "boolean"},
                "policy_ref": {"type": ["string", "null"]},
            },
        },
        "policy": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "kind", "config"],
            "properties": {"id": {"type": "string"}, "kind": {"type": "string"}, "config": {"type": "object"}},
        },
    },
}

WORKFLOW_IR_SCHEMA: Mapping[str, Any] = MappingProxyType(freeze_json(_IR_SCHEMA))
_VALIDATOR = Draft202012Validator(to_primitive(WORKFLOW_IR_SCHEMA))


def validate_workflow_ir(value: Any) -> None:
    errors = sorted(
        _VALIDATOR.iter_errors(to_primitive(value)),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        error = errors[0]
        path = tuple(error.absolute_path)
        if error.validator == "required" and "'" in error.message:
            path += (error.message.split("'")[1],)
        raise SchemaValidationError(path, error.message)


def _port(value: Mapping[str, Any]) -> IRPort:
    policy = value["data_policy"]
    return IRPort(
        value["id"], value["schema_id"], value["required"], value["has_default"],
        value["default"], value["description"],
        PortDataPolicy(
            PortTransport(policy["transport"]), policy["max_size_bytes"],
            tuple(policy["content_types"]),
            None if policy["visibility"] is None else ArtifactVisibility(policy["visibility"]),
        ),
    )


def _extension(value: Mapping[str, Any]) -> IRExtension:
    return IRExtension(value["extension_id"], value["extension_version"], value["config"])


def workflow_ir_from_primitive(value: Mapping[str, Any]) -> WorkflowIR:
    validate_workflow_ir(value)
    nodes = []
    for node in value["nodes"]:
        handler = node["handler"]
        extension = node["extension"]
        nodes.append(
            IRNode(
                node["id"], node["kind"], tuple(_port(item) for item in node["inputs"]),
                tuple(_port(item) for item in node["outputs"]),
                None if handler is None else IRHandlerRef(
                    handler["name"], handler["version"],
                    handler["manifest_fingerprint"],
                ),
                node["config"], tuple(node["policies"]),
                None if extension is None else _extension(extension),
                node.get("route_mode"),
            )
        )
    return WorkflowIR(
        value["ir_version"], value["workflow_id"], value["name"], value["description"],
        value["labels"], tuple(_port(item) for item in value["inputs"]),
        tuple(_port(item) for item in value["outputs"]), tuple(nodes),
        tuple(
            IREdge(
                item["id"], item["source_node"], item["source_port"], item["target_node"],
                item["target_port"], item["route"], item["condition"], item["mapping"],
                item.get("priority", 0), item.get("back_edge", False),
                item.get("policy_ref"),
            )
            for item in value["edges"]
        ),
        tuple(value["entry"]), tuple(value["terminals"]),
        tuple(IRPolicy(item["id"], item["kind"], item["config"]) for item in value["policies"]),
        tuple(_extension(item) for item in value["extensions"]), value["indexes"],
    )
