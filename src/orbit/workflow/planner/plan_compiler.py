"""Pure validation and compilation of committed dynamic plan versions."""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Mapping

from ..domain.execution_plan import GraphExecutionPlan, PlanNode
from ..domain.graph import EdgeRoute, PlanEdge
from ..domain.ids import EntityId
from ..domain.plan_patch import (
    AgenticRegion, DynamicDagLimits, PatchOperationKind, PlanPatch,
)
from ..domain.serialization import definition_hash
from ..domain.states import NodeRunStatus
from ..domain.versions import Revision


class PatchValidationError(ValueError):
    def __init__(self, path: str, rule: str, message: str) -> None:
        self.path, self.rule = path, rule
        super().__init__(f"{path}: [{rule}] {message}")


def _node(value) -> PlanNode:
    fields = {"node_id", "kind", "handler_name", "handler_version", "handler_manifest_fingerprint", "inputs", "outputs", "config"}
    if set(value) != fields:
        raise PatchValidationError("$.operations[].value", "node_schema", "invalid node fields")
    return PlanNode(**value)


def _edge(value) -> PlanEdge:
    fields = {"edge_id", "source_node_id", "target_node_id", "route", "priority", "source_port", "target_port", "condition", "mapping", "back_edge", "policy_ref"}
    if set(value) != fields:
        raise PatchValidationError("$.operations[].value", "edge_schema", "invalid edge fields")
    return PlanEdge(
        value["edge_id"], value["source_node_id"], value["target_node_id"],
        EdgeRoute(value["route"]), value["priority"], value["source_port"],
        value["target_port"], value["condition"], value["mapping"],
        value["back_edge"], value["policy_ref"],
    )


def validate_patch(
    base: GraphExecutionPlan, patch: PlanPatch, region: AgenticRegion,
    node_statuses: Mapping[str, NodeRunStatus | str],
    limits: DynamicDagLimits = DynamicDagLimits(),
) -> tuple[PlanNode, ...]:
    if patch.run_id != base.run_id or patch.base_plan_version != base.plan_version:
        raise PatchValidationError("$.base_plan_version", "plan_cas", "patch base does not match plan")
    added: list[PlanNode] = []
    added_edges = 0
    mutable = frozenset(region.mutable_node_ids)
    base_edges = {edge.edge_id: edge for edge in base.edges}
    new_node_ids = {
        operation.target_id for operation in patch.operations
        if operation.kind is PatchOperationKind.ADD_NODE
    }
    for index, operation in enumerate(patch.operations):
        if operation.kind in {PatchOperationKind.REMOVE_PENDING_NODE, PatchOperationKind.REPLACE_PENDING_NODE}:
            if operation.target_id not in mutable:
                raise PatchValidationError(f"$.operations[{index}].target_id", "agentic_region", "target outside Agentic Region")
            status = node_statuses.get(operation.target_id, NodeRunStatus.PENDING)
            if NodeRunStatus(status) is not NodeRunStatus.PENDING:
                raise PatchValidationError(f"$.operations[{index}]", "pending_only", "ready or historical nodes are immutable")
        if operation.kind is PatchOperationKind.ADD_NODE:
            node = _node(operation.value)
            if node.node_id != operation.target_id:
                raise PatchValidationError(f"$.operations[{index}].target_id", "node_identity", "target must equal node_id")
            added.append(node)
        if operation.kind is PatchOperationKind.ADD_EDGE:
            added_edges += 1
            edge = _edge(operation.value)
            for endpoint in (edge.source_node_id, edge.target_node_id):
                if endpoint in new_node_ids:
                    continue
                if endpoint not in mutable:
                    raise PatchValidationError(f"$.operations[{index}]", "agentic_region", "edge endpoint outside Agentic Region")
                if NodeRunStatus(node_statuses.get(endpoint, NodeRunStatus.PENDING)) is not NodeRunStatus.PENDING:
                    raise PatchValidationError(f"$.operations[{index}]", "pending_only", "edge touches ready or historical node")
        if operation.kind is PatchOperationKind.REMOVE_PENDING_EDGE:
            edge = base_edges.get(operation.target_id)
            if edge is None:
                raise PatchValidationError(f"$.operations[{index}].target_id", "edge_exists", "edge not found")
            for endpoint in (edge.source_node_id, edge.target_node_id):
                if endpoint not in mutable or NodeRunStatus(node_statuses.get(endpoint, NodeRunStatus.PENDING)) is not NodeRunStatus.PENDING:
                    raise PatchValidationError(f"$.operations[{index}]", "pending_only", "edge touches immutable node")
    if len(added) > limits.max_nodes_per_patch or added_edges > limits.max_edges_per_patch:
        raise PatchValidationError("$.operations", "patch_size", "dynamic DAG exceeds hard limit")
    return tuple(added)


