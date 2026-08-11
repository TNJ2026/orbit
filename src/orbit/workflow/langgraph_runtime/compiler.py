"""Restricted WorkflowIR-to-LangGraph compiler.

The authoring Agent never supplies Python.  It produces Orbit's declarative
DSL, the existing compiler turns that into a validated ``WorkflowIR``, and
this module binds the IR to an explicit allow-list of trusted callables.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..data.mapping import evaluate_mapping
from ..domain.definitions import IREdge, IRNode, WorkflowIR
from ..domain.serialization import freeze_json, to_primitive
from ..dsl.compiler import compile_source
from ..graph.conditions import evaluate_condition


class LangGraphCompileError(ValueError):
    """The validated IR uses semantics this adapter cannot execute safely."""


class HandlerBindingError(LangGraphCompileError):
    """A node does not resolve to the exact trusted Handler build in its IR."""


class LangGraphUnknownExternalResult(RuntimeError):
    """An external Handler may have acted and must not be executed again."""


@dataclass(frozen=True)
class LangGraphExecutionContext:
    workflow_id: str
    node_id: str
    run_id: str = ""
    attempt_id: str = ""


@dataclass(frozen=True)
class HandlerOutcome:
    """A trusted Handler's data plus the edge family that should consume it."""

    output: Mapping[str, Any]
    route: str = "success"

    def __post_init__(self) -> None:
        if self.route not in {"success", "error", "timeout", "cancel"}:
            raise ValueError(f"unsupported Handler outcome route: {self.route!r}")
        if not isinstance(self.output, Mapping):
            raise TypeError("Handler outcome output must be a mapping")


HandlerCallable = Callable[
    [Mapping[str, Any], Mapping[str, Any], LangGraphExecutionContext],
    Mapping[str, Any] | HandlerOutcome,
]


@dataclass(frozen=True)
class BoundHandler:
    name: str
    version: str
    manifest_fingerprint: str
    invoke: HandlerCallable

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("handler name and version are required")
        if not self.manifest_fingerprint.startswith("sha256:"):
            raise ValueError("handler fingerprint must be sha256")
        if not callable(self.invoke):
            raise TypeError("handler invoke must be callable")


class LangGraphHandlerRegistry:
    """Sealed exact-version allow-list used during graph compilation."""

    def __init__(self, handlers: Iterable[BoundHandler]) -> None:
        entries: dict[tuple[str, str], BoundHandler] = {}
        for handler in handlers:
            key = (handler.name, handler.version)
            if key in entries:
                raise ValueError(f"duplicate LangGraph handler: {handler.name}@{handler.version}")
            entries[key] = handler
        self._entries = MappingProxyType(entries)

    def resolve(self, node: IRNode) -> BoundHandler:
        reference = node.handler
        if reference is None:
            raise HandlerBindingError(f"node {node.id!r} has no Handler binding")
        handler = self._entries.get((reference.name, reference.version))
        if handler is None:
            raise HandlerBindingError(
                f"handler not registered: {reference.name}@{reference.version}"
            )
        if handler.manifest_fingerprint != reference.manifest_fingerprint:
            raise HandlerBindingError(
                f"handler manifest mismatch: {reference.name}@{reference.version}"
            )
        return handler


