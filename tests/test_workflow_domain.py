from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from orbit.workflow.domain.accounting import (
    BudgetAccount,
    BudgetReservation,
    UsageSnapshot,
)
from orbit.workflow.domain.concurrency import (
    CommandDisposition,
    ConcurrencyConflictError,
    IdempotencyConflictError,
    ProcessedCommand,
    command_fingerprint,
    evaluate_command,
)
from orbit.workflow.domain.envelopes import CommandEnvelope, EventEnvelope
from orbit.workflow.domain.errors import (
    ERROR_CATEGORY_POLICIES,
    ERROR_CODE_REGISTRY,
    ErrorCategory,
    ErrorInfo,
    InvalidTransitionError,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.models import (
    AttemptRef,
    ArtifactRef,
    ExecutionPlanRef,
    NodeRunRef,
    Value,
    WorkflowRunRef,
    WorkflowVersionRef,
)
from orbit.workflow.domain.replay import replay_events
from orbit.workflow.domain.schemas import (
    SchemaValidationError,
    schema_for,
    validate_contract,
)
from orbit.workflow.domain.serialization import (
    canonical_json,
    definition_hash,
    freeze_json,
    to_primitive,
)
from orbit.workflow.domain.stability import CONTRACT_STABILITY
from orbit.workflow.domain.states import (
    AttemptStatus,
    BranchTokenStatus,
    HumanTaskStatus,
    JobStatus,
    LeaseStatus,
    NodeRunStatus,
    TimerStatus,
    WorkflowRunStatus,
    allowed_transitions,
    transition_matrix,
    validate_transition,
)
from orbit.workflow.domain.transitions import transition_contract
from orbit.workflow.domain.upcasting import UpcasterRegistry, with_payload
from orbit.workflow.domain.versions import (
    AggregateVersion,
    DefinitionHash,
    Revision,
    SchemaVersion,
)
from orbit.workflow.testing import SideEffectDetected, guarded_replay


FIXTURES = Path(__file__).parent / "fixtures" / "workflow_contracts" / "v1"
UTC = timezone.utc


def event_from_dict(value):
    return EventEnvelope(
        event_id=EntityId.parse(value["event_id"]),
        event_type=value["event_type"],
        event_version=Revision(value["event_version"]),
        aggregate_id=EntityId.parse(value["aggregate_id"]),
        sequence=Revision(value["sequence"]),
        correlation_id=EntityId.parse(value["correlation_id"]),
        causation_id=EntityId.parse(value["causation_id"]),
        occurred_at=datetime.fromisoformat(value["occurred_at"].replace("Z", "+00:00")),
        payload=value["payload"],
    )


class IdentifierAndVersionTests(unittest.TestCase):
    def test_identifier_round_trip(self):
        identifier = EntityId("node_run", "nr-001")
        self.assertEqual(identifier, EntityId.parse(str(identifier)))
        self.assertEqual("node_run:nr-001", str(identifier))

    def test_identifier_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            EntityId("Bad Kind", "x")
        with self.assertRaises(ValueError):
            EntityId.parse("missing-prefix")

    def test_revision_and_schema_version(self):
        self.assertEqual(Revision(2), Revision(1).next())
        self.assertEqual(AggregateVersion(1), AggregateVersion(0).next())
        self.assertEqual("1.0", SchemaVersion().value)
        with self.assertRaises(ValueError):
            Revision(0)
        with self.assertRaises(ValueError):
            SchemaVersion("draft")


class SerializationTests(unittest.TestCase):
    def test_canonical_json_and_definition_hash_are_order_independent(self):
        first = {"b": [2, 3], "a": {"x": 1}}
        second = {"a": {"x": 1}, "b": [2, 3]}
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(definition_hash(first), definition_hash(second))

    def test_canonical_json_and_hash_match_golden_fixture(self):
        fixture = json.loads((FIXTURES / "definition-hash.json").read_text())
        self.assertEqual(fixture["canonical_json"], canonical_json(fixture["definition"]))
        self.assertEqual(
            fixture["definition_hash"], definition_hash(fixture["definition"]).value
        )

    def test_non_finite_numbers_and_naive_datetimes_are_rejected(self):
        with self.assertRaises(ValueError):
            freeze_json({"bad": float("nan")})
        with self.assertRaises(ValueError):
            canonical_json({"when": datetime(2026, 7, 17)})

    def test_frozen_json_is_deeply_immutable(self):
        value = freeze_json({"nested": {"items": [1, 2]}})
        with self.assertRaises(TypeError):
            value["new"] = 1
        with self.assertRaises(TypeError):
            value["nested"]["items"] += (3,)


class EnvelopeContractTests(unittest.TestCase):
    def command(self) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=EntityId("command", "cmd-001"),
            command_type="start_run",
            aggregate_id=EntityId("run", "run-001"),
            correlation_id=EntityId("run", "run-001"),
            expected_version=AggregateVersion(0),
            idempotency_key="start-run-001",
            actor="test-suite",
            issued_at=datetime(2026, 7, 17, tzinfo=UTC),
            payload={"workflow_version": 1},
        )

    def event(self, sequence: int = 1) -> EventEnvelope:
        return EventEnvelope(
            event_id=EntityId("event", f"event-{sequence:03d}"),
            event_type="run_started",
            event_version=Revision(1),
            aggregate_id=EntityId("run", "run-001"),
            sequence=Revision(sequence),
            correlation_id=EntityId("run", "run-001"),
            causation_id=EntityId("command", "cmd-001"),
            occurred_at=datetime(2026, 7, 17, 0, 0, sequence, tzinfo=UTC),
            payload={"status": "running"},
        )

    def test_command_matches_golden_fixture(self):
        expected = json.loads((FIXTURES / "command-envelope.json").read_text())
        self.assertEqual(expected, to_primitive(self.command()))

    def test_event_matches_golden_fixture(self):
        expected = json.loads((FIXTURES / "event-envelope.json").read_text())
        self.assertEqual(expected, to_primitive(self.event()))

    def test_payload_is_immutable_and_datetime_must_be_aware(self):
        command = self.command()
        with self.assertRaises(TypeError):
            command.payload["x"] = 1
        with self.assertRaises(ValueError):
            CommandEnvelope(
                command_id=EntityId("command", "cmd-002"),
                command_type="start_run",
                aggregate_id=EntityId("run", "run-001"),
                correlation_id=EntityId("run", "run-001"),
                expected_version=AggregateVersion(0),
                idempotency_key="key",
                actor="test-suite",
                issued_at=datetime(2026, 7, 17),
            )

    def test_schema_registry_exposes_versioned_envelopes(self):
        self.assertIn("command_id", schema_for("command-envelope/1.0")["required"])
        self.assertIn("event_version", schema_for("event-envelope/1.0")["required"])
        with self.assertRaises(KeyError):
            schema_for("missing/1.0")
        with self.assertRaises(TypeError):
            schema_for("command-envelope/1.0")["properties"]["actor"][
                "minLength"
            ] = 0

    def test_schema_validation_rejects_with_exact_field_path(self):
        command = to_primitive(self.command())
        validate_contract(command, "command-envelope/1.0")
        command["unexpected"] = True
        with self.assertRaises(SchemaValidationError) as raised:
            validate_contract(command, "command-envelope/1.0")
        self.assertEqual(("unexpected",), raised.exception.path)
        self.assertEqual("$.unexpected", raised.exception.json_path)

        del command["unexpected"]
        command["aggregate_id"] = "invalid"
        with self.assertRaises(SchemaValidationError) as raised:
            validate_contract(command, "command-envelope/1.0")
        self.assertEqual("$.aggregate_id", raised.exception.json_path)


