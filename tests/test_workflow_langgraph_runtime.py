from __future__ import annotations

import json
import unittest

from orbit.workflow.catalogs import (
    HandlerManifest,
    InMemoryHandlerCatalog,
    InMemorySchemaCatalog,
)
from orbit.workflow.domain.definitions import (
    IREdge,
    IRHandlerRef,
    IRNode,
    IRPort,
    IRResult,
    WorkflowIR,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.langgraph_runtime import (
    BoundHandler,
    HandlerBindingError,
    LangGraphHandlerRegistry,
    compile_generated_workflow,
    compile_workflow,
)


FINGERPRINT = "sha256:" + "a" * 64
SCHEMA = "example://value/1.0"
TRUE = {"op": "literal", "value": True}
IDENTITY = {"op": "identity"}


def port(name: str) -> IRPort:
    return IRPort(name, SCHEMA, True, False, None, "")


def node(
    node_id: str,
    *,
    inputs=(),
    outputs=(),
    kind="action",
    route_mode=None,
    handler=True,
) -> IRNode:
    reference = (
        IRHandlerRef(node_id, "1.0.0", FINGERPRINT) if handler else None
    )
    return IRNode(
        node_id,
        kind,
        tuple(port(item) for item in inputs),
        tuple(port(item) for item in outputs),
        reference,
        {},
        (),
        None,
        route_mode,
    )


def edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    source_port="value",
    target_port="value",
    condition=TRUE,
    mapping=IDENTITY,
    priority=0,
    back_edge=False,
) -> IREdge:
    return IREdge(
        edge_id,
        source,
        source_port,
        target,
        target_port,
        "success",
        condition,
        mapping,
        priority,
        back_edge,
    )


def workflow(nodes, edges, *, entry, terminals, result) -> WorkflowIR:
    return WorkflowIR(
        "1.3",
        "workflow:generated",
        "Agent generated",
        "",
        {},
        (port("value"),),
        (),
        tuple(nodes),
        tuple(edges),
        tuple(entry),
        tuple(terminals),
        (),
        (),
        {},
        IRResult(*result),
    )


def binding(name, invoke) -> BoundHandler:
    return BoundHandler(name, "1.0.0", FINGERPRINT, invoke)


