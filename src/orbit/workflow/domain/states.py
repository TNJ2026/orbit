"""Frozen state machines for the Agentic Workflow 1.0 baseline."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import TypeVar

from .errors import InvalidTransitionError


class WorkflowRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    BUDGET_EXHAUSTED = "budget_exhausted"
    WAITING_FOR_BUDGET = "waiting_for_budget"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeRunStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class AttemptStatus(str, Enum):
    CREATED = "created"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    LOST = "lost"
    UNKNOWN_EXTERNAL_RESULT = "unknown_external_result"


class JobStatus(str, Enum):
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class TimerStatus(str, Enum):
    SCHEDULED = "scheduled"
    LEASED = "leased"
    FIRED = "fired"
    CANCELLED = "cancelled"


class HumanTaskStatus(str, Enum):
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class BranchTokenStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_SELECTED = "not_selected"


_TRANSITIONS = MappingProxyType(
    {
        WorkflowRunStatus: {
            WorkflowRunStatus.CREATED: frozenset(
                {WorkflowRunStatus.RUNNING, WorkflowRunStatus.CANCELLED}
            ),
            WorkflowRunStatus.RUNNING: frozenset(
                {
                    WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.BUDGET_EXHAUSTED,
                    WorkflowRunStatus.SUCCEEDED,
                    WorkflowRunStatus.FAILED,
                    WorkflowRunStatus.CANCELLED,
                }
            ),
            WorkflowRunStatus.WAITING: frozenset(
                {
                    WorkflowRunStatus.RUNNING,
                    WorkflowRunStatus.FAILED,
                    WorkflowRunStatus.CANCELLED,
                }
            ),
            WorkflowRunStatus.BUDGET_EXHAUSTED: frozenset(
                {
                    WorkflowRunStatus.WAITING_FOR_BUDGET,
                    WorkflowRunStatus.FAILED,
                    WorkflowRunStatus.CANCELLED,
                }
            ),
            WorkflowRunStatus.WAITING_FOR_BUDGET: frozenset(
                {
                    WorkflowRunStatus.RUNNING,
                    WorkflowRunStatus.FAILED,
                    WorkflowRunStatus.CANCELLED,
                }
            ),
        },
        NodeRunStatus: {
            NodeRunStatus.PENDING: frozenset(
                {NodeRunStatus.READY, NodeRunStatus.SKIPPED, NodeRunStatus.CANCELLED}
            ),
            NodeRunStatus.READY: frozenset(
                {NodeRunStatus.RUNNING, NodeRunStatus.CANCELLED}
            ),
            NodeRunStatus.RUNNING: frozenset(
                {
                    NodeRunStatus.WAITING,
                    NodeRunStatus.SUCCEEDED,
                    NodeRunStatus.FAILED,
                    NodeRunStatus.CANCELLED,
                }
            ),
            NodeRunStatus.WAITING: frozenset(
                {
                    NodeRunStatus.RUNNING,
                    NodeRunStatus.SUCCEEDED,
                    NodeRunStatus.FAILED,
                    NodeRunStatus.CANCELLED,
                }
            ),
        },
        AttemptStatus: {
            AttemptStatus.CREATED: frozenset(
                {AttemptStatus.LEASED, AttemptStatus.CANCELLED}
            ),
            AttemptStatus.LEASED: frozenset(
                {
                    AttemptStatus.RUNNING,
                    AttemptStatus.LOST,
                    AttemptStatus.CANCELLED,
                }
            ),
            AttemptStatus.RUNNING: frozenset(
                {
                    AttemptStatus.SUCCEEDED,
                    AttemptStatus.FAILED,
                    AttemptStatus.TIMED_OUT,
                    AttemptStatus.CANCELLED,
                    AttemptStatus.LOST,
                    AttemptStatus.UNKNOWN_EXTERNAL_RESULT,
                }
            ),
        },
        JobStatus: {
            JobStatus.READY: frozenset({JobStatus.LEASED, JobStatus.CANCELLED}),
            JobStatus.LEASED: frozenset(
                {JobStatus.READY, JobStatus.RUNNING, JobStatus.CANCELLED}
            ),
            JobStatus.RUNNING: frozenset(
                {
                    JobStatus.RETRY_WAIT,
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                }
            ),
            JobStatus.RETRY_WAIT: frozenset(
                {JobStatus.READY, JobStatus.CANCELLED}
            ),
        },
        LeaseStatus: {
            LeaseStatus.ACTIVE: frozenset(
                {LeaseStatus.RELEASED, LeaseStatus.EXPIRED}
            )
        },
        TimerStatus: {
            TimerStatus.SCHEDULED: frozenset(
                {TimerStatus.LEASED, TimerStatus.CANCELLED}
            ),
            TimerStatus.LEASED: frozenset(
                {TimerStatus.SCHEDULED, TimerStatus.FIRED, TimerStatus.CANCELLED}
            ),
        },
        HumanTaskStatus: {
            HumanTaskStatus.WAITING: frozenset(
                {HumanTaskStatus.COMPLETED, HumanTaskStatus.CANCELLED}
            )
        },
        BranchTokenStatus: {
            BranchTokenStatus.ACTIVE: frozenset(
                {
                    BranchTokenStatus.COMPLETED,
                    BranchTokenStatus.FAILED,
                    BranchTokenStatus.CANCELLED,
                    BranchTokenStatus.NOT_SELECTED,
                }
            )
        },
    }
)

_MACHINE_NAMES = MappingProxyType(
    {
        WorkflowRunStatus: "workflow_run",
        NodeRunStatus: "node_run",
        AttemptStatus: "attempt",
        JobStatus: "job",
        LeaseStatus: "lease",
        TimerStatus: "timer",
        HumanTaskStatus: "human_task",
        BranchTokenStatus: "branch_token",
    }
)


StatusT = TypeVar("StatusT", bound=Enum)


def allowed_transitions(current: StatusT) -> frozenset[StatusT]:
    """Return the frozen set of legal targets for a status."""

    return _TRANSITIONS.get(type(current), {}).get(current, frozenset())


def validate_transition(current: StatusT, target: StatusT) -> StatusT:
    """Return *target* when legal, otherwise raise InvalidTransitionError."""

    if type(current) is not type(target) or target not in allowed_transitions(current):
        raise InvalidTransitionError(
            type(current).__name__, str(current.value), str(target.value)
        )
    return target


def transition_matrix() -> dict[str, dict[str, list[str]]]:
    """Return the entire frozen state matrix in canonical fixture form."""

    return {
        _MACHINE_NAMES[machine]: {
            current.value: sorted(target.value for target in targets)
            for current, targets in sorted(
                transitions.items(), key=lambda item: item[0].value
            )
        }
        for machine, transitions in sorted(
            _TRANSITIONS.items(), key=lambda item: _MACHINE_NAMES[item[0]]
        )
    }


def machine_name(status: Enum) -> str:
    try:
        return _MACHINE_NAMES[type(status)]
    except KeyError:
        raise ValueError(f"unknown state machine: {type(status).__name__}") from None