def _merge_dicts(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Merge parallel results and let a loop replace its prior node output."""

    merged = dict(left)
    merged.update(right)
    return merged


def _append_order(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    """Keep deterministic order across serializers that decode tuples as lists."""

    return (*left, *right)


class _GraphState(TypedDict):
    workflow_inputs: Mapping[str, Any]
    node_outputs: Annotated[dict[str, Mapping[str, Any]], _merge_dicts]
    node_routes: Annotated[dict[str, str], _merge_dicts]
    execution_order: Annotated[tuple[str, ...], _append_order]


def _edge_value(
    edge: IREdge,
    source_output: Mapping[str, Any],
    workflow_inputs: Mapping[str, Any],
) -> Any:
    mapped = evaluate_mapping(
        edge.mapping, source_output, workflow_inputs=workflow_inputs
    )
    if isinstance(mapped, Mapping):
        if edge.target_port in mapped:
            return mapped[edge.target_port]
        if edge.source_port in mapped:
            return mapped[edge.source_port]
    return mapped


def _assemble_inputs(
    ir: WorkflowIR, node: IRNode, state: _GraphState
) -> Mapping[str, Any]:
    workflow_inputs = state["workflow_inputs"]
    assembled: dict[str, Any] = (
        {
            port.id: workflow_inputs[port.id]
            for port in node.inputs
            if port.id in workflow_inputs
        }
        if node.id in ir.entry
        else {}
    )
    incoming = sorted(
        (edge for edge in ir.edges if edge.target_node == node.id),
        key=lambda edge: (edge.priority, edge.id),
    )
    for edge in incoming:
        source_output = state["node_outputs"].get(edge.source_node)
        if source_output is None:
            continue
        if state["node_routes"].get(edge.source_node, "success") != edge.route:
            continue
        if not evaluate_condition(
            edge.condition, source_output, workflow_inputs=workflow_inputs
        ):
            continue
        value = _edge_value(edge, source_output, workflow_inputs)
        if edge.target_port in assembled and assembled[edge.target_port] != value:
            # Once a loop has produced a value, its declared back edge is the
            # next generation's input and supersedes the original ingress.
            if not edge.back_edge:
                raise ValueError(
                    f"multiple selected edges write input {node.id}.{edge.target_port}"
                )
        assembled[edge.target_port] = value
    for port in node.inputs:
        if port.id not in assembled and port.has_default:
            assembled[port.id] = to_primitive(port.default)
        if port.required and port.id not in assembled:
            raise ValueError(f"missing required input {node.id}.{port.id}")
    return to_primitive(assembled)


def _normalize_outputs(node: IRNode, result: Mapping[str, Any]) -> Mapping[str, Any]:
    declared = {port.id for port in node.outputs}
    unknown = set(result) - declared
    if unknown:
        raise ValueError(
            f"handler for {node.id!r} returned undeclared outputs: {sorted(unknown)}"
        )
    missing = {port.id for port in node.outputs if port.required and port.id not in result}
    if missing:
        raise ValueError(
            f"handler for {node.id!r} omitted required outputs: {sorted(missing)}"
        )
    return to_primitive(dict(result))


def _handlerless_outputs(
    node: IRNode, inputs: Mapping[str, Any]
) -> Mapping[str, Any]:
    if not node.outputs:
        return {}
    copied = {port.id: inputs[port.id] for port in node.outputs if port.id in inputs}
    return _normalize_outputs(node, copied)


@dataclass(frozen=True)
class CompiledLangGraphWorkflow:
    """A compiled graph plus a small public result-oriented invocation API."""

    ir: WorkflowIR
    graph: Any

    def invoke(
        self,
        inputs: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if inputs is None:
            state = self.graph.invoke(None, config=dict(config or {}))
            return self._result(state)
        declared = {port.id for port in self.ir.inputs}
        unknown = set(inputs) - declared
        if unknown:
            raise ValueError(f"unknown workflow inputs: {sorted(unknown)}")
        normalized = dict(inputs)
        for port in self.ir.inputs:
            if port.id not in normalized and port.has_default:
                normalized[port.id] = to_primitive(port.default)
            if port.required and port.id not in normalized:
                raise ValueError(f"missing workflow input {port.id!r}")
        state = self.graph.invoke(
            {
                "workflow_inputs": to_primitive(normalized),
                "node_outputs": {},
                "node_routes": {},
                "execution_order": (),
            },
            config=dict(config or {}),
        )
        return self._result(state)

    def stream(
        self,
        inputs: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
        stream_mode: str = "updates",
    ):
        """Yield LangGraph execution chunks without weakening input validation."""

        if inputs is None:
            graph_input = None
        else:
            declared = {port.id for port in self.ir.inputs}
            unknown = set(inputs) - declared
            if unknown:
                raise ValueError(f"unknown workflow inputs: {sorted(unknown)}")
            normalized = dict(inputs)
            for port in self.ir.inputs:
                if port.id not in normalized and port.has_default:
                    normalized[port.id] = to_primitive(port.default)
                if port.required and port.id not in normalized:
                    raise ValueError(f"missing workflow input {port.id!r}")
            graph_input = {
                "workflow_inputs": to_primitive(normalized),
                "node_outputs": {},
                "node_routes": {},
                "execution_order": (),
            }
        return self.graph.stream(
            graph_input, config=dict(config or {}), stream_mode=stream_mode
        )

    def resume(
        self,
        value: Any,
        *,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Resume a checkpointed LangGraph interrupt with its external value."""

        state = self.graph.invoke(Command(resume=value), config=dict(config))
        return self._result(state)

    def _result(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        result = None
        if self.ir.result is not None:
            producer = state.get("node_outputs", {}).get(self.ir.result.node_id)
            if producer is not None:
                result = producer.get(self.ir.result.output_port_id)
        return {
            "result": to_primitive(result),
            "node_outputs": to_primitive(state.get("node_outputs", {})),
            "node_routes": dict(state.get("node_routes", {})),
            "execution_order": list(state.get("execution_order", ())),
        }


def compile_workflow(
    ir: WorkflowIR,
    registry: LangGraphHandlerRegistry,
    *,
    checkpointer: Any = None,
) -> CompiledLangGraphWorkflow:
    """Bind a validated WorkflowIR to trusted handlers and compile StateGraph."""

    if not isinstance(ir, WorkflowIR):
        raise TypeError("compile_workflow requires WorkflowIR")
    bound = {
        node.id: registry.resolve(node)
        for node in ir.nodes
        if node.handler is not None
    }
    for node in ir.nodes:
        if node.handler is None and node.kind not in {"terminal", "join", "human"}:
            raise HandlerBindingError(
                f"executable node {node.id!r} has no Handler binding"
            )

    builder = StateGraph(_GraphState)

    for node in ir.nodes:
        handler = bound.get(node.id)

        def execute(
            state: _GraphState,
            config: RunnableConfig,
            *,
            current=node,
            implementation=handler,
        ):
            inputs = _assemble_inputs(ir, current, state)
            if implementation is None:
                if current.kind == "human":
                    resumed = interrupt({
                        "workflow_id": ir.workflow_id,
                        "node_id": current.id,
                        "input": to_primitive(inputs),
                        "config": to_primitive(current.config),
                    })
                    if isinstance(resumed, Mapping):
                        human_output = resumed
                    elif len(current.outputs) == 1:
                        human_output = {current.outputs[0].id: resumed}
                    else:
                        raise ValueError(
                            f"human node {current.id!r} requires an object response"
                        )
                    output = _normalize_outputs(current, human_output)
                else:
                    output = _handlerless_outputs(current, inputs)
            else:
                raw = implementation.invoke(
                    inputs,
                    current.config,
                    LangGraphExecutionContext(
                        ir.workflow_id,
                        current.id,
                        str(config.get("configurable", {}).get("thread_id", "")),
                        (
                            "langgraph_attempt:"
                            + str(config.get("configurable", {}).get("thread_id", ""))
                            + f":{current.id}:"
                            + str(state.get("execution_order", ()).count(current.id) + 1)
                        ),
                    ),
                )
                outcome = raw if isinstance(raw, HandlerOutcome) else HandlerOutcome(raw)
                if not isinstance(outcome.output, Mapping):
                    raise TypeError(f"handler for {current.id!r} must return a mapping")
                output = _normalize_outputs(current, outcome.output)
            route_name = "success" if implementation is None else outcome.route
            return {
                "node_outputs": {current.id: output},
                "node_routes": {current.id: route_name},
                "execution_order": (current.id,),
            }

        builder.add_node(node.id, execute)

    for entry in ir.entry:
        builder.add_edge(START, entry)

    for node in ir.nodes:
        outgoing = tuple(
            sorted(
                (edge for edge in ir.edges if edge.source_node == node.id),
                key=lambda edge: (
                    (node.route_mode or "exclusive") == "exclusive"
                    and edge.condition == {"op": "literal", "value": True},
                    edge.priority,
                    edge.id,
                ),
            )
        )
        if not outgoing:
            builder.add_edge(node.id, END)
            continue

        def route(state: _GraphState, *, current=node, edges=outgoing):
            source = state["node_outputs"][current.id]
            selected = [
                edge.target_node
                for edge in edges
                if edge.route == state["node_routes"].get(current.id, "success")
                and evaluate_condition(
                    edge.condition,
                    source,
                    workflow_inputs=state["workflow_inputs"],
                )
            ]
            mode = current.route_mode or "exclusive"
            return selected if mode == "parallel" else selected[:1]

        builder.add_conditional_edges(
            node.id, route, sorted({edge.target_node for edge in outgoing})
        )

    return CompiledLangGraphWorkflow(
        ir, builder.compile(checkpointer=checkpointer)
    )


def compile_generated_workflow(
    source: str,
    handler_catalog: Any,
    schema_catalog: Any,
    runtime_registry: LangGraphHandlerRegistry,
    *,
    source_name: str = "<agent-generated>",
    source_format: str = "json",
    extension_registry: Any = None,
    checkpointer: Any = None,
) -> CompiledLangGraphWorkflow:
    """Validate Agent-authored DSL and compile only its trusted Handler refs.

    This is the intended public entry point for the generation pipeline.  A
    caller cannot skip Orbit's structural/semantic compiler and hand arbitrary
    Python to LangGraph.
    """

    compiled = compile_source(
        source,
        handler_catalog,
        schema_catalog,
        source_name=source_name,
        source_format=source_format,
        extensions=extension_registry,
    )
    return compile_workflow(
        compiled.ir, runtime_registry, checkpointer=checkpointer
    )
