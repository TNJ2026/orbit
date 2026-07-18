"""Offline deterministic evaluation for the Planner protocol boundary."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.ids import EntityId
from ..domain.planner import PlannerActionKind, strict_parse_proposal


@dataclass(frozen=True)
class PlannerEvalCase:
    name: str
    run_id: EntityId
    raw_response: str
    expected_valid: bool
    expected_action: PlannerActionKind | None = None
    task_success: bool = True
    policy_rejected: bool = False
    duplicate_nodes: int = 0
    human_interventions: int = 0
    decision_count: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0
    duration_ms: int = 0
    premature_finish: bool = False
    no_progress_loop: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip() or self.run_id.kind != "run":
            raise ValueError("invalid Planner Eval case")
        if self.expected_valid != (self.expected_action is not None):
            raise ValueError("valid Eval case requires expected action")
        for name in ("duplicate_nodes", "human_interventions", "decision_count", "input_tokens", "output_tokens", "cost_microunits", "duration_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class PlannerEvalReport:
    total: int
    passed: int
    valid_proposals: int
    invalid_proposals: int
    action_counts: tuple[tuple[str, int], ...]
    failures: tuple[str, ...]
    task_successes: int
    policy_rejections: int
    duplicate_nodes: int
    human_interventions: int
    decision_count: int
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    duration_ms: int
    premature_finishes: int
    no_progress_loops: int

    @property
    def pass_rate(self): return 1.0 if self.total == 0 else self.passed / self.total

    @property
    def invalid_proposal_rate(self): return 0.0 if self.total == 0 else self.invalid_proposals / self.total
    @property
    def task_success_rate(self): return 1.0 if self.total == 0 else self.task_successes / self.total
    @property
    def policy_rejection_rate(self): return 0.0 if self.total == 0 else self.policy_rejections / self.total
    @property
    def human_intervention_rate(self): return 0.0 if self.total == 0 else self.human_interventions / self.total
    @property
    def average_decisions(self): return 0.0 if self.total == 0 else self.decision_count / self.total
    @property
    def no_progress_rate(self): return 0.0 if self.total == 0 else self.no_progress_loops / self.total


class PlannerEvalHarness:
    def run(self, cases):
        cases = tuple(cases); passed = valid = invalid = 0; failures = []; counts = {}
        for case in cases:
            try:
                proposal = strict_parse_proposal(case.raw_response, expected_run_id=case.run_id)
                valid += 1; counts[proposal.action.kind.value] = counts.get(proposal.action.kind.value, 0) + 1
                ok = case.expected_valid and proposal.action.kind is case.expected_action
            except ValueError:
                invalid += 1; ok = not case.expected_valid
            if ok: passed += 1
            else: failures.append(case.name)
        return PlannerEvalReport(
            len(cases), passed, valid, invalid, tuple(sorted(counts.items())), tuple(failures),
            sum(case.task_success for case in cases),
            sum(case.policy_rejected for case in cases),
            sum(case.duplicate_nodes for case in cases),
            sum(case.human_interventions for case in cases),
            sum(case.decision_count for case in cases),
            sum(case.input_tokens for case in cases),
            sum(case.output_tokens for case in cases),
            sum(case.cost_microunits for case in cases),
            sum(case.duration_ms for case in cases),
            sum(case.premature_finish for case in cases),
            sum(case.no_progress_loop for case in cases),
        )