class StableSchemaTests(unittest.TestCase):
    def test_all_frozen_and_stable_cross_module_objects_validate(self):
        workflow_version = WorkflowVersionRef(
            EntityId("workflow", "wf-schema"),
            Revision(1),
            DefinitionHash("sha256:" + "c" * 64),
        )
        run = WorkflowRunRef(EntityId("run", "run-schema"), workflow_version)
        plan = ExecutionPlanRef(
            EntityId("plan", "plan-schema"),
            run.run_id,
            Revision(1),
            workflow_version,
        )
        node_run = NodeRunRef(
            EntityId("node_run", "nr-schema"),
            run.run_id,
            Revision(1),
            "analyze",
        )
        attempt = AttemptRef(
            EntityId("attempt", "attempt-schema"),
            node_run.node_run_id,
            Revision(1),
        )
        objects = {
            "workflow-version-ref/1.0": workflow_version,
            "workflow-run-ref/1.0": run,
            "execution-plan-ref/1.0": plan,
            "node-run-ref/1.0": node_run,
            "attempt-ref/1.0": attempt,
            "error-info/1.0": ErrorInfo(
                "worker_lost", ErrorCategory.LOST, "worker lost"
            ),
            "usage-snapshot/1.0": UsageSnapshot(
                attempt.attempt_id,
                Revision(1),
                10,
                5,
                1,
                "provider-request",
                datetime(2026, 7, 17, tzinfo=UTC),
            ),
            "budget-reservation/1.0": BudgetReservation(
                EntityId("reservation", "reservation-schema"), run.run_id, 500
            ),
            "budget-account/1.0": BudgetAccount(run.run_id, 1_000),
            "value/1.0": Value("score", "score/1.0", {"value": 0.8}),
            "artifact-ref/1.0": ArtifactRef(
                EntityId("artifact", "artifact-schema"),
                "report/1.0",
                "text/markdown",
                DefinitionHash("sha256:" + "d" * 64),
                100,
            ),
        }
        for contract, value in objects.items():
            with self.subTest(contract):
                validate_contract(to_primitive(value), contract)


