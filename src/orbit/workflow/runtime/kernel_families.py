"""Transactional Run/Node/Graph command-family implementation.

The public entry point lives in ``runtime.kernel``.  Keeping this implementation
module private lets later command families evolve without growing the public
Kernel boundary or introducing nested Units of Work.
"""

from __future__ import annotations

from dataclasses import replace
import json
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
from ..domain.foreach import derive_group_id, derive_item_id, stable_aggregate
from ..domain.graph_persistence import JoinGroupRecord, JoinGroupStatus
from ..handlers.registry import (
    HandlerContractMismatchError, HandlerNotAvailableError,
)
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
from ..planner.context import build_planning_context


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
        planner_service: Any = None,
        budget_service: Any = None,
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
        self.planner_service = planner_service
        self.budget_service = budget_service
        self.command_router = CommandRouter(self)

    def handle(self, command: CommandEnvelope) -> CommandResult:
        if command.command_type not in RUNTIME_COMMAND_TYPES:
            return self._rejected("UNKNOWN_COMMAND", f"unsupported command {command.command_type}", command)
        if command.command_type in {"schedule_node", "advance_graph", "apply_planner_proposal"} and not command.actor.startswith("system:"):
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
        except (HandlerNotAvailableError, HandlerContractMismatchError) as exc:
            # A plan names the exact Handler build it was published against. When
            # that build is no longer registered the run cannot start, and the
            # operator needs to read which one is missing — not "internal error".
            return self._rejected("HANDLER_UNAVAILABLE", str(exc), command)
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
        if command.command_type == "apply_planner_proposal":
            return {"proposal_id": command.payload["proposal_id"]}
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
        goal = str(payload.get("goal", "")).strip() or None
        display_name = (
            goal.splitlines()[0][:120] if goal is not None else str(command.aggregate_id)
        )
        artifact_inputs = tuple(payload.get("artifact_inputs", ()))
        budget_microunits = payload.get("budget_microunits")
        if (
            isinstance(budget_microunits, bool)
            or budget_microunits is not None
            and (not isinstance(budget_microunits, int) or budget_microunits < 0)
        ):
            raise ValueError("Run budget must be a non-negative integer")
        artifact_subjects = tuple(payload.get("artifact_subjects", ()))
        artifact_scope = tuple(payload.get("artifact_scope", ()))
        if (artifact_subjects or artifact_scope) and command.actor != "system:subflow":
            raise ValueError("Artifact ACL transfer is system:subflow-only")
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
            command.issued_at, command.issued_at, goal, display_name,
        )
        uow.runs.create(run)
        if budget_microunits is not None:
            if self.budget_service is None or getattr(uow, "connection", None) is None:
                raise ValueError("Run budget requires durable Budget persistence")
            self.budget_service.ensure_account_in_uow(
                uow.connection, run.run_id, budget_microunits,
                actor=command.actor, now=command.issued_at,
            )
        connection = getattr(uow, "connection", None)
        if connection is not None and not command.actor.startswith("system:"):
            connection.execute(
                "INSERT OR IGNORE INTO run_artifact_subjects(run_id,subject,role,created_at) VALUES (?,?,'owner',?)",
                (str(run.run_id), command.actor, command.issued_at.isoformat()),
            )
        for subject in artifact_subjects:
            if not isinstance(subject, str) or not subject.strip():
                raise ValueError("Artifact transfer subject is invalid")
            if connection is None:
                raise ValueError("Artifact ACL transfer requires durable persistence")
            connection.execute(
                "INSERT OR IGNORE INTO run_artifact_subjects(run_id,subject,role,created_at) VALUES (?,?,'participant',?)",
                (str(run.run_id), subject, command.issued_at.isoformat()),
            )
        for raw_artifact_id in artifact_scope:
            if connection is None:
                raise ValueError("Artifact ACL transfer requires durable persistence")
            artifact_id = EntityId.parse(str(raw_artifact_id))
            artifact = uow.artifacts.get(artifact_id, committed_only=True)
            if artifact is None:
                raise ValueError("Subflow Artifact transfer source is missing")
            for subject in artifact_subjects:
                prior = connection.execute(
                    "SELECT 1 FROM artifact_acl WHERE artifact_id=? AND subject=? AND permission='read'",
                    (str(artifact_id), subject),
                ).fetchone()
                if prior is None:
                    raise ValueError("Subflow Artifact transfer would expand subject authority")
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_acl(artifact_id,subject,permission,granted_by,created_at) VALUES (?,?,'read','system:subflow',?)",
                    (str(artifact_id), subject, command.issued_at.isoformat()),
                )
        run_event = events.make(
            run.run_id, 1, "workflow_run_transitioned",
            _transition_payload(
                "workflow_run", WorkflowRunStatus.CREATED, WorkflowRunStatus.RUNNING,
                workflow_id=str(workflow_id), workflow_version=workflow_version.value,
                definition_hash=expected_hash.value, plan_id=str(plan_id), plan_version=1,
                input=initial_input, **({"goal": goal} if goal is not None else {}),
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
        foreach_children = uow.connection.execute(
            """SELECT g.group_id,i.item_id,i.child_run_id
                FROM foreach_groups g JOIN foreach_items i ON i.group_id=g.group_id
                 JOIN workflow_runs child ON child.run_id=i.child_run_id
                WHERE g.run_id=? AND g.status='running' AND i.status='running'
                ORDER BY g.group_id,i.item_index""",
            (str(run.run_id),),
        ).fetchall()
        for item in foreach_children:
            child_run_id = EntityId.parse(item["child_run_id"])
            child = uow.runs.get(child_run_id)
            if child.status not in {
                WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED,
                WorkflowRunStatus.CANCELLED,
            }:
                child_command = CommandEnvelope.create(
                    command_type="cancel_run", aggregate_id=child_run_id,
                    correlation_id=run.run_id,
                    expected_version=child.aggregate_version,
                    idempotency_key=f"foreach-parent-cancel:{item['item_id']}",
                    actor="system:foreach", issued_at=command.issued_at,
                    payload={"reason": "foreach_parent_cancelled"},
                )
                self._cancel_run(uow, child_command, _EventBuilder(child_command))
            if self.budget_service is not None:
                child_budget = uow.connection.execute(
                    "SELECT consumed_microunits FROM budget_accounts WHERE run_id=?",
                    (str(child_run_id),),
                ).fetchone()
                self.budget_service.settle_transfer_in_uow(
                    uow.connection,
                    self.budget_service.derive_reservation_id(
                        run.run_id, EntityId.parse(item["item_id"]),
                    ),
                    0 if child_budget is None else int(child_budget["consumed_microunits"]),
                    actor=command.actor, now=command.issued_at,
                )
        uow.connection.execute(
            """UPDATE foreach_items SET status='cancelled',
                   aggregate_version=aggregate_version+1,updated_at=?
                 WHERE group_id IN (SELECT group_id FROM foreach_groups
                                     WHERE run_id=? AND status='running')
                   AND status IN ('pending','ready','running')""",
            (command.issued_at.isoformat(), str(run.run_id)),
        )
        uow.connection.execute(
            """UPDATE foreach_groups SET status='cancelled',
                   aggregate_version=aggregate_version+1,updated_at=?
                 WHERE run_id=? AND status='running'""",
            (command.issued_at.isoformat(), str(run.run_id)),
        )
        linked_children = uow.connection.execute(
            """SELECT l.* FROM subflow_links l
                 JOIN workflow_runs child ON child.run_id=l.child_run_id
                WHERE l.parent_run_id=? AND l.status IN ('starting','running')
                  AND child.status NOT IN ('succeeded','failed','cancelled')
                ORDER BY l.link_id""",
            (str(run.run_id),),
        ).fetchall()
        for link in linked_children:
            propagation = json.loads(link["propagation_policy_json"])
            if not propagation.get("parent_cancel_to_child", True):
                continue
            child_run_id = EntityId.parse(link["child_run_id"])
            child = uow.runs.get(child_run_id)
            child_command = CommandEnvelope.create(
                command_type="cancel_run", aggregate_id=child_run_id,
                correlation_id=run.run_id,
                expected_version=child.aggregate_version,
                idempotency_key=f"subflow-parent-cancel:{link['link_id']}",
                actor="system:subflow", issued_at=command.issued_at,
                payload={"reason": "parent_run_cancelled"},
            )
            self._cancel_run(uow, child_command, _EventBuilder(child_command))
            append_control_event(
                uow.connection, run_id=run.run_id,
                aggregate_id=EntityId.parse(link["link_id"]),
                event_type="subflow_link_transitioned",
                payload={
                    "from": link["status"], "to": "cancelled",
                    "child_run_id": link["child_run_id"],
                    "reason": "parent_run_cancelled",
                },
                actor=command.actor,
                idempotency_key=f"parent-cancel:{command.idempotency_key}",
                occurred_at=command.issued_at,
            )
            uow.connection.execute(
                """UPDATE subflow_links SET status='cancelled',
                       aggregate_version=aggregate_version+1,updated_at=?
                     WHERE link_id=? AND status IN ('starting','running')""",
                (command.issued_at.isoformat(), link["link_id"]),
            )
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
            port = by_id[port_id]
            if port["data_policy"]["transport"] == PortTransport.ARTIFACT_REF.value:
                if not isinstance(item, Mapping) or "artifact_id" not in item:
                    raise ValueError(f"{kind} Artifact port {port_id} requires an artifact_id")
            else:
                self.schema_validator(port["schema_id"], item)

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
        if node.kind == "agentic":
            ids.extend(self._activate_agentic_controller(
                uow, command, events, plan, ready_record,
            ))
            return ids
        if node.kind == "foreach":
            ids.extend(self._activate_foreach_controller(
                uow, command, events, plan, ready_record, input_value,
            ))
            return ids
        if node.kind == "subflow":
            ids.extend(self._activate_subflow_controller(
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

    def _activate_agentic_controller(self, uow, command, events, plan, ready_record):
        if self.planner_service is None:
            raise ValueError("agentic node requires a configured Planner provider")
        node = plan.node(ready_record.node_id)
        config = dict(node.config)
        row = uow.connection.execute(
            "SELECT goal FROM workflow_runs WHERE run_id=?", (str(plan.run_id),)
        ).fetchone()
        goal = "" if row is None or row["goal"] is None else str(row["goal"])
        if not goal.strip():
            raise ValueError("agentic Run requires a non-empty goal")
        runtime_nodes = [
            {"node_id": item.node_id, "status": item.status.value,
             "generation": item.generation}
            for item in uow.node_runs.list_by_run(plan.run_id)
        ]
        context = build_planning_context(
            run_id=plan.run_id, plan_version=plan.plan_version, goal=goal,
            graph_summary={
                "status": "waiting", "plan_version": plan.plan_version.value,
                "nodes": runtime_nodes, "tokens": [], "joins": [],
                "waiting_reason": f"planner:{ready_record.node_run_id}",
            },
            available_capabilities=tuple(config.get("capabilities", ())),
            remaining_limits=dict(config.get("remaining_limits", {})),
        )
        attempt = self.planner_service.request_decision_in_uow(
            uow, context,
            prompt_hash=definition_hash({
                "node_id": node.node_id, "config": config,
                "workflow_definition_hash": plan.workflow_definition_hash.value,
            }),
            capability_manifest_hash=definition_hash(
                tuple(sorted(config.get("capabilities", ())))
            ),
            model_id=str(config.get("model_id", "default")),
            provider_id=str(config.get("provider_id", "trusted-cli")),
            now=command.issued_at,
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
                ), updated_at=command.issued_at,
            ),
            ready_record.aggregate_version,
        )
        ids = [running.event_id, waiting.event_id]
        run = uow.runs.get(plan.run_id)
        if run.status is WorkflowRunStatus.RUNNING:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.RUNNING,
                    WorkflowRunStatus.WAITING, reason="planner_wait",
                ),
            )
            uow.events.append(run.run_id, run.run_id, run.aggregate_version, (run_event,))
            uow.runs.update(
                replace(run, status=WorkflowRunStatus.WAITING,
                        aggregate_version=run.aggregate_version.next(),
                        updated_at=command.issued_at),
                run.aggregate_version,
            )
            ids.append(run_event.event_id)
        return ids

    def _activate_foreach_controller(
        self, uow, command, events, plan, ready_record, input_value,
    ):
        node = plan.node(ready_record.node_id)
        config = dict(node.config)
        items = input_value.get(config["items_port"])
        if not isinstance(items, (list, tuple)):
            raise ValueError("Foreach items port must contain an array")
        if len(items) > 100_000:
            raise ValueError("Foreach item limit exceeded")
        if self.budget_service is None:
            raise ValueError("Foreach requires a configured Budget service")
        item_budget = int(config["item_budget_microunits"])
        self.budget_service.ensure_account_in_uow(
            uow.connection, plan.run_id, item_budget * len(items),
            actor=command.actor, now=command.issued_at,
        )
        checksum = definition_hash(tuple(items)).value
        group_id = derive_group_id(
            plan.run_id, node.node_id, checksum, plan.plan_version,
        )
        append_control_event(
            uow.connection, run_id=plan.run_id, aggregate_id=group_id,
            event_type="foreach_group_created",
            payload={
                "item_count": len(items), "source_checksum": checksum,
                "plan_version": plan.plan_version.value,
                "node_run_id": str(ready_record.node_run_id),
            },
            actor=command.actor, idempotency_key=checksum,
            occurred_at=command.issued_at,
        )
        uow.connection.execute(
            """INSERT INTO foreach_groups(
                   group_id,run_id,node_run_id,source_checksum,plan_version,
                   status,failure_policy,concurrency_limit,item_count,
                   aggregate_json,aggregate_checksum,aggregate_version,
                   created_at,updated_at
               ) VALUES (?,?,?,?,?,'running',?,?,?,NULL,NULL,1,?,?)""",
            (
                str(group_id), str(plan.run_id), str(ready_record.node_run_id),
                checksum, plan.plan_version.value,
                config.get("failure_policy", "fail_fast"),
                int(config.get("concurrency_limit", 8)), len(items),
                command.issued_at.isoformat(), command.issued_at.isoformat(),
            ),
        )
        for index, value in enumerate(items):
            key = str(index)
            item_id = derive_item_id(
                group_id, key, index, checksum, plan.plan_version,
            )
            uow.connection.execute(
                """INSERT INTO foreach_items(
                       item_id,group_id,run_id,item_key,item_index,status,
                       input_json,output_json,error_json,retry_count,
                       aggregate_version,created_at,updated_at,child_run_id
                   ) VALUES (?,?,?,?,?,'pending',?,NULL,NULL,0,0,?,?,NULL)""",
                (
                    str(item_id), str(group_id), str(plan.run_id), key, index,
                    canonical_json(value), command.issued_at.isoformat(),
                    command.issued_at.isoformat(),
                ),
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
                ), updated_at=command.issued_at,
            ),
            ready_record.aggregate_version,
        )
        ids = [running.event_id, waiting.event_id]
        run = uow.runs.get(plan.run_id)
        if run.status is WorkflowRunStatus.RUNNING:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.RUNNING,
                    WorkflowRunStatus.WAITING, reason="foreach_wait",
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
        return ids

    def _activate_subflow_controller(
        self, uow, command, events, plan, ready_record, input_value,
    ):
        node = plan.node(ready_record.node_id)
        config = dict(node.config)
        child_run_id = derived_id("run", ready_record.node_run_id, "subflow")
        prior_depth = uow.connection.execute(
            "SELECT recursion_depth FROM subflow_links WHERE child_run_id=?",
            (str(plan.run_id),),
        ).fetchone()
        recursion_depth = 1 if prior_depth is None else int(prior_depth[0]) + 1
        if recursion_depth > 16:
            raise ValueError("Subflow recursion limit exceeded")
        artifact_ids = []
        for port in node.inputs:
            if port["data_policy"]["transport"] != PortTransport.ARTIFACT_REF.value:
                continue
            raw = input_value.get(port["id"])
            if not isinstance(raw, Mapping) or "artifact_id" not in raw:
                raise ValueError("Subflow Artifact input requires an artifact_id")
            artifact_id = EntityId.parse(str(raw["artifact_id"]))
            artifact = uow.artifacts.get(artifact_id, committed_only=True)
            if artifact is None or artifact.schema_id != port["schema_id"]:
                raise ValueError("Subflow Artifact input is missing or has the wrong schema")
            allowed = (
                artifact.visibility is ArtifactVisibility.RUN
                and artifact.run_id == plan.run_id
                or artifact.visibility is ArtifactVisibility.WORKFLOW
                and artifact.workflow_id == plan.workflow_id
                or artifact.visibility is ArtifactVisibility.NODE
                and artifact.scope_id == ready_record.node_run_id
            )
            if not allowed:
                raise ValueError("Subflow Artifact visibility denies the parent NodeRun")
            artifact_ids.append(artifact_id)
        parent_subjects = [
            row[0] for row in uow.connection.execute(
                "SELECT subject FROM run_artifact_subjects WHERE run_id=? ORDER BY subject",
                (str(plan.run_id),),
            )
        ]
        artifact_subjects = [
            subject for subject in parent_subjects
            if all(uow.connection.execute(
                "SELECT 1 FROM artifact_acl WHERE artifact_id=? AND subject=? AND permission='read'",
                (str(artifact_id), subject),
            ).fetchone() is not None for artifact_id in artifact_ids)
        ]
        child_command = CommandEnvelope.create(
            command_type="start_run", aggregate_id=child_run_id,
            correlation_id=plan.run_id, expected_version=AggregateVersion(0),
            idempotency_key=f"subflow-start:{ready_record.node_run_id}",
            actor="system:subflow", issued_at=command.issued_at,
            payload={
                "workflow_id": config["workflow_id"],
                "workflow_version": int(config["workflow_version"]),
                "definition_hash": config["definition_hash"],
                "input": dict(input_value),
                "artifact_subjects": artifact_subjects,
                "artifact_scope": [str(item) for item in artifact_ids],
            },
        )
        child_events = _EventBuilder(child_command)
        self._start_run(uow, child_command, child_events)
        child_plan = self._load_plan(uow, child_run_id, 1)
        if (
            len(child_plan.entry_node_ids) != 1
            or len(child_plan.terminal_node_ids) != 1
        ):
            raise ValueError("Subflow child requires one entry and one terminal")
        child_entry = child_plan.node(child_plan.entry_node_ids[0])
        child_terminal = child_plan.node(child_plan.terminal_node_ids[0])
        def port_contract(ports):
            return {item["id"]: item["schema_id"] for item in ports}
        if port_contract(node.inputs) != port_contract(child_entry.inputs):
            raise ValueError("Subflow parent inputs do not match child entry")
        if port_contract(node.outputs) != port_contract(child_terminal.inputs):
            raise ValueError("Subflow parent outputs do not match child terminal")
        link_hash = definition_hash({
            "parent_run_id": str(plan.run_id),
            "child_run_id": str(child_run_id),
            "parent_node_run_id": str(ready_record.node_run_id),
        })
        link_id = EntityId(
            "subflow_link", link_hash.value.removeprefix("sha256:")
        )
        append_control_event(
            uow.connection, run_id=plan.run_id, aggregate_id=link_id,
            event_type="subflow_link_created",
            payload={
                "child_run_id": str(child_run_id),
                "workflow_id": config["workflow_id"],
                "workflow_version": int(config["workflow_version"]),
                "recursion_depth": recursion_depth,
                "parent_node_run_id": str(ready_record.node_run_id),
            },
            actor=command.actor, idempotency_key=link_hash.value,
            occurred_at=command.issued_at,
        )
        propagation = {
            "parent_cancel_to_child": config.get("parent_cancel_to_child", True),
            "child_failure": config.get("child_failure", "fail_parent"),
            "child_unknown": "wait",
        }
        uow.connection.execute(
            """INSERT INTO subflow_links(
                   link_id,parent_run_id,child_run_id,parent_node_run_id,
                   workflow_id,workflow_version,status,correlation_id,
                   propagation_policy_json,input_mapping_json,output_mapping_json,
                   artifact_scope_json,recursion_depth,aggregate_version,
                   created_at,updated_at
               ) VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?,0,?,?)""",
            (
                str(link_id), str(plan.run_id), str(child_run_id),
                str(ready_record.node_run_id), config["workflow_id"],
                int(config["workflow_version"]), str(plan.run_id),
                canonical_json(propagation), canonical_json({"kind": "identity"}),
                canonical_json({"kind": "identity"}),
                canonical_json([str(item) for item in artifact_ids]),
                recursion_depth, command.issued_at.isoformat(),
                command.issued_at.isoformat(),
            ),
        )
        if artifact_ids:
            audit(
                uow.connection, run_id=plan.run_id, actor=command.actor,
                action="subflow.artifact_acl_transfer", target_id=str(link_id),
                decision="allowed",
                details={
                    "child_run_id": str(child_run_id),
                    "artifact_count": len(artifact_ids),
                    "subject_count": len(artifact_subjects),
                },
                occurred_at=command.issued_at,
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
                ), updated_at=command.issued_at,
            ),
            ready_record.aggregate_version,
        )
        ids = [running.event_id, waiting.event_id]
        run = uow.runs.get(plan.run_id)
        if run.status is WorkflowRunStatus.RUNNING:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.RUNNING,
                    WorkflowRunStatus.WAITING, reason="subflow_wait",
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
            """SELECT 1 FROM node_runs WHERE run_id=? AND node_run_id<>?
               AND status='waiting' LIMIT 1""",
            (str(node.run_id), str(node.node_run_id)),
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

    def _apply_planner_proposal(self, uow, command, events):
        node = uow.node_runs.get(command.aggregate_id)
        if node is None:
            raise ValueError("Planner NodeRun was not found")
        self._check_version(node, command)
        if node.status is not NodeRunStatus.WAITING:
            raise ValueError("Planner NodeRun is not waiting")
        row = uow.connection.execute(
            """SELECT p.*,a.context_json FROM planner_proposals p
                 JOIN planner_attempts a ON a.attempt_id=p.attempt_id
                WHERE p.proposal_id=?""",
            (str(command.payload["proposal_id"]),),
        ).fetchone()
        if row is None or row["run_id"] != str(node.run_id):
            raise ValueError("Planner Proposal was not found for this Run")
        action = json.loads(row["action_json"])
        kind, arguments = action["kind"], action["arguments"]
        if row["status"] != "protocol_accepted" and not (
            kind == "dispatch" and row["status"] == "consumed"
        ):
            raise ValueError("Planner Proposal is not ready to apply")
        context = json.loads(row["context_json"])
        if context["graph_summary"].get("waiting_reason") != f"planner:{node.node_run_id}":
            raise ValueError("Planner Proposal targets a different Agentic node")
        source_plan = self._load_plan(
            uow, node.run_id, node.source_plan_version.value,
        )
        plan_node = source_plan.node(node.node_id)
        if plan_node.kind != "agentic":
            raise ValueError("Planner Proposal target is not an Agentic node")
        plan = source_plan
        if kind == "finish":
            output = dict(arguments["outputs"])
            self._validate_ports(plan_node.outputs, output, "output")
            target, route = NodeRunStatus.SUCCEEDED, EdgeRoute.SUCCESS
            source_value = output
        elif kind == "fail":
            target, route = NodeRunStatus.FAILED, EdgeRoute.ERROR
            source_value = {"error": {
                "code": arguments["code"], "message": arguments["message"],
                "category": "permanent_error", "source": "planner",
            }}
        elif kind == "dispatch":
            patch_row = uow.connection.execute(
                """SELECT result_plan_version FROM plan_patches
                    WHERE proposal_id=? AND run_id=? AND status='committed'""",
                (row["proposal_id"], str(node.run_id)),
            ).fetchone()
            if patch_row is None:
                raise ValueError("Planner dispatch requires a committed PlanPatch")
            result_version = int(patch_row["result_plan_version"])
            if command.payload.get("plan_version") != result_version:
                raise ValueError("Planner dispatch PlanVersion does not match committed patch")
            plan = self._load_plan(uow, node.run_id, result_version)
            output = dict(arguments["inputs"])
            self._validate_ports(plan_node.outputs, output, "output")
            target, route = NodeRunStatus.SUCCEEDED, EdgeRoute.SUCCESS
            source_value = output
        else:
            raise ValueError(f"Planner action {kind!r} requires the PlanPatch/Human reconciler")
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.WAITING, target,
                run_id=str(node.run_id), node_id=node.node_id,
            ),
        )
        uow.events.append(node.run_id, node.node_run_id, node.aggregate_version, (node_event,))
        finished = replace(
            node, status=target, aggregate_version=node.aggregate_version.next(),
            updated_at=command.issued_at,
        )
        uow.node_runs.update(finished, node.aggregate_version)
        if kind != "dispatch":
            uow.connection.execute(
                "UPDATE planner_proposals SET status='consumed' WHERE proposal_id=?"
                " AND status='protocol_accepted'",
                (row["proposal_id"],),
            )
        ids = [node_event.event_id]
        run = uow.runs.get(node.run_id)
        other_waiting = uow.connection.execute(
            """SELECT 1 FROM node_runs WHERE run_id=? AND node_run_id<>?
               AND status='waiting' LIMIT 1""",
            (str(node.run_id), str(node.node_run_id)),
        ).fetchone()
        if run.status is WorkflowRunStatus.WAITING and other_waiting is None:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.RUNNING, reason="planner_proposal_applied",
                ),
            )
            uow.events.append(run.run_id, run.run_id, run.aggregate_version, (run_event,))
            uow.runs.update(
                replace(run, status=WorkflowRunStatus.RUNNING,
                        aggregate_version=run.aggregate_version.next(),
                        updated_at=command.issued_at),
                run.aggregate_version,
            )
            ids.append(run_event.event_id)
        ids.extend(self._propagate_graph(
            uow, command, events, plan, finished, source_value, route=route,
        ))
        audit(
            uow.connection, run_id=node.run_id, actor=command.actor,
            action="planner.apply", target_id=row["proposal_id"], decision="allowed",
            details={"node_run_id": str(node.node_run_id), "action": kind},
            occurred_at=command.issued_at,
        )
        return ids, finished.aggregate_version, node.run_id, {
            "proposal_id": row["proposal_id"], "action": kind,
            "status": target.value,
        }

    def _reject_planner_proposal(self, uow, command, events):
        if not command.actor.startswith("system:"):
            raise ValueError("Planner proposal rejection is system-only")
        node = uow.node_runs.get(command.aggregate_id)
        if node is None:
            raise ValueError("Planner NodeRun was not found")
        self._check_version(node, command)
        if node.status is not NodeRunStatus.WAITING:
            raise ValueError("Planner NodeRun is not waiting")
        row = uow.connection.execute(
            """SELECT p.*,a.context_json FROM planner_proposals p
                 JOIN planner_attempts a ON a.attempt_id=p.attempt_id
                WHERE p.proposal_id=?""",
            (str(command.payload["proposal_id"]),),
        ).fetchone()
        if row is None or row["run_id"] != str(node.run_id):
            raise ValueError("Planner Proposal was not found for this Run")
        if row["status"] != "protocol_accepted":
            raise ValueError("Planner Proposal is not ready to reject")
        context = json.loads(row["context_json"])
        if context["graph_summary"].get("waiting_reason") != f"planner:{node.node_run_id}":
            raise ValueError("Planner Proposal targets a different Agentic node")
        error = dict(command.payload["error"])
        validation = json.loads(row["validation_json"])
        validation = {
            **(validation if isinstance(validation, dict) else {}),
            "application": {"accepted": False, "error": error},
        }
        uow.connection.execute(
            """UPDATE planner_proposals SET status='protocol_rejected',
                   validation_json=? WHERE proposal_id=? AND status='protocol_accepted'""",
            (canonical_json(validation), row["proposal_id"]),
        )
        append_control_event(
            uow.connection, run_id=node.run_id,
            aggregate_id=EntityId.parse(row["proposal_id"]),
            event_type="planner_proposal_application_rejected",
            payload={"node_run_id": str(node.node_run_id), "error": error},
            actor=command.actor,
            idempotency_key=f"reject:{row['proposal_id']}",
            occurred_at=command.issued_at,
        )
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.WAITING, NodeRunStatus.FAILED,
                run_id=str(node.run_id), node_id=node.node_id,
            ),
        )
        uow.events.append(
            node.run_id, node.node_run_id, node.aggregate_version, (node_event,)
        )
        finished = replace(
            node, status=NodeRunStatus.FAILED,
            aggregate_version=node.aggregate_version.next(),
            updated_at=command.issued_at,
        )
        uow.node_runs.update(finished, node.aggregate_version)
        ids = [node_event.event_id]
        run = uow.runs.get(node.run_id)
        other_waiting = uow.connection.execute(
            """SELECT 1 FROM node_runs WHERE run_id=? AND node_run_id<>?
               AND status='waiting' LIMIT 1""",
            (str(node.run_id), str(node.node_run_id)),
        ).fetchone()
        if run.status is WorkflowRunStatus.WAITING and other_waiting is None:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.RUNNING, reason="planner_proposal_rejected",
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
        plan = self._load_plan(uow, node.run_id, node.source_plan_version.value)
        ids.extend(self._propagate_graph(
            uow, command, events, plan, finished,
            {"error": error}, route=EdgeRoute.ERROR,
        ))
        audit(
            uow.connection, run_id=node.run_id, actor=command.actor,
            action="planner.reject", target_id=row["proposal_id"],
            decision="denied", details={"error": error},
            occurred_at=command.issued_at,
        )
        return ids, finished.aggregate_version, node.run_id, {
            "proposal_id": row["proposal_id"], "status": "protocol_rejected",
        }

    def _apply_subflow_result(self, uow, command, events):
        if not command.actor.startswith("system:"):
            raise ValueError("Subflow result apply is system-only")
        node = uow.node_runs.get(command.aggregate_id)
        if node is None:
            raise ValueError("Subflow parent NodeRun was not found")
        self._check_version(node, command)
        if node.status is not NodeRunStatus.WAITING:
            raise ValueError("Subflow parent NodeRun is not waiting")
        link = uow.connection.execute(
            "SELECT * FROM subflow_links WHERE link_id=? AND parent_node_run_id=?",
            (str(command.payload["link_id"]), str(node.node_run_id)),
        ).fetchone()
        if link is None or link["parent_run_id"] != str(node.run_id):
            raise ValueError("Subflow link was not found for this parent")
        child = uow.runs.get(EntityId.parse(link["child_run_id"]))
        if child is None or child.status not in {
            WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }:
            raise ValueError("Subflow child is not terminal")
        plan = self._load_plan(uow, node.run_id, node.source_plan_version.value)
        plan_node = plan.node(node.node_id)
        if plan_node.kind != "subflow":
            raise ValueError("Subflow link parent is not a subflow node")
        if child.status is WorkflowRunStatus.SUCCEEDED:
            child_plan_version = uow.connection.execute(
                "SELECT MAX(plan_version) FROM execution_plans WHERE run_id=?",
                (str(child.run_id),),
            ).fetchone()[0]
            child_plan = self._load_plan(
                uow, child.run_id, int(child_plan_version),
            )
            terminals = [
                item for item in uow.node_runs.list_by_run(child.run_id)
                if item.node_id in child_plan.terminal_node_ids
                and item.status is NodeRunStatus.SUCCEEDED
            ]
            if len(terminals) != 1:
                raise ValueError("Subflow child must have one completed terminal")
            output = {
                item.port_id: item.data
                for item in uow.values.list_by_owner(
                    DataOwnerKind.NODE_INPUT, terminals[0].node_run_id,
                )
            }
            prepared = uow.connection.execute(
                """SELECT payload_json FROM run_events
                    WHERE aggregate_id=? AND event_type='node_input_prepared'
                    ORDER BY aggregate_sequence DESC LIMIT 1""",
                (str(terminals[0].node_run_id),),
            ).fetchone()
            prepared_input = (
                {} if prepared is None
                else dict(json.loads(prepared["payload_json"]).get("input", {}))
            )
            output.update({
                port["id"]: prepared_input[port["id"]]
                for port in plan_node.outputs
                if port["data_policy"]["transport"] == PortTransport.ARTIFACT_REF.value
                and port["id"] in prepared_input
            })
            self._validate_ports(plan_node.outputs, output, "output")
            returned_artifacts = []
            for port in plan_node.outputs:
                if port["data_policy"]["transport"] != PortTransport.ARTIFACT_REF.value:
                    continue
                raw = output.get(port["id"])
                if not isinstance(raw, Mapping) or "artifact_id" not in raw:
                    raise ValueError("Subflow Artifact output requires an artifact_id")
                artifact_id = EntityId.parse(str(raw["artifact_id"]))
                artifact = uow.artifacts.get(artifact_id, committed_only=True)
                if artifact is None or artifact.schema_id != port["schema_id"]:
                    raise ValueError("Subflow Artifact output is missing or has the wrong schema")
                returned_artifacts.append(artifact_id)
            if returned_artifacts:
                scope = set(json.loads(link["artifact_scope_json"]))
                scope.update(str(item) for item in returned_artifacts)
                uow.connection.execute(
                    "UPDATE subflow_links SET artifact_scope_json=? WHERE link_id=?",
                    (canonical_json(sorted(scope)), link["link_id"]),
                )
                audit(
                    uow.connection, run_id=node.run_id, actor=command.actor,
                    action="subflow.artifact_acl_return",
                    target_id=link["link_id"], decision="allowed",
                    details={"artifact_count": len(returned_artifacts)},
                    occurred_at=command.issued_at,
                )
            target, route, link_status = (
                NodeRunStatus.SUCCEEDED, EdgeRoute.SUCCESS, "succeeded",
            )
            source_value = output
        else:
            target, route = NodeRunStatus.FAILED, EdgeRoute.ERROR
            link_status = (
                "cancelled" if child.status is WorkflowRunStatus.CANCELLED
                else "failed"
            )
            source_value = {"error": {
                "code": "subflow_child_terminal",
                "category": "permanent_error", "source": "subflow",
                "message": f"child Run ended {child.status.value}",
            }}
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
        finished = replace(
            node, status=target, aggregate_version=node.aggregate_version.next(),
            updated_at=command.issued_at,
        )
        uow.node_runs.update(finished, node.aggregate_version)
        uow.connection.execute(
            """UPDATE subflow_links SET status=?,aggregate_version=aggregate_version+1,
                   updated_at=? WHERE link_id=? AND status='running'""",
            (link_status, command.issued_at.isoformat(), link["link_id"]),
        )
        ids = [node_event.event_id]
        run = uow.runs.get(node.run_id)
        other_waiting = uow.connection.execute(
            """SELECT 1 FROM node_runs WHERE run_id=? AND node_run_id<>?
               AND status='waiting' LIMIT 1""",
            (str(node.run_id), str(node.node_run_id)),
        ).fetchone()
        if run.status is WorkflowRunStatus.WAITING and other_waiting is None:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.RUNNING, reason="subflow_terminal",
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
        policy = json.loads(link["propagation_policy_json"])
        if target is NodeRunStatus.FAILED and policy["child_failure"] == "fail_parent":
            ids.extend(self._fail_graph_run(
                uow, command, events, node.run_id, "subflow_child_failed",
            ))
        else:
            ids.extend(self._propagate_graph(
                uow, command, events, plan, finished, source_value, route=route,
            ))
        audit(
            uow.connection, run_id=node.run_id, actor=command.actor,
            action="subflow.apply", target_id=link["link_id"],
            decision="allowed",
            details={"child_run_id": link["child_run_id"], "status": link_status},
            occurred_at=command.issued_at,
        )
        return ids, finished.aggregate_version, node.run_id, {
            "link_id": link["link_id"], "child_run_id": link["child_run_id"],
            "status": link_status,
        }

    def _advance_foreach(self, uow, command, events):
        if not command.actor.startswith("system:"):
            raise ValueError("Foreach advance is system-only")
        group = uow.connection.execute(
            "SELECT * FROM foreach_groups WHERE group_id=?",
            (str(command.aggregate_id),),
        ).fetchone()
        if group is None:
            raise ValueError("Foreach group was not found")
        if int(group["aggregate_version"]) != command.expected_version.value:
            raise ConcurrencyConflictError(
                command.aggregate_id, command.expected_version.value,
                int(group["aggregate_version"]),
            )
        if group["status"] != "running":
            raise ValueError("Foreach group is terminal")
        parent = uow.node_runs.get(EntityId.parse(group["node_run_id"]))
        if parent is None or parent.status is not NodeRunStatus.WAITING:
            raise ValueError("Foreach parent NodeRun is not waiting")
        plan = self._load_plan(uow, parent.run_id, parent.source_plan_version.value)
        plan_node = plan.node(parent.node_id)
        if plan_node.kind != "foreach":
            raise ValueError("Foreach group parent is not a foreach node")
        config = dict(plan_node.config)
        ids = []

        running_items = uow.connection.execute(
            "SELECT * FROM foreach_items WHERE group_id=? AND status='running' ORDER BY item_index",
            (group["group_id"],),
        ).fetchall()
        for item in running_items:
            child = uow.runs.get(EntityId.parse(item["child_run_id"]))
            if child is None or child.status not in {
                WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED,
                WorkflowRunStatus.CANCELLED,
            }:
                continue
            if child.status is WorkflowRunStatus.SUCCEEDED:
                child_plan_version = uow.connection.execute(
                    "SELECT MAX(plan_version) FROM execution_plans WHERE run_id=?",
                    (str(child.run_id),),
                ).fetchone()[0]
                child_plan = self._load_plan(
                    uow, child.run_id, int(child_plan_version),
                )
                terminals = [
                    node for node in uow.node_runs.list_by_run(child.run_id)
                    if node.node_id in child_plan.terminal_node_ids
                    and node.status is NodeRunStatus.SUCCEEDED
                ]
                if len(terminals) != 1:
                    raise ValueError("Foreach child must have one completed terminal")
                result = uow.values.get_by_owner_port(
                    DataOwnerKind.NODE_INPUT, terminals[0].node_run_id,
                    config["result_port"],
                )
                if result is None:
                    raise ValueError("Foreach child result port was not recorded")
                status, output, error = "succeeded", result.data, None
            else:
                status, output = "failed", None
                error = {
                    "code": "foreach_child_terminal",
                    "message": f"child Run ended {child.status.value}",
                    "child_run_id": str(child.run_id),
                }
            child_budget = uow.connection.execute(
                "SELECT consumed_microunits FROM budget_accounts WHERE run_id=?",
                (str(child.run_id),),
            ).fetchone()
            reservation_id = self.budget_service.derive_reservation_id(
                parent.run_id, EntityId.parse(item["item_id"]),
            )
            self.budget_service.settle_transfer_in_uow(
                uow.connection, reservation_id,
                0 if child_budget is None else int(child_budget["consumed_microunits"]),
                actor=command.actor, now=command.issued_at,
            )
            append_control_event(
                uow.connection, run_id=parent.run_id,
                aggregate_id=EntityId.parse(item["item_id"]),
                event_type="foreach_item_transitioned",
                payload={"from": "running", "to": status,
                         "group_id": group["group_id"]},
                actor=command.actor,
                idempotency_key=f"terminal:{child.run_id}:{child.aggregate_version.value}",
                occurred_at=command.issued_at,
            )
            uow.connection.execute(
                """UPDATE foreach_items SET status=?,output_json=?,error_json=?,
                       aggregate_version=aggregate_version+1,updated_at=?
                     WHERE item_id=? AND status='running'""",
                (
                    status, None if output is None else canonical_json(output),
                    None if error is None else canonical_json(error),
                    command.issued_at.isoformat(), item["item_id"],
                ),
            )

        failed = uow.connection.execute(
            "SELECT 1 FROM foreach_items WHERE group_id=? AND status='failed' LIMIT 1",
            (group["group_id"],),
        ).fetchone() is not None
        if failed and group["failure_policy"] == "fail_fast":
            active = uow.connection.execute(
                "SELECT * FROM foreach_items WHERE group_id=? AND status='running' ORDER BY item_index",
                (group["group_id"],),
            ).fetchall()
            for item in active:
                child_run_id = EntityId.parse(item["child_run_id"])
                child = uow.runs.get(child_run_id)
                if child.status not in {
                    WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED,
                    WorkflowRunStatus.CANCELLED,
                }:
                    cancel = CommandEnvelope.create(
                        command_type="cancel_run", aggregate_id=child_run_id,
                        correlation_id=parent.run_id,
                        expected_version=child.aggregate_version,
                        idempotency_key=f"foreach-fail-fast:{item['item_id']}",
                        actor="system:foreach", issued_at=command.issued_at,
                        payload={"reason": "foreach_fail_fast"},
                    )
                    self._cancel_run(uow, cancel, _EventBuilder(cancel))
                child_budget = uow.connection.execute(
                    "SELECT consumed_microunits FROM budget_accounts WHERE run_id=?",
                    (str(child_run_id),),
                ).fetchone()
                self.budget_service.settle_transfer_in_uow(
                    uow.connection,
                    self.budget_service.derive_reservation_id(
                        parent.run_id, EntityId.parse(item["item_id"]),
                    ),
                    0 if child_budget is None else int(child_budget["consumed_microunits"]),
                    actor=command.actor, now=command.issued_at,
                )
            uow.connection.execute(
                """UPDATE foreach_items SET status='cancelled',
                       aggregate_version=aggregate_version+1,updated_at=?
                     WHERE group_id=? AND status IN ('pending','ready','running')""",
                (command.issued_at.isoformat(), group["group_id"]),
            )

        active_count = uow.connection.execute(
            "SELECT COUNT(*) FROM foreach_items WHERE group_id=? AND status='running'",
            (group["group_id"],),
        ).fetchone()[0]
        capacity = max(0, int(group["concurrency_limit"]) - int(active_count))
        pending = uow.connection.execute(
            "SELECT * FROM foreach_items WHERE group_id=? AND status IN ('pending','ready')"
            " ORDER BY item_index LIMIT ?",
            (group["group_id"], capacity),
        ).fetchall()
        for item in pending:
            item_id = EntityId.parse(item["item_id"])
            account = uow.connection.execute(
                "SELECT total_microunits,reserved_microunits,consumed_microunits FROM budget_accounts WHERE run_id=?",
                (str(parent.run_id),),
            ).fetchone()
            required_budget = int(config["item_budget_microunits"])
            remaining_budget = (
                -1 if account is None else int(account["total_microunits"])
                - int(account["reserved_microunits"])
                - int(account["consumed_microunits"])
            )
            if remaining_budget < required_budget:
                if active_count == 0:
                    uow.connection.execute(
                        "UPDATE workflow_runs SET status='budget_exhausted', aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=? AND status IN ('running','waiting')",
                        (command.issued_at.isoformat(), str(parent.run_id)),
                    )
                break
            child_run_id = derived_id("run", item_id, "foreach")
            child_command = CommandEnvelope.create(
                command_type="start_run", aggregate_id=child_run_id,
                correlation_id=parent.run_id, expected_version=AggregateVersion(0),
                idempotency_key=f"foreach-start:{item_id}",
                actor="system:foreach", issued_at=command.issued_at,
                payload={
                    "workflow_id": config["workflow_id"],
                    "workflow_version": int(config["workflow_version"]),
                    "definition_hash": config["definition_hash"],
                    "input": {config["item_port"]: json.loads(item["input_json"])},
                    "budget_microunits": int(config["item_budget_microunits"]),
                },
            )
            self.budget_service.reserve_in_uow(
                uow.connection, parent.run_id, item_id,
                required_budget,
                actor=command.actor, now=command.issued_at,
            )
            self._start_run(uow, child_command, _EventBuilder(child_command))
            child_plan = self._load_plan(uow, child_run_id, 1)
            if len(child_plan.entry_node_ids) != 1 or len(child_plan.terminal_node_ids) != 1:
                raise ValueError("Foreach child requires one entry and one terminal")
            entry = child_plan.node(child_plan.entry_node_ids[0])
            terminal = child_plan.node(child_plan.terminal_node_ids[0])
            if config["item_port"] not in {port["id"] for port in entry.inputs}:
                raise ValueError("Foreach item port is not a child entry input")
            if config["result_port"] not in {port["id"] for port in terminal.inputs}:
                raise ValueError("Foreach result port is not a child terminal input")
            append_control_event(
                uow.connection, run_id=parent.run_id, aggregate_id=item_id,
                event_type="foreach_item_transitioned",
                payload={"from": item["status"], "to": "running",
                         "group_id": group["group_id"],
                         "child_run_id": str(child_run_id)},
                actor=command.actor, idempotency_key=f"start:{child_run_id}",
                occurred_at=command.issued_at,
            )
            uow.connection.execute(
                """UPDATE foreach_items SET status='running',child_run_id=?,
                       aggregate_version=aggregate_version+1,updated_at=?
                     WHERE item_id=? AND status IN ('pending','ready')""",
                (str(child_run_id), command.issued_at.isoformat(), item["item_id"]),
            )
            active_count += 1

        remaining = uow.connection.execute(
            "SELECT 1 FROM foreach_items WHERE group_id=?"
            " AND status IN ('pending','ready','running') LIMIT 1",
            (group["group_id"],),
        ).fetchone()
        next_group_version = command.expected_version.next()
        if remaining is not None:
            progress = events.make(
                command.aggregate_id, next_group_version.value,
                "foreach_advanced",
                {"run_id": str(parent.run_id), "group_id": group["group_id"],
                 "status": "running", "item_count": int(group["item_count"])},
            )
            uow.events.append(
                parent.run_id, command.aggregate_id,
                command.expected_version, (progress,),
            )
            ids.append(progress.event_id)
            uow.connection.execute(
                """UPDATE foreach_groups SET aggregate_version=?,updated_at=?
                     WHERE group_id=? AND aggregate_version=?""",
                (
                    next_group_version.value, command.issued_at.isoformat(),
                    group["group_id"], command.expected_version.value,
                ),
            )
            return ids, next_group_version, parent.run_id, {
                "group_id": group["group_id"], "status": "running",
            }

        rows = uow.connection.execute(
            """SELECT item_index,item_key,status,output_json,error_json
                 FROM foreach_items WHERE group_id=? ORDER BY item_index""",
            (group["group_id"],),
        ).fetchall()
        aggregate = stable_aggregate(tuple(
            (
                int(row["item_index"]), row["item_key"], row["status"],
                None if row["output_json"] is None else json.loads(row["output_json"]),
                None if row["error_json"] is None else json.loads(row["error_json"]),
            )
            for row in rows
        ))
        has_failure = any(row["status"] != "succeeded" for row in rows)
        group_status = (
            "partial" if has_failure and group["failure_policy"] == "partial_success"
            else "failed" if has_failure else "completed"
        )
        aggregate_checksum = definition_hash(aggregate).value
        progress = events.make(
            command.aggregate_id, next_group_version.value,
            "foreach_advanced",
            {"run_id": str(parent.run_id), "group_id": group["group_id"],
             "status": group_status, "item_count": len(rows)},
        )
        uow.events.append(
            parent.run_id, command.aggregate_id,
            command.expected_version, (progress,),
        )
        ids.append(progress.event_id)
        append_control_event(
            uow.connection, run_id=parent.run_id,
            aggregate_id=command.aggregate_id, event_type="foreach_aggregated",
            payload={"status": group_status, "checksum": aggregate_checksum},
            actor=command.actor, idempotency_key=aggregate_checksum,
            occurred_at=command.issued_at,
        )
        uow.connection.execute(
            """UPDATE foreach_groups SET status=?,aggregate_json=?,
                   aggregate_checksum=?,aggregate_version=?,updated_at=?
                 WHERE group_id=? AND aggregate_version=?""",
            (
                group_status, canonical_json(aggregate), aggregate_checksum,
                next_group_version.value, command.issued_at.isoformat(),
                group["group_id"], command.expected_version.value,
            ),
        )
        output = {config["output_port"]: aggregate}
        target = (
            NodeRunStatus.FAILED if group_status == "failed"
            else NodeRunStatus.SUCCEEDED
        )
        route = EdgeRoute.ERROR if target is NodeRunStatus.FAILED else EdgeRoute.SUCCESS
        if target is NodeRunStatus.SUCCEEDED:
            self._validate_ports(plan_node.outputs, output, "output")
            source_value = output
        else:
            source_value = {"error": {
                "code": "foreach_failed", "message": "one or more items failed",
                "category": "permanent_error", "source": "foreach",
            }}
        node_event = events.make(
            parent.node_run_id, parent.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.WAITING, target,
                run_id=str(parent.run_id), node_id=parent.node_id,
            ),
        )
        uow.events.append(
            parent.run_id, parent.node_run_id, parent.aggregate_version, (node_event,)
        )
        finished = replace(
            parent, status=target, aggregate_version=parent.aggregate_version.next(),
            updated_at=command.issued_at,
        )
        uow.node_runs.update(finished, parent.aggregate_version)
        ids.append(node_event.event_id)
        run = uow.runs.get(parent.run_id)
        other_waiting = uow.connection.execute(
            """SELECT 1 FROM node_runs WHERE run_id=? AND node_run_id<>?
               AND status='waiting' LIMIT 1""",
            (str(parent.run_id), str(parent.node_run_id)),
        ).fetchone()
        if run.status is WorkflowRunStatus.WAITING and other_waiting is None:
            run_event = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.RUNNING, reason="foreach_aggregated",
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
        ids.extend(self._propagate_graph(
            uow, command, events, plan, finished, source_value, route=route,
        ))
        return ids, next_group_version, parent.run_id, {
            "group_id": group["group_id"], "status": group_status,
            "item_count": len(rows),
        }

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
            and plan.node(item.node_id).kind in {"human", "agentic", "foreach", "subflow", "decision", "join", "terminal"}
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
            elif plan.node(node.node_id).kind == "agentic":
                ids.extend(self._activate_agentic_controller(
                    uow, command, events, plan, node,
                ))
            elif plan.node(node.node_id).kind == "foreach":
                ids.extend(self._activate_foreach_controller(
                    uow, command, events, plan, node, prepared,
                ))
            elif plan.node(node.node_id).kind == "subflow":
                ids.extend(self._activate_subflow_controller(
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
                # An external result nobody has settled is not a stalled graph:
                # the Runtime is not stuck, it is *waiting for a person* to say
                # whether the Agent acted. Failing here would throw that
                # decision away and leave the operator no lever at all.
                if self._unsettled_unknown(uow, run.run_id):
                    ids.extend(self._wait_graph_run(
                        uow, command, events, run.run_id,
                        "unknown_external_result",
                    ))
                else:
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

    @staticmethod
    def _unsettled_unknown(uow, run_id):
        """Is some NodeRun parked on an external result nobody has settled?

        An attempt never leaves ``unknown_external_result`` — the Runtime never
        learns what the Agent did. What makes the question *settled* is the
        NodeRun moving on: a retry supersedes it with a later generation, and
        cancelling the run ends it.
        """

        return uow.connection.execute(
            """SELECT 1 FROM node_attempts a
               JOIN node_runs n ON n.node_run_id = a.node_run_id
               WHERE n.run_id = ? AND a.status = 'unknown_external_result'
                 AND n.status IN ('pending','ready','running','waiting')
               LIMIT 1""",
            (str(run_id),),
        ).fetchone() is not None

    def _wait_graph_run(self, uow, command, events, run_id, reason):
        run = uow.runs.get(run_id)
        if run.status is not WorkflowRunStatus.RUNNING:
            return []
        event = events.make(
            run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
            _transition_payload(
                "workflow_run", WorkflowRunStatus.RUNNING,
                WorkflowRunStatus.WAITING, reason=reason,
            ),
        )
        uow.events.append(run_id, run_id, run.aggregate_version, (event,))
        uow.runs.update(
            replace(
                run, status=WorkflowRunStatus.WAITING,
                aggregate_version=run.aggregate_version.next(),
                updated_at=command.issued_at,
            ),
            run.aggregate_version,
        )
        return [event.event_id]

    def _retry_node_run(self, uow, command, events):
        """Take over a NodeRun parked on an unknown external result.

        The unknown attempt is never rewritten — the Runtime still does not
        know what happened, and saying otherwise would be a lie in the event
        log. Instead the operator's decision *supersedes* it: the parked
        NodeRun is cancelled and the same plan node is scheduled again at the
        next generation, which is a new NodeRun with its own Job and Attempt.
        """

        node = uow.node_runs.get(command.aggregate_id)
        if node is None:
            raise ValueError("NodeRun was not found")
        self._check_version(node, command)
        if node.status not in {NodeRunStatus.READY, NodeRunStatus.WAITING}:
            raise ValueError("RetryNodeRun requires a ready or waiting NodeRun")
        attempts = uow.attempts.list_by_node_run(node.node_run_id)
        if not any(
            item.status is AttemptStatus.UNKNOWN_EXTERNAL_RESULT for item in attempts
        ):
            raise ValueError("RetryNodeRun requires an unknown external result")
        if any(
            item.node_run_id == node.node_run_id
            and item.status in {
                JobStatus.READY, JobStatus.LEASED, JobStatus.RUNNING,
                JobStatus.RETRY_WAIT,
            }
            for item in uow.jobs.list_by_run(node.run_id)
        ):
            raise IntegrityViolationError("NodeRun already has active Job")
        run = uow.runs.get(node.run_id)
        if run is None or run.status not in {
            WorkflowRunStatus.RUNNING, WorkflowRunStatus.WAITING,
        }:
            raise ValueError("RetryNodeRun requires a running or waiting Run")
        plan = self._load_plan(uow, node.run_id, node.source_plan_version.value)
        if not isinstance(plan, GraphExecutionPlan):
            raise ValueError("RetryNodeRun requires ExecutionPlan 1.2")
        input_value = next(
            (
                item.envelope.payload["input"]
                for item in uow.events.read_stream(node.node_run_id, limit=1000)
                if item.envelope.event_type == "node_input_prepared"
            ),
            None,
        )
        if input_value is None:
            raise ValueError("NodeRun input is missing")

        cancelled = events.make(
            node.node_run_id, node.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", node.status, NodeRunStatus.CANCELLED,
                run_id=str(node.run_id), node_id=node.node_id,
                generation=node.generation, activation_key=node.activation_key,
            ),
        )
        uow.events.append(
            node.run_id, node.node_run_id, node.aggregate_version, (cancelled,)
        )
        uow.node_runs.update(
            replace(
                node, status=NodeRunStatus.CANCELLED,
                aggregate_version=node.aggregate_version.next(),
                updated_at=command.issued_at,
            ),
            node.aggregate_version,
        )
        ids = [cancelled.event_id]
        if run.status is WorkflowRunStatus.WAITING:
            resumed = events.make(
                run.run_id, run.aggregate_version.value + 1,
                "workflow_run_transitioned",
                _transition_payload(
                    "workflow_run", WorkflowRunStatus.WAITING,
                    WorkflowRunStatus.RUNNING, reason="node_retried",
                ),
            )
            uow.events.append(
                run.run_id, run.run_id, run.aggregate_version, (resumed,)
            )
            uow.runs.update(
                replace(
                    run, status=WorkflowRunStatus.RUNNING,
                    aggregate_version=run.aggregate_version.next(),
                    updated_at=command.issued_at,
                ),
                run.aggregate_version,
            )
            ids.append(resumed.event_id)
        ids.extend(self._schedule_graph(
            uow, command, events, plan, node.node_id, input_value,
            generation=node.generation + 1, activation_key=node.activation_key,
        ))
        current = uow.node_runs.get(node.node_run_id)
        return ids, current.aggregate_version, node.run_id, {
            "node_run_id": str(node.node_run_id), "node_id": node.node_id,
            "generation": node.generation + 1,
        }

    def _fail_graph_run(self, uow, command, events, run_id, reason):
        run = uow.runs.get(run_id)
        if run.status not in {
            WorkflowRunStatus.RUNNING, WorkflowRunStatus.WAITING,
        }:
            return []
        event = events.make(
            run_id, run.aggregate_version.value + 1, "workflow_run_transitioned",
            _transition_payload(
                "workflow_run", run.status,
                WorkflowRunStatus.FAILED, reason=reason,
            ),
        )
        uow.events.append(run_id, run_id, run.aggregate_version, (event,))
        uow.runs.update(
            replace(run, status=WorkflowRunStatus.FAILED, aggregate_version=run.aggregate_version.next(), updated_at=command.issued_at),
            run.aggregate_version,
        )
        return [event.event_id]