class LangGraphWorkflowCompilerTests(unittest.TestCase):
    def test_agent_dsl_is_validated_before_langgraph_compilation(self) -> None:
        schema_catalog = InMemorySchemaCatalog(
            {SCHEMA: {"type": "integer"}}
        )
        manifest = HandlerManifest(
            name="increment",
            version="1.0.0",
            node_kinds=("action",),
            inputs={"value": SCHEMA},
            outputs={"value": SCHEMA},
            config_schema={"type": "object", "additionalProperties": False},
            execution_safety=ExecutionSafety.REPLAY_SAFE,
            resource_profile=ResourceProfile(0, 0, 0, 30, 0, "free"),
            result_schema_id=SCHEMA,
        )
        source = json.dumps({
            "dsl_version": "1.3",
            "metadata": {"id": "generated", "name": "Generated"},
            "inputs": [{"id": "value", "schema_id": SCHEMA}],
            "nodes": [
                {
                    "id": "increment",
                    "kind": "action",
                    "inputs": [{"id": "value", "schema_id": SCHEMA}],
                    "outputs": [{"id": "value", "schema_id": SCHEMA}],
                    "handler": {"name": "increment", "version": "1.0.0"},
                },
                {
                    "id": "done",
                    "kind": "terminal",
                    "inputs": [{"id": "value", "schema_id": SCHEMA}],
                },
            ],
            "edges": [{
                "id": "increment_done",
                "from": {"node": "increment", "port": "value"},
                "to": {"node": "done", "port": "value"},
            }],
            "entry": ["increment"],
            "terminals": ["done"],
            "result": {"node": "increment", "port": "value"},
        })
        runtime_registry = LangGraphHandlerRegistry([
            BoundHandler(
                "increment",
                "1.0.0",
                manifest.fingerprint,
                lambda values, config, context: {"value": values["value"] + 1},
            )
        ])

        compiled = compile_generated_workflow(
            source,
            InMemoryHandlerCatalog([manifest]),
            schema_catalog,
            runtime_registry,
        )

        self.assertEqual(42, compiled.invoke({"value": 41})["result"])

    def test_compiles_exact_handlers_and_routes_conditionally(self) -> None:
        decide = node(
            "decide",
            inputs=("value",),
            outputs=("approved",),
            route_mode="exclusive",
        )
        accepted = node(
            "accepted", inputs=("approved",), kind="terminal", handler=False
        )
        rejected = node(
            "rejected", inputs=("approved",), kind="terminal", handler=False
        )
        ir = workflow(
            (decide, accepted, rejected),
            (
                edge(
                    "accept",
                    "decide",
                    "accepted",
                    source_port="approved",
                    target_port="approved",
                    condition={
                        "op": "eq",
                        "left": {"op": "ref", "path": "source.approved"},
                        "right": {"op": "literal", "value": True},
                    },
                ),
                edge(
                    "reject",
                    "decide",
                    "rejected",
                    source_port="approved",
                    target_port="approved",
                    condition=TRUE,
                    priority=10,
                ),
            ),
            entry=("decide",),
            terminals=("accepted", "rejected"),
            result=("decide", "approved"),
        )
        registry = LangGraphHandlerRegistry(
            [binding("decide", lambda values, config, context: {
                "approved": values["value"] == "allow"
            })]
        )

        compiled = compile_workflow(ir, registry)
        allowed = compiled.invoke({"value": "allow"})
        denied = compiled.invoke({"value": "deny"})

        self.assertIs(True, allowed["result"])
        self.assertEqual(["decide", "accepted"], allowed["execution_order"])
        self.assertIs(False, denied["result"])
        self.assertEqual(["decide", "rejected"], denied["execution_order"])

    def test_parallel_fanout_merges_before_join(self) -> None:
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel"
        )
        left = node("left", inputs=("value",), outputs=("value",))
        right = node("right", inputs=("value",), outputs=("value",))
        join = node(
            "join", inputs=("left", "right"), kind="terminal", handler=False
        )
        ir = workflow(
            (fan, left, right, join),
            (
                edge("fan_left", "fan", "left"),
                edge("fan_right", "fan", "right"),
                edge(
                    "left_join",
                    "left",
                    "join",
                    target_port="left",
                    mapping={
                        "op": "object",
                        "schema_id": SCHEMA,
                        "fields": {"left": {"op": "ref", "path": "source.value"}},
                    },
                ),
                edge(
                    "right_join",
                    "right",
                    "join",
                    target_port="right",
                    mapping={
                        "op": "object",
                        "schema_id": SCHEMA,
                        "fields": {"right": {"op": "ref", "path": "source.value"}},
                    },
                ),
            ),
            entry=("fan",),
            terminals=("join",),
            result=("left", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [
                binding("fan", lambda values, config, context: dict(values)),
                binding("left", lambda values, config, context: {
                    "value": "L:" + values["value"]
                }),
                binding("right", lambda values, config, context: {
                    "value": "R:" + values["value"]
                }),
            ]
        )

        result = compile_workflow(ir, registry).invoke({"value": "x"})

        self.assertEqual("L:x", result["result"])
        self.assertEqual("L:x", result["node_outputs"]["left"]["value"])
        self.assertEqual("R:x", result["node_outputs"]["right"]["value"])
        self.assertEqual("join", result["execution_order"][-1])

    def test_rejects_manifest_fingerprint_mismatch_before_execution(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",),
            terminals=("terminal",),
            result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [
                BoundHandler(
                    "action",
                    "1.0.0",
                    "sha256:" + "b" * 64,
                    lambda values, config, context: values,
                )
            ]
        )

        with self.assertRaisesRegex(HandlerBindingError, "manifest mismatch"):
            compile_workflow(ir, registry)

    def test_back_edge_loop_replaces_previous_node_output(self) -> None:
        count = node(
            "count",
            inputs=("value",),
            outputs=("value",),
            route_mode="exclusive",
        )
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        less_than_three = {
            "op": "lt",
            "left": {"op": "ref", "path": "source.value"},
            "right": {"op": "literal", "value": 3},
        }
        ir = workflow(
            (count, terminal),
            (
                edge(
                    "again",
                    "count",
                    "count",
                    condition=less_than_three,
                    back_edge=True,
                ),
                edge("done", "count", "terminal", priority=10),
            ),
            entry=("count",),
            terminals=("terminal",),
            result=("count", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [binding("count", lambda values, config, context: {
                "value": values["value"] + 1
            })]
        )

        result = compile_workflow(ir, registry).invoke({"value": 0})

        self.assertEqual(3, result["result"])
        self.assertEqual(
            ["count", "count", "count", "terminal"],
            result["execution_order"],
        )


if __name__ == "__main__":
    unittest.main()
