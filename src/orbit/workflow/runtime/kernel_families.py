"""Transactional Run/Node/Graph command-family implementation.

The public entry point lives in ``runtime.kernel``.  Keeping this implementation
module private lets later command families evolve without growing the public
Kernel boundary or introducing nested Units of Work.
"""

from __future__ import annotations

from dataclasses import replace
import secrets
from typing import Any, Callable, Mapping

from ..data.mapping import evaluate_mapping
from ..domain.data import (
    ArtifactLink, ArtifactLinkType, ArtifactStatus, DataOwnerKind,
    ArtifactMetadata, ArtifactVisibility, PortTransport, ValueLink,
    ValueLinkType, ValueRecord, derive_artifact_id, derive_value_id,
)
from ..domain.concurrency import CommandDisposition
from ..domain.envelopes import CommandEnvelope
from ..domain.execution_plan import ExecutionPlan, GraphExecutionPlan, execution_plan_from_primitive
from ..domain.graph import (
    EdgeRoute, JoinMergeMode, JoinMode, JoinPolicy, derive_branch_token_id,
    derive_graph_node_run_id, derive_join_group_id,
)
from ..domain.graph_persistence import JoinGroupRecord, JoinGroupStatus
from ..domain.errors import ErrorCategory, ErrorInfo
from ..domain.ids import EntityId
from ..domain.human import submission_token_hash
from ..domain.persistence import (
    AttemptRecord, BranchTokenRecord, ConcurrencyConflictError, ExecutionPlanRecord,
    IdempotencyConflictError, IntegrityViolationError, NodeRunRecord,
    RepositoryAlreadyExistsError,
    PersistenceError, WorkflowRunRecord,
)
from ..domain.runtime import (
    CommandResult, CommandResultDisposition, KernelDiagnostic, RUNTIME_COMMAND_TYPES,
    validate_runtime_command_payload,
)
from ..domain.serialization import canonical_json, definition_hash, to_primitive
from ..domain.schemas import validate_contract
from ..domain.states import (
    AttemptStatus, BranchTokenStatus, JobStatus, LeaseStatus, NodeRunStatus, TimerStatus,
    WorkflowRunStatus, validate_transition,
)
from ..domain.versions import AggregateVersion, DefinitionHash, Revision
from .events import derived_id, runtime_event
from .plan_instantiator import UnsupportedPlanShapeError, instantiate_execution_plan
from .kernel_context import KernelContext
from .commands import CommandRouter
from ..persistence.control import append_control_event, audit
from ..graph.joins import JoinTokenFact, evaluate_join
from ..graph.routing import evaluate_route


class _EventBuilder:
    def __init__(self, command: CommandEnvelope) -> None:
        self.command = command
        self.ordinal = 0
        self.graph_reactions = 0
        self.human_deliveries = []

    def make(self, aggregate_id, sequence, event_type, payload):
        self.ordinal += 1
        return runtime_event(
            self.command, ordinal=self.ordinal, aggregate_id=aggregate_id,
            sequence=sequence, event_type=event_type, payload=payload,
        )


def _transition_payload(machine: str, source, target, **context):
    validate_transition(source, target)
    return {"machine": machine, "from": source.value, "to": target.value, **context}


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


