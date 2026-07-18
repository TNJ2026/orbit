from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest

from orbit.workflow.application.durable_runtime_service import DurableRuntimeApplicationService
from orbit.workflow.application.data_service import RunInputIngressSession
from orbit.workflow.artifacts import LocalCASBackend, ScopedArtifactAccess, check_artifacts
from orbit.workflow.artifacts.gc import ArtifactGarbageCollector
from orbit.workflow.data.lineage import LineageQueryService
from orbit.workflow.data.manifests import InputManifestBuilder
from orbit.workflow.domain.data import ArtifactVisibility, PortDataPolicy, PortTransport
from orbit.workflow.domain.definitions import CompiledWorkflow, IREdge, IRHandlerRef, IRNode, IRPort, WorkflowIR
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.handlers import ExternalEffect, HandlerResult, HandlerResultStatus
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.runtime import CommandResultDisposition
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.persistence import SQLiteUnitOfWork


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def artifact_ir():
    inline = IRPort("value", "example://integer/1.0", True, False, None, "")
    artifact = IRPort(
        "report", "example://artifact/1.0", True, False, None, "",
        PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN),
    )
    return WorkflowIR(
        "1.1", "workflow:artifact", "Artifact", "", {}, (), (),
        (
            IRNode("produce", "action", (inline,), (artifact,), IRHandlerRef("produce", "1.0.0", "sha256:" + "a" * 64), {}, (), None),
            IRNode("done", "terminal", (artifact,), (), None, {}, (), None),
        ),
        (IREdge("produce_done", "produce", "report", "done", "report", "success", {"op": "literal", "value": True}, {"op": "identity", "schema_id": "example://artifact/1.0"}),),
        ("produce",), ("done",), (), (), {},
    )


def ingress_ir():
    artifact = IRPort(
        "source", "example://artifact/1.0", True, False, None, "",
        PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN),
    )
    inline = IRPort("value", "example://integer/1.0", True, False, None, "")
    return WorkflowIR(
        "1.1", "workflow:ingress", "Ingress", "", {}, (), (),
        (
            IRNode("consume", "action", (artifact,), (inline,), IRHandlerRef("consume", "1.0.0", "sha256:" + "c" * 64), {}, (), None),
            IRNode("done", "terminal", (inline,), (), None, {}, (), None),
        ),
        (IREdge("consume_done", "consume", "value", "done", "value", "success", {"op": "literal", "value": True}, {"op": "identity", "schema_id": "example://integer/1.0"}),),
        ("consume",), ("done",), (), (), {},
    )


