"""Stable immutable ExecutionPlan 1.1 used by the deterministic kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .data import ArtifactVisibility, PortDataPolicy, PortTransport
from .graph import EdgeRoute, PlanEdge
from .ids import EntityId
from .serialization import freeze_json
from .schemas import validate_contract
from .versions import DefinitionHash, Revision, SchemaVersion


PLAN_SCHEMA_VERSION = SchemaVersion("1.1")
GRAPH_PLAN_SCHEMA_VERSION = SchemaVersion("1.2")


def _plan_port(value: Any) -> Any:
    if not isinstance(value, Mapping):
        raise TypeError("ExecutionPlan port must be an object")
    fields = {
        "id", "schema_id", "required", "has_default", "default",
        "description", "data_policy",
    }
    if set(value) != fields:
        raise ValueError("invalid ExecutionPlan port fields")
    if not isinstance(value["id"], str) or not value["id"].strip():
        raise ValueError("ExecutionPlan port id is required")
    if not isinstance(value["schema_id"], str) or not value["schema_id"].strip():
        raise ValueError("ExecutionPlan port schema_id is required")
    if not isinstance(value["required"], bool) or not isinstance(value["has_default"], bool):
        raise TypeError("ExecutionPlan port required/default flags must be booleans")
    policy = value["data_policy"]
    if not isinstance(policy, Mapping):
        raise TypeError("ExecutionPlan port data_policy must be an object")
    normalized = PortDataPolicy(
        PortTransport(policy["transport"]), policy["max_size_bytes"],
        tuple(policy["content_types"]),
        None if policy["visibility"] is None else ArtifactVisibility(policy["visibility"]),
    )
    if value["has_default"] and normalized.transport is not PortTransport.INLINE:
        raise ValueError("only inline ExecutionPlan ports may have defaults")
    return freeze_json(value)


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    kind: str
    handler_name: str | None
    handler_version: str | None
    handler_manifest_fingerprint: str | None
    inputs: Any
    outputs: Any
    config: Any

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("plan node_id is required")
        if self.kind not in {"action", "decision", "join", "terminal"}:
            raise ValueError(f"unsupported plan node kind: {self.kind}")
        if self.kind == "action" and (not self.handler_name or not self.handler_version):
            raise ValueError("action plan nodes require an exact handler")
        if self.kind == "action" and not self.handler_manifest_fingerprint:
            raise ValueError("action plan nodes require a handler manifest fingerprint")
        if self.kind != "action" and any(
            value is not None for value in (
                self.handler_name, self.handler_version,
                self.handler_manifest_fingerprint,
            )
        ):
            raise ValueError("controller plan nodes cannot declare a handler")
        object.__setattr__(self, "inputs", tuple(_plan_port(item) for item in self.inputs))
        object.__setattr__(self, "outputs", tuple(_plan_port(item) for item in self.outputs))
        object.__setattr__(self, "config", freeze_json(self.config))


@dataclass(frozen=True)
class ExecutionPlan:
    schema_version: SchemaVersion
    plan_id: EntityId
    run_id: EntityId
    plan_version: Revision
    workflow_id: EntityId
    workflow_version: Revision
    workflow_definition_hash: DefinitionHash
    entry_node_id: str
    terminal_node_id: str
    ordered_node_ids: tuple[str, ...]
    nodes: tuple[PlanNode, ...]
    successors: Mapping[str, str | None]
    mappings: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported ExecutionPlan schema version")
        if self.plan_id.kind != "plan" or self.run_id.kind != "run":
            raise ValueError("invalid plan or run id kind")
        if self.workflow_id.kind != "workflow":
            raise ValueError("invalid workflow id kind")
        node_ids = tuple(item.node_id for item in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("ExecutionPlan node ids must be unique")
        if tuple(self.ordered_node_ids) != node_ids:
            raise ValueError("ordered_node_ids must match nodes")
        if self.entry_node_id not in node_ids or self.terminal_node_id not in node_ids:
            raise ValueError("entry and terminal must be plan nodes")
        if set(self.successors) != set(node_ids):
            raise ValueError("successor index must contain every plan node exactly once")
        for index, node_id in enumerate(node_ids):
            expected = None if index == len(node_ids) - 1 else node_ids[index + 1]
            if self.successors[node_id] != expected:
                raise ValueError("successor index must match the linear node order")
        if self.terminal_node_id != node_ids[-1]:
            raise ValueError("terminal must be the final linear plan node")
        if not set(self.mappings).issubset(set(node_ids) - {self.entry_node_id}):
            raise ValueError("mapping index references an invalid target node")
        object.__setattr__(self, "ordered_node_ids", tuple(self.ordered_node_ids))
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "successors", freeze_json(self.successors))
        object.__setattr__(self, "mappings", freeze_json(self.mappings))

    def node(self, node_id: str) -> PlanNode:
        for item in self.nodes:
            if item.node_id == node_id:
                return item
        raise KeyError(node_id)

    def successor(self, node_id: str) -> str | None:
        return self.successors[node_id]


@dataclass(frozen=True)
class GraphExecutionPlan:
    """Immutable ExecutionPlan 1.2 for deterministic static graphs."""

    schema_version: SchemaVersion
    plan_id: EntityId
    run_id: EntityId
    plan_version: Revision
    workflow_id: EntityId
    workflow_version: Revision
    workflow_definition_hash: DefinitionHash
    entry_node_ids: tuple[str, ...]
    terminal_node_ids: tuple[str, ...]
    ordered_node_ids: tuple[str, ...]
    nodes: tuple[PlanNode, ...]
    edges: tuple[PlanEdge, ...]
    outgoing_edges: Mapping[str, tuple[str, ...]]
    incoming_edges: Mapping[str, tuple[str, ...]]
    policies: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.schema_version != GRAPH_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported GraphExecutionPlan schema version")
        if self.plan_id.kind != "plan" or self.run_id.kind != "run":
            raise ValueError("invalid plan or run id kind")
        if self.workflow_id.kind != "workflow":
            raise ValueError("invalid workflow id kind")
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        node_ids = tuple(node.node_id for node in nodes)
        edge_ids = tuple(edge.edge_id for edge in edges)
        if len(set(node_ids)) != len(node_ids) or len(set(edge_ids)) != len(edge_ids):
            raise ValueError("GraphExecutionPlan node and edge ids must be unique")
        if tuple(self.ordered_node_ids) != node_ids:
            raise ValueError("ordered_node_ids must match nodes")
        if not self.entry_node_ids or not self.terminal_node_ids:
            raise ValueError("GraphExecutionPlan requires entry and terminal nodes")
        if not set(self.entry_node_ids).issubset(node_ids) or not set(self.terminal_node_ids).issubset(node_ids):
            raise ValueError("entry or terminal references an unknown node")
        if any(self.node(item).kind != "terminal" for item in self.terminal_node_ids):
            raise ValueError("terminal_node_ids must reference terminal nodes")
        if set(self.outgoing_edges) != set(node_ids) or set(self.incoming_edges) != set(node_ids):
            raise ValueError("edge indexes must contain every node")
        by_edge = {edge.edge_id: edge for edge in edges}
        for node_id in node_ids:
            expected_out = tuple(
                edge.edge_id for edge in edges if edge.source_node_id == node_id
            )
            expected_in = tuple(
                edge.edge_id for edge in edges if edge.target_node_id == node_id
            )
            if tuple(self.outgoing_edges[node_id]) != expected_out:
                raise ValueError("outgoing edge index does not match edges")
            if tuple(self.incoming_edges[node_id]) != expected_in:
                raise ValueError("incoming edge index does not match edges")
        if any(edge.source_node_id not in node_ids or edge.target_node_id not in node_ids for edge in by_edge.values()):
            raise ValueError("edge references an unknown node")
        object.__setattr__(self, "entry_node_ids", tuple(self.entry_node_ids))
        object.__setattr__(self, "terminal_node_ids", tuple(self.terminal_node_ids))
        object.__setattr__(self, "ordered_node_ids", tuple(self.ordered_node_ids))
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "outgoing_edges", freeze_json(self.outgoing_edges))
        object.__setattr__(self, "incoming_edges", freeze_json(self.incoming_edges))
        object.__setattr__(self, "policies", freeze_json(self.policies))

    def node(self, node_id: str) -> PlanNode:
        for item in self.nodes:
            if item.node_id == node_id:
                return item
        raise KeyError(node_id)

    def edge(self, edge_id: str) -> PlanEdge:
        for item in self.edges:
            if item.edge_id == edge_id:
                return item
        raise KeyError(edge_id)

    def outgoing(self, node_id: str, route: EdgeRoute | None = None) -> tuple[PlanEdge, ...]:
        values = tuple(self.edge(item) for item in self.outgoing_edges[node_id])
        return values if route is None else tuple(item for item in values if item.route is route)

    def incoming(self, node_id: str) -> tuple[PlanEdge, ...]:
        return tuple(self.edge(item) for item in self.incoming_edges[node_id])


def execution_plan_from_primitive(value: Mapping[str, Any]) -> ExecutionPlan | GraphExecutionPlan:
    if value.get("schema_version") == "1.2":
        return _graph_execution_plan_from_primitive(value)
    validate_contract(value, "execution-plan/1.1")
    required = {
        "schema_version", "plan_id", "run_id", "plan_version", "workflow_id",
        "workflow_version", "workflow_definition_hash", "entry_node_id",
        "terminal_node_id", "ordered_node_ids", "nodes", "successors", "mappings",
    }
    extra = set(value) - required
    missing = required - set(value)
    if missing or extra:
        raise ValueError(f"invalid ExecutionPlan fields; missing={sorted(missing)}, extra={sorted(extra)}")
    nodes = tuple(
        _plan_node_from_primitive(item)
        for item in value["nodes"]
    )
    return ExecutionPlan(
        SchemaVersion(value["schema_version"]), EntityId.parse(value["plan_id"]),
        EntityId.parse(value["run_id"]), Revision(value["plan_version"]),
        EntityId.parse(value["workflow_id"]), Revision(value["workflow_version"]),
        DefinitionHash(value["workflow_definition_hash"]), value["entry_node_id"],
        value["terminal_node_id"], tuple(value["ordered_node_ids"]), nodes,
        value["successors"], value["mappings"],
    )


def _plan_node_from_primitive(item: Mapping[str, Any]) -> PlanNode:
    fields = {
        "node_id", "kind", "handler_name", "handler_version", "inputs",
        "handler_manifest_fingerprint", "outputs", "config",
    }
    if set(item) != fields:
        raise ValueError("invalid ExecutionPlan node fields")
    return PlanNode(
        item["node_id"], item["kind"], item["handler_name"],
        item["handler_version"], item["handler_manifest_fingerprint"],
        item["inputs"], item["outputs"], item["config"],
    )


def _graph_execution_plan_from_primitive(value: Mapping[str, Any]) -> GraphExecutionPlan:
    validate_contract(value, "execution-plan/1.2")
    nodes = tuple(_plan_node_from_primitive(item) for item in value["nodes"])
    edges = tuple(
        PlanEdge(
            item["edge_id"], item["source_node_id"], item["target_node_id"],
            EdgeRoute(item["route"]), item["priority"], item["source_port"],
            item["target_port"], item["condition"], item["mapping"],
            item["back_edge"], item["policy_ref"],
        )
        for item in value["edges"]
    )
    return GraphExecutionPlan(
        SchemaVersion(value["schema_version"]), EntityId.parse(value["plan_id"]),
        EntityId.parse(value["run_id"]), Revision(value["plan_version"]),
        EntityId.parse(value["workflow_id"]), Revision(value["workflow_version"]),
        DefinitionHash(value["workflow_definition_hash"]),
        tuple(value["entry_node_ids"]), tuple(value["terminal_node_ids"]),
        tuple(value["ordered_node_ids"]), nodes, edges,
        value["outgoing_edges"], value["incoming_edges"], value["policies"],
    )
