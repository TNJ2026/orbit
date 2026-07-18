"""Pure stable-priority edge routing."""

from __future__ import annotations

from ..domain.execution_plan import GraphExecutionPlan
from ..domain.graph import EdgeRoute, RouteDecision, RouteMode
from ..domain.ids import EntityId
from ..domain.serialization import definition_hash
from .conditions import evaluate_condition


def _is_default_condition(condition) -> bool:
    """Return whether the compiled condition denotes the exclusive fallback."""
    return condition == {"op": "literal", "value": True}


def evaluate_route(
    plan: GraphExecutionPlan,
    node_run_id: EntityId,
    node_id: str,
    route: EdgeRoute,
    source,
    *,
    workflow_inputs=None,
) -> RouteDecision:
    node = plan.node(node_id)
    mode = RouteMode(node.config.get("route_mode", "exclusive"))
    candidates = tuple(sorted(
        plan.outgoing(node_id, route),
        key=lambda edge: (
            mode is RouteMode.EXCLUSIVE and _is_default_condition(edge.condition),
            edge.priority,
            edge.edge_id,
        ),
    ))
    matched = tuple(
        edge.edge_id for edge in candidates
        if evaluate_condition(edge.condition, source, workflow_inputs=workflow_inputs or {})
    )
    selected = matched[:1] if mode is RouteMode.EXCLUSIVE else matched
    rejected = tuple(edge.edge_id for edge in candidates if edge.edge_id not in selected)
    return RouteDecision(
        node_run_id, route, mode, tuple(edge.edge_id for edge in candidates),
        selected, rejected,
        definition_hash({"source": source, "workflow_inputs": workflow_inputs or {}}),
    )