class RuntimeKernel:
    MAX_GRAPH_REACTIONS_PER_COMMAND = 128
    def __init__(
        self,
        uow_factory: Callable[[], Any],
        workflow_versions: Any,
        *,
        snapshot_coordinator: Any = None,
        logger: Callable[[str, Mapping[str, Any]], None] | None = None,
        schema_validator: Callable[[str, Any], None] | None = None,
        work_scheduler: Any = None,
        human_task_delivery: Callable[[EntityId, tuple[str, ...], str], None] | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.workflow_versions = workflow_versions
        self.snapshot_coordinator = snapshot_coordinator
        self.logger = logger or (lambda message, fields: None)
        self.schema_validator = schema_validator or (lambda schema_id, value: None)
        self.work_scheduler = work_scheduler
        self.human_task_delivery = human_task_delivery or (
            lambda task_id, participants, token: None
        )
        self.command_router = CommandRouter(self)

    def handle(self, command: CommandEnvelope) -> CommandResult:
        if command.command_type not in RUNTIME_COMMAND_TYPES:
            return self._rejected("UNKNOWN_COMMAND", f"unsupported command {command.command_type}", command)
        if command.command_type in {"schedule_node", "advance_graph"} and not command.actor.startswith("system:"):
            return self._rejected(
                "POLICY_REJECTED", f"{command.command_type} is a system-only command", command
            )
        try:
            validate_contract(
                command.payload,
                f"runtime-command/{command.command_type.replace('_', '-')}/1.0",
            )
            validate_runtime_command_payload(command.command_type, command.payload)
            with self.uow_factory() as uow:
                prior = uow.receipts.decide(command)
                if prior is not None and prior.disposition is CommandDisposition.REPLAY_PRIOR_RESULT:
                    return CommandResult(
                        CommandResultDisposition.REPLAYED, prior.prior_event_ids,
                        summary=self._replay_summary(command),
                    )
                builder = _EventBuilder(command)
                event_ids, version, run_id, summary = self.command_router.dispatch(
                    KernelContext(uow, command, builder)
                )
                uow.receipts.record(run_id, command, tuple(event_ids), command.issued_at)
                uow.commit()
            self._dispatch_human_deliveries(builder)
            result = CommandResult(
                CommandResultDisposition.APPLIED, tuple(event_ids), version,
                summary=summary,
            )
            self._log("runtime_command_applied", {"command_id": str(command.command_id), "event_count": len(event_ids)})
            if self.snapshot_coordinator is not None:
                try:
                    self.snapshot_coordinator.consider(run_id)
                except Exception as exc:
                    self._log("runtime_snapshot_failed", {"run_id": str(run_id), "error": type(exc).__name__})
            return result
        except IdempotencyConflictError as exc:
            return self._rejected("IDEMPOTENCY_CONFLICT", str(exc), command)
        except ConcurrencyConflictError as exc:
            return CommandResult(
                CommandResultDisposition.REJECTED,
                diagnostics=(KernelDiagnostic(
                    "CONCURRENCY_CONFLICT", str(exc), command.aggregate_id,
                    exc.expected, exc.actual, True,
                ),),
            )
        except UnsupportedPlanShapeError as exc:
            return self._rejected("UNSUPPORTED_PLAN_SHAPE", str(exc), command)
        except RepositoryAlreadyExistsError as exc:
            return self._rejected("ALREADY_EXISTS", str(exc), command)
        except IntegrityViolationError as exc:
            return self._rejected("INTEGRITY_VIOLATION", str(exc), command)
        except PersistenceError as exc:
            return CommandResult(
                CommandResultDisposition.REJECTED,
                diagnostics=(KernelDiagnostic(
                    "PERSISTENCE_ERROR", str(exc), command.aggregate_id,
                    retryable=True, details={"error_type": type(exc).__name__},
                ),),
            )
        except PermissionError as exc:
            return self._rejected("POLICY_REJECTED", str(exc), command)
        except (ValueError, KeyError) as exc:
            return self._rejected("VALIDATION_FAILED", str(exc), command)
        except Exception as exc:
            self._log(
                "runtime_command_internal_error",
                {"command_id": str(command.command_id), "error_type": type(exc).__name__},
            )
            return CommandResult(
                CommandResultDisposition.REJECTED,
                diagnostics=(KernelDiagnostic(
                    "INTERNAL_ERROR", "runtime command failed", command.aggregate_id,
                    retryable=True, details={"error_type": type(exc).__name__},
                ),),
            )

    def _rejected(self, code: str, message: str, command: CommandEnvelope) -> CommandResult:
        self._log("runtime_command_rejected", {"command_id": str(command.command_id), "code": code})
        return CommandResult(
            CommandResultDisposition.REJECTED,
            diagnostics=(KernelDiagnostic(code, message, command.aggregate_id),),
        )

    def _log(self, message: str, fields: Mapping[str, Any]) -> None:
        try:
            self.logger(message, fields)
        except Exception:
            pass

    def _dispatch_human_deliveries(self, builder: _EventBuilder) -> None:
        """Best-effort post-commit token hand-off; loss is recoverable.

        A delivery that fails (or a process that dies first) does not strand
        the task: a participant can rotate the token through the reissue
        endpoint, which is why this is a log rather than a retry loop.
        """
        for task_id, participants, token in builder.human_deliveries:
            try:
                self.human_task_delivery(task_id, participants, token)
            except Exception as exc:
                self._log(
                    "human_task_delivery_failed",
                    {"task_id": str(task_id), "error_type": type(exc).__name__},
                )

    @staticmethod
    def _replay_summary(command: CommandEnvelope) -> Mapping[str, Any]:
        if command.command_type == "start_run":
            return {
                "run_id": str(command.aggregate_id),
                "plan_id": str(derived_id("plan", command.aggregate_id, 1)),
            }
        if command.command_type == "start_attempt":
            return {
                "node_run_id": str(command.aggregate_id),
                "attempt_id": str(derived_id("attempt", command.aggregate_id, 1)),
            }
        if command.command_type in {"complete_attempt", "fail_attempt"}:
            return {"attempt_id": str(command.aggregate_id)}
        if command.command_type == "cancel_node":
            return {"node_run_id": str(command.aggregate_id), "status": "cancelled"}
        if command.command_type == "schedule_node":
            return {"node_run_id": str(command.aggregate_id)}
        if command.command_type == "submit_human_task":
            decision = command.payload["decision"]
            return {
                "task_id": str(command.aggregate_id), "decision": decision,
                "status": "rejected" if decision == "reject" else "completed",
            }
        return {"run_id": str(command.aggregate_id)}

    @staticmethod
    def _check_version(record, command: CommandEnvelope) -> None:
        if record.aggregate_version != command.expected_version:
            raise ConcurrencyConflictError(
                command.aggregate_id, command.expected_version.value,
                record.aggregate_version.value,
            )

    def _start_run(self, uow, command, events):
        payload = _require_object(command.payload, "payload")
        if command.aggregate_id.kind != "run" or command.expected_version != AggregateVersion(0):
            raise ValueError("StartRun requires a new run aggregate at version 0")
        workflow_id = EntityId.parse(str(payload["workflow_id"]))
        workflow_version = Revision(int(payload["workflow_version"]))
        expected_hash = DefinitionHash(str(payload["definition_hash"]))
        initial_input = dict(_require_object(payload.get("input", {}), "input"))
        artifact_inputs = tuple(payload.get("artifact_inputs", ()))
        version_record = self.workflow_versions.get(str(workflow_id), workflow_version.value)
        if version_record is None:
            raise ValueError("WorkflowVersion was not found")
        if version_record.definition_hash != expected_hash:
            raise ValueError("WorkflowVersion definition hash mismatch")
        plan_id = derived_id("plan", command.aggregate_id, 1)
        plan = instantiate_execution_plan(
            version_record.ir, run_id=command.aggregate_id, plan_id=plan_id,
            workflow_version=workflow_version, workflow_definition_hash=expected_hash,
        )
        run = WorkflowRunRecord(
            command.aggregate_id, workflow_id, workflow_version, expected_hash,
            WorkflowRunStatus.CREATED, AggregateVersion(0), command.correlation_id,
            command.issued_at, command.issued_at,
        )
        uow.runs.create(run)
        run_event = events.make(
            run.run_id, 1, "workflow_run_transitioned",
            _transition_payload(
                "workflow_run", WorkflowRunStatus.CREATED, WorkflowRunStatus.RUNNING,
                workflow_id=str(workflow_id), workflow_version=workflow_version.value,
                definition_hash=expected_hash.value, plan_id=str(plan_id), plan_version=1,
                input=initial_input,
                **({"artifact_refs": [str(item["artifact_id"]) for item in artifact_inputs]} if artifact_inputs else {}),
            ),
        )
        stored = uow.events.append(run.run_id, run.run_id, AggregateVersion(0), (run_event,))
        run = replace(run, status=WorkflowRunStatus.RUNNING, aggregate_version=AggregateVersion(1), updated_at=command.issued_at)
        uow.runs.update(run, AggregateVersion(0))
        plan_record = ExecutionPlanRecord(
            plan.plan_id, plan.run_id, plan.plan_version, plan.workflow_id,
            plan.workflow_version, plan.schema_version, to_primitive(plan),
            definition_hash(plan), run_event.event_id, command.issued_at,
        )
        uow.plans.append(plan_record)
        entry_id = plan.entry_node_ids[0] if isinstance(plan, GraphExecutionPlan) else plan.entry_node_id
        entry = plan.node(entry_id)
        self._record_inline_values(
            uow, run.run_id, DataOwnerKind.RUN_INPUT, run.run_id,
            entry.inputs, initial_input, run_event.event_id, command.issued_at,
        )
        self._commit_run_ingress_artifacts(
            uow, run, entry.inputs, artifact_inputs, run_event.event_id,
            command.issued_at,
        )
        result_events = [run_event.event_id]
        if isinstance(plan, GraphExecutionPlan):
            for node_id in plan.entry_node_ids:
                result_events.extend(self._schedule_graph(
                    uow, command, events, plan, node_id, initial_input,
                    generation=1, activation_key="root",
                ))
            primary = uow.runs.get(run.run_id).aggregate_version
        elif entry.kind == "terminal":
            terminal = events.make(
                run.run_id, 2, "workflow_run_transitioned",
                _transition_payload("workflow_run", WorkflowRunStatus.RUNNING, WorkflowRunStatus.SUCCEEDED, reason="empty_plan"),
            )
            uow.events.append(run.run_id, run.run_id, AggregateVersion(1), (terminal,))
            uow.runs.update(replace(run, status=WorkflowRunStatus.SUCCEEDED, aggregate_version=AggregateVersion(2)), AggregateVersion(1))
            result_events.append(terminal.event_id)
            primary = AggregateVersion(2)
        else:
            result_events.extend(self._schedule(uow, command, events, plan, entry.node_id, initial_input))
            primary = AggregateVersion(1)
        return result_events, primary, run.run_id, {"run_id": str(run.run_id), "plan_id": str(plan.plan_id)}

    def _commit_run_ingress_artifacts(self, uow, run, ports, inputs, event_id, now):
        declared = {
            port["id"]: port for port in ports
            if port["data_policy"]["transport"] == PortTransport.ARTIFACT_REF.value
        }
        for raw in inputs:
            port_id = str(raw["port_id"]); port = declared.get(port_id)
            if port is None: raise ValueError("Run ingress Artifact port was not declared")
            policy = port["data_policy"]
            if raw["schema_id"] != port["schema_id"] or raw["content_type"] not in policy["content_types"]:
                raise ValueError("Run ingress Artifact policy mismatch")
            checksum = DefinitionHash(str(raw["checksum"]))
            size = int(raw["size_bytes"])
            if size > policy["max_size_bytes"] or raw["blob_key"] != checksum.value:
                raise ValueError("Run ingress Artifact size or checksum mismatch")
            artifact_id = derive_artifact_id(run.run_id, port_id, port_id)
            if str(artifact_id) != str(raw["artifact_id"]):
                raise ValueError("Run ingress Artifact ID is not deterministic")
            visibility = ArtifactVisibility(policy["visibility"])
            scope_id = run.run_id if visibility is ArtifactVisibility.RUN else run.workflow_id if visibility is ArtifactVisibility.WORKFLOW else None
            if scope_id is None: raise ValueError("Run ingress supports run/workflow visibility only")
            staged = ArtifactMetadata(
                artifact_id, run.run_id, run.workflow_id, "run_ingress", run.run_id,
                None, port_id, port["schema_id"], raw["content_type"], checksum,
                size, raw["blob_key"], visibility, scope_id, ArtifactStatus.STAGED, now,
            )
            uow.artifacts.stage(staged)
            uow.artifacts.commit(replace(
                staged, status=ArtifactStatus.COMMITTED,
                committed_at=now, created_event_id=event_id,
            ))
            uow.artifact_links.insert(ArtifactLink(
                derived_id("artifact_link", artifact_id, "producer", run.run_id),
                run.workflow_id, run.run_id, artifact_id, ArtifactLinkType.PRODUCER,
                run.run_id, event_id, now,
            ))

    def _schedule_node(self, uow, command, events):
        payload = _require_object(command.payload, "payload")
        if command.aggregate_id.kind != "node_run" or command.expected_version != AggregateVersion(0):
            raise ValueError("ScheduleNode requires a new node_run at version 0")
        run_id = EntityId.parse(str(payload["run_id"]))
        plan = self._load_plan(uow, run_id, int(payload.get("plan_version", 1)))
        node_id = str(payload["node_id"])
        expected_id = derived_id("node_run", run_id, plan.plan_version.value, node_id, 1)
        if command.aggregate_id != expected_id:
            raise ValueError("ScheduleNode aggregate id is not the deterministic node id")
        ids = self._schedule(uow, command, events, plan, node_id, dict(_require_object(payload.get("input", {}), "input")))
        return ids, AggregateVersion(2), run_id, {"node_run_id": str(expected_id)}

    def _schedule(
        self, uow, command, events, plan: ExecutionPlan, node_id: str,
        input_value: Mapping[str, Any], *, source_values=None, mapping_hash=None,
    ):
        node = plan.node(node_id)
        if node.kind == "terminal":
            raise ValueError("terminal nodes cannot be scheduled")
        run = uow.runs.get(plan.run_id)
        if run is None or run.status is not WorkflowRunStatus.RUNNING:
            raise ValueError("nodes can only be scheduled for a running Run")
        existing = [
            item for item in uow.node_runs.list_by_run(plan.run_id)
            if item.node_id == node_id and item.source_plan_version == plan.plan_version
        ]
        if existing:
            raise IntegrityViolationError(f"node {node_id} is already scheduled")
        if node_id != plan.entry_node_id:
            predecessors = [
                source for source, target in plan.successors.items() if target == node_id
            ]
            if len(predecessors) != 1:
                raise ValueError(f"node {node_id} has no unique predecessor")
            predecessor = next(
                (
                    item for item in uow.node_runs.list_by_run(plan.run_id)
                    if item.node_id == predecessors[0]
                    and item.source_plan_version == plan.plan_version
                ),
                None,
            )
            if predecessor is None or predecessor.status is not NodeRunStatus.SUCCEEDED:
                raise ValueError(f"predecessor for node {node_id} has not succeeded")
        self._validate_ports(node.inputs, input_value, "input")
        node_run_id = derived_id("node_run", plan.run_id, plan.plan_version.value, node_id, 1)
        record = NodeRunRecord(
            node_run_id, plan.run_id, node_id, plan.plan_version,
            NodeRunStatus.PENDING, AggregateVersion(0), command.issued_at, command.issued_at,
        )
        uow.node_runs.create(record)
        prepared = events.make(
            node_run_id, 1, "node_input_prepared",
            {"run_id": str(plan.run_id), "node_id": node_id, "input": dict(input_value)},
        )
        ready = events.make(
            node_run_id, 2, "node_run_transitioned",
            _transition_payload("node_run", NodeRunStatus.PENDING, NodeRunStatus.READY, run_id=str(plan.run_id), node_id=node_id, plan_version=plan.plan_version.value),
        )
        uow.events.append(plan.run_id, node_run_id, AggregateVersion(0), (prepared, ready))
        input_values = self._record_inline_values(
            uow, plan.run_id, DataOwnerKind.NODE_INPUT, node_run_id,
            node.inputs, input_value, prepared.event_id, command.issued_at,
        )
        self._record_artifact_consumers(
            uow, plan, node_run_id, node.inputs, input_value,
            prepared.event_id, command.issued_at,
        )
        if source_values:
            for port_id, target in input_values.items():
                source = source_values.get(port_id)
                if source is None and len(source_values) == 1:
                    source = next(iter(source_values.values()))
                if source is not None:
                    link = ValueLink(
                        derived_id("value_link", source.value_id, target.value_id, "mapped_from"),
                        plan.run_id, source.value_id, target.value_id,
                        ValueLinkType.MAPPED_FROM, mapping_hash or definition_hash({"op": "identity"}),
                        prepared.event_id, command.issued_at,
                    )
                    uow.value_links.insert(link)
        uow.node_runs.update(
            replace(record, status=NodeRunStatus.READY, aggregate_version=AggregateVersion(2)),
            AggregateVersion(0),
        )
        ids = [prepared.event_id, ready.event_id]
        if self.work_scheduler is not None:
            ids.extend(self.work_scheduler.create_for_node(
                uow, command, events,
                replace(record, status=NodeRunStatus.READY, aggregate_version=AggregateVersion(2)),
            ))
        return ids

    def _record_artifact_consumers(self, uow, plan, node_run_id, ports, data, event_id, now):
        for port in ports:
            if port["data_policy"]["transport"] != PortTransport.ARTIFACT_REF.value:
                continue
            raw = data.get(port["id"])
            if not isinstance(raw, Mapping) or "artifact_id" not in raw:
                continue
            artifact_id = EntityId.parse(str(raw["artifact_id"]))
            artifact = uow.artifacts.get(artifact_id, committed_only=True)
            if artifact is None or artifact.schema_id != port["schema_id"]:
                raise ValueError("input Artifact is missing or has the wrong schema")
            allowed = (
                artifact.visibility is ArtifactVisibility.RUN and artifact.run_id == plan.run_id
                or artifact.visibility is ArtifactVisibility.WORKFLOW and artifact.workflow_id == plan.workflow_id
                or artifact.visibility is ArtifactVisibility.NODE and artifact.scope_id == node_run_id
            )
            if not allowed: raise ValueError("input Artifact visibility denies this NodeRun")
            uow.artifact_links.insert(ArtifactLink(
                derived_id("artifact_link", artifact_id, "consumer", node_run_id),
                plan.workflow_id, plan.run_id, artifact_id, ArtifactLinkType.CONSUMER,
                node_run_id, event_id, now,
            ))

    def _record_inline_values(
        self, uow, run_id, owner_kind, owner_id, ports, data,
        created_event_id, created_at,
    ):
        records = {}
        for port in ports:
            policy = port["data_policy"]
            if policy["transport"] != PortTransport.INLINE.value or port["id"] not in data:
                continue
            value = data[port["id"]]
            self.schema_validator(port["schema_id"], value)
            checksum = definition_hash(value)
            record = ValueRecord(
                derive_value_id(owner_id, port["id"]), run_id, owner_kind, owner_id,
                port["id"], port["schema_id"], value, checksum,
                len(canonical_json(value).encode("utf-8")),
                created_event_id, created_at,
            )
            uow.values.insert(record)
            records[port["id"]] = record
        return records

    def _start_attempt(self, uow, command, events):
        node = uow.node_runs.get(command.aggregate_id)
        if node is None:
            raise ValueError("NodeRun was not found")
        self._check_version(node, command)
        if node.status is not NodeRunStatus.READY:
            raise ValueError("StartAttempt requires a ready NodeRun")
        attempt_id = derived_id("attempt", node.node_run_id, 1)
        attempt = AttemptRecord(
            attempt_id, node.node_run_id, Revision(1), AttemptStatus.CREATED,
            AggregateVersion(0), command.issued_at, command.issued_at,
        )
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned",
            _transition_payload("node_run", NodeRunStatus.READY, NodeRunStatus.RUNNING, run_id=str(node.run_id), node_id=node.node_id),
        )
        uow.events.append(node.run_id, node.node_run_id, node.aggregate_version, (node_event,))
        uow.node_runs.update(
            replace(node, status=NodeRunStatus.RUNNING, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at),
            node.aggregate_version,
        )
        uow.attempts.create(attempt)
        leased = events.make(
            attempt_id, 1, "attempt_transitioned",
            _transition_payload("attempt", AttemptStatus.CREATED, AttemptStatus.LEASED, run_id=str(node.run_id), node_run_id=str(node.node_run_id), attempt_number=1),
        )
        running = events.make(
            attempt_id, 2, "attempt_transitioned",
            _transition_payload("attempt", AttemptStatus.LEASED, AttemptStatus.RUNNING, run_id=str(node.run_id), node_run_id=str(node.node_run_id), attempt_number=1),
        )
        uow.events.append(node.run_id, attempt_id, AggregateVersion(0), (leased, running))
        uow.attempts.update(
            replace(attempt, status=AttemptStatus.RUNNING, aggregate_version=AggregateVersion(2), updated_at=command.issued_at),
            AggregateVersion(0),
        )
        ids = [node_event.event_id, leased.event_id, running.event_id]
        return ids, AggregateVersion(3), node.run_id, {"attempt_id": str(attempt_id), "node_run_id": str(node.node_run_id)}

    def _complete_attempt(self, uow, command, events):
        attempt = uow.attempts.get(command.aggregate_id)
        if attempt is None:
            raise ValueError("Attempt was not found")
        self._check_version(attempt, command)
        if attempt.status is not AttemptStatus.RUNNING:
            raise ValueError("CompleteAttempt requires a running Attempt")
        output = dict(_require_object(command.payload.get("output", {}), "output"))
        artifact_ids = tuple(EntityId.parse(str(item)) for item in command.payload.get("artifact_refs", ()))
        node = uow.node_runs.get(attempt.node_run_id)
        if node is None or node.status is not NodeRunStatus.RUNNING:
            raise ValueError("Attempt NodeRun is not running")
        run = uow.runs.get(node.run_id)
        if run is None or run.status is not WorkflowRunStatus.RUNNING:
            raise ValueError("Attempt Run is not running")
        plan = self._load_plan(uow, run.run_id, node.source_plan_version.value)
        self._validate_ports(plan.node(node.node_id).outputs, output, "output")
        output_event = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 1,
            "attempt_output_recorded", {
                "run_id": str(run.run_id), "node_run_id": str(node.node_run_id),
                "output": output, **({"artifact_refs": [str(item) for item in artifact_ids]} if artifact_ids else {}),
            },
        )
        succeeded = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 2,
            "attempt_transitioned",
            _transition_payload("attempt", AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED, run_id=str(run.run_id), node_run_id=str(node.node_run_id), attempt_number=attempt.attempt_number.value),
        )
        uow.events.append(run.run_id, attempt.attempt_id, attempt.aggregate_version, (output_event, succeeded))
        plan_node = plan.node(node.node_id)
        output_values = self._record_inline_values(
            uow, run.run_id, DataOwnerKind.ATTEMPT_OUTPUT, attempt.attempt_id,
            plan_node.outputs, output, output_event.event_id, command.issued_at,
        )
        declared_artifact_ports = {
            port["id"]: port for port in plan_node.outputs
            if port["data_policy"]["transport"] == PortTransport.ARTIFACT_REF.value
        }
        referenced_by_output = {
            EntityId.parse(str(value["artifact_id"]))
            for port_id, value in output.items()
            if port_id in declared_artifact_ports and isinstance(value, Mapping) and "artifact_id" in value
        }
        if referenced_by_output != set(artifact_ids):
            raise ValueError("HandlerResult artifact_refs must exactly match Artifact outputs")
        for artifact_id in artifact_ids:
            staged = uow.artifacts.get(artifact_id)
            if staged is None or staged.status is not ArtifactStatus.STAGED:
                raise ValueError("Artifact output is not staged")
            if staged.producer_id != attempt.attempt_id or staged.run_id != run.run_id:
                raise ValueError("Artifact output is not owned by this Attempt")
            port = declared_artifact_ports.get(staged.output_port_id)
            if port is None or staged.schema_id != port["schema_id"]:
                raise ValueError("staged Artifact does not match output port")
            committed = replace(
                staged, status=ArtifactStatus.COMMITTED,
                committed_at=command.issued_at, created_event_id=output_event.event_id,
            )
            uow.artifacts.commit(committed)
            uow.artifact_links.insert(ArtifactLink(
                derived_id("artifact_link", artifact_id, "producer", attempt.attempt_id),
                run.workflow_id, run.run_id, artifact_id, ArtifactLinkType.PRODUCER,
                attempt.attempt_id, output_event.event_id, command.issued_at,
            ))
            input_artifacts = {
                link.artifact_id for link in uow.artifact_links.list_for_target(node.node_run_id)
                if link.link_type is ArtifactLinkType.CONSUMER
            }
            for source_id in sorted(input_artifacts, key=str):
                uow.artifact_links.insert(ArtifactLink(
                    derived_id("artifact_link", artifact_id, "derived_from", source_id),
                    run.workflow_id, run.run_id, artifact_id,
                    ArtifactLinkType.DERIVED_FROM, source_id,
                    output_event.event_id, command.issued_at,
                ))
        new_attempt_version = AggregateVersion(attempt.aggregate_version.value + 2)
        uow.attempts.update(
            replace(attempt, status=AttemptStatus.SUCCEEDED, aggregate_version=new_attempt_version, updated_at=command.issued_at),
            attempt.aggregate_version,
        )
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned",
            _transition_payload("node_run", NodeRunStatus.RUNNING, NodeRunStatus.SUCCEEDED, run_id=str(run.run_id), node_id=node.node_id),
        )
        uow.events.append(run.run_id, node.node_run_id, node.aggregate_version, (node_event,))
        succeeded_node = replace(node, status=NodeRunStatus.SUCCEEDED, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at)
        uow.node_runs.update(
            succeeded_node,
            node.aggregate_version,
        )
        ids = [output_event.event_id, succeeded.event_id, node_event.event_id]
        if isinstance(plan, GraphExecutionPlan):
            ids.extend(self._propagate_graph(
                uow, command, events, plan, succeeded_node, output,
            ))
            return ids, new_attempt_version, run.run_id, {"attempt_id": str(attempt.attempt_id), "status": "succeeded"}
        successor = plan.successor(node.node_id)
        if successor is None or successor == plan.terminal_node_id:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
                _transition_payload("workflow_run", WorkflowRunStatus.RUNNING, WorkflowRunStatus.SUCCEEDED, reason="plan_complete"),
            )
            uow.events.append(run.run_id, run.run_id, run.aggregate_version, (run_event,))
            uow.runs.update(
                replace(run, status=WorkflowRunStatus.SUCCEEDED, aggregate_version=run.aggregate_version.next(), updated_at=command.issued_at),
                run.aggregate_version,
            )
            ids.append(run_event.event_id)
        else:
            mapping = plan.mappings.get(successor, {"op": "identity"})
            mapped = evaluate_mapping(mapping, output)
            ids.extend(self._schedule(
                uow, command, events, plan, successor, mapped,
                source_values=output_values, mapping_hash=definition_hash(mapping),
            ))
        return ids, new_attempt_version, run.run_id, {"attempt_id": str(attempt.attempt_id), "status": "succeeded"}

    def _fail_attempt(self, uow, command, events):
        attempt = uow.attempts.get(command.aggregate_id)
        if attempt is None:
            raise ValueError("Attempt was not found")
        self._check_version(attempt, command)
        if attempt.status is not AttemptStatus.RUNNING:
            raise ValueError("FailAttempt requires a running Attempt")
        error = dict(_require_object(command.payload.get("error", {}), "error"))
        error_info = ErrorInfo(
            error["code"], ErrorCategory(error["category"]), error["message"],
            error["source"], error["details"], error["cause"],
        )
        node = uow.node_runs.get(attempt.node_run_id)
        run = None if node is None else uow.runs.get(node.run_id)
        if node is None or run is None:
            raise ValueError("Attempt ownership is missing")
        if node.status is not NodeRunStatus.RUNNING or run.status is not WorkflowRunStatus.RUNNING:
            raise ValueError("Attempt NodeRun and Run must be running")
        recorded = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 1,
            "attempt_failed_recorded", {"run_id": str(run.run_id), "node_run_id": str(node.node_run_id), "error": error},
        )
        failed = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 2, "attempt_transitioned",
            _transition_payload("attempt", AttemptStatus.RUNNING, AttemptStatus.FAILED, run_id=str(run.run_id), node_run_id=str(node.node_run_id), attempt_number=attempt.attempt_number.value),
        )
        uow.events.append(run.run_id, attempt.attempt_id, attempt.aggregate_version, (recorded, failed))
        new_attempt_version = AggregateVersion(attempt.aggregate_version.value + 2)
        uow.attempts.update(replace(attempt, status=AttemptStatus.FAILED, aggregate_version=new_attempt_version, updated_at=command.issued_at), attempt.aggregate_version)
        plan = self._load_plan(uow, run.run_id, node.source_plan_version.value)
        if isinstance(plan, GraphExecutionPlan):
            refs = plan.node(node.node_id).config.get("policy_refs", ())
            retry_values = [plan.policies[item]["config"] for item in refs if plan.policies[item]["kind"] == "retry"]
            attempt_count = len(uow.attempts.list_by_node_run(node.node_run_id))
            if retry_values:
                retry = retry_values[0]
                categories = tuple(retry.get("categories", ("transient_error", "timeout", "lost")))
                maximum = int(retry.get("max_attempts", 1))
                if error_info.category.value in categories and attempt_count < maximum:
                    node_event = events.make(
                        node.node_run_id, node.aggregate_version.value + 1,
                        "node_run_transitioned",
                        _transition_payload(
                            "node_run", NodeRunStatus.RUNNING, NodeRunStatus.WAITING,
                            run_id=str(run.run_id), node_id=node.node_id,
                        ),
                    )
                    uow.events.append(run.run_id, node.node_run_id, node.aggregate_version, (node_event,))
                    uow.node_runs.update(
                        replace(node, status=NodeRunStatus.WAITING, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at),
                        node.aggregate_version,
                    )
                    delays = tuple(retry.get("backoff_seconds", ()))
                    delay = delays[min(attempt_count - 1, len(delays) - 1)] if delays else 0
                    return (
                        [recorded.event_id, failed.event_id, node_event.event_id],
                        new_attempt_version, run.run_id,
                        {"attempt_id": str(attempt.attempt_id), "status": "retry_wait", "backoff_seconds": delay},
                    )
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned",
            _transition_payload("node_run", NodeRunStatus.RUNNING, NodeRunStatus.FAILED, run_id=str(run.run_id), node_id=node.node_id),
        )
        uow.events.append(run.run_id, node.node_run_id, node.aggregate_version, (node_event,))
        uow.node_runs.update(replace(node, status=NodeRunStatus.FAILED, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at), node.aggregate_version)
        if isinstance(plan, GraphExecutionPlan):
            failed_node = replace(node, status=NodeRunStatus.FAILED, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at)
            ids = [recorded.event_id, failed.event_id, node_event.event_id]
            ids.extend(self._propagate_graph(
                uow, command, events, plan, failed_node, {"error": error},
                route=(EdgeRoute.TIMEOUT if error_info.category is ErrorCategory.TIMEOUT else EdgeRoute.ERROR),
            ))
            return ids, new_attempt_version, run.run_id, {"attempt_id": str(attempt.attempt_id), "status": "failed"}
        run_event = events.make(
            run.run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
            _transition_payload("workflow_run", WorkflowRunStatus.RUNNING, WorkflowRunStatus.FAILED, reason="attempt_failed"),
        )
        uow.events.append(run.run_id, run.run_id, run.aggregate_version, (run_event,))
        uow.runs.update(replace(run, status=WorkflowRunStatus.FAILED, aggregate_version=run.aggregate_version.next(), updated_at=command.issued_at), run.aggregate_version)
        ids = [recorded.event_id, failed.event_id, node_event.event_id, run_event.event_id]
        return ids, new_attempt_version, run.run_id, {"attempt_id": str(attempt.attempt_id), "status": "failed"}

    def _cancel_run(self, uow, command, events):
        run = uow.runs.get(command.aggregate_id)
        if run is None:
            raise ValueError("WorkflowRun was not found")
        self._check_version(run, command)
        if run.status in {WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELLED}:
            raise ValueError("WorkflowRun is already terminal")
        ids = []
        if self.work_scheduler is not None:
            for job in uow.jobs.list_by_run(run.run_id):
                if job.status in {JobStatus.READY, JobStatus.LEASED, JobStatus.RUNNING, JobStatus.RETRY_WAIT}:
                    lease = uow.leases.get_active_for_job(job.job_id)
                    if lease is not None:
                        lease_event = events.make(
                            lease.lease_id, lease.aggregate_version.value + 1,
                            "lease_transitioned",
                            _transition_payload(
                                "lease", LeaseStatus.ACTIVE, LeaseStatus.RELEASED
                            ),
                        )
                        uow.events.append(run.run_id, lease.lease_id, lease.aggregate_version, (lease_event,))
                        uow.leases.update(
                            replace(
                                lease, status=LeaseStatus.RELEASED,
                                released_at=command.issued_at,
                                aggregate_version=lease.aggregate_version.next(),
                            ),
                            lease.aggregate_version,
                        )
                        ids.append(lease_event.event_id)
                    job_event = events.make(
                        job.job_id, job.aggregate_version.value + 1,
                        "job_transitioned",
                        _transition_payload("job", job.status, JobStatus.CANCELLED),
                    )
                    uow.events.append(run.run_id, job.job_id, job.aggregate_version, (job_event,))
                    uow.jobs.update(
                        replace(
                            job, status=JobStatus.CANCELLED,
                            aggregate_version=job.aggregate_version.next(),
                            updated_at=command.issued_at,
                        ),
                        job.aggregate_version,
                    )
                    ids.append(job_event.event_id)
            for timer in uow.timers.list_by_run(run.run_id):
                if timer.status in {TimerStatus.SCHEDULED, TimerStatus.LEASED}:
                    timer_event = events.make(
                        timer.timer_id, timer.aggregate_version.value + 1,
                        "timer_transitioned",
                        _transition_payload("timer", timer.status, TimerStatus.CANCELLED),
                    )
                    uow.events.append(run.run_id, timer.timer_id, timer.aggregate_version, (timer_event,))
                    uow.timers.update(
                        replace(
                            timer, status=TimerStatus.CANCELLED,
                            lease_owner=None, lease_token_hash=None,
                            lease_expires_at=None,
                            aggregate_version=timer.aggregate_version.next(),
                            updated_at=command.issued_at,
                        ),
                        timer.aggregate_version,
                    )
                    ids.append(timer_event.event_id)
        # Repository order is stable (created_at, node_run_id), but intentionally
        # is not plan-topological order. Step 8 must define topology-aware cancel.
        for node in uow.node_runs.list_by_run(run.run_id):
            for attempt in uow.attempts.list_by_node_run(node.node_run_id):
                if attempt.status in {AttemptStatus.CREATED, AttemptStatus.LEASED, AttemptStatus.RUNNING}:
                    event = events.make(
                        attempt.attempt_id, attempt.aggregate_version.value + 1, "attempt_transitioned",
                        _transition_payload("attempt", attempt.status, AttemptStatus.CANCELLED, run_id=str(run.run_id), node_run_id=str(node.node_run_id), attempt_number=attempt.attempt_number.value),
                    )
                    uow.events.append(run.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
                    uow.attempts.update(replace(attempt, status=AttemptStatus.CANCELLED, aggregate_version=attempt.aggregate_version.next(), updated_at=command.issued_at), attempt.aggregate_version)
                    ids.append(event.event_id)
            if node.status in {NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING, NodeRunStatus.WAITING}:
                event = events.make(
                    node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned",
                    _transition_payload("node_run", node.status, NodeRunStatus.CANCELLED, run_id=str(run.run_id), node_id=node.node_id),
                )
                uow.events.append(run.run_id, node.node_run_id, node.aggregate_version, (event,))
                uow.node_runs.update(replace(node, status=NodeRunStatus.CANCELLED, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at), node.aggregate_version)
                ids.append(event.event_id)
        human_rows = uow.connection.execute(
            "SELECT * FROM human_tasks WHERE run_id=? AND status IN ('waiting','claimed')",
            (str(run.run_id),),
        ).fetchall()
        for row in human_rows:
            append_control_event(
                uow.connection, run_id=run.run_id,
                aggregate_id=EntityId.parse(row["task_id"]),
                event_type="human_task_cancelled",
                payload={"reason": str(command.payload.get("reason", "cancelled"))},
                actor=command.actor,
                idempotency_key=f"cancel-run:{command.idempotency_key}",
                occurred_at=command.issued_at,
            )
            uow.connection.execute(
                """UPDATE human_tasks SET status='cancelled',
                     submission_token_hash='used',aggregate_version=aggregate_version+1,
                     updated_at=? WHERE task_id=?""",
                (command.issued_at.isoformat(), row["task_id"]),
            )
        event = events.make(
            run.run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
            _transition_payload("workflow_run", run.status, WorkflowRunStatus.CANCELLED, reason=str(command.payload.get("reason", "cancelled"))),
        )
        uow.events.append(run.run_id, run.run_id, run.aggregate_version, (event,))
        new_version = run.aggregate_version.next()
        uow.runs.update(replace(run, status=WorkflowRunStatus.CANCELLED, aggregate_version=new_version, updated_at=command.issued_at), run.aggregate_version)
        ids.append(event.event_id)
        return ids, new_version, run.run_id, {"run_id": str(run.run_id), "status": "cancelled"}

    def _cancel_node(self, uow, command, events):
        node = uow.node_runs.get(command.aggregate_id)
        if node is None:
            raise ValueError("NodeRun was not found")
        self._check_version(node, command)
        if node.status not in {NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING, NodeRunStatus.WAITING}:
            raise ValueError("NodeRun is already terminal")
        ids = []
        for attempt in uow.attempts.list_by_node_run(node.node_run_id):
            if attempt.status in {AttemptStatus.CREATED, AttemptStatus.LEASED, AttemptStatus.RUNNING}:
                event = events.make(
                    attempt.attempt_id, attempt.aggregate_version.value + 1,
                    "attempt_transitioned",
                    _transition_payload(
                        "attempt", attempt.status, AttemptStatus.CANCELLED,
                        run_id=str(node.run_id), node_run_id=str(node.node_run_id),
                        attempt_number=attempt.attempt_number.value,
                    ),
                )
                uow.events.append(node.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
                uow.attempts.update(
                    replace(attempt, status=AttemptStatus.CANCELLED, aggregate_version=attempt.aggregate_version.next(), updated_at=command.issued_at),
                    attempt.aggregate_version,
                )
                ids.append(event.event_id)
        event = events.make(
            node.node_run_id, node.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", node.status, NodeRunStatus.CANCELLED,
                run_id=str(node.run_id), node_id=node.node_id,
            ),
        )
        uow.events.append(node.run_id, node.node_run_id, node.aggregate_version, (event,))
        cancelled = replace(
            node, status=NodeRunStatus.CANCELLED,
            aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at,
        )
        uow.node_runs.update(cancelled, node.aggregate_version)
        ids.append(event.event_id)
        plan = self._load_plan(uow, node.run_id, node.source_plan_version.value)
        if isinstance(plan, GraphExecutionPlan):
            ids.extend(self._propagate_graph(
                uow, command, events, plan, cancelled,
                {"reason": str(command.payload.get("reason", "cancelled"))},
                route=EdgeRoute.CANCEL,
            ))
        return ids, cancelled.aggregate_version, node.run_id, {
            "node_run_id": str(node.node_run_id), "status": "cancelled",
        }

    @staticmethod
    def _load_plan(uow, run_id: EntityId, version: int) -> ExecutionPlan:
        record = uow.plans.get(run_id, Revision(version))
        if record is None:
            raise ValueError("ExecutionPlan was not found")
        if definition_hash(record.plan) != record.definition_hash:
            raise ValueError("ExecutionPlan hash mismatch")
        return execution_plan_from_primitive(to_primitive(record.plan))

    def _validate_ports(self, ports, value: Mapping[str, Any], kind: str) -> None:
        by_id = {item["id"]: item for item in ports}
        missing = [
            port_id for port_id, port in by_id.items()
            if port.get("required") and port_id not in value and not port.get("has_default")
        ]
        if missing:
            raise ValueError(f"missing required {kind} ports: {missing}")
        extra = set(value) - set(by_id)
        if extra:
            raise ValueError(f"unknown {kind} ports: {sorted(extra)}")
        for port_id, item in value.items():
            self.schema_validator(by_id[port_id]["schema_id"], item)

    def _schedule_graph(
        self, uow, command, events, plan: GraphExecutionPlan, node_id: str,
        input_value: Mapping[str, Any], *, generation: int, activation_key: str,
    ):
        node = plan.node(node_id)
        run = uow.runs.get(plan.run_id)
        if run is None or run.status not in {
            WorkflowRunStatus.RUNNING, WorkflowRunStatus.WAITING,
        }:
            raise ValueError("Graph nodes require a running or waiting Run")
        node_run_id = derive_graph_node_run_id(
            plan.run_id, plan.plan_version, node_id, generation, activation_key,
        )
        existing = uow.node_runs.get(node_run_id)
        if existing is not None:
            return []
        self._validate_ports(node.inputs, input_value, "input")
        record = NodeRunRecord(
            node_run_id, plan.run_id, node_id, plan.plan_version,
            NodeRunStatus.PENDING, AggregateVersion(0), command.issued_at,
            command.issued_at, generation, activation_key,
        )
        uow.node_runs.create(record)
        prepared = events.make(
            node_run_id, 1, "node_input_prepared",
            {"run_id": str(plan.run_id), "node_id": node_id, "input": dict(input_value)},
        )
        ready = events.make(
            node_run_id, 2, "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.PENDING, NodeRunStatus.READY,
                run_id=str(plan.run_id), node_id=node_id,
                plan_version=plan.plan_version.value,
                generation=generation, activation_key=activation_key,
            ),
        )
        uow.events.append(plan.run_id, node_run_id, AggregateVersion(0), (prepared, ready))
        self._record_inline_values(
            uow, plan.run_id, DataOwnerKind.NODE_INPUT, node_run_id,
            node.inputs, input_value, prepared.event_id, command.issued_at,
        )
        ready_record = replace(record, status=NodeRunStatus.READY, aggregate_version=AggregateVersion(2))
        uow.node_runs.update(ready_record, AggregateVersion(0))
        ids = [prepared.event_id, ready.event_id]
        if node.kind == "action":
            if self.work_scheduler is not None:
                ids.extend(self.work_scheduler.create_for_node(uow, command, events, ready_record))
            return ids
        if node.kind == "human":
            ids.extend(self._activate_human_controller(
                uow, command, events, plan, ready_record, input_value,
            ))
            return ids
        if events.graph_reactions >= self.MAX_GRAPH_REACTIONS_PER_COMMAND:
            # The READY controller is a durable continuation. Recovery submits
            # AdvanceGraph, which resumes from node_input_prepared without
            # reevaluating any already-recorded RouteDecision.
            return ids
        events.graph_reactions += 1
        ids.extend(self._execute_graph_controller(
            uow, command, events, plan, ready_record, input_value,
        ))
        return ids

    def _activate_human_controller(
        self, uow, command, events, plan, ready_record, input_value,
    ):
        node = plan.node(ready_record.node_id)
        config = dict(node.config)
        participants = tuple(sorted(set(config.get("participants", ()))))
        if not participants:
            raise ValueError("human node requires participants")
        kind = config.get("task_kind")
        if kind != "approval":
            raise ValueError("static human node task_kind must be approval")
        payload = {
            "node_id": node.node_id,
            "node_run_id": str(ready_record.node_run_id),
            "input": dict(input_value),
        }
        request_hash = definition_hash({
            "run": str(plan.run_id), "node_run": str(ready_record.node_run_id),
            "kind": kind, "payload": payload, "participants": participants,
            "quorum": "any", "count": 1,
        })
        task_id = EntityId(
            "human_task", request_hash.value.removeprefix("sha256:")
        )
        token = secrets.token_urlsafe(32)
        human_event = append_control_event(
            uow.connection, run_id=plan.run_id, aggregate_id=task_id,
            event_type="human_task_created",
            payload={
                "kind": kind, "request_hash": request_hash.value,
                "node_run_id": str(ready_record.node_run_id),
            },
            actor=command.actor,
            idempotency_key=f"activate:{ready_record.node_run_id}",
            occurred_at=command.issued_at,
        )
        uow.connection.execute(
            """INSERT INTO human_tasks(
                 task_id,run_id,node_run_id,kind,status,request_hash,
                 capability_scope,submission_token_hash,actor,payload_json,
                 result_json,deadline_at,aggregate_version,created_at,updated_at,
                 assignee,role,form_schema_json,quorum_kind,quorum_count,
                 reminder_interval_seconds,escalation_policy_json,claimed_by,revision
               ) VALUES (?,?,?,?,'waiting',?,NULL,?,?,?,NULL,NULL,1,?,?,NULL,NULL,
                         NULL,'any',1,NULL,NULL,NULL,1)""",
            (
                str(task_id), str(plan.run_id), str(ready_record.node_run_id),
                kind, request_hash.value, submission_token_hash(token),
                command.actor, canonical_json(payload),
                command.issued_at.isoformat(), command.issued_at.isoformat(),
            ),
        )
        for participant in participants:
            uow.connection.execute(
                "INSERT INTO human_task_participants(task_id,actor,revision) VALUES (?,?,1)",
                (str(task_id), participant),
            )
        running = events.make(
            ready_record.node_run_id, ready_record.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.READY, NodeRunStatus.RUNNING,
                run_id=str(plan.run_id), node_id=ready_record.node_id,
            ),
        )
        waiting = events.make(
            ready_record.node_run_id, ready_record.aggregate_version.value + 2,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.RUNNING, NodeRunStatus.WAITING,
                run_id=str(plan.run_id), node_id=ready_record.node_id,
            ),
        )
        uow.events.append(
            plan.run_id, ready_record.node_run_id,
            ready_record.aggregate_version, (running, waiting),
        )
        uow.node_runs.update(
            replace(
                ready_record, status=NodeRunStatus.WAITING,
                aggregate_version=AggregateVersion(
                    ready_record.aggregate_version.value + 2
                ),
                updated_at=command.issued_at,
            ),
            ready_record.aggregate_version,
        )
        run = uow.runs.get(plan.run_id)
        # Control events use their own deterministic command identity; Runtime
        # command receipts therefore list only events caused by this command.
        ids = [running.event_id, waiting.event_id]
        if run.status is WorkflowRunStatus.RUNNING:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.RUNNING,
                    WorkflowRunStatus.WAITING, reason="human_wait",
                ),
            )
            uow.events.append(
                run.run_id, run.run_id, run.aggregate_version, (run_event,)
            )
            uow.runs.update(
                replace(
                    run, status=WorkflowRunStatus.WAITING,
                    aggregate_version=run.aggregate_version.next(),
                    updated_at=command.issued_at,
                ),
                run.aggregate_version,
            )
            ids.append(run_event.event_id)
        audit(
            uow.connection, run_id=plan.run_id, actor=command.actor,
            action="human.create", target_id=str(task_id), decision="allowed",
            details={"node_run_id": str(ready_record.node_run_id)},
            occurred_at=command.issued_at,
        )
        events.human_deliveries.append((task_id, participants, token))
        return ids

    def _submit_human_task(self, uow, command, events):
        row = uow.connection.execute(
            "SELECT * FROM human_tasks WHERE task_id=?",
            (str(command.aggregate_id),),
        ).fetchone()
        if row is None or row["node_run_id"] is None:
            raise ValueError("linked HumanTask was not found")
        if row["aggregate_version"] != command.expected_version.value:
            raise ConcurrencyConflictError(
                command.aggregate_id, command.expected_version.value,
                row["aggregate_version"],
            )
        if row["status"] not in {"waiting", "claimed"}:
            raise ValueError("HumanTask is terminal")
        if submission_token_hash(command.payload["submission_token"]) != row["submission_token_hash"]:
            raise PermissionError("invalid submission token")
        participant = uow.connection.execute(
            "SELECT 1 FROM human_task_participants WHERE task_id=? AND actor=?",
            (row["task_id"], command.actor),
        ).fetchone()
        if participant is None:
            raise PermissionError("actor is not a HumanTask participant")
        decision = command.payload["decision"]
        value = command.payload.get("value")
        if decision == "withdraw":
            raise ValueError("static Human node does not support withdraw")
        status = "rejected" if decision == "reject" else "completed"
        human_event = append_control_event(
            uow.connection, run_id=EntityId.parse(row["run_id"]),
            aggregate_id=command.aggregate_id,
            event_type="human_task_submitted",
            payload={
                "actor": command.actor, "decision": decision,
                "status": status, "value": value,
                "node_run_id": row["node_run_id"],
            },
            actor=command.actor, idempotency_key=command.idempotency_key,
            occurred_at=command.issued_at,
        )
        uow.connection.execute(
            """UPDATE human_tasks SET status=?,actor=?,result_json=?,
                 submission_token_hash='used',aggregate_version=aggregate_version+1,
                 updated_at=? WHERE task_id=? AND aggregate_version=?""",
            (
                status, command.actor,
                canonical_json({"decision": decision, "value": value}),
                command.issued_at.isoformat(), row["task_id"],
                command.expected_version.value,
            ),
        )
        uow.connection.execute(
            """UPDATE human_task_participants SET decision=?,value_json=?,
                 submitted_at=?,revision=revision+1 WHERE task_id=? AND actor=?""",
            (
                decision, canonical_json(value), command.issued_at.isoformat(),
                row["task_id"], command.actor,
            ),
        )
        node = uow.node_runs.get(EntityId.parse(row["node_run_id"]))
        if node is None or node.status is not NodeRunStatus.WAITING:
            raise ValueError("HumanTask NodeRun is not waiting")
        run = uow.runs.get(node.run_id)
        plan = self._load_plan(uow, node.run_id, node.source_plan_version.value)
        plan_node = plan.node(node.node_id)
        if len(plan_node.outputs) != 1:
            raise ValueError("human node requires exactly one output port")
        output = {
            plan_node.outputs[0]["id"]: {
                "decision": decision, "value": value,
            }
        }
        self._validate_ports(plan_node.outputs, output, "output")
        target = (
            NodeRunStatus.FAILED if status == "rejected"
            else NodeRunStatus.SUCCEEDED
        )
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.WAITING, target,
                run_id=str(node.run_id), node_id=node.node_id,
            ),
        )
        uow.events.append(
            node.run_id, node.node_run_id, node.aggregate_version, (node_event,)
        )
        finished_node = replace(
            node, status=target, aggregate_version=node.aggregate_version.next(),
            updated_at=command.issued_at,
        )
        uow.node_runs.update(finished_node, node.aggregate_version)
        ids = [node_event.event_id]
        other_waiting = uow.connection.execute(
            """SELECT 1 FROM human_tasks WHERE run_id=? AND task_id<>?
               AND status IN ('waiting','claimed') LIMIT 1""",
            (str(node.run_id), row["task_id"]),
        ).fetchone()
        if run.status is WorkflowRunStatus.WAITING and other_waiting is None:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.RUNNING, reason="human_submitted",
                ),
            )
            uow.events.append(
                run.run_id, run.run_id, run.aggregate_version, (run_event,)
            )
            uow.runs.update(
                replace(
                    run, status=WorkflowRunStatus.RUNNING,
                    aggregate_version=run.aggregate_version.next(),
                    updated_at=command.issued_at,
                ),
                run.aggregate_version,
            )
            ids.append(run_event.event_id)
        route = EdgeRoute.ERROR if status == "rejected" else EdgeRoute.SUCCESS
        ids.extend(self._propagate_graph(
            uow, command, events, plan, finished_node, output, route=route,
        ))
        audit(
            uow.connection, run_id=node.run_id, actor=command.actor,
            action="human.submit", target_id=row["task_id"], decision=decision,
            details={"status": status, "node_run_id": row["node_run_id"]},
            occurred_at=command.issued_at,
        )
        return (
            ids, AggregateVersion(command.expected_version.value + 1),
            node.run_id,
            {"task_id": row["task_id"], "decision": decision, "status": status},
        )

    def _execute_graph_controller(
        self, uow, command, events, plan, ready_record, input_value,
    ):
        node_run_id = ready_record.node_run_id
        node_id = ready_record.node_id
        node = plan.node(node_id)
        running = events.make(
            node_run_id, 3, "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.READY, NodeRunStatus.RUNNING,
                run_id=str(plan.run_id), node_id=node_id,
            ),
        )
        succeeded = events.make(
            node_run_id, 4, "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.RUNNING, NodeRunStatus.SUCCEEDED,
                run_id=str(plan.run_id), node_id=node_id,
            ),
        )
        uow.events.append(plan.run_id, node_run_id, ready_record.aggregate_version, (running, succeeded))
        succeeded_record = replace(
            ready_record, status=NodeRunStatus.SUCCEEDED,
            aggregate_version=AggregateVersion(ready_record.aggregate_version.value + 2), updated_at=command.issued_at,
        )
        uow.node_runs.update(succeeded_record, ready_record.aggregate_version)
        ids = [running.event_id, succeeded.event_id]
        if node.kind == "terminal":
            ids.extend(self._maybe_complete_graph(uow, command, events, plan))
        else:
            ids.extend(self._propagate_graph(
                uow, command, events, plan, succeeded_record, input_value,
            ))
        return ids

    def _propagate_graph(
        self, uow, command, events, plan, source_node, source_value,
        *, route=EdgeRoute.SUCCESS,
    ):
        decision = evaluate_route(
            plan, source_node.node_run_id, source_node.node_id,
            route, source_value,
        )
        route_event = events.make(
            source_node.node_run_id, source_node.aggregate_version.value + 1,
            "graph_route_decided",
            {
                "run_id": str(plan.run_id),
                "node_run_id": str(source_node.node_run_id),
                "decision": to_primitive(decision),
            },
        )
        uow.events.append(
            plan.run_id, source_node.node_run_id,
            source_node.aggregate_version, (route_event,),
        )
        uow.node_runs.update(
            replace(source_node, aggregate_version=source_node.aggregate_version.next()),
            source_node.aggregate_version,
        )
        ids = [route_event.event_id]
        selected = set(decision.selected_edge_ids)
        touched_joins = set()
        for edge_id in decision.evaluated_edge_ids:
            edge = plan.edge(edge_id)
            target_generation = source_node.generation + 1 if edge.back_edge else 1
            token_id = derive_branch_token_id(
                plan.run_id, plan.plan_version, edge.edge_id,
                source_node.generation, str(source_node.node_run_id),
            )
            if uow.tokens.get(token_id) is not None:
                continue
            status = BranchTokenStatus.COMPLETED if edge_id in selected else BranchTokenStatus.NOT_SELECTED
            mapped = evaluate_mapping(edge.mapping, source_value) if edge_id in selected else None
            scope = {
                "plan_version": plan.plan_version.value, "edge_id": edge.edge_id,
                "target_node_id": edge.target_node_id,
                "target_generation": target_generation,
                "branch_group": str(source_node.node_run_id), "value": mapped,
            }
            token = BranchTokenRecord(
                token_id, plan.run_id, source_node.node_run_id,
                BranchTokenStatus.ACTIVE, AggregateVersion(0), scope,
                command.issued_at, command.issued_at,
            )
            uow.tokens.create(token)
            token_event = events.make(
                token_id, 1, "branch_token_transitioned",
                _transition_payload(
                    "branch_token", BranchTokenStatus.ACTIVE, status,
                    run_id=str(plan.run_id), edge_id=edge.edge_id,
                    target_node_id=edge.target_node_id,
                    target_generation=target_generation,
                    scope=to_primitive(scope),
                ),
            )
            uow.events.append(plan.run_id, token_id, AggregateVersion(0), (token_event,))
            uow.tokens.update(
                replace(token, status=status, aggregate_version=AggregateVersion(1)),
                AggregateVersion(0),
            )
            ids.append(token_event.event_id)
            target = plan.node(edge.target_node_id)
            if target.kind == "join":
                touched_joins.add(target.node_id)
            elif edge_id in selected:
                if edge.back_edge:
                    allowed, counter_event_id = self._consume_graph_counter(
                        uow, command, events, plan, edge, source_node,
                    )
                    if counter_event_id is not None:
                        ids.append(counter_event_id)
                    if not allowed:
                        ids.extend(self._fail_graph_run(
                            uow, command, events, plan.run_id,
                            "loop_limit_exceeded" if plan.policies[edge.policy_ref]["kind"] == "loop" else "rework_limit_exceeded",
                        ))
                        continue
                ids.extend(self._schedule_graph(
                    uow, command, events, plan, target.node_id, mapped,
                    generation=target_generation, activation_key=str(token_id),
                ))
        for join_node_id in sorted(touched_joins):
            ids.extend(self._consider_join(uow, command, events, plan, join_node_id))
        if not decision.selected_edge_ids and not touched_joins:
            ids.extend(self._fail_graph_run(uow, command, events, plan.run_id, "graph_stalled"))
        elif uow.runs.get(plan.run_id).status is WorkflowRunStatus.RUNNING:
            ids.extend(self._maybe_complete_graph(uow, command, events, plan))
        return ids

    def _advance_graph(self, uow, command, events):
        if not command.actor.startswith("system:"):
            raise ValueError("AdvanceGraph is system-only")
        run = uow.runs.get(command.aggregate_id)
        if run is None:
            raise ValueError("WorkflowRun was not found")
        self._check_version(run, command)
        plan = self._load_plan(uow, run.run_id, int(command.payload.get("plan_version", 1)))
        if not isinstance(plan, GraphExecutionPlan):
            raise ValueError("AdvanceGraph requires ExecutionPlan 1.2")
        ids = []
        ready_controllers = [
            item for item in uow.node_runs.list_by_run(run.run_id)
            if item.status is NodeRunStatus.READY
            and plan.node(item.node_id).kind in {"human", "decision", "join", "terminal"}
        ]
        for node in sorted(ready_controllers, key=lambda item: (item.generation, item.node_id, str(item.node_run_id))):
            if events.graph_reactions >= self.MAX_GRAPH_REACTIONS_PER_COMMAND:
                break
            prepared = next(
                (
                    item.envelope.payload["input"]
                    for item in uow.events.read_stream(node.node_run_id, limit=10)
                    if item.envelope.event_type == "node_input_prepared"
                ),
                None,
            )
            if prepared is None:
                raise ValueError("Graph continuation input is missing")
            events.graph_reactions += 1
            if plan.node(node.node_id).kind == "human":
                ids.extend(self._activate_human_controller(
                    uow, command, events, plan, node, prepared,
                ))
            else:
                ids.extend(self._execute_graph_controller(
                    uow, command, events, plan, node, prepared,
                ))
        for group in uow.joins.list_by_run(run.run_id, waiting_only=True):
            ids.extend(self._consider_join(uow, command, events, plan, group.node_id))
        if not ids:
            ids.extend(self._maybe_complete_graph(uow, command, events, plan))
        if not ids:
            waiting_join = bool(uow.joins.list_by_run(run.run_id, waiting_only=True))
            active_job = any(
                item.status in {JobStatus.READY, JobStatus.LEASED, JobStatus.RUNNING, JobStatus.RETRY_WAIT}
                for item in uow.jobs.list_by_run(run.run_id)
            )
            active_timer = any(
                item.status in {TimerStatus.SCHEDULED, TimerStatus.LEASED}
                for item in uow.timers.list_by_run(run.run_id)
            )
            active_human = uow.connection.execute(
                """SELECT 1 FROM human_tasks WHERE run_id=?
                   AND status IN ('waiting','claimed') LIMIT 1""",
                (str(run.run_id),),
            ).fetchone() is not None
            if (
                not waiting_join and not active_job and not active_timer
                and not active_human
            ):
                ids.extend(self._fail_graph_run(
                    uow, command, events, run.run_id, "graph_stalled",
                ))
        current = uow.runs.get(run.run_id)
        return ids, current.aggregate_version, run.run_id, {
            "run_id": str(run.run_id), "event_count": len(ids),
        }

    def _join_policy(self, plan, node):
        refs = node.config.get("policy_refs", ())
        values = [plan.policies[item] for item in refs if plan.policies[item]["kind"] == "join"]
        if len(values) != 1:
            raise ValueError("join node requires one compiled join policy")
        config = values[0]["config"]
        return JoinPolicy(
            JoinMode(config["mode"]), JoinMergeMode(config.get("merge_mode", "array_by_edge")),
            config.get("threshold"), config.get("deadline_seconds"),
            config.get("min_successful"),
        )

    def _consider_join(self, uow, command, events, plan, node_id, *, deadline_fired=False):
        node = plan.node(node_id)
        incoming = tuple(sorted(plan.incoming(node_id), key=lambda edge: (edge.priority, edge.edge_id)))
        tokens = uow.tokens.list_by_run(plan.run_id)
        by_edge = {}
        for token in tokens:
            edge_id = token.scope.get("edge_id")
            if edge_id in {edge.edge_id for edge in incoming}:
                by_edge[edge_id] = token
        facts = tuple(
            JoinTokenFact(
                edge.edge_id, edge.priority,
                by_edge[edge.edge_id].status if edge.edge_id in by_edge else BranchTokenStatus.ACTIVE,
                by_edge[edge.edge_id].scope.get("value") if edge.edge_id in by_edge else None,
            )
            for edge in incoming
        )
        group_id = derive_join_group_id(plan.run_id, plan.plan_version, node_id, 1)
        group = uow.joins.get(group_id)
        policy = self._join_policy(plan, node)
        created = group is None
        if created:
            group = JoinGroupRecord(
                group_id, plan.run_id, node_id, 1, policy,
                tuple(edge.edge_id for edge in incoming), JoinGroupStatus.WAITING,
                None, AggregateVersion(0), command.issued_at, command.issued_at,
            )
            uow.joins.create(group)
        timer_ids = []
        if created and policy.mode is JoinMode.DEADLINE and hasattr(self, "_make_timer"):
            from datetime import timedelta
            from ..domain.durable_execution import TimerPurpose
            _, timer_ids = self._make_timer(
                uow, command, events, run_id=plan.run_id,
                purpose=TimerPurpose.JOIN_DEADLINE,
                dedupe_key=f"{group_id}:deadline", target_type="join_group",
                target_id=group_id, payload={"node_id": node_id},
                due_at=command.issued_at + timedelta(seconds=policy.deadline_seconds),
            )
        decision, merged = evaluate_join(
            group_id, policy, facts, deadline_fired=deadline_fired,
        )
        if decision.disposition.value == "wait":
            return timer_ids
        if group.status is not JoinGroupStatus.WAITING:
            return []
        target_status = {
            "open": JoinGroupStatus.OPEN, "fail": JoinGroupStatus.FAILED,
            "timed_out": JoinGroupStatus.TIMED_OUT,
        }[decision.disposition.value]
        event = events.make(
            group_id, group.aggregate_version.value + 1, "join_decided",
            {
                "run_id": str(plan.run_id), "join_group_id": str(group_id),
                "decision": to_primitive(decision),
                **({"input": to_primitive(merged)} if merged is not None else {}),
            },
        )
        uow.events.append(plan.run_id, group_id, group.aggregate_version, (event,))
        uow.joins.update(
            replace(
                group, status=target_status, decision=to_primitive(decision),
                aggregate_version=group.aggregate_version.next(),
                updated_at=command.issued_at,
            ), group.aggregate_version,
        )
        ids = [*timer_ids, event.event_id]
        if target_status is JoinGroupStatus.OPEN:
            port_ids = tuple(port["id"] for port in node.inputs)
            join_input = merged
            if not isinstance(join_input, Mapping) or not set(join_input).issubset(port_ids):
                if len(port_ids) != 1:
                    raise ValueError("non-object Join merge requires exactly one input port")
                join_input = {port_ids[0]: merged}
            ids.extend(self._schedule_graph(
                uow, command, events, plan, node_id, join_input,
                generation=1, activation_key=str(group_id),
            ))
        else:
            ids.extend(self._fail_graph_run(uow, command, events, plan.run_id, "join_failed"))
        return ids

    def _consume_graph_counter(self, uow, command, events, plan, edge, source_node):
        # Counter persistence is wired in Migration v5.  The full policy object
        # is validated by the compiler; derive a stable scope per source branch.
        policy = plan.policies.get(edge.policy_ref)
        if policy is None or policy["kind"] not in {"loop", "rework"}:
            raise ValueError("back edge has no loop/rework policy")
        field = "max_iterations" if policy["kind"] == "loop" else "max_generations"
        limit = int(policy["config"][field])
        from .events import derived_id
        scope_key = edge.edge_id
        counter_id = derived_id("control_counter", plan.run_id, edge.policy_ref, scope_key)
        record = uow.counters.get(counter_id)
        if record is None:
            from ..domain.graph_persistence import ControlCounterRecord
            record = ControlCounterRecord(
                counter_id, plan.run_id, edge.policy_ref, scope_key,
                0, limit, AggregateVersion(0), command.issued_at,
            )
            uow.counters.create(record)
        if record.value >= record.limit:
            return False, None
        updated = uow.counters.increment(counter_id, record.aggregate_version, command.issued_at)
        event = events.make(
            counter_id, updated.aggregate_version.value,
            "control_counter_incremented",
            {
                "run_id": str(plan.run_id), "policy_id": edge.policy_ref,
                "scope_key": scope_key, "value": updated.value,
                "limit": updated.limit,
            },
        )
        uow.events.append(plan.run_id, counter_id, record.aggregate_version, (event,))
        return True, event.event_id

    def _maybe_complete_graph(self, uow, command, events, plan):
        nodes = uow.node_runs.list_by_run(plan.run_id)
        active = {
            NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING,
            NodeRunStatus.WAITING,
        }
        if any(item.status in active for item in nodes):
            return []
        if not any(
            item.node_id in plan.terminal_node_ids
            and item.status is NodeRunStatus.SUCCEEDED
            for item in nodes
        ):
            return []
        return self._complete_graph_run(uow, command, events, plan.run_id)

    def _complete_graph_run(self, uow, command, events, run_id):
        run = uow.runs.get(run_id)
        if run.status is not WorkflowRunStatus.RUNNING:
            return []
        event = events.make(
            run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
            _transition_payload(
                "workflow_run", WorkflowRunStatus.RUNNING,
                WorkflowRunStatus.SUCCEEDED, reason="graph_complete",
            ),
        )
        uow.events.append(run_id, run_id, run.aggregate_version, (event,))
        uow.runs.update(
            replace(run, status=WorkflowRunStatus.SUCCEEDED, aggregate_version=run.aggregate_version.next(), updated_at=command.issued_at),
            run.aggregate_version,
        )
        return [event.event_id]

    def _fail_graph_run(self, uow, command, events, run_id, reason):
        run = uow.runs.get(run_id)
        if run.status is not WorkflowRunStatus.RUNNING:
            return []
        event = events.make(
            run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
            _transition_payload(
                "workflow_run", WorkflowRunStatus.RUNNING,
                WorkflowRunStatus.FAILED, reason=reason,
            ),
        )
        uow.events.append(run_id, run_id, run.aggregate_version, (event,))
        uow.runs.update(
            replace(run, status=WorkflowRunStatus.FAILED, aggregate_version=run.aggregate_version.next(), updated_at=command.issued_at),
            run.aggregate_version,
        )
        return [event.event_id]