def compile_patch(
    base: GraphExecutionPlan, patch: PlanPatch, region: AgenticRegion,
    node_statuses: Mapping[str, NodeRunStatus | str],
    limits: DynamicDagLimits = DynamicDagLimits(),
) -> GraphExecutionPlan:
    validate_patch(base, patch, region, node_statuses, limits)
    nodes = {node.node_id: node for node in base.nodes}
    edges = {edge.edge_id: edge for edge in base.edges}
    for operation in patch.operations:
        if operation.kind is PatchOperationKind.ADD_NODE:
            node = _node(operation.value)
            if node.node_id in nodes: raise PatchValidationError("$.operations", "unique_node", node.node_id)
            nodes[node.node_id] = node
        elif operation.kind is PatchOperationKind.ADD_EDGE:
            edge = _edge(operation.value)
            if edge.edge_id in edges: raise PatchValidationError("$.operations", "unique_edge", edge.edge_id)
            edges[edge.edge_id] = edge
        elif operation.kind is PatchOperationKind.REMOVE_PENDING_EDGE:
            edges.pop(operation.target_id, None)
        elif operation.kind is PatchOperationKind.REMOVE_PENDING_NODE:
            nodes.pop(operation.target_id, None)
            edges = {key: edge for key, edge in edges.items() if operation.target_id not in {edge.source_node_id, edge.target_node_id}}
        elif operation.kind is PatchOperationKind.REPLACE_PENDING_NODE:
            node = _node(operation.value); nodes[operation.target_id] = node
    if any(edge.source_node_id not in nodes or edge.target_node_id not in nodes for edge in edges.values()):
        raise PatchValidationError("$.operations", "graph_closed", "edge references an unknown node")
    ordered_nodes = tuple(sorted(nodes.values(), key=lambda item: item.node_id))
    ordered_edges = tuple(sorted(edges.values(), key=lambda item: (item.source_node_id, item.priority, item.edge_id)))
    outgoing = {node.node_id: tuple(edge.edge_id for edge in ordered_edges if edge.source_node_id == node.node_id) for node in ordered_nodes}
    incoming = {node.node_id: tuple(edge.edge_id for edge in ordered_edges if edge.target_node_id == node.node_id) for node in ordered_nodes}
    _validate_acyclic(tuple(node.node_id for node in ordered_nodes), ordered_edges, limits)
    _validate_reachability(base.entry_node_ids, base.terminal_node_ids, ordered_nodes, ordered_edges)
    plan_hash = definition_hash({"base": str(base.plan_id), "patch": patch.content_hash.value})
    return GraphExecutionPlan(
        base.schema_version, EntityId("plan", plan_hash.value.removeprefix("sha256:")),
        base.run_id, base.plan_version.next(), base.workflow_id, base.workflow_version,
        base.workflow_definition_hash, base.entry_node_ids, base.terminal_node_ids,
        tuple(item.node_id for item in ordered_nodes), ordered_nodes, ordered_edges,
        outgoing, incoming, base.policies,
    )


def _validate_acyclic(node_ids: tuple[str, ...], edges: Iterable[PlanEdge], limits: DynamicDagLimits) -> None:
    successors = {node: [] for node in node_ids}; indegree = {node: 0 for node in node_ids}
    for edge in edges:
        if edge.back_edge: continue
        successors[edge.source_node_id].append(edge.target_node_id); indegree[edge.target_node_id] += 1
    ready = sorted(node for node, count in indegree.items() if count == 0); depth = {node: 1 for node in ready}; seen = 0
    if len(ready) > limits.max_width: raise PatchValidationError("$.operations", "max_width", "graph width exceeded")
    while ready:
        current = ready.pop(0); seen += 1
        if depth[current] > limits.max_depth: raise PatchValidationError("$.operations", "max_depth", "graph depth exceeded")
        for target in sorted(successors[current]):
            depth[target] = max(depth.get(target, 1), depth[current] + 1); indegree[target] -= 1
            if indegree[target] == 0: ready.append(target); ready.sort()
    if seen != len(node_ids): raise PatchValidationError("$.operations", "dag", "non-back-edge cycle detected")


def _validate_reachability(entries, terminals, nodes, edges) -> None:
    node_ids={node.node_id for node in nodes};forward={node:[] for node in node_ids};reverse={node:[] for node in node_ids}
    for edge in edges:
        if edge.back_edge:continue
        forward[edge.source_node_id].append(edge.target_node_id);reverse[edge.target_node_id].append(edge.source_node_id)
    reachable=set(entries);stack=list(entries)
    while stack:
        for target in forward[stack.pop()]:
            if target not in reachable:reachable.add(target);stack.append(target)
    can_finish=set(terminals);stack=list(terminals)
    while stack:
        for source in reverse[stack.pop()]:
            if source not in can_finish:can_finish.add(source);stack.append(source)
    if reachable!=node_ids:raise PatchValidationError("$.operations","reachability","dynamic graph contains unreachable nodes")
    if can_finish!=node_ids:raise PatchValidationError("$.operations","completion_path","dynamic graph node cannot reach a terminal")
    if any(forward[terminal] for terminal in terminals):raise PatchValidationError("$.operations","terminal","terminal node cannot have outgoing edges")