class StateMachineTests(unittest.TestCase):
    def test_entire_transition_matrix_matches_golden_fixture(self):
        expected = json.loads((FIXTURES / "transition-matrix.json").read_text())
        self.assertEqual(expected, transition_matrix())

    def test_workflow_budget_path_and_terminal_states(self):
        self.assertEqual(
            WorkflowRunStatus.BUDGET_EXHAUSTED,
            validate_transition(
                WorkflowRunStatus.RUNNING, WorkflowRunStatus.BUDGET_EXHAUSTED
            ),
        )
        validate_transition(
            WorkflowRunStatus.BUDGET_EXHAUSTED,
            WorkflowRunStatus.WAITING_FOR_BUDGET,
        )
        validate_transition(
            WorkflowRunStatus.WAITING_FOR_BUDGET, WorkflowRunStatus.RUNNING
        )
        self.assertEqual(frozenset(), allowed_transitions(WorkflowRunStatus.SUCCEEDED))

    def test_retry_is_attempt_level_and_rework_is_not_a_status(self):
        validate_transition(AttemptStatus.CREATED, AttemptStatus.LEASED)
        validate_transition(AttemptStatus.LEASED, AttemptStatus.RUNNING)
        validate_transition(AttemptStatus.RUNNING, AttemptStatus.FAILED)
        self.assertNotIn("rework", {status.value for status in NodeRunStatus})
        self.assertEqual(
            frozenset(),
            allowed_transitions(AttemptStatus.UNKNOWN_EXTERNAL_RESULT),
        )

    def test_invalid_and_cross_machine_transitions_are_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            validate_transition(WorkflowRunStatus.CREATED, WorkflowRunStatus.SUCCEEDED)
        with self.assertRaises(InvalidTransitionError):
            validate_transition(WorkflowRunStatus.RUNNING, NodeRunStatus.RUNNING)

    def test_human_and_branch_tokens_have_explicit_terminal_states(self):
        validate_transition(HumanTaskStatus.WAITING, HumanTaskStatus.COMPLETED)
        validate_transition(BranchTokenStatus.ACTIVE, BranchTokenStatus.NOT_SELECTED)

    def test_every_legal_transition_has_command_event_and_idempotency_contract(self):
        status_types = (
            WorkflowRunStatus,
            NodeRunStatus,
            AttemptStatus,
            JobStatus,
            LeaseStatus,
            TimerStatus,
            HumanTaskStatus,
            BranchTokenStatus,
        )
        seen = 0
        for status_type in status_types:
            for current in status_type:
                for target in allowed_transitions(current):
                    contract = transition_contract(current, target)
                    self.assertTrue(contract.command_type.startswith("transition_"))
                    self.assertTrue(contract.event_type.endswith("_transitioned"))
                    self.assertIn("expected_version", contract.precondition)
                    self.assertEqual(
                        "aggregate_id + idempotency_key", contract.idempotency_scope
                    )
                    seen += 1
        self.assertGreater(seen, 20)