class WorkflowArtifactE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.path = root / "runtime.db"; self.backend = LocalCASBackend(root / "blobs")
        ir = artifact_ir(); self.digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, self.digest, "1.0", "sha256:" + "b" * 64),
            expected_latest_version=0, source_format="json", source_text=None, actor="test",
        )
        self.service = DurableRuntimeApplicationService(
            self.path, artifact_backend=self.backend
        ); self.run_id = EntityId("run", "artifact-e2e")
        result = self.service.submit(CommandEnvelope(
            EntityId("command", "artifact-start"), "start_run", self.run_id, self.run_id,
            AggregateVersion(0), "artifact-start", "test", NOW,
            {"workflow_id": "workflow:artifact", "workflow_version": 1,
             "definition_hash": self.digest.value, "input": {"value": 1}},
        ))
        self.assertIs(CommandResultDisposition.APPLIED, result.disposition)

    def tearDown(self): self.temp.cleanup()

    def test_scoped_stage_and_complete_job_commit_atomically(self):
        claimed = self.service.claim_job("worker", NOW); self.service.start_job(claimed, NOW)
        access = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=self.service.get_job(claimed.job_id).node_run_id,
            attempt_id=claimed.attempt_id, output_policies={
                "report": ("example://artifact/1.0", PortDataPolicy(
                    PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN,
                ))
            }, clock=lambda: NOW,
        )
        artifact_id = access.write(name="report", content=b"complete", content_type="text/plain")
        with self.service.uow_factory() as uow: staged = uow.artifacts.get(artifact_id)
        output = {"report": to_primitive(staged.ref)}
        handler_result = HandlerResult(
            HandlerResultStatus.SUCCEEDED, output, None, None, True,
            ExternalEffect.NONE, artifact_refs=(artifact_id,),
        )
        result = self.service.complete_job(claimed, NOW, output, handler_result=handler_result)
        self.assertIs(CommandResultDisposition.APPLIED, result.disposition)
        with self.service.uow_factory() as uow:
            committed = uow.artifacts.get(artifact_id, committed_only=True)
            self.assertIsNotNone(committed)
            self.assertEqual(1, len(uow.artifact_links.list_for_artifact(artifact_id)))
        self.assertEqual((), check_artifacts(self.service.uow_factory, self.backend, run_id=self.run_id))
        reader = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=EntityId("node_run", "consumer"),
            attempt_id=EntityId("attempt", "consumer"), output_policies={},
            authorized_inputs=(committed,), clock=lambda: NOW,
        )
        self.assertEqual(b"complete", reader.read(artifact_id))
        manifest = InputManifestBuilder(self.service.uow_factory).build(
            run_id=self.run_id, node_run_id=EntityId("node_run", "consumer"),
            attempt_id=EntityId("attempt", "consumer"),
            port_policies={"report": (
                "example://artifact/1.0", PortDataPolicy(
                    PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN,
                ),
            )}, bindings={"report": artifact_id},
        )
        self.assertEqual(artifact_id, manifest.items[0].artifact.artifact_id)
        graph = LineageQueryService(self.service.uow_factory).artifact(artifact_id)
        self.assertEqual((artifact_id,), graph.nodes)

    def test_staged_gc_has_dry_run_and_grace_period(self):
        claimed = self.service.claim_job("worker", NOW); self.service.start_job(claimed, NOW)
        access = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=self.service.get_job(claimed.job_id).node_run_id,
            attempt_id=claimed.attempt_id, output_policies={"report": (
                "example://artifact/1.0", PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN),
            )}, clock=lambda: NOW,
        )
        artifact_id = access.write(name="report", content=b"orphan", content_type="text/plain")
        gc = ArtifactGarbageCollector(self.service.uow_factory, self.backend)
        dry = gc.collect(staged_before=NOW + timedelta(hours=1), dry_run=True)
        self.assertEqual((artifact_id,), dry.abandoned_artifact_ids)
        with self.service.uow_factory() as uow: self.assertEqual("staged", uow.artifacts.get(artifact_id).status.value)
        done = gc.collect(staged_before=NOW + timedelta(hours=1), dry_run=False)
        self.assertEqual(1, len(done.deleted_blob_keys))
        with self.service.uow_factory() as uow: self.assertEqual("abandoned", uow.artifacts.get(artifact_id).status.value)

    def test_artifact_commit_fault_rolls_back_result_lineage_and_metadata(self):
        claimed = self.service.claim_job("worker", NOW); self.service.start_job(claimed, NOW)
        access = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=self.service.get_job(claimed.job_id).node_run_id,
            attempt_id=claimed.attempt_id, output_policies={"report": (
                "example://artifact/1.0", PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN),
            )}, clock=lambda: NOW,
        )
        artifact_id = access.write(name="report", content=b"rollback", content_type="text/plain")
        with self.service.uow_factory() as uow: staged = uow.artifacts.get(artifact_id)
        output = {"report": to_primitive(staged.ref)}
        handler_result = HandlerResult(
            HandlerResultStatus.SUCCEEDED, output, None, None, True,
            ExternalEffect.NONE, artifact_refs=(artifact_id,),
        )
        def fault(point):
            if point == "before_artifact_link_insert": raise RuntimeError("kill")
        normal = self.service.kernel.uow_factory
        self.service.kernel.uow_factory = lambda: SQLiteUnitOfWork(self.path, fault_hook=fault)
        failed = self.service.complete_job(claimed, NOW, output, handler_result=handler_result)
        self.assertIs(CommandResultDisposition.REJECTED, failed.disposition)
        self.service.kernel.uow_factory = normal
        with self.service.uow_factory() as uow:
            self.assertEqual("staged", uow.artifacts.get(artifact_id).status.value)
            self.assertEqual((), uow.artifact_links.list_for_artifact(artifact_id))
            self.assertEqual("running", uow.attempts.get(claimed.attempt_id).status.value)
        retried = self.service.complete_job(claimed, NOW, output, handler_result=handler_result)
        self.assertIs(CommandResultDisposition.APPLIED, retried.disposition)

    def test_run_input_ingress_commits_blob_and_consumer_in_start_transaction(self):
        ir = ingress_ir(); digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, digest, "1.0", "sha256:" + "d" * 64),
            expected_latest_version=0, source_format="json", source_text=None, actor="test",
        )
        run_id = EntityId("run", "ingress")
        policy = PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN)
        ingress = RunInputIngressSession(
            self.backend, run_id=run_id,
            policies={"source": ("example://artifact/1.0", policy)},
        )
        artifact_id = ingress.write(port_id="source", content=b"input", content_type="text/plain")
        item = ingress.command_payload()[0]
        ref = {key: item[key] for key in ("artifact_id", "schema_id", "content_type", "checksum", "size_bytes")}
        result = self.service.submit(CommandEnvelope(
            EntityId("command", "ingress-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "ingress-start", "test", NOW,
            {"workflow_id": "workflow:ingress", "workflow_version": 1,
             "definition_hash": digest.value, "input": {"source": ref},
             "artifact_inputs": ingress.command_payload()},
        ))
        self.assertIs(CommandResultDisposition.APPLIED, result.disposition)
        with self.service.uow_factory() as uow:
            artifact = uow.artifacts.get(artifact_id, committed_only=True)
            links = uow.artifact_links.list_for_artifact(artifact_id)
            self.assertIsNotNone(artifact)
            self.assertEqual(["consumer", "producer"], sorted(link.link_type.value for link in links))

    def test_gc_and_cas_dedupe_stage_are_serialized(self):
        claimed = self.service.claim_job("worker", NOW); self.service.start_job(claimed, NOW)
        policy = PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN)
        first = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=self.service.get_job(claimed.job_id).node_run_id,
            attempt_id=claimed.attempt_id, output_policies={"report": ("example://artifact/1.0", policy)}, clock=lambda: NOW,
        )
        first.write(name="report", content=b"shared", content_type="text/plain")
        entered, release, writer_done = threading.Event(), threading.Event(), threading.Event()
        original_delete = self.backend.delete
        def blocked_delete(key):
            entered.set(); release.wait(2); return original_delete(key)
        self.backend.delete = blocked_delete
        gc = ArtifactGarbageCollector(self.service.uow_factory, self.backend)
        gc_thread = threading.Thread(target=lambda: gc.collect(
            staged_before=NOW + timedelta(hours=1), dry_run=False
        ))
        gc_thread.start(); self.assertTrue(entered.wait(1))
        second = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=EntityId("node_run", "second"),
            attempt_id=EntityId("attempt", "second"), output_policies={"report": ("example://artifact/1.0", policy)}, clock=lambda: NOW,
        )
        writer_thread = threading.Thread(target=lambda: (
            second.write(name="report", content=b"shared", content_type="text/plain"), writer_done.set()
        ))
        writer_thread.start()
        self.assertFalse(writer_done.wait(0.05))
        release.set(); gc_thread.join(2); writer_thread.join(2)
        metadata = self.database_artifact(second.produced_artifact_ids[0])
        self.assertTrue(self.backend.verify(metadata.blob_key, metadata.checksum, metadata.size_bytes))

    def database_artifact(self, artifact_id):
        with self.service.uow_factory() as uow:
            return uow.artifacts.get(artifact_id)

    def test_complete_job_preflight_rejects_missing_or_corrupt_blob(self):
        claimed = self.service.claim_job("worker", NOW); self.service.start_job(claimed, NOW)
        policy = PortDataPolicy(PortTransport.ARTIFACT_REF, 1024, ("text/plain",), ArtifactVisibility.RUN)
        access = ScopedArtifactAccess(
            self.backend, self.service.uow_factory, run_id=self.run_id,
            workflow_id=EntityId("workflow", "artifact"), node_run_id=self.service.get_job(claimed.job_id).node_run_id,
            attempt_id=claimed.attempt_id, output_policies={"report": ("example://artifact/1.0", policy)}, clock=lambda: NOW,
        )
        artifact_id = access.write(name="report", content=b"valid", content_type="text/plain")
        metadata = self.database_artifact(artifact_id)
        self.backend._path(metadata.blob_key).write_bytes(b"corrupt")
        output = {"report": to_primitive(metadata.ref)}
        result = HandlerResult(
            HandlerResultStatus.SUCCEEDED, output, None, None, True,
            ExternalEffect.NONE, artifact_refs=(artifact_id,),
        )
        with self.assertRaisesRegex(ValueError, "pre-commit integrity"):
            self.service.complete_job(claimed, NOW, output, handler_result=result)
        with self.service.uow_factory() as uow:
            self.assertEqual("staged", uow.artifacts.get(artifact_id).status.value)
            self.assertEqual("running", uow.attempts.get(claimed.attempt_id).status.value)
