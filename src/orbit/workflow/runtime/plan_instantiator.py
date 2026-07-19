"""Pure WorkflowIR -> static linear ExecutionPlan v1 instantiation."""

from __future__ import annotations

from ..domain.definitions import WorkflowIR
from ..domain.execution_plan import (
    ExecutionPlan, GraphExecutionPlan, GRAPH_PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION, PlanNode,
)
from ..domain.graph import EdgeRoute, PlanEdge
from ..domain.ids import EntityId
from ..domain.serialization import to_primitive
from ..domain.schemas import validate_contract
from ..domain.versions import DefinitionHash, Revision


class UnsupportedPlanShapeError(ValueError):
    pass


def instantiate_execution_plan(
    ir: WorkflowIR,
    *,
    run_id: EntityId,
    plan_id: EntityId,
    workflow_version: Revision,
    workflow_definition_hash: DefinitionHash,
) -> ExecutionPlan | GraphExecutionPlan:
    if ir.ir_version == "1.2":
        return _instantiate_graph_plan(
            ir, run_id=run_id, plan_id=plan_id,
            workflow_version=workflow_version,
            workflow_definition_hash=workflow_definition_hash,
        )
    if len(ir.entry) != 1 or len(ir.terminals) != 1:
        raise UnsupportedPlanShapeError("Step 4 requires exactly one entry and terminal")
    if ir.policies or ir.extensions:
        raise UnsupportedPlanShapeError("Step 4 does not execute policies or extensions")
    by_id = {item.id: item for item in ir.nodes}
    outgoing: dict[str, list] = {item.id: [] for item in ir.nodes}
    incoming: dict[str, list] = {item.id: [] for item in ir.nodes}
    for edge in ir.edges:
        if edge.route != "success":
            raise UnsupportedPlanShapeError(f"error route is unsupported: {edge.id}")
        condition = to_primitive(edge.condition)
        if condition != {"op": "literal", "value": True}:
            raise UnsupportedPlanShapeError(f"conditional edge is unsupported: {edge.id}")
        outgoing[edge.source_node].append(edge)
        incoming[edge.target_node].append(edge)
    for node_id in by_id:
        if len(outgoing[node_id]) > 1 or len(incoming[node_id]) > 1:
            raise UnsupportedPlanShapeError(f"non-linear degree at node {node_id}")
    ordered = []
    successors: dict[str, str | None] = {}
    mappings = {}
    current = ir.entry[0]
    seen = set()
    while True:
        if current in seen or current not in by_id:
            raise UnsupportedPlanShapeError("cycle or missing node in linear plan")
        seen.add(current)
        ordered.append(current)
        edges = outgoing[current]
        if not edges:
            successors[current] = None
            break
        edge = edges[0]
        successors[current] = edge.target_node
        mappings[edge.target_node] = to_primitive(edge.mapping)
        current = edge.target_node
    if current != ir.terminals[0] or seen != set(by_id):
        raise UnsupportedPlanShapeError("all nodes must form one entry-to-terminal chain")
    nodes = []
    for node_id in ordered:
        item = by_id[node_id]
        if item.kind not in {"action", "terminal"}:
            raise UnsupportedPlanShapeError(f"unsupported node kind {item.kind}: {node_id}")
        handler = item.handler
        nodes.append(
            PlanNode(
                item.id, item.kind, None if handler is None else handler.name,
                None if handler is None else handler.version,
                None if handler is None else handler.manifest_fingerprint,
                tuple(to_primitive(port) for port in item.inputs),
                tuple(to_primitive(port) for port in item.outputs),
                to_primitive(item.config),
            )
        )
    plan = ExecutionPlan(
        PLAN_SCHEMA_VERSION, plan_id, run_id, Revision(1),
        EntityId.parse(ir.workflow_id), workflow_version, workflow_definition_hash,
        ir.entry[0], ir.terminals[0], tuple(ordered), tuple(nodes), successors, mappings,
    )
    validate_contract(to_primitive(plan), "execution-plan/1.1")
    return plan


def _instantiate_graph_plan(
    ir: WorkflowIR,
    *,
    run_id: EntityId,
    plan_id: EntityId,
    workflow_version: Revision,
    workflow_definition_hash: DefinitionHash,
) -> GraphExecutionPlan:
    if ir.extensions:
        raise UnsupportedPlanShapeError("ExecutionPlan 1.2 does not execute extensions")
    by_id = {item.id: item for item in ir.nodes}
    indegree = {item.id: 0 for item in ir.nodes}
    dag_targets = {item.id: [] for item in ir.nodes}
    for edge in ir.edges:
        if not edge.back_edge:
            indegree[edge.target_node] += 1
            dag_targets[edge.source_node].append(edge.target_node)
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    ordered: list[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for target in sorted(dag_targets[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(ordered) != len(by_id):
        raise UnsupportedPlanShapeError("non-back edges must form a DAG")

    nodes = []
    for node_id in ordered:
        item = by_id[node_id]
        if item.kind not in {"action", "human", "agentic", "foreach", "subflow", "decision", "join", "terminal"}:
            raise UnsupportedPlanShapeError(f"unsupported node kind {item.kind}: {node_id}")
        handler = item.handler
        config = dict(to_primitive(item.config))
        if item.route_mode is not None:
            config["route_mode"] = item.route_mode
        if item.policies:
            config["policy_refs"] = list(item.policies)
        nodes.append(
            PlanNode(
                item.id, item.kind, None if handler is None else handler.name,
                None if handler is None else handler.version,
                None if handler is None else handler.manifest_fingerprint,
                tuple(to_primitive(port) for port in item.inputs),
                tuple(to_primitive(port) for port in item.outputs), config,
            )
        )
    edges = tuple(
        PlanEdge(
            item.id, item.source_node, item.target_node, EdgeRoute(item.route),
            item.priority, item.source_port, item.target_port,
            to_primitive(item.condition), to_primitive(item.mapping),
            item.back_edge, item.policy_ref,
        )
        for item in sorted(ir.edges, key=lambda value: (value.source_node, value.priority, value.id))
    )
    outgoing = {
        node_id: tuple(edge.edge_id for edge in edges if edge.source_node_id == node_id)
        for node_id in ordered
    }
    incoming = {
        node_id: tuple(edge.edge_id for edge in edges if edge.target_node_id == node_id)
        for node_id in ordered
    }
    plan = GraphExecutionPlan(
        GRAPH_PLAN_SCHEMA_VERSION, plan_id, run_id, Revision(1),
        EntityId.parse(ir.workflow_id), workflow_version, workflow_definition_hash,
        tuple(ir.entry), tuple(ir.terminals), tuple(ordered), tuple(nodes), edges,
        outgoing, incoming,
        {item.id: {"kind": item.kind, "config": to_primitive(item.config)} for item in ir.policies},
    )
    validate_contract(to_primitive(plan), "execution-plan/1.2")
    return plan
