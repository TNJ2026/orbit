"""Shared error taxonomy for the Agentic Workflow runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ErrorCategory(str, Enum):
    VALIDATION_ERROR = "validation_error"
    POLICY_REJECTED = "policy_rejected"
    TRANSIENT_ERROR = "transient_error"
    PERMANENT_ERROR = "permanent_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    LOST = "lost"
    UNKNOWN_EXTERNAL_RESULT = "unknown_external_result"


@dataclass(frozen=True)
class FailurePolicy:
    retry: bool
    rework: bool
    human_intervention: bool
    terminate: bool


ERROR_CATEGORY_POLICIES = MappingProxyType(
    {
        ErrorCategory.VALIDATION_ERROR: FailurePolicy(False, False, False, True),
        ErrorCategory.POLICY_REJECTED: FailurePolicy(False, False, True, False),
        ErrorCategory.TRANSIENT_ERROR: FailurePolicy(True, False, False, False),
        ErrorCategory.PERMANENT_ERROR: FailurePolicy(False, True, True, True),
        ErrorCategory.TIMEOUT: FailurePolicy(True, False, True, False),
        ErrorCategory.CANCELLED: FailurePolicy(False, False, False, True),
        ErrorCategory.LOST: FailurePolicy(True, False, True, False),
        ErrorCategory.UNKNOWN_EXTERNAL_RESULT: FailurePolicy(
            False, False, True, False
        ),
    }
)


ERROR_CODE_REGISTRY = MappingProxyType(
    {
        "validation_failed": ErrorCategory.VALIDATION_ERROR,
        "policy_rejected": ErrorCategory.POLICY_REJECTED,
        "handler_transient": ErrorCategory.TRANSIENT_ERROR,
        "handler_permanent": ErrorCategory.PERMANENT_ERROR,
        "attempt_timeout": ErrorCategory.TIMEOUT,
        "operation_cancelled": ErrorCategory.CANCELLED,
        "worker_lost": ErrorCategory.LOST,
        "external_result_unknown": ErrorCategory.UNKNOWN_EXTERNAL_RESULT,
        "data_validation_failed": ErrorCategory.VALIDATION_ERROR,
        "mapping_failed": ErrorCategory.VALIDATION_ERROR,
        "artifact_access_denied": ErrorCategory.POLICY_REJECTED,
        "artifact_integrity_failed": ErrorCategory.PERMANENT_ERROR,
        "graph_contract_invalid": ErrorCategory.VALIDATION_ERROR,
        "graph_stalled": ErrorCategory.PERMANENT_ERROR,
        "join_failed": ErrorCategory.PERMANENT_ERROR,
        "join_deadline_exceeded": ErrorCategory.TIMEOUT,
        "retry_exhausted": ErrorCategory.PERMANENT_ERROR,
        "rework_limit_exceeded": ErrorCategory.PERMANENT_ERROR,
        "loop_limit_exceeded": ErrorCategory.PERMANENT_ERROR,
        "planner_proposal_invalid": ErrorCategory.VALIDATION_ERROR,
        "planner_provider_transient": ErrorCategory.TRANSIENT_ERROR,
        "planner_provider_permanent": ErrorCategory.PERMANENT_ERROR,
        "planner_attempt_timeout": ErrorCategory.TIMEOUT,
        "planner_result_unknown": ErrorCategory.UNKNOWN_EXTERNAL_RESULT,
        "plan_patch_invalid": ErrorCategory.VALIDATION_ERROR,
        "plan_version_conflict": ErrorCategory.TRANSIENT_ERROR,
        "approval_required": ErrorCategory.POLICY_REJECTED,
        "human_submission_invalid": ErrorCategory.VALIDATION_ERROR,
        "budget_exhausted": ErrorCategory.POLICY_REJECTED,
        "foreach_item_failed": ErrorCategory.PERMANENT_ERROR,
        "subflow_failed": ErrorCategory.PERMANENT_ERROR,
        "capability_denied": ErrorCategory.POLICY_REJECTED,
        "sandbox_limit_exceeded": ErrorCategory.PERMANENT_ERROR,
        "recovery_manual_required": ErrorCategory.UNKNOWN_EXTERNAL_RESULT,
    }
)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    category: ErrorCategory
    message: str
    source: str = "domain"
    details: Mapping[str, Any] = field(default_factory=dict)
    cause: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("error code is required")
        if not self.message.strip():
            raise ValueError("error message is required")
        registered = ERROR_CODE_REGISTRY.get(self.code)
        if registered is None:
            raise ValueError(f"unregistered error code: {self.code}")
        if registered is not self.category:
            raise ValueError(
                f"error code {self.code!r} belongs to {registered.value}, "
                f"not {self.category.value}"
            )
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def retryable(self) -> bool:
        return ERROR_CATEGORY_POLICIES[self.category].retry


class InvalidTransitionError(ValueError):
    def __init__(self, machine: str, current: str, target: str) -> None:
        self.machine = machine
        self.current = current
        self.target = target
        super().__init__(f"invalid {machine} transition: {current} -> {target}")


class LeaseAuthorityError(ValueError):
    """The supplied lease credential/fence no longer owns the work."""
