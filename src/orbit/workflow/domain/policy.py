"""Deterministic, fail-closed policy decision facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .ids import EntityId
from .serialization import definition_hash, freeze_json
from .versions import DefinitionHash


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    version: str
    capability: str
    effect: PolicyEffect
    external_write: bool = False

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.rule_id, self.version, self.capability)):
            raise ValueError("policy rule identifiers are required")


@dataclass(frozen=True)
class PolicyRuleResult:
    rule_id: str
    effect: PolicyEffect
    matched: bool
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    decision_id: EntityId
    run_id: EntityId
    patch_id: EntityId
    input_hash: DefinitionHash
    rule_set_version: str
    allowed: bool
    requires_approval: bool
    results: tuple[PolicyRuleResult, ...]
    reasons: tuple[str, ...]


def evaluate_policy(
    *, run_id: EntityId, patch_id: EntityId, required_capabilities: Iterable[str],
    rules: Iterable[PolicyRule], approval_capabilities: Iterable[str] = (),
    context: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Pure policy evaluation. Deny wins; missing rules deny by default."""
    required = tuple(sorted(set(required_capabilities)))
    approvals = frozenset(approval_capabilities)
    ordered_rules = tuple(sorted(rules, key=lambda item: (item.capability, item.rule_id, item.version)))
    results: list[PolicyRuleResult] = []
    reasons: list[str] = []
    requires_approval = False
    denied = False
    for capability in required:
        matches = tuple(rule for rule in ordered_rules if rule.capability == capability)
        if not matches:
            denied = True; reasons.append(f"missing allow rule for {capability}")
            results.append(PolicyRuleResult(f"default:{capability}", PolicyEffect.DENY, True, "fail closed"))
            continue
        if any(rule.effect is PolicyEffect.DENY for rule in matches):
            denied = True; reasons.append(f"explicit deny for {capability}")
        for rule in matches:
            approved = capability in approvals
            matched = rule.effect is not PolicyEffect.REQUIRE_APPROVAL or approved
            if rule.effect is PolicyEffect.REQUIRE_APPROVAL and not approved:
                requires_approval = True; reasons.append(f"approval required for {capability}")
            results.append(PolicyRuleResult(rule.rule_id, rule.effect, matched, "matched" if matched else "approval missing"))
    input_value = {
        "run_id": str(run_id), "patch_id": str(patch_id), "required": required,
        "rules": ordered_rules, "approvals": sorted(approvals), "context": freeze_json(context or {}),
    }
    input_hash = definition_hash(input_value)
    decision_id = EntityId("policy_decision", input_hash.value.removeprefix("sha256:"))
    return PolicyDecision(
        decision_id, run_id, patch_id, input_hash,
        definition_hash(ordered_rules).value, not denied and not requires_approval,
        requires_approval, tuple(results), tuple(sorted(set(reasons))),
    )

