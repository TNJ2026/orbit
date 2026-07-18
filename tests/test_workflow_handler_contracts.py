from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from orbit.workflow.domain.accounting import UsageSnapshot
from orbit.workflow.domain.errors import ErrorCategory
from orbit.workflow.domain.handlers import (
    CancelAck,
    CancelDisposition,
    ExternalEffect,
    HandlerCancelledError,
    HandlerPermanentError,
    HandlerResult,
    HandlerResultStatus,
    HandlerTransientError,
    HandlerValidationIssue,
    HandlerValidationResult,
    NodeHandler,
    PreparedExecution,
    RawHandlerResult,
    RecoveryDisposition,
    RecoveryResult,
    UnknownExternalResultError,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.schemas import SchemaValidationError, validate_contract
from orbit.workflow.domain.serialization import to_primitive
from orbit.workflow.domain.stability import CONTRACT_STABILITY, ContractStability
from orbit.workflow.domain.versions import Revision
from orbit.workflow.testing import assert_reducer_source_is_pure, side_effect_guard
from orbit.workflow.handlers.transform import TransformHandler


NOW = datetime(2026, 7, 17, 0, 0, 1, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "workflow_handlers"
    / "v1"
    / "handler-result.json"
)


def usage(provider_request_id: str | None = "request-001") -> UsageSnapshot:
    return UsageSnapshot(
        EntityId("attempt", "attempt-001"), Revision(1), 10, 4, 0,
        provider_request_id, NOW,
    )


class _ConformingHandler:
    def validate(self, manifest, config):
        return HandlerValidationResult()

    def prepare(self, request, context):
        return PreparedExecution({"input": 1}, "execution-1")

    def execute(self, prepared, context):
        return RawHandlerResult({"answer": 42}, usage())

    def cancel(self, execution_ref, context):
        return CancelAck(CancelDisposition.CONFIRMED_STOPPED)

    def recover(self, recovery_ref, context):
        return RecoveryResult(RecoveryDisposition.NOT_FOUND)

    def normalize_result(self, raw, context):
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED, raw.output, None, raw.usage,
            False, ExternalEffect.KNOWN_APPLIED, "request-001",
        )


class HandlerContractTests(unittest.TestCase):
    def successful_result(self) -> HandlerResult:
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED,
            {"answer": 42},
            None,
            usage(),
            False,
            ExternalEffect.KNOWN_APPLIED,
            "request-001",
        )

    def test_success_result_matches_schema_and_golden(self):
        primitive = to_primitive(self.successful_result())
        self.assertEqual(json.loads(FIXTURE.read_text()), primitive)
        validate_contract(primitive, "handler-result/1.0")

    def test_result_is_deeply_immutable(self):
        result = self.successful_result()
        with self.assertRaises(TypeError):
            result.output["answer"] = 0
        with self.assertRaises(FrozenInstanceError):
            result.status = HandlerResultStatus.FAILED

    def test_status_output_error_and_usage_invariants(self):
        with self.assertRaisesRegex(ValueError, "only succeeded"):
            HandlerResult(
                HandlerResultStatus.FAILED, {"bad": True}, None, usage(),
                False, ExternalEffect.NONE,
            )
        with self.assertRaisesRegex(ValueError, "missing final usage"):
            HandlerResult(
                HandlerResultStatus.SUCCEEDED, {}, None, None, False,
                ExternalEffect.NONE,
            )
        with self.assertRaisesRegex(ValueError, "provider_request_id"):
            HandlerResult(
                HandlerResultStatus.SUCCEEDED, {}, None, usage("one"), False,
                ExternalEffect.NONE, "two",
            )

    def test_typed_failures_map_without_message_matching(self):
        transient = HandlerTransientError("any wording").failure.to_result()
        self.assertIs(HandlerResultStatus.FAILED, transient.status)
        self.assertIs(ErrorCategory.TRANSIENT_ERROR, transient.error.category)

        cancelled = HandlerCancelledError("stop").failure.to_result()
        self.assertIs(HandlerResultStatus.CANCELLED, cancelled.status)
        self.assertIs(ErrorCategory.CANCELLED, cancelled.error.category)

        unknown = UnknownExternalResultError("response lost").failure.to_result()
        self.assertIs(HandlerResultStatus.UNKNOWN_EXTERNAL_RESULT, unknown.status)
        self.assertIs(ExternalEffect.UNKNOWN, unknown.external_effect)

        permanent = HandlerPermanentError("same response lost text").failure.to_result()
        self.assertIs(HandlerResultStatus.FAILED, permanent.status)
        self.assertIs(ExternalEffect.NONE, permanent.external_effect)

    def test_unknown_effect_cannot_be_disguised_as_failure(self):
        with self.assertRaisesRegex(ValueError, "unknown external effect"):
            HandlerResult(
                HandlerResultStatus.FAILED, None,
                HandlerPermanentError("failed").failure.error,
                None, True, ExternalEffect.UNKNOWN,
            )

    def test_validation_and_recovery_contracts_are_explicit(self):
        validation = HandlerValidationResult(
            (HandlerValidationIssue(("config", "model"), "is required"),)
        )
        self.assertFalse(validation.valid)
        self.assertTrue(HandlerValidationResult().valid)
        with self.assertRaisesRegex(ValueError, "only found"):
            RecoveryResult(RecoveryDisposition.FOUND)

    def test_protocol_and_stability_are_exposed(self):
        self.assertIsInstance(_ConformingHandler(), NodeHandler)
        self.assertIs(
            ContractStability.STABLE, CONTRACT_STABILITY["handler_sdk"]
        )
        self.assertIs(
            ContractStability.STABLE, CONTRACT_STABILITY["handler_result"]
        )

    def test_schema_rejects_unknown_status_with_exact_path(self):
        primitive = to_primitive(self.successful_result())
        primitive["status"] = "maybe"
        with self.assertRaises(SchemaValidationError) as caught:
            validate_contract(primitive, "handler-result/1.0")
        self.assertEqual("$.status", caught.exception.json_path)

    def test_pure_lifecycle_phases_are_guarded_against_external_calls(self):
        handler = TransformHandler()
        for method in (handler.validate, handler.prepare, handler.normalize_result):
            assert_reducer_source_is_pure(method)
        with side_effect_guard():
            self.assertTrue(handler.validate(None, {"operation": "identity"}).valid)


if __name__ == "__main__":
    unittest.main()
