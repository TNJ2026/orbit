from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.planner_service import PlannerApplicationService
from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.planner import (
    PLANNER_ATTEMPT_TRANSITIONS, PLANNER_SCHEMA_VERSION, PlannerActionKind,
    PlannerAttemptStatus, PlannerUsage, strict_parse_proposal,
    validate_planner_transition,
)
from orbit.workflow.domain.schemas import validate_contract
from orbit.workflow.domain.serialization import to_primitive
from orbit.workflow.domain.stability import CONTRACT_STABILITY, ContractStability
from orbit.workflow.domain.versions import AggregateVersion, DefinitionHash, Revision
from orbit.workflow.dsl import compile_source
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase, MemoryUnitOfWork
from orbit.workflow.persistence.integrity import check_database
from orbit.workflow.persistence.uow import SQLiteUnitOfWork
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.planner import (
    FakePlannerProvider, PlannerEvalCase, PlannerEvalHarness,
    PlannerProviderResponse, build_planning_context,
)
from orbit.workflow.planner.provider import PlannerTransientError
from orbit.workflow.runtime.planner_recovery import PlannerRecoveryScanner
from orbit.workflow.runtime.reducers import reduce_run_view
from orbit.workflow.testing import side_effect_guard


NOW = datetime(2026, 7, 17, 15, tzinfo=timezone.utc)
HASH_A = DefinitionHash("sha256:" + "a" * 64)
HASH_B = DefinitionHash("sha256:" + "b" * 64)


def proposal_raw(run_id, *, proposal_id="p1", action="finish", arguments=None):
    arguments = {"outputs": {}} if arguments is None else arguments
    return json.dumps({
        "schema_version": "1.0", "proposal_id": f"proposal:{proposal_id}",
        "run_id": str(run_id), "base_plan_version": 1,
        "action": {"kind": action, "arguments": arguments}, "reason": "done",
    }, separators=(",", ":"))


class PlannerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "planner.db"
        dsl = {
            "dsl_version": "1.2", "metadata": {"id": "planner_flow", "name": "Planner"},
            "nodes": [{"id": "done", "kind": "terminal", "inputs": [{"id": "value", "schema_id": "schema://value/1.0"}]}],
            "edges": [], "entry": ["done"], "terminals": ["done"],
        }
        compiled = compile_source(
            json.dumps(dsl), InMemoryHandlerCatalog([]),
            InMemorySchemaCatalog({"schema://value/1.0": {}}), source_format="json",
        )
        SQLiteWorkflowVersionStore(self.path).publish(
            compiled, expected_latest_version=0, source_format="json", source_text=None, actor="test",
        )
        self.run_id = EntityId("run", "planner")
        RuntimeApplicationService(self.path).submit(CommandEnvelope(
            EntityId("command", "planner-start"), "start_run", self.run_id, self.run_id,
            AggregateVersion(0), "planner-start", "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
             "definition_hash": compiled.definition_hash.value, "input": {"value": 1}},
        ))
        self.context = build_planning_context(
            run_id=self.run_id, plan_version=Revision(1), goal="finish the task",
            graph_summary={"status": "succeeded", "plan_version": 1, "nodes": [], "tokens": [], "joins": [], "waiting_reason": None},
            available_data=[{"port_id": "value", "schema_id": "schema://value/1.0", "transport": "inline", "checksum": HASH_A.value, "size_bytes": 1}],
            available_capabilities=["finish"], remaining_limits={"decisions": 3, "tokens": 1000},
        )

    def request(self, service):
        return service.request_decision(
            self.context, prompt_hash=HASH_A, capability_manifest_hash=HASH_B,
            model_id="fake-model", provider_id="fake", now=NOW,
        )

    def test_contract_context_schema_strict_parser_and_stability(self):
        validate_contract(to_primitive(self.context), "planning-context/1.0")
        proposal = strict_parse_proposal(proposal_raw(self.run_id), expected_run_id=self.run_id)
        validate_contract(to_primitive(proposal), "action-proposal/1.0")
        fixture = Path(__file__).parent / "fixtures" / "workflow_planner" / "v1" / "action-proposal-1.0.json"
        self.assertEqual(json.loads(fixture.read_text()), to_primitive(proposal))
        self.assertIs(PlannerActionKind.FINISH, proposal.action.kind)
        self.assertEqual(self.context.context_hash, build_planning_context(
            run_id=self.run_id, plan_version=Revision(1), goal="finish the task",
            graph_summary={"joins": [], "tokens": [], "nodes": [], "plan_version": 1, "status": "succeeded", "waiting_reason": None},
            available_data=[{"size_bytes": 1, "checksum": HASH_A.value, "transport": "inline", "schema_id": "schema://value/1.0", "port_id": "value"}],
            available_capabilities=["finish", "finish"], remaining_limits={"tokens": 1000, "decisions": 3},
        ).context_hash)
        with self.assertRaisesRegex(ValueError, "strict JSON"):
            strict_parse_proposal("```json\n{}\n```", expected_run_id=self.run_id)
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            build_planning_context(
                run_id=self.run_id, plan_version=Revision(1), goal="x",
                graph_summary={}, available_data=[{"secret": "leak"}],
            )
        for name in (
            "planner_provider_port", "planner_unknown_replay_semantics",
            "planning_context", "planner_attempt", "planner_action",
            "action_proposal", "action_proposal_v1",
        ):
            self.assertIs(ContractStability.STABLE, CONTRACT_STABILITY[name])
        for source, targets in PLANNER_ATTEMPT_TRANSITIONS.items():
            for target in PlannerAttemptStatus:
                if target in targets:
                    validate_planner_transition(source, target)
                else:
                    with self.assertRaises(ValueError):
                        validate_planner_transition(source, target)

    def test_raw_response_commits_before_parse_and_recovery_finishes_it(self):
        fired = {"value": False}
        def fault(point):
            if point == "before_planner_proposal_create" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("kill after raw commit")
        provider = FakePlannerProvider([PlannerProviderResponse(proposal_raw(self.run_id), "req-1", PlannerUsage(10, 4, 20))])
        service = PlannerApplicationService(self.path, provider=provider, fault_hook=fault)
        attempt = self.request(service); claim = service.claim("worker", NOW)
        with self.assertRaisesRegex(RuntimeError, "kill after raw"):
            service.execute_claimed(claim, NOW)
        stored = service.get_attempt(attempt.attempt_id)
        self.assertIs(PlannerAttemptStatus.RESPONSE_RECEIVED, stored.status)
        self.assertIsNotNone(stored.raw_response_checksum)

        clean = PlannerApplicationService(self.path, provider=provider)
        report = PlannerRecoveryScanner(clean).scan_once(NOW)
        self.assertEqual(1, report.parsed_responses)
        self.assertIs(PlannerAttemptStatus.ACCEPTED, clean.get_attempt(attempt.attempt_id).status)
        proposals = clean.list_proposals(self.run_id)
        self.assertEqual(1, len(proposals))
        validate_contract(to_primitive(clean.get_attempt(attempt.attempt_id)), "planner-attempt/1.0")
        validate_contract(to_primitive(proposals[0]), "planner-proposal-record/1.0")
        self.assertTrue(check_database(self.path, run_id=self.run_id).ok)
        calls = len(provider.calls)
        RuntimeApplicationService(self.path).recovery.rehydrate(self.run_id)
        self.assertEqual(calls, len(provider.calls))
        with clean.uow_factory() as uow:
            planner_events = tuple(
                item for item in uow.events.read_run(self.run_id, limit=1000)
                if item.envelope.event_type.startswith("planner_")
            )
        state = {"run_status": None, "nodes": {}, "attempts": {}, "outputs": {}, "jobs": {}, "leases": {}, "timers": {}, "usage": {}}
        with side_effect_guard():
            for item in planner_events:
                state = reduce_run_view(state, item)
        self.assertEqual("accepted", state["planner_attempts"][str(attempt.attempt_id)]["status"])

    def test_unknown_retry_and_late_response_are_isolated(self):
        provider = FakePlannerProvider([TimeoutError("unknown")])
        service = PlannerApplicationService(self.path, provider=provider)
        original = self.request(service); claim = service.claim("worker", NOW)
        service.execute_claimed(claim, NOW)
        self.assertIs(PlannerAttemptStatus.UNKNOWN, service.get_attempt(original.attempt_id).status)
        retry = service.retry_unknown(original.attempt_id, NOW + timedelta(seconds=1))
        self.assertNotEqual(original.attempt_id, retry.attempt_id)
        service.record_late_response(
            original.attempt_id,
            PlannerProviderResponse(proposal_raw(self.run_id, proposal_id="late"), "late", PlannerUsage(8, 3, 12, True)),
            NOW + timedelta(seconds=2),
        )
        self.assertEqual(0, len(service.list_proposals(self.run_id)))
        self.assertIs(PlannerAttemptStatus.UNKNOWN, service.get_attempt(original.attempt_id).status)

    def test_invalid_response_is_rejected_and_duplicate_request_is_idempotent(self):
        provider = FakePlannerProvider([PlannerProviderResponse("not json")])
        service = PlannerApplicationService(self.path, provider=provider)
        first = self.request(service); second = self.request(service)
        self.assertEqual(first.attempt_id, second.attempt_id)
        claim = service.claim("worker", NOW); service.execute_claimed(claim, NOW)
        self.assertIs(PlannerAttemptStatus.REJECTED, service.get_attempt(first.attempt_id).status)
        self.assertEqual("planner_proposal_invalid", service.get_attempt(first.attempt_id).error["code"])

    def test_expired_lease_recovery_marks_unknown_without_provider_call(self):
        provider = FakePlannerProvider([])
        service = PlannerApplicationService(self.path, provider=provider)
        attempt = self.request(service)
        service.claim("worker", NOW, lease_ttl=timedelta(seconds=1))
        report = PlannerRecoveryScanner(service).scan_once(NOW + timedelta(seconds=2))
        self.assertEqual(1, report.expired_unknown)
        self.assertIs(PlannerAttemptStatus.UNKNOWN, service.get_attempt(attempt.attempt_id).status)
        self.assertEqual([], provider.calls)

    def test_transient_failure_creates_new_attempt_and_exhaustion_escalates(self):
        provider = FakePlannerProvider([PlannerTransientError("busy"), PlannerTransientError("busy")])
        service = PlannerApplicationService(self.path, provider=provider, max_attempts=2)
        original = self.request(service)
        retry = service.execute_claimed(service.claim("worker", NOW), NOW)
        self.assertNotEqual(original.attempt_id, retry.attempt_id)
        service.execute_claimed(service.claim("worker", NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1))
        attempts = service.list_attempts(self.run_id)
        self.assertEqual(2, len(attempts))
        self.assertTrue(attempts[-1].error["escalation_requested"])

    def test_planner_lease_renewal_preserves_event_stream_version(self):
        service = PlannerApplicationService(self.path)
        attempt = self.request(service); claim = service.claim("worker", NOW)
        renewed = service.renew_claim(claim, NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=90))
        self.assertEqual(attempt.aggregate_version.next(), renewed.aggregate_version)
        with service.uow_factory() as uow:
            self.assertEqual(renewed.aggregate_version, uow.events.stream_head(attempt.attempt_id))

    def test_claim_fault_rolls_back_event_and_projection_together(self):
        clean = PlannerApplicationService(self.path); attempt = self.request(clean)
        def fault(point):
            if point == "before_planner_attempt_update": raise RuntimeError("kill claim")
        broken = PlannerApplicationService(self.path, fault_hook=fault)
        with self.assertRaisesRegex(RuntimeError, "kill claim"):
            broken.claim("worker", NOW)
        stored = clean.get_attempt(attempt.attempt_id)
        self.assertIs(PlannerAttemptStatus.REQUESTED, stored.status)
        with clean.uow_factory() as uow:
            self.assertEqual(AggregateVersion(1), uow.events.stream_head(attempt.attempt_id))

    def test_migration_six_and_memory_sqlite_repository_parity(self):
        with connect_workflow_database(self.path) as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM workflow_schema_migrations ORDER BY version")]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(list(range(1, 10)), versions)
        self.assertTrue({"planner_attempts", "planner_proposals"}.issubset(tables))

        sqlite_service = PlannerApplicationService(self.path)
        record = self.request(sqlite_service)
        memory = MemoryRuntimeDatabase()
        # Repository parity is exercised with the same immutable record; the
        # in-memory database does not need a duplicate WorkflowRun FK fixture.
        with MemoryUnitOfWork(memory) as uow:
            uow.planner_attempts.create(record); uow.commit()
        with SQLiteUnitOfWork(self.path) as uow:
            sqlite_record = uow.planner_attempts.get(record.attempt_id)
        self.assertEqual(to_primitive(record), to_primitive(memory.planner_attempts.get(record.attempt_id)))
        self.assertEqual(to_primitive(record), to_primitive(sqlite_record))

    def test_eval_harness_has_deterministic_baseline_metrics(self):
        fixture = Path(__file__).parent / "fixtures" / "workflow_planner" / "v1" / "eval-cases.json"
        cases = tuple(
            PlannerEvalCase(
                name=item["name"], run_id=EntityId.parse(item["run_id"]),
                raw_response=item["raw_response"], expected_valid=item["expected_valid"],
                expected_action=None if item["expected_action"] is None else PlannerActionKind(item["expected_action"]),
                task_success=item["task_success"], decision_count=item["decision_count"],
                input_tokens=item["input_tokens"], output_tokens=item["output_tokens"],
                cost_microunits=item["cost_microunits"], duration_ms=item["duration_ms"],
            ) for item in json.loads(fixture.read_text())
        )
        left = PlannerEvalHarness().run(cases); right = PlannerEvalHarness().run(reversed(cases))
        self.assertEqual((2, 2, 1, 1), (left.total, left.passed, left.valid_proposals, left.invalid_proposals))
        self.assertEqual(left.action_counts, right.action_counts)
        self.assertEqual(1.0, left.pass_rate)
        self.assertEqual(0.5, left.task_success_rate)
        self.assertEqual(1.0, left.average_decisions)
        self.assertEqual((75, 27, 40, 210), (left.input_tokens, left.output_tokens, left.cost_microunits, left.duration_ms))


if __name__ == "__main__": unittest.main()
