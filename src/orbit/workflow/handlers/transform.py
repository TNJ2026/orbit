"""Deterministic built-in JSON transforms; no eval or Mapping DSL."""

from __future__ import annotations

from datetime import timezone
from collections.abc import Mapping

from ..domain.accounting import UsageSnapshot
from ..domain.handlers import (
    CancelAck, CancelDisposition, ExternalEffect, HandlerResult,
    HandlerResultStatus, HandlerValidationIssue, HandlerValidationResult,
    PreparedExecution, RawHandlerResult, RecoveryDisposition, RecoveryResult,
)
from ..domain.versions import Revision


class TransformHandler:
    OPERATIONS = frozenset({"identity", "select_fields", "build_object"})

    def validate(self, manifest, config):
        operation = config.get("operation", "identity")
        issues = []
        if operation not in self.OPERATIONS:
            issues.append(HandlerValidationIssue(("operation",), "unsupported transform operation"))
        if operation == "select_fields" and not isinstance(config.get("fields"), (list, tuple)):
            issues.append(HandlerValidationIssue(("fields",), "fields must be an array"))
        if operation == "build_object" and not isinstance(config.get("value"), Mapping):
            issues.append(HandlerValidationIssue(("value",), "value must be an object"))
        return HandlerValidationResult(tuple(issues))

    def prepare(self, request, context):
        return PreparedExecution(
            {"input": request.input, "config": request.config},
            f"transform:{request.attempt_id}",
        )

    def execute(self, prepared, context):
        value = prepared.payload["input"]
        config = prepared.payload["config"]
        operation = config.get("operation", "identity")
        if operation == "identity":
            output = dict(value)
        elif operation == "select_fields":
            output = {key: value[key] for key in config["fields"] if key in value}
        else:
            output = dict(config["value"])
        snapshot = UsageSnapshot(
            context.request.attempt_id, Revision(1), 0, 0, 0, None,
            context.clock().astimezone(timezone.utc),
        )
        return RawHandlerResult(output, snapshot, external_effect=ExternalEffect.NONE)

    def normalize_result(self, raw, context):
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED, raw.output, None, raw.usage, False,
            raw.external_effect, raw.provider_request_id,
        )

    def cancel(self, execution_ref, context):
        return CancelAck(CancelDisposition.CONFIRMED_STOPPED)

    def recover(self, recovery_ref, context):
        return RecoveryResult(RecoveryDisposition.NOT_FOUND)
