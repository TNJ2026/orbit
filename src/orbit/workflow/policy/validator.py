"""Policy boundary kept separate from Planner and repositories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Any

from ..domain.ids import EntityId
from ..domain.policy import PolicyDecision, PolicyRule, evaluate_policy


@dataclass(frozen=True)
class PolicyValidator:
    rules: tuple[PolicyRule, ...]

    def __init__(self, rules: Iterable[PolicyRule]) -> None:
        object.__setattr__(self, "rules", tuple(rules))

    def validate(self, *, run_id: EntityId, patch_id: EntityId, capabilities: Iterable[str], approvals: Iterable[str] = (), context: Mapping[str, Any] | None = None) -> PolicyDecision:
        return evaluate_policy(run_id=run_id, patch_id=patch_id, required_capabilities=capabilities, rules=self.rules, approval_capabilities=approvals, context=context)

