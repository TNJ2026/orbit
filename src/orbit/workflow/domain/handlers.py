"""Stable Handler SDK contracts for executable workflow nodes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from .accounting import UsageSnapshot
from .errors import ErrorCategory, ErrorInfo
from .ids import EntityId
from .serialization import freeze_json


MAX_HANDLER_DURATION_SECONDS = 86_400


@dataclass(frozen=True)
class ResourceProfile:
    max_input_tokens: int
    max_output_tokens: int
    max_tool_calls: int
    max_duration_seconds: int
    max_cost_microunits: int
    cost_class: str

    def __post_init__(self) -> None:
        for field in (
            "max_input_tokens", "max_output_tokens", "max_tool_calls",
            "max_duration_seconds", "max_cost_microunits",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if self.max_duration_seconds < 1:
            raise ValueError("max_duration_seconds must be positive")
        if self.max_duration_seconds > MAX_HANDLER_DURATION_SECONDS:
            raise ValueError("max_duration_seconds exceeds system hard limit")
        if not self.cost_class.strip():
            raise ValueError("cost_class is required")


class HandlerResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN_EXTERNAL_RESULT = "unknown_external_result"


class ExternalEffect(str, Enum):
    NONE = "none"
    KNOWN_APPLIED = "known_applied"
    UNKNOWN = "unknown"


class CancelDisposition(str, Enum):
    CONFIRMED_STOPPED = "confirmed_stopped"
    UNKNOWN = "unknown"
    NOT_SUPPORTED = "not_supported"


class RecoveryDisposition(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HandlerValidationIssue:
    path: tuple[str | int, ...]
    message: str
    code: str = "validation_failed"

    def __post_init__(self) -> None:
        if not self.message.strip() or not self.code.strip():
            raise ValueError("validation issue code and message are required")
        if any(not isinstance(item, (str, int)) for item in self.path):
            raise TypeError("validation issue path must contain strings or integers")
        object.__setattr__(self, "path", tuple(self.path))


@dataclass(frozen=True)
class HandlerValidationResult:
    issues: tuple[HandlerValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class PreparedExecution:
    payload: Any
    execution_ref: str | None = None

    def __post_init__(self) -> None:
        if self.execution_ref is not None and not self.execution_ref.strip():
            raise ValueError("execution_ref cannot be empty")
        object.__setattr__(self, "payload", freeze_json(self.payload))


@dataclass(frozen=True)
class RawHandlerResult:
    output: Any
    usage: UsageSnapshot | None
    provider_request_id: str | None = None
    external_effect: ExternalEffect = ExternalEffect.NONE
    # Artifacts the Handler staged for its output ports. Carried here so a
    # Handler that writes to CAS in `execute` can hand the ids to
    # `normalize_result`, which turns them into the HandlerResult refs the
    # kernel commits. Empty for the inline path.
    artifact_refs: tuple[EntityId, ...] = ()

    def __post_init__(self) -> None:
        if self.provider_request_id is not None and not self.provider_request_id.strip():
            raise ValueError("provider_request_id cannot be empty")
        if (
            self.usage is not None
            and self.provider_request_id is not None
            and self.usage.provider_request_id not in {None, self.provider_request_id}
        ):
            raise ValueError("usage provider_request_id does not match raw result")
        for reference in self.artifact_refs:
            if not isinstance(reference, EntityId) or reference.kind != "artifact":
                raise ValueError("artifact_refs must contain artifact ids")
        object.__setattr__(self, "output", freeze_json(self.output))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))


@dataclass(frozen=True)
class HandlerResult:
    status: HandlerResultStatus
    output: Mapping[str, Any] | None
    error: ErrorInfo | None
    usage: UsageSnapshot | None
    usage_incomplete: bool
    external_effect: ExternalEffect
    provider_request_id: str | None = None
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    artifact_refs: tuple[EntityId, ...] = ()

    def __post_init__(self) -> None:
        succeeded = self.status is HandlerResultStatus.SUCCEEDED
        if succeeded != (self.output is not None):
            raise ValueError("only succeeded HandlerResult may contain output")
        if succeeded == (self.error is not None):
            raise ValueError("non-success HandlerResult requires exactly one error")
        if self.usage is None and not self.usage_incomplete:
            raise ValueError("missing final usage must be marked usage_incomplete")
        if self.provider_request_id is not None and not self.provider_request_id.strip():
            raise ValueError("provider_request_id cannot be empty")
        if (
            self.usage is not None
            and self.provider_request_id is not None
            and self.usage.provider_request_id not in {None, self.provider_request_id}
        ):
            raise ValueError("usage provider_request_id does not match HandlerResult")
        if self.status is HandlerResultStatus.UNKNOWN_EXTERNAL_RESULT:
            if self.external_effect is not ExternalEffect.UNKNOWN:
                raise ValueError("unknown result requires unknown external effect")
            if self.error is None or self.error.category is not ErrorCategory.UNKNOWN_EXTERNAL_RESULT:
                raise ValueError("unknown result requires unknown_external_result error")
        elif self.external_effect is ExternalEffect.UNKNOWN:
            raise ValueError("unknown external effect requires unknown result status")
        if self.status is HandlerResultStatus.CANCELLED:
            if self.error is None or self.error.category is not ErrorCategory.CANCELLED:
                raise ValueError("cancelled result requires cancelled error")
        elif self.error is not None and self.error.category is ErrorCategory.CANCELLED:
            raise ValueError("cancelled error requires cancelled result status")
        if self.status is HandlerResultStatus.FAILED and self.error is not None:
            if self.error.category is ErrorCategory.UNKNOWN_EXTERNAL_RESULT:
                raise ValueError("unknown error cannot be represented as failed")
        for reference in self.artifact_refs:
            if reference.kind != "artifact":
                raise ValueError("artifact_refs must contain artifact ids")
        object.__setattr__(self, "output", None if self.output is None else freeze_json(self.output))
        object.__setattr__(
            self, "diagnostics", tuple(freeze_json(item) for item in self.diagnostics)
        )
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))


@dataclass(frozen=True)
class HandlerFailure:
    error: ErrorInfo
    external_effect: ExternalEffect = ExternalEffect.NONE
    usage: UsageSnapshot | None = None
    usage_incomplete: bool = True
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if self.external_effect is ExternalEffect.UNKNOWN and (
            self.error.category is not ErrorCategory.UNKNOWN_EXTERNAL_RESULT
        ):
            raise ValueError("unknown effect requires unknown_external_result error")
        if self.error.category is ErrorCategory.UNKNOWN_EXTERNAL_RESULT and (
            self.external_effect is not ExternalEffect.UNKNOWN
        ):
            raise ValueError("unknown_external_result error requires unknown effect")

    def to_result(self) -> HandlerResult:
        if self.error.category is ErrorCategory.UNKNOWN_EXTERNAL_RESULT:
            status = HandlerResultStatus.UNKNOWN_EXTERNAL_RESULT
        elif self.error.category is ErrorCategory.CANCELLED:
            status = HandlerResultStatus.CANCELLED
        else:
            status = HandlerResultStatus.FAILED
        return HandlerResult(
            status, None, self.error, self.usage, self.usage_incomplete,
            self.external_effect, self.provider_request_id,
        )


@dataclass(frozen=True)
class CancelAck:
    disposition: CancelDisposition
    message: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    disposition: RecoveryDisposition
    result: HandlerResult | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if (self.disposition is RecoveryDisposition.FOUND) != (self.result is not None):
            raise ValueError("only found recovery result may contain HandlerResult")
        if self.provider_request_id is not None and not self.provider_request_id.strip():
            raise ValueError("provider_request_id cannot be empty")


@runtime_checkable
class NodeHandler(Protocol):
    def validate(
        self, manifest: object, config: Mapping[str, Any]
    ) -> HandlerValidationResult: ...

    def prepare(
        self, request: object, context: object
    ) -> PreparedExecution: ...

    def execute(
        self, prepared: PreparedExecution, context: object
    ) -> RawHandlerResult: ...

    def cancel(self, execution_ref: str, context: object) -> CancelAck: ...

    def recover(self, recovery_ref: str, context: object) -> RecoveryResult: ...

    def normalize_result(
        self, raw: RawHandlerResult, context: object
    ) -> HandlerResult: ...


class HandlerExecutionError(Exception):
    """Typed execution exception; Worker mapping never inspects its message."""

    code = "handler_permanent"
    category = ErrorCategory.PERMANENT_ERROR
    default_effect = ExternalEffect.NONE

    def __init__(
        self,
        message: str,
        *,
        source: str = "handler",
        details: Mapping[str, Any] | None = None,
        cause: str | None = None,
        usage: UsageSnapshot | None = None,
        usage_incomplete: bool = True,
        provider_request_id: str | None = None,
        external_effect: ExternalEffect | None = None,
    ) -> None:
        if not message.strip():
            raise ValueError("handler error message is required")
        effect = self.default_effect if external_effect is None else external_effect
        self.failure = HandlerFailure(
            ErrorInfo(
                self.code, self.category, message, source,
                MappingProxyType(dict(details or {})), cause,
            ),
            effect, usage, usage_incomplete, provider_request_id,
        )
        super().__init__(message)


class HandlerValidationError(HandlerExecutionError):
    code = "validation_failed"
    category = ErrorCategory.VALIDATION_ERROR


class HandlerTransientError(HandlerExecutionError):
    code = "handler_transient"
    category = ErrorCategory.TRANSIENT_ERROR


class HandlerPermanentError(HandlerExecutionError):
    pass


class HandlerPolicyRejectedError(HandlerExecutionError):
    code = "policy_rejected"
    category = ErrorCategory.POLICY_REJECTED


class HandlerCancelledError(HandlerExecutionError):
    code = "operation_cancelled"
    category = ErrorCategory.CANCELLED


class HandlerTimeoutError(HandlerExecutionError):
    code = "attempt_timeout"
    category = ErrorCategory.TIMEOUT


class UnknownExternalResultError(HandlerExecutionError):
    code = "external_result_unknown"
    category = ErrorCategory.UNKNOWN_EXTERNAL_RESULT
    default_effect = ExternalEffect.UNKNOWN