class ModelSpineTests(unittest.TestCase):
    def test_refs_bind_run_plan_node_and_attempt(self):
        workflow_version = WorkflowVersionRef(
            workflow_id=EntityId("workflow", "wf-001"),
            version=Revision(1),
            definition_hash=DefinitionHash("sha256:" + "a" * 64),
        )
        run = WorkflowRunRef(EntityId("run", "run-001"), workflow_version)
        plan = ExecutionPlanRef(
            EntityId("plan", "plan-001"),
            run.run_id,
            Revision(1),
            workflow_version,
        )
        node_run = NodeRunRef(
            EntityId("node_run", "nr-001"),
            run.run_id,
            plan.plan_version,
            "analyze",
        )
        attempt = AttemptRef(
            EntityId("attempt", "attempt-001"), node_run.node_run_id, Revision(1)
        )
        self.assertEqual(run.run_id, plan.run_id)
        self.assertEqual(plan.plan_version, node_run.plan_version)
        self.assertEqual(node_run.node_run_id, attempt.node_run_id)

    def test_refs_are_frozen_and_validate_id_kind(self):
        version = WorkflowVersionRef(
            EntityId("workflow", "wf-001"),
            Revision(1),
            DefinitionHash("sha256:" + "b" * 64),
        )
        with self.assertRaises(FrozenInstanceError):
            version.version = Revision(2)
        with self.assertRaises(ValueError):
            WorkflowRunRef(EntityId("workflow", "wrong"), version)


class ConcurrencyContractTests(unittest.TestCase):
    def command(self, expected_version: int, payload=None):
        return CommandEnvelope(
            command_id=EntityId("command", "cmd-vector"),
            command_type="start_run",
            aggregate_id=EntityId("run", "run-vector"),
            correlation_id=EntityId("run", "run-vector"),
            expected_version=AggregateVersion(expected_version),
            idempotency_key="vector-key",
            actor="test-suite",
            issued_at=datetime(2026, 7, 17, tzinfo=UTC),
            payload=payload or {"value": 1},
        )

    def test_idempotency_and_expected_version_golden_vectors(self):
        vectors = json.loads((FIXTURES / "idempotency-vectors.json").read_text())
        for vector in vectors:
            with self.subTest(vector["name"]):
                command = self.command(vector["expected_version"])
                processed = {}
                if vector["processed"] == "same":
                    processed[command.idempotency_key] = ProcessedCommand(
                        command.idempotency_key,
                        command_fingerprint(command),
                        (EntityId("event", "prior-001"),),
                    )
                elif vector["processed"] == "different":
                    other = self.command(vector["expected_version"], {"value": 2})
                    processed[command.idempotency_key] = ProcessedCommand(
                        command.idempotency_key,
                        command_fingerprint(other),
                        (EntityId("event", "prior-001"),),
                    )

                if vector.get("error") == "concurrency_conflict":
                    with self.assertRaises(ConcurrencyConflictError):
                        evaluate_command(
                            AggregateVersion(vector["current_version"]),
                            command,
                            processed,
                        )
                elif vector.get("error") == "idempotency_conflict":
                    with self.assertRaises(IdempotencyConflictError):
                        evaluate_command(
                            AggregateVersion(vector["current_version"]),
                            command,
                            processed,
                        )
                else:
                    decision = evaluate_command(
                        AggregateVersion(vector["current_version"]), command, processed
                    )
                    self.assertEqual(vector["result"], decision.disposition.value)

    def test_duplicate_command_replays_original_event_ids(self):
        command = self.command(0)
        event_id = EntityId("event", "prior-001")
        processed = {
            command.idempotency_key: ProcessedCommand(
                command.idempotency_key,
                command_fingerprint(command),
                (event_id,),
            )
        }
        decision = evaluate_command(AggregateVersion(1), command, processed)
        self.assertEqual(CommandDisposition.REPLAY_PRIOR_RESULT, decision.disposition)
        self.assertEqual((event_id,), decision.prior_event_ids)


