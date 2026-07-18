"""Programmable Handler used by Worker, fault and Planner tests."""

from __future__ import annotations

from ..domain.handlers import (
    CancelAck, CancelDisposition, HandlerResult, HandlerValidationResult,
    PreparedExecution, RawHandlerResult, RecoveryDisposition, RecoveryResult,
)


class FakeHandler:
    def __init__(self, *, raw=None, result: HandlerResult | None = None, error=None) -> None:
        self.raw = raw or RawHandlerResult({}, None)
        self.result = result
        self.error = error
        self.calls = []

    def validate(self, manifest, config): return HandlerValidationResult()
    def prepare(self, request, context):
        self.calls.append("prepare")
        return PreparedExecution({"input": request.input}, f"fake:{request.attempt_id}")
    def execute(self, prepared, context):
        self.calls.append("execute")
        if self.error is not None: raise self.error
        return self.raw
    def normalize_result(self, raw, context):
        self.calls.append("normalize")
        if self.result is None: raise RuntimeError("FakeHandler result was not configured")
        return self.result
    def cancel(self, execution_ref, context):
        self.calls.append("cancel")
        return CancelAck(CancelDisposition.CONFIRMED_STOPPED)
    def recover(self, recovery_ref, context):
        self.calls.append("recover")
        return RecoveryResult(RecoveryDisposition.NOT_FOUND)
