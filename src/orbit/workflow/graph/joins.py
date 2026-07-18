"""Pure Join decisions independent of arrival order."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.graph import JoinDecision, JoinDisposition, JoinMode, JoinPolicy
from ..domain.ids import EntityId
from ..domain.serialization import definition_hash
from ..domain.states import BranchTokenStatus
from .input_assembly import assemble_join_inputs


@dataclass(frozen=True)
class JoinTokenFact:
    edge_id: str
    priority: int
    status: BranchTokenStatus
    value: Any = None


def evaluate_join(
    join_group_id: EntityId,
    policy: JoinPolicy,
    facts: tuple[JoinTokenFact, ...],
    *,
    deadline_fired: bool = False,
) -> tuple[JoinDecision, Any | None]:
    ordered = tuple(sorted(facts, key=lambda item: (item.priority, item.edge_id)))
    participants = tuple(item.edge_id for item in ordered)
    settled_states = {
        BranchTokenStatus.COMPLETED, BranchTokenStatus.FAILED,
        BranchTokenStatus.CANCELLED, BranchTokenStatus.NOT_SELECTED,
    }
    settled = tuple(item.edge_id for item in ordered if item.status in settled_states)
    successes = tuple(item for item in ordered if item.status is BranchTokenStatus.COMPLETED)
    failures = tuple(item for item in ordered if item.status in {BranchTokenStatus.FAILED, BranchTokenStatus.CANCELLED})
    active = tuple(item for item in ordered if item.status is BranchTokenStatus.ACTIVE)

    winners: tuple[JoinTokenFact, ...] = ()
    disposition = JoinDisposition.WAIT
    if policy.mode is JoinMode.ALL:
        if not active:
            disposition = JoinDisposition.FAIL if failures or not successes else JoinDisposition.OPEN
            winners = successes if disposition is JoinDisposition.OPEN else ()
    elif policy.mode is JoinMode.ALL_SUCCESSFUL:
        # Wait for every participant, then merge every successful branch while
        # explicitly tolerating failed/cancelled participants.  It fails only
        # when no successful participant remains; this is intentionally
        # different from ALL, which requires every participant to succeed.
        if not active:
            disposition = JoinDisposition.OPEN if successes else JoinDisposition.FAIL
            winners = successes if disposition is JoinDisposition.OPEN else ()
    elif policy.mode is JoinMode.ANY:
        if successes:
            candidate = successes[0]
            candidate_key = (candidate.priority, candidate.edge_id)
            higher_unsettled = any(
                (item.priority, item.edge_id) < candidate_key
                and item.status is BranchTokenStatus.ACTIVE
                for item in ordered
            )
            if not higher_unsettled:
                disposition, winners = JoinDisposition.OPEN, (candidate,)
        elif not active:
            disposition = JoinDisposition.FAIL
    elif policy.mode is JoinMode.N_OF_M:
        threshold = policy.threshold or 1
        if len(successes) >= threshold:
            candidates = successes[:threshold]
            cutoff = (candidates[-1].priority, candidates[-1].edge_id)
            if not any(
                (item.priority, item.edge_id) < cutoff
                and item.status is BranchTokenStatus.ACTIVE
                for item in ordered
            ):
                disposition, winners = JoinDisposition.OPEN, candidates
        elif len(successes) + len(active) < threshold:
            disposition = JoinDisposition.FAIL
    elif policy.mode is JoinMode.DEADLINE:
        minimum = policy.min_successful or 1
        if not active and len(successes) >= minimum:
            disposition, winners = JoinDisposition.OPEN, successes
        elif deadline_fired:
            if len(successes) >= minimum:
                disposition, winners = JoinDisposition.OPEN, successes
            else:
                disposition = JoinDisposition.TIMED_OUT

    winner_ids = tuple(item.edge_id for item in winners)
    ignored = tuple(item.edge_id for item in ordered if item.edge_id not in winner_ids and item.status is not BranchTokenStatus.ACTIVE)
    merged = None
    merged_hash = None
    if disposition is JoinDisposition.OPEN:
        merged = assemble_join_inputs(
            policy.merge_mode,
            {item.edge_id: item.value for item in winners}, winner_ids,
        )
        merged_hash = definition_hash(merged)
    return JoinDecision(
        join_group_id, disposition, participants, settled, winner_ids, ignored,
        merged_hash,
    ), merged
