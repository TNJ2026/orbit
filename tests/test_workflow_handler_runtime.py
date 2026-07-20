from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from orbit.workflow.application.durable_runtime_service import DurableRuntimeApplicationService
from orbit.workflow.application.handler_runtime_service import HandlerRuntimeBuilder
from orbit.workflow.catalogs import HandlerManifest, InMemorySchemaCatalog
from orbit.workflow.domain.accounting import UsageSnapshot
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.handler_context import ExecutorRequest
from orbit.workflow.domain.handlers import (
    ExternalEffect, HandlerPermanentError, HandlerResult, HandlerResultStatus,
    ResourceProfile, UnknownExternalResultError,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import AttemptStatus, JobStatus, WorkflowRunStatus
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.handlers import (
    AgentHandler, ExecutionRegistry, FakeAgentClient, FakeHandler,
    HandlerExecutor, InMemoryUsageReporter, ToolHandler, ToolManifest, ToolRegistry,
    TransformHandler,
)
from orbit.workflow.handlers.agent import AgentRequest, AgentResponse, TrustedCliAgentClient
from orbit.workflow.handlers.context import ScopedSecretResolver
from orbit.workflow.handlers.tools import ToolResult
from orbit.workflow.handlers.usage import UsageConflictError
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.worker.runtime import CancellationToken, WorkerRuntime
from tests.test_workflow_runtime import linear_ir


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
SCHEMAS = InMemorySchemaCatalog(
    {
        "schema://object/1.0": {"type": "object"},
        "schema://integer/1.0": {"type": "integer"},
    }
)


def profile():
    return ResourceProfile(100, 100, 5, 60, 1_000_000, "test")


def manifest(name="transform", *, safety=ExecutionSafety.REPLAY_SAFE, secrets=()):
    return HandlerManifest(
        name, "1.0.0", ("action",), {"value": "schema://integer/1.0"},
        {"value": "schema://integer/1.0"},
        {"type": "object"}, safety, profile(), "schema://object/1.0",
        (), secrets, True, True,
    )


def usage(sequence=1, *, input_tokens=1, provider="req-1"):
    return UsageSnapshot(
        EntityId("attempt", "a1"), Revision(sequence), input_tokens, 2, 0,
        provider, NOW + timedelta(seconds=sequence),
    )


def tool_manifest():
    return ToolManifest(
        "echo", "1.0.0", ExecutionSafety.REPLAY_SAFE,
        {"value": "schema://integer/1.0"}, "schema://object/1.0",
        30, True, True, True,
    )


def request(value):
    return ExecutorRequest(
        EntityId("run", "r1"), EntityId("plan", "p1"), Revision(1),
        EntityId("node_run", "n1"), EntityId("attempt", "a1"), Revision(1),
        EntityId("job", "j1"), EntityId("lease", "l1"), "node", value.name,
        value.version, value.fingerprint, {"operation": "identity"}, {"value": 1},
        value.inputs, value.outputs, "run+node+1", NOW + timedelta(seconds=60),
        value.execution_safety, value.resource_profile,
    )


class _Tool:
    def execute(self, request, context):
        return ToolResult({"value": request.input["value"]}, external_effect=ExternalEffect.NONE)
    def cancel(self, execution_ref, context):
        from orbit.workflow.domain.handlers import CancelAck, CancelDisposition
        return CancelAck(CancelDisposition.CONFIRMED_STOPPED)
    def recover(self, recovery_ref, context):
        from orbit.workflow.domain.handlers import RecoveryDisposition, RecoveryResult
        return RecoveryResult(RecoveryDisposition.NOT_FOUND)


class HandlerRuntimeContractTests(unittest.TestCase):
    def test_usage_reporter_deduplicates_and_rejects_regression(self):
        reporter = InMemoryUsageReporter()
        first = usage()
        self.assertTrue(reporter.report(first))
        self.assertFalse(reporter.report(first))
        self.assertTrue(reporter.report(usage(2, input_tokens=3)))
        self.assertEqual(2, reporter.latest(first.attempt_id).sequence.value)
        with self.assertRaises(UsageConflictError):
            reporter.report(usage(2, input_tokens=4))
        with self.assertRaises(UsageConflictError):
            reporter.report(usage(3, input_tokens=2))

    def test_secret_scope_and_repr_are_fail_closed(self):
        resolver = ScopedSecretResolver(("API_KEY",), {"API_KEY": "super-secret"})
        secret = resolver.resolve("API_KEY")
        self.assertEqual("super-secret", secret.reveal())
        self.assertNotIn("super-secret", repr(secret))
        with self.assertRaises(PermissionError): resolver.resolve("OTHER")

    def test_transform_executor_is_deterministic_and_schema_checked(self):
        value = manifest()
        registry = ExecutionRegistry()
        registry.register(value, TransformHandler(), implementation_id="transform.v1")
        registry.seal()
        executor = HandlerExecutor(registry, SCHEMAS, clock=lambda: NOW)
        first = executor.execute(request(value), CancellationToken())
        second = executor.execute(request(value), CancellationToken())
        self.assertEqual(first, second)
        self.assertIs(HandlerResultStatus.SUCCEEDED, first.status)
        self.assertEqual({"value": 1}, dict(first.output))

    def test_typed_error_is_redacted_and_unknown_is_preserved(self):
        value = manifest("fake", safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS, secrets=("API_KEY",))
        registry = ExecutionRegistry()
        registry.register(
            value, FakeHandler(error=HandlerPermanentError(
                "leaked super-secret",
                details={"nested": ["also super-secret"]},
                cause="caused by super-secret",
            )),
            implementation_id="fake.error",
        )
        registry.seal()
        executor = HandlerExecutor(
            registry, SCHEMAS, secret_values={"API_KEY": "super-secret"}
        )
        result = executor.execute(request(value), CancellationToken())
        self.assertNotIn("super-secret", result.error.message)
        self.assertNotIn("super-secret", str(result.error.details))
        self.assertNotIn("super-secret", result.error.cause)

        unknown = UnknownExternalResultError("response lost").failure.to_result()
        self.assertIs(HandlerResultStatus.UNKNOWN_EXTERNAL_RESULT, unknown.status)

    def test_tool_and_agent_use_pre_registered_adapters(self):
        tools = ToolRegistry()
        tools.register(tool_manifest(), _Tool())
        tools.seal()
        tool = ToolHandler(tools)
        self.assertTrue(tool.validate(manifest(), {"tool_name": "echo", "tool_version": "1.0.0"}).valid)

        response = AgentResponse({"value": 2}, usage(), "req-1")
        client = FakeAgentClient(response=response)
        agent = AgentHandler(client)
        self.assertTrue(agent.validate(
            manifest("agent", safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS),
            {"model": "test"},
        ).valid)

        mismatched = ToolHandler(tools).validate(
            manifest("tool", safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS),
            {"tool_name": "echo", "tool_version": "1.0.0"},
        )
        self.assertFalse(mismatched.valid)

    def test_trusted_cli_agent_uses_structured_json_protocol(self):
        script = (
            "import json,sys; value=json.load(sys.stdin); "
            "json.dump({'output': value['input'], 'provider_request_id': 'cli-1'}, sys.stdout)"
        )
        client = TrustedCliAgentClient((sys.executable, "-c", script), timeout_seconds=5)
        client.preflight()
        context = SimpleNamespace(request=SimpleNamespace(attempt_id=EntityId("attempt", "test")))
        response = client.execute(AgentRequest({"value": 7}, {}, "key"), context)
        self.assertEqual({"value": 7}, response.output)
        self.assertEqual("cli-1", response.provider_request_id)

    def test_trusted_cli_agent_gets_identity_without_shell_secrets(self):
        script = (
            "import json,os; json.dump({'output': {"
            "'user': os.environ.get('USER'), "
            "'logname': os.environ.get('LOGNAME'), "
            "'leak': os.environ.get('ANTHROPIC_API_KEY')}}, __import__('sys').stdout)"
        )
        client = TrustedCliAgentClient(
            (sys.executable, "-c", script), timeout_seconds=5,
            environment={
                "PATH": "/usr/bin:/bin", "HOME": "/tmp",
                "USER": "orbit-user", "LOGNAME": "orbit-user",
            },
        )
        context = SimpleNamespace(
            request=SimpleNamespace(attempt_id=EntityId("attempt", "identity"))
        )
        response = client.execute(AgentRequest({}, {}, "key"), context)
        self.assertEqual(
            {"user": "orbit-user", "logname": "orbit-user", "leak": None},
            response.output,
        )

    def test_trusted_cli_cancel_terminates_and_reports_unknown(self):
        client = TrustedCliAgentClient(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            timeout_seconds=20, kill_grace_seconds=0.2,
        )
        errors = []
        thread = threading.Thread(
            target=lambda: self._capture_error(
                errors, lambda: client.execute(
                    AgentRequest({}, {}, "key"),
                    SimpleNamespace(request=SimpleNamespace(attempt_id=EntityId("attempt", "test"))),
                )
            )
        )
        thread.start()
        acknowledgement = None
        for _ in range(100):
            acknowledgement = client.cancel("agent:attempt:test")
            if acknowledgement.disposition.value == "unknown":
                break
            time.sleep(0.01)
        self.assertEqual("unknown", acknowledgement.disposition.value)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], UnknownExternalResultError)

    def test_trusted_cli_output_is_bounded_while_streaming(self):
        client = TrustedCliAgentClient(
            (sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"),
            timeout_seconds=5, max_output_bytes=1024,
        )
        context = SimpleNamespace(request=SimpleNamespace(attempt_id=EntityId("attempt", "bounded")))
        with self.assertRaises(Exception) as captured:
            client.execute(AgentRequest({}, {}, "key"), context)
        self.assertIn("output exceeds size limit", str(captured.exception))

    def test_trusted_cli_cancel_targets_only_matching_execution(self):
        script = (
            "import json,sys,time; value=json.load(sys.stdin); "
            "time.sleep(10 if value['input']['value']==1 else .15); "
            "json.dump({'output': value['input']}, sys.stdout)"
        )
        client = TrustedCliAgentClient(
            (sys.executable, "-c", script), timeout_seconds=20,
            kill_grace_seconds=0.2,
        )
        errors, responses = [], []
        def run(value, attempt):
            try:
                responses.append(client.execute(
                    AgentRequest({"value": value}, {}, f"key-{value}"),
                    SimpleNamespace(request=SimpleNamespace(attempt_id=EntityId("attempt", attempt))),
                ))
            except Exception as error: errors.append((attempt, error))
        slow = threading.Thread(target=run, args=(1, "slow")); fast = threading.Thread(target=run, args=(2, "fast"))
        slow.start(); fast.start()
        acknowledgement = None
        for _ in range(100):
            acknowledgement = client.cancel("agent:attempt:slow")
            if acknowledgement.disposition.value == "unknown": break
            time.sleep(0.01)
        slow.join(2); fast.join(2)
        self.assertEqual("unknown", acknowledgement.disposition.value)
        self.assertEqual([2], [item.output["value"] for item in responses])
        self.assertEqual(["slow"], [item[0] for item in errors])

    @staticmethod
    def _capture_error(errors, callback):
        try:
            callback()
        except Exception as error:
            errors.append(error)

    def test_builder_seals_and_exposes_read_only_summary(self):
        value = manifest()
        builder = HandlerRuntimeBuilder(SCHEMAS)
        executor = builder.register(
            value, TransformHandler(), implementation_id="builtin.transform.v1"
        ).build()
        self.assertIsInstance(executor, HandlerExecutor)
        summary = builder.summary()
        self.assertEqual(1, summary.handler_count)
        self.assertEqual(value.fingerprint, summary.handlers[0].manifest_fingerprint)


class HandlerDurableEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/handler.db"
        base = linear_ir()
        nodes = tuple(
            replace(
                node,
                handler=(
                    None if node.handler is None else replace(
                        node.handler,
                        manifest_fingerprint=manifest(
                            node.id,
                            safety=(ExecutionSafety.UNKNOWN_ON_LEASE_LOSS
                                    if node.id == "publish" else ExecutionSafety.REPLAY_SAFE),
                        ).fingerprint,
                    )
                ),
                config=(
                    {"tool_name": "echo", "tool_version": "1.0.0"}
                    if node.id == "transform"
                    else {"model": "fake"} if node.id == "publish"
                    else {"operation": "identity"} if node.id == "collect"
                    else node.config
                ),
            )
            for node in base.nodes
        )
        ir = replace(base, nodes=nodes)
        self.digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, self.digest, "1.0", "sha256:" + "a" * 64),
            expected_latest_version=0, source_format="json", source_text=None,
            actor="test",
        )
        self.run_id = EntityId("run", "handler-e2e")
        self.start = CommandEnvelope(
            EntityId("command", "handler-e2e-start"), "start_run", self.run_id,
            self.run_id, AggregateVersion(0), "handler-e2e-start", "test", NOW,
            {
                "workflow_id": "workflow:linear", "workflow_version": 1,
                "definition_hash": self.digest.value, "input": {"value": 1},
            },
        )

    def tearDown(self): self.temp.cleanup()

    def _registry(self, first=None, *, mixed=False):
        registry = ExecutionRegistry()
        tools = ToolRegistry()
        tools.register(tool_manifest(), _Tool())
        tools.seal()
        for name in ("collect", "transform", "publish"):
            implementation = (
                first if name == "collect" and first is not None
                else ToolHandler(tools) if mixed and name == "transform"
                else AgentHandler(FakeAgentClient(AgentResponse({"value": 1}, None, "req-agent")))
                if mixed and name == "publish"
                else TransformHandler()
            )
            value = manifest(
                name,
                safety=(ExecutionSafety.UNKNOWN_ON_LEASE_LOSS
                        if mixed and name == "publish" else ExecutionSafety.REPLAY_SAFE),
            )
            registry.register(value, implementation, implementation_id=f"{name}.v1")
        registry.seal()
        return registry

    def test_transform_tool_agent_execution_path_records_final_usage(self):
        registry = self._registry(mixed=True)
        service = DurableRuntimeApplicationService(self.path, execution_registry=registry)
        executor = HandlerExecutor(registry, SCHEMAS, clock=lambda: NOW)
        service.submit(self.start)
        worker = WorkerRuntime(service, executor, clock=lambda: NOW)
        for _ in range(3): self.assertTrue(worker.run_once())
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(self.run_id).status)
        timeline = service.get_timeline(self.run_id)
        self.assertEqual(3, sum(item.envelope.event_type == "attempt_usage_recorded" for item in timeline))
        report = service.recovery.rehydrate(self.run_id)
        self.assertEqual(3, len(report.state["usage"]))

    def test_handler_unknown_is_atomic_and_never_materialized(self):
        result = UnknownExternalResultError(
            "provider response lost", provider_request_id="provider-1"
        ).failure.to_result()
        fake = FakeHandler(result=result)
        registry = self._registry(first=fake)
        service = DurableRuntimeApplicationService(self.path, execution_registry=registry)
        executor = HandlerExecutor(registry, SCHEMAS, clock=lambda: NOW)
        service.submit(self.start)
        self.assertTrue(WorkerRuntime(service, executor, clock=lambda: NOW).run_once())
        self.assertIs(JobStatus.FAILED, service.list_jobs(self.run_id)[0].status)
        with service.uow_factory() as uow:
            node = uow.node_runs.list_by_run(self.run_id)[0]
            attempt = uow.attempts.list_by_node_run(node.node_run_id)[0]
            self.assertIs(AttemptStatus.UNKNOWN_EXTERNAL_RESULT, attempt.status)
        recovery = service.durable_recovery.scan_once(NOW)
        self.assertEqual(1, recovery.unknown_attempts)
        self.assertEqual(0, recovery.materialized_jobs)

    def test_unknown_command_replays_identical_prior_result(self):
        registry = self._registry()
        service = DurableRuntimeApplicationService(self.path, execution_registry=registry)
        service.submit(self.start)
        claimed = service.claim_job("worker", NOW)
        service.start_job(claimed, NOW)
        result = UnknownExternalResultError(
            "response lost", provider_request_id="provider-1"
        ).failure.to_result()
        first = service.report_unknown_job_result(claimed, NOW, result)
        second = service.report_unknown_job_result(claimed, NOW, result)
        self.assertEqual("applied", first.disposition.value)
        self.assertEqual("replayed", second.disposition.value)
        self.assertEqual(first.event_ids, second.event_ids)


if __name__ == "__main__": unittest.main()
