"""Deterministic Workflow DSL to Canonical WorkflowIR compiler."""

from __future__ import annotations

import hashlib
from typing import Any

from ..catalogs.handlers import HandlerCatalog
from ..catalogs.extensions import InMemoryExtensionRegistry
from ..catalogs.schemas import InMemorySchemaCatalog
from ..domain.definitions import (
    CompiledWorkflow,
    IREdge,
    IRExtension,
    IRHandlerRef,
    IRNode,
    IRPolicy,
    IRPort,
    WorkflowIR,
)
from ..domain.data import ArtifactVisibility, PortDataPolicy, PortTransport
from ..domain.serialization import canonical_json, definition_hash, to_primitive
from .diagnostics import Diagnostic, DiagnosticError
from .expressions import compile_condition, expression_references
from .mapping import compile_mapping, mapping_references
from .parser import ParsedDslDocument, parse_dsl
from .semantic import analyze_dsl
from .validator import validate_dsl_structure


COMPILER_VERSION = "1.2"


def _port(value: dict[str, Any]) -> IRPort:
    transport = PortTransport(value.get("transport", "inline"))
    return IRPort(
        id=value["id"],
        schema_id=value["schema_id"],
        required=value.get("required", True),
        has_default="default" in value,
        default=value.get("default"),
        description=value.get("description", ""),
        data_policy=PortDataPolicy(
            transport,
            value.get("max_size_bytes"),
            tuple(value.get("content_types", ())),
            None if "visibility" not in value else ArtifactVisibility(value["visibility"]),
        ),
    )


def _extension(value: dict[str, Any]) -> IRExtension:
    return IRExtension(value["extension_id"], value["extension_version"], value["config"])


def compile_document(
    document: ParsedDslDocument,
    handlers: HandlerCatalog,
    schemas: InMemorySchemaCatalog,
    extensions: InMemoryExtensionRegistry | None = None,
) -> CompiledWorkflow:
    extensions = extensions or InMemoryExtensionRegistry()
    validate_dsl_structure(document)
    analysis = analyze_dsl(document, handlers, schemas, extensions)
    data = to_primitive(document.data)
    node_data = {item["id"]: item for item in data["nodes"]}

    nodes: list[IRNode] = []
    for value in sorted(data["nodes"], key=lambda item: item["id"]):
        manifest = analysis.handlers.get(value["id"])
        nodes.append(
            IRNode(
                id=value["id"],
                kind=value["kind"],
                inputs=tuple(_port(item) for item in sorted(value.get("inputs", []), key=lambda item: item["id"])),
                outputs=tuple(_port(item) for item in sorted(value.get("outputs", []), key=lambda item: item["id"])),
                handler=None if manifest is None else IRHandlerRef(
                    manifest.name, manifest.version, manifest.fingerprint
                ),
                config=value.get("config", {}),
                policies=tuple(sorted(value.get("policies", []))),
                extension=_extension(value["extension"]) if "extension" in value else None,
                route_mode=value.get("route_mode"),
            )
        )

    edges: list[IREdge] = []
    for index, value in sorted(enumerate(data["edges"]), key=lambda item: item[1]["id"]):
        source = node_data[value["from"]["node"]]
        source_port = next(item for item in source.get("outputs", []) if item["id"] == value["from"]["port"])
        condition = compile_condition(value.get("condition"), ("edges", index, "condition"))
        mapping = compile_mapping(value.get("mapping"), source_port["schema_id"], ("edges", index, "mapping"))
        allowed_references = {
            f"source.{source_port['id']}",
            *(f"workflow.inputs.{item['id']}" for item in data.get("inputs", [])),
        }
        invalid = [
            reference
            for reference in expression_references(condition) + mapping_references(mapping)
            if not any(reference == allowed or reference.startswith(allowed + ".") for allowed in allowed_references)
        ]
        if invalid:
            raise DiagnosticError(
                [
                    Diagnostic(
                        "DSL_REFERENCE_NOT_FOUND",
                        f"reference {reference!r} is outside this edge scope",
                        "compile",
                        ("edges", index),
                        hint=(
                            f"source references on this edge must start with "
                            f"'source.{source_port['id']}'"
                        ),
                    )
                    for reference in sorted(set(invalid))
                ]
            )
        edges.append(
            IREdge(
                id=value["id"],
                source_node=value["from"]["node"],
                source_port=value["from"]["port"],
                target_node=value["to"]["node"],
                target_port=value["to"]["port"],
                route=value.get("route", "success"),
                condition=condition,
                mapping=mapping,
                priority=value.get("priority", 0),
                back_edge=value.get("back_edge", False),
                policy_ref=value.get("policy"),
            )
        )

    outgoing_edges = {
        node.id: tuple(edge.id for edge in edges if edge.source_node == node.id)
        for node in nodes
    }
    incoming_edges = {
        node.id: tuple(edge.id for edge in edges if edge.target_node == node.id)
        for node in nodes
    }
    indexes = {
        "node_ordinals": {node.id: index for index, node in enumerate(nodes)},
        "outgoing_edges": outgoing_edges,
        "incoming_edges": incoming_edges,
        "input_ports": {node.id: [port.id for port in node.inputs] for node in nodes},
        "output_ports": {node.id: [port.id for port in node.outputs] for node in nodes},
    }
    metadata = data["metadata"]
    ir = WorkflowIR(
        ir_version="1.2" if data["dsl_version"] == "1.2" else "1.1",
        workflow_id=f"workflow:{metadata['id']}",
        name=metadata["name"],
        description=metadata.get("description", ""),
        labels=dict(sorted(metadata.get("labels", {}).items())),
        inputs=tuple(_port(item) for item in sorted(data.get("inputs", []), key=lambda item: item["id"])),
        outputs=tuple(_port(item) for item in sorted(data.get("outputs", []), key=lambda item: item["id"])),
        nodes=tuple(nodes),
        edges=tuple(edges),
        entry=tuple(sorted(data["entry"])),
        terminals=tuple(sorted(data["terminals"])),
        policies=tuple(
            IRPolicy(item["id"], item["kind"], item["config"])
            for item in sorted(data.get("policies", []), key=lambda item: item["id"])
        ),
        extensions=tuple(
            _extension(item)
            for item in sorted(
                data.get("extensions", []),
                key=lambda item: (item["extension_id"], item["extension_version"]),
            )
        ),
        indexes=indexes,
    )
    catalog_fingerprint = "sha256:" + hashlib.sha256(
        canonical_json(
            {
                "extensions": extensions.fingerprint,
                "handlers": handlers.fingerprint,
                "schemas": schemas.fingerprint,
            }
        ).encode()
    ).hexdigest()
    return CompiledWorkflow(ir, definition_hash(ir), COMPILER_VERSION, catalog_fingerprint)


def compile_source(
    text: str,
    handlers: HandlerCatalog,
    schemas: InMemorySchemaCatalog,
    *,
    source_name: str = "<memory>",
    source_format: str | None = None,
    extensions: InMemoryExtensionRegistry | None = None,
) -> CompiledWorkflow:
    return compile_document(
        parse_dsl(text, source_name=source_name, source_format=source_format),
        handlers,
        schemas,
        extensions,
    )


def canonical_ir_json(compiled: CompiledWorkflow) -> str:
    return canonical_json(compiled.ir)