class UpcasterAndFlowTests(unittest.TestCase):
    def test_upcaster_fixture_and_identity_invariants(self):
        original_data = json.loads((FIXTURES / "upcaster-v1-event.json").read_text())
        expected_data = json.loads((FIXTURES / "upcaster-v2-event.json").read_text())
        original = event_from_dict(original_data)

        registry = UpcasterRegistry()

        def v1_to_v2(event):
            self.assertEqual("done", event.payload["status"])
            return with_payload(
                event,
                {"status": "succeeded", "result": None},
                version=2,
            )

        registry.register("run_finished", 1, v1_to_v2)
        upgraded = registry.upcast(original, 2)
        self.assertEqual(expected_data, to_primitive(upgraded))
        self.assertEqual(original.event_id, upgraded.event_id)
        self.assertEqual(original.sequence, upgraded.sequence)

    def test_linear_flow_keeps_root_run_correlation_across_aggregates(self):
        values = json.loads((FIXTURES / "linear-event-flow.json").read_text())
        events = [event_from_dict(value) for value in values]
        self.assertEqual(
            {EntityId("run", "run-001")}, {event.correlation_id for event in events}
        )
        self.assertEqual(
            {"run", "node_run", "attempt"},
            {event.aggregate_id.kind for event in events},
        )

    def test_event_from_command_inherits_explicit_root_correlation(self):
        command = CommandEnvelope(
            command_id=EntityId("command", "cmd-node"),
            command_type="schedule_node",
            aggregate_id=EntityId("node_run", "nr-001"),
            correlation_id=EntityId("run", "run-001"),
            expected_version=AggregateVersion(0),
            idempotency_key="schedule-node-001",
            actor="kernel",
            issued_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
        event = EventEnvelope.from_command(
            command,
            event_type="node_run_created",
            sequence=Revision(1),
        )
        self.assertEqual(EntityId("run", "run-001"), event.correlation_id)
        self.assertEqual(command.command_id, event.causation_id)


class ReplayAndAccountingTests(unittest.TestCase):
    def event(self, sequence: int, value: int) -> EventEnvelope:
        return EventEnvelope(
            event_id=EntityId("event", f"e-{sequence}"),
            event_type="value_added",
            event_version=Revision(1),
            aggregate_id=EntityId("run", "run-001"),
            sequence=Revision(sequence),
            correlation_id=EntityId("run", "run-001"),
            causation_id=EntityId("command", f"c-{sequence}"),
            occurred_at=datetime(2026, 7, 17, 0, 0, sequence, tzinfo=UTC),
            payload={"value": value},
        )

    def test_replay_is_deterministic_and_rejects_bad_order(self):
        def reducer(state, event):
            return state + event.payload["value"]

        events = [self.event(1, 2), self.event(2, 3)]
        self.assertEqual(5, guarded_replay(0, events, reducer))
        self.assertEqual(5, guarded_replay(0, events, reducer))
        with self.assertRaises(ValueError):
            replay_events(0, reversed(events), reducer)

    def test_replay_guard_actively_rejects_external_side_effects(self):
        events = [self.event(1, 1)]

        def file_reducer(state, event):
            open("/tmp/workflow-reducer-must-not-write", "w")
            return state

        def clock_reducer(state, event):
            return datetime.now(tz=UTC)

        with self.assertRaises(SideEffectDetected):
            guarded_replay(0, events, file_reducer)
        with self.assertRaises(SideEffectDetected):
            guarded_replay(0, events, clock_reducer)

    def test_budget_reserve_settle_and_release_are_immutable(self):
        account = BudgetAccount(EntityId("run", "run-001"), 1_000)
        reserved = account.reserve(600)
        self.assertEqual(1_000, account.remaining_microunits)
        self.assertEqual(400, reserved.remaining_microunits)
        settled = reserved.settle(600, 450)
        self.assertEqual(450, settled.consumed_microunits)
        self.assertEqual(550, settled.remaining_microunits)
        released = reserved.release(100)
        self.assertEqual(500, released.reserved_microunits)
        with self.assertRaises(ValueError):
            reserved.reserve(401)

    def test_budget_settlement_records_real_overspend(self):
        account = BudgetAccount(EntityId("run", "run-001"), 1_000).reserve(600)
        settled = account.settle(600, 1_200)
        self.assertEqual(1_200, settled.consumed_microunits)
        self.assertEqual(-200, settled.remaining_microunits)
        self.assertTrue(settled.is_exhausted)
        with self.assertRaises(ValueError):
            settled.reserve(1)

    def test_usage_snapshot_requires_cumulative_non_negative_values(self):
        snapshot = UsageSnapshot(
            attempt_id=EntityId("attempt", "attempt-001"),
            sequence=Revision(1),
            input_tokens=10,
            output_tokens=5,
            tool_calls=1,
            provider_request_id="request-1",
            observed_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
        self.assertEqual(15, snapshot.input_tokens + snapshot.output_tokens)
        with self.assertRaises(ValueError):
            UsageSnapshot(
                attempt_id=EntityId("attempt", "attempt-001"),
                sequence=Revision(1),
                input_tokens=-1,
                output_tokens=0,
                tool_calls=0,
                provider_request_id=None,
                observed_at=datetime(2026, 7, 17, tzinfo=UTC),
            )

    def test_contract_stability_is_explicit(self):
        expected = json.loads((FIXTURES / "contract-stability.json").read_text())
        actual = {name: level.value for name, level in CONTRACT_STABILITY.items()}
        self.assertEqual(expected, actual)


class ErrorContractTests(unittest.TestCase):
    def test_error_info_is_immutable(self):
        error = ErrorInfo(
            code="worker_lost",
            category=ErrorCategory.LOST,
            message="worker lease expired",
            details={"worker": "w-1"},
        )
        self.assertTrue(error.retryable)
        with self.assertRaises(TypeError):
            error.details["worker"] = "w-2"

    def test_error_codes_and_failure_policies_are_registry_controlled(self):
        self.assertEqual(ErrorCategory.LOST, ERROR_CODE_REGISTRY["worker_lost"])
        self.assertTrue(ERROR_CATEGORY_POLICIES[ErrorCategory.TRANSIENT_ERROR].retry)
        self.assertTrue(
            ERROR_CATEGORY_POLICIES[
                ErrorCategory.UNKNOWN_EXTERNAL_RESULT
            ].human_intervention
        )
        with self.assertRaises(ValueError):
            ErrorInfo("free_form", ErrorCategory.LOST, "not registered")
        with self.assertRaises(ValueError):
            ErrorInfo("worker_lost", ErrorCategory.TIMEOUT, "wrong category")


if __name__ == "__main__":
    unittest.main()
