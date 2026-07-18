"""Pure Node activation identity decision."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.graph import derive_graph_node_run_id
from ..domain.ids import EntityId
from ..domain.versions import Revision


@dataclass(frozen=True)
class ActivationDecision:
    node_run_id: EntityId
    node_id: str
    generation: int
    activation_key: str


def decide_activation(
    run_id: EntityId,
    plan_version: Revision,
    node_id: str,
    generation: int,
    activation_key: str,
) -> ActivationDecision:
    return ActivationDecision(
        derive_graph_node_run_id(run_id, plan_version, node_id, generation, activation_key),
        node_id, generation, activation_key,
    )
