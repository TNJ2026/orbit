"""Pure static Graph completion evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.graph import CompletionDecision, CompletionDisposition
from ..domain.ids import EntityId


@dataclass(frozen=True)
class CompletionFacts:
    terminal_node_run_ids: tuple[EntityId, ...] = ()
    active_responsibility_ids: tuple[EntityId, ...] = ()
    failed_node_run_ids: tuple[EntityId, ...] = ()
    waiting_reason: str | None = None
    required_terminal_count: int = 1


def evaluate_completion(facts: CompletionFacts) -> CompletionDecision:
    if facts.failed_node_run_ids:
        return CompletionDecision(
            CompletionDisposition.FAIL, "unhandled_node_failure",
            facts.terminal_node_run_ids, facts.active_responsibility_ids,
        )
    if facts.active_responsibility_ids:
        if facts.waiting_reason:
            return CompletionDecision(
                CompletionDisposition.WAIT, "durable_responsibility_pending",
                facts.terminal_node_run_ids, facts.active_responsibility_ids,
                facts.waiting_reason,
            )
        return CompletionDecision(
            CompletionDisposition.CONTINUE, "active_graph_responsibility",
            facts.terminal_node_run_ids, facts.active_responsibility_ids,
        )
    if len(facts.terminal_node_run_ids) >= facts.required_terminal_count:
        return CompletionDecision(
            CompletionDisposition.SUCCEED, "completion_policy_satisfied",
            facts.terminal_node_run_ids,
        )
    return CompletionDecision(CompletionDisposition.FAIL, "graph_stalled")
