"""Single-step durable worker and timer loops with injected execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from threading import Event
from typing import Any, Callable

from jsonschema import Draft202012Validator

from ..domain.handlers import HandlerResultStatus
from ..domain.envelopes import CommandEnvelope
from ..domain.execution_plan import execution_plan_from_primitive
from ..domain.ids import EntityId
from ..domain.plan_patch import AgenticRegion, PatchOperation, PatchOperationKind, PlanPatch
from ..domain.policy import PolicyEffect, PolicyRule
from ..domain.versions import AggregateVersion, Revision
from ..domain.serialization import definition_hash, to_primitive
from .supervisor import LeaseSupervisor


class CancellationToken:
    def __init__(self, checker=None) -> None:
        self._event = Event()
        self._checker = checker or (lambda: False)
        self.reason = "cancelled"
    def cancel(self, reason="cancelled") -> None:
        self.reason = reason
        self._event.set()
    @property
    def cancelled(self) -> bool:
        try: external = bool(self._checker())
        except Exception: external = False
        return self._event.is_set() or external
    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            from ..domain.handlers import HandlerCancelledError, HandlerTimeoutError
            if self.reason == "timeout": raise HandlerTimeoutError("handler deadline expired")
            raise HandlerCancelledError("handler execution cancelled")


@dataclass
class InMemoryMetrics:
    counters: dict[tuple[str, tuple], int] = field(default_factory=dict)
    observations: list[tuple[str, float, dict[str, str]]] = field(default_factory=list)
    def increment(self, name, value=1, **labels):
        key = (name, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0) + value
    def observe(self, name, value, **labels): self.observations.append((name, value, labels))


class WorkerRuntime:
    def __init__(self, service, executor: Callable[[str, dict, CancellationToken], dict], *, worker_id="worker-1", clock: Callable[[], datetime], metrics=None, renew_interval_seconds=10.0) -> None:
        self.service = service
        self.executor = executor
        self.worker_id = worker_id
        self.clock = clock
        self.metrics = metrics or InMemoryMetrics()
        self.current_token = None
        self.renew_interval_seconds = renew_interval_seconds

    def _increment(self, name):
        try: self.metrics.increment(name)
        except Exception: pass

    def run_once(self) -> bool:
        now = self.clock()
        self._increment("worker_heartbeat")
        claimed = self.service.claim_job(self.worker_id, now)
        if claimed is None:
            self._increment("worker_empty")
            return False
        started = self.service.start_job(claimed, now)
        if started.disposition.value != "applied":
            self._increment("worker_start_rejected")
            return True
        token = CancellationToken(
            lambda: self.service.get_job(claimed.job_id).status.value == "cancelled"
        )
        self.current_token = token
        try:
            if hasattr(self.executor, "execute"):
                request = self.service.build_executor_request(claimed, self.clock())
                supervisor = LeaseSupervisor(
                    self.service, claimed, token, clock=self.clock,
                    deadline=request.deadline,
                    renew_interval_seconds=self.renew_interval_seconds,
                    on_cancel=lambda: getattr(
                        self.executor, "cancel_current", lambda *_: None
                    )(request.attempt_id),
                )
                supervisor.start()
                try:
                    result = self.executor.execute(request, token)
                finally:
                    supervisor.stop()
                if result.status is HandlerResultStatus.SUCCEEDED:
                    self.service.complete_job(
                        claimed, self.clock(), dict(result.output),
                        handler_result=result,
                    )
                    self._increment("worker_completed")
                elif result.status is HandlerResultStatus.UNKNOWN_EXTERNAL_RESULT:
                    self.service.report_unknown_job_result(claimed, self.clock(), result)
                    self._increment("worker_unknown")
                else:
                    self.service.fail_job(
                        claimed, self.clock(), to_primitive(result.error),
                        handler_result=result,
                    )
                    self._increment("worker_failed")
            else:
                node_id, input_value = self.service.build_legacy_executor_input(claimed)
                output = self.executor(node_id, input_value, token)
                self.service.complete_job(claimed, self.clock(), output)
                self._increment("worker_completed")
        except Exception as exc:
            error = {
                "code": "handler_permanent", "category": "permanent_error",
                "message": f"worker execution failed: {type(exc).__name__}",
                "source": "worker",
                "details": {}, "cause": None,
            }
            self.service.fail_job(claimed, self.clock(), error)
            self._increment("worker_failed")
        finally:
            self.current_token = None
        return True

    def cancel_current(self) -> bool:
        if self.current_token is None: return False
        self.current_token.cancel()
        return True


class TimerDispatcher:
    def __init__(self, service, *, worker_id="timer-1", clock, metrics=None):
        self.service = service
        self.worker_id = worker_id
        self.clock = clock
        self.metrics = metrics or InMemoryMetrics()
    def _increment(self, name):
        try: self.metrics.increment(name)
        except Exception: pass
    def run_once(self):
        self._increment("timer_heartbeat")
        claimed = self.service.claim_timer(self.worker_id, self.clock())
        if claimed is None:
            self._increment("timer_empty")
            return False
        self.service.fire_timer(claimed, self.clock())
        self._increment("timer_fired")
        return True


class RevisionDispatcher:
    """Claim and run one queued Agent workflow-revision job.

    The editor enqueues a prompt and returns; this loop is what actually
    spends the model call. It mirrors PlannerDispatcher: lease long enough to
    cover the CLI's own timeout, settle under the fence, and never hold a
    transaction across the call.
    """

    def __init__(
        self, service, *, worker_id="revision-1", clock, metrics=None,
        lease_seconds=360, agent_command=None, agent_commands=None,
        model_id=None,
    ):
        self.service = service
        self.worker_id = worker_id
        self.clock = clock
        self.metrics = metrics or InMemoryMetrics()
        self.agent_command = agent_command
        # The author may have named an Agent when queueing; the audit trail
        # should name the CLI that really ran, not the Runtime's default.
        self.agent_commands = dict(agent_commands or {})
        self.model_id = model_id
        if lease_seconds <= 0 or lease_seconds > 600:
            raise ValueError(
                "revision lease must be positive and at most ten minutes"
            )
        self.lease_ttl = timedelta(seconds=lease_seconds)

    def _increment(self, name):
        try: self.metrics.increment(name)
        except Exception: pass

    def run_once(self) -> bool:
        self._increment("revision_heartbeat")
        claimed = self.service.claim_revision(
            self.worker_id, self.clock(), lease_ttl=self.lease_ttl
        )
        if claimed is None:
            self._increment("revision_empty")
            return False
        job, token = claimed
        settled = self.service.execute_revision(
            job, token, clock=self.clock,
            agent_command=self.agent_commands.get(
                job.requested_agent, self.agent_command
            ),
            model_id=job.requested_agent or self.model_id,
        )
        self._increment(f"revision_{settled.status}")
        return True


class RevisionRecoveryScanner:
    """Fail revision jobs whose worker died holding the lease."""

    def __init__(self, service, *, clock):
        self.service = service
        self.clock = clock

    def run_once(self) -> bool:
        return bool(self.service.expire_revisions(self.clock()))


class PlannerDispatcher:
    """Claim and execute one durable Planner attempt."""

    def __init__(
        self, service, *, worker_id="planner-1", clock, metrics=None,
        lease_margin_seconds=60, budget_service=None,
    ):
        self.service = service
        self.worker_id = worker_id
        self.clock = clock
        self.metrics = metrics or InMemoryMetrics()
        self.budget_service = budget_service
        provider_timeout = float(
            getattr(getattr(service, "provider", None), "timeout_seconds", 0)
        )
        lease_seconds = max(60, float(provider_timeout) + lease_margin_seconds)
        # Silently capping this would reintroduce the fence race for providers
        # configured above 540s. Refuse an unsafe Runtime instead. The
        # production CLI's 300s timeout claims 360s.
        if lease_seconds > 600:
            raise ValueError(
                "Planner provider timeout plus lease margin exceeds the "
                "10-minute maximum lease"
            )
        self.lease_ttl = timedelta(seconds=lease_seconds)

    def _reserve_budget(self, claim, now):
        """Fence the paid provider call behind the Run's durable budget.

        ``cost_microunits`` is the maximum cost declared by the static
        agentic node. The first attempt opens the Run account; retries reserve
        only what remains. A missing limit retains compatibility for callers
        which deliberately run the Planner without budget accounting.
        """
        if self.budget_service is None:
            return None
        attempt = self.service.get_attempt(claim.attempt_id)
        limit = attempt.context.remaining_limits.get("cost_microunits")
        if limit is None:
            return None
        if limit <= 0:
            raise ValueError("Planner budget is exhausted")
        account = self.budget_service.get_account(attempt.run_id)
        if account is None:
            account = self.budget_service.open_account(
                attempt.run_id, limit, actor=self.worker_id, now=now
            )
        amount = account.remaining_microunits
        if amount <= 0:
            raise ValueError("Planner budget is exhausted")
        return self.budget_service.reserve(
            attempt.run_id, attempt.attempt_id, amount,
            actor=self.worker_id, now=now,
        )

    def _settle_budget(self, reservation, result, now):
        if reservation is None:
            return
        usage = getattr(result, "usage", None)
        if usage is not None:
            self.budget_service.report_usage(
                reservation.reservation_id, 1, usage.cost_microunits,
                actor=self.worker_id, now=now,
            )
        status_object = getattr(result, "status", None)
        status = getattr(status_object, "value", status_object)
        if status == "unknown":
            self.budget_service.settle(
                reservation.reservation_id, actor=self.worker_id, now=now,
                unknown=True,
            )
        elif usage is None:
            self.budget_service.release(
                reservation.reservation_id, actor=self.worker_id, now=now
            )
        else:
            self.budget_service.settle(
                reservation.reservation_id, actor=self.worker_id, now=now
            )

    def _increment(self, name):
        try: self.metrics.increment(name)
        except Exception: pass

    def run_once(self):
        self._increment("planner_heartbeat")
        claimed = self.service.claim(
            self.worker_id, self.clock(), lease_ttl=self.lease_ttl
        )
        if claimed is None:
            self._increment("planner_empty")
            return False
        started_at = self.clock()
        try:
            reservation = self._reserve_budget(claimed, started_at)
        except ValueError as exc:
            self.service.mark_failed(
                claimed, self.clock(), code="planner_budget_unavailable",
                message=str(exc),
            )
            self._increment("planner_budget_rejected")
            return True
        result = self.service.execute_claimed(claimed, started_at, clock=self.clock)
        accounting_result = (
            self.service.get_attempt(claimed.attempt_id)
            if reservation is not None else result
        )
        self._settle_budget(reservation, accounting_result, self.clock())
        status_object = getattr(result, "status", None)
        status = getattr(status_object, "value", status_object)
        if status == "unknown" and getattr(result, "raw_response", None) is not None:
            self._increment("planner_unknown_preserved")
        else:
            self._increment("planner_completed")
        return True


class PlannerProposalReconciler:
    """Apply one accepted Planner action through PlanPatch and Kernel fences."""

    def __init__(
        self, planner_service, runtime_service, *, clock,
        execution_registry=None, plan_service_factory=None,
    ):
        self.planner_service = planner_service
        self.runtime_service = runtime_service
        self.clock = clock
        self.execution_registry = execution_registry
        self.plan_service_factory = plan_service_factory

    @staticmethod
    def _port_contract(ports) -> dict[str, str]:
        return {item["id"]: item["schema_id"] for item in ports}

    def _commit_dispatch(self, selected) -> int:
        if self.execution_registry is None:
            raise RuntimeError("Planner dispatch requires an ExecutionRegistry")
        if self.plan_service_factory is None:
            raise RuntimeError("Planner dispatch requires a PlanService factory")
        action = selected["action"]
        arguments = action["arguments"]
        handler_ref = arguments["handler"]
        if "@" not in handler_ref:
            raise ValueError("Planner dispatch handler must be exact name@version")
        handler_name, handler_version = handler_ref.rsplit("@", 1)
        registered = self.execution_registry.resolve(handler_name, handler_version)
        manifest = registered.manifest
        plan = execution_plan_from_primitive(selected["plan"])
        agentic = plan.node(selected["agentic_node_id"])
        mutable = tuple(agentic.config.get("mutable_nodes", ()))
        if len(mutable) != 1:
            raise ValueError("Planner dispatch requires exactly one mutable placeholder")
        target_id = mutable[0]
        placeholder = plan.node(target_id)
        if self._port_contract(placeholder.inputs) != dict(manifest.inputs):
            raise ValueError("Planner dispatch handler inputs do not match placeholder")
        if self._port_contract(placeholder.outputs) != dict(manifest.outputs):
            raise ValueError("Planner dispatch handler outputs do not match placeholder")
        allowed = frozenset(agentic.config.get("capabilities", ()))
        undeclared = set(manifest.capabilities) - allowed
        if undeclared:
            raise PermissionError(
                f"Planner dispatch requires undeclared capabilities: {sorted(undeclared)}"
            )
        config = dict(arguments["config"])
        config_errors = sorted(
            Draft202012Validator(to_primitive(manifest.config_schema)).iter_errors(config),
            key=lambda item: tuple(str(part) for part in item.path),
        )
        if config_errors:
            raise ValueError(f"Planner dispatch config is invalid: {config_errors[0].message}")
        handler_validation = registered.implementation.validate(manifest, config)
        if not handler_validation.valid:
            issue = handler_validation.issues[0]
            raise ValueError(
                f"Planner dispatch handler validation failed at {issue.path}: {issue.message}"
            )
        replacement = {
            "node_id": target_id,
            "kind": "action",
            "handler_name": manifest.name,
            "handler_version": manifest.version,
            "handler_manifest_fingerprint": manifest.fingerprint,
            "inputs": to_primitive(placeholder.inputs),
            "outputs": to_primitive(placeholder.outputs),
            "config": config,
        }
        patch_digest = definition_hash({
            "proposal_id": selected["proposal_id"],
            "target_id": target_id,
            "replacement": replacement,
        }).value.removeprefix("sha256:")
        patch = PlanPatch(
            EntityId("plan_patch", patch_digest),
            EntityId.parse(selected["proposal_id"]),
            EntityId.parse(selected["run_id"]),
            Revision(int(selected["base_plan_version"])),
            selected["reason"],
            (PatchOperation(
                PatchOperationKind.REPLACE_PENDING_NODE, target_id, replacement,
            ),),
        )
        rules = tuple(
            PolicyRule(
                f"agentic:{agentic.node_id}:{capability}", "1", capability,
                PolicyEffect.ALLOW,
            )
            for capability in sorted(allowed)
        )
        def resolve_capabilities(node_value):
            entry = self.execution_registry.resolve(
                node_value["handler_name"], node_value["handler_version"],
                expected_manifest_fingerprint=node_value[
                    "handler_manifest_fingerprint"
                ],
            )
            return entry.manifest.capabilities

        committed = self.plan_service_factory(
            rules=rules,
            capability_resolver=resolve_capabilities,
        ).commit(
            patch, AgenticRegion(agentic.node_id, mutable),
            actor="system:planner-reconciler", now=self.clock(),
        )
        return committed.plan_version.value

    def run_once(self) -> bool:
        with self.planner_service.uow_factory() as uow:
            connection = uow.connection
            rows = connection.execute(
                """SELECT p.proposal_id,p.run_id,p.base_plan_version,p.status,
                          p.reason,p.action_json,a.context_json
                     FROM planner_proposals p
                     JOIN planner_attempts a ON a.attempt_id=p.attempt_id
                    WHERE p.status IN ('protocol_accepted','consumed')
                    ORDER BY p.created_at,p.proposal_id LIMIT 20"""
            ).fetchall()
            selected = None
            for row in rows:
                action = json.loads(row["action_json"])
                if action["kind"] not in {"finish", "fail", "dispatch"}:
                    continue
                if row["status"] == "consumed" and action["kind"] != "dispatch":
                    continue
                waiting = json.loads(row["context_json"])["graph_summary"].get(
                    "waiting_reason", ""
                )
                if not waiting.startswith("planner:node_run:"):
                    continue
                node_run_id = waiting.removeprefix("planner:")
                node = connection.execute(
                    "SELECT run_id,node_id,source_plan_version,aggregate_version FROM node_runs"
                    " WHERE node_run_id=? AND status='waiting'",
                    (node_run_id,),
                ).fetchone()
                if node is not None:
                    plan_row = connection.execute(
                        "SELECT canonical_plan_json FROM execution_plans"
                        " WHERE run_id=? AND plan_version=?",
                        (node["run_id"], row["base_plan_version"]),
                    ).fetchone()
                    if plan_row is None:
                        continue
                    selected = {
                        "proposal_id": row["proposal_id"],
                        "run_id": row["run_id"],
                        "base_plan_version": row["base_plan_version"],
                        "reason": row["reason"],
                        "status": row["status"],
                        "action": action,
                        "plan": json.loads(plan_row["canonical_plan_json"]),
                        "agentic_node_id": node["node_id"],
                        "node_run_id": node_run_id,
                        "node": dict(node),
                    }
                    break
        if selected is None:
            return False
        proposal_id = selected["proposal_id"]
        node_run_id = selected["node_run_id"]
        node = selected["node"]
        plan_version = None
        if selected["action"]["kind"] == "dispatch":
            try:
                plan_version = self._commit_dispatch(selected)
            except (LookupError, PermissionError, ValueError) as exc:
                result = self.runtime_service.submit(CommandEnvelope.create(
                    command_type="reject_planner_proposal",
                    aggregate_id=EntityId.parse(node_run_id),
                    correlation_id=EntityId.parse(node["run_id"]),
                    expected_version=AggregateVersion(int(node["aggregate_version"])),
                    idempotency_key=f"reject-planner:{proposal_id}",
                    actor="system:planner-reconciler",
                    payload={"proposal_id": proposal_id, "error": {
                        "code": "planner_dispatch_rejected",
                        "message": str(exc), "stage": "application",
                    }}, issued_at=self.clock(),
                ))
                if result.disposition.value not in {"applied", "replayed"}:
                    detail = "; ".join(item.message for item in result.diagnostics)
                    raise RuntimeError(f"Planner proposal reject failed: {detail}")
                return True
        result = self.runtime_service.submit(CommandEnvelope.create(
            command_type="apply_planner_proposal",
            aggregate_id=EntityId.parse(node_run_id),
            correlation_id=EntityId.parse(node["run_id"]),
            expected_version=AggregateVersion(int(node["aggregate_version"])),
            idempotency_key=f"apply-planner:{proposal_id}",
            actor="system:planner-reconciler",
            payload={
                "proposal_id": proposal_id,
                **({"plan_version": plan_version} if plan_version is not None else {}),
            }, issued_at=self.clock(),
        ))
        if result.disposition.value not in {"applied", "replayed"}:
            detail = "; ".join(item.message for item in result.diagnostics)
            raise RuntimeError(f"Planner proposal apply rejected: {detail}")
        return True


class SubflowReconciler:
    """Resume a waiting parent once its linked child Run is terminal."""

    def __init__(self, runtime_service, *, clock):
        self.runtime_service = runtime_service
        self.clock = clock

    def run_once(self) -> bool:
        with self.runtime_service.uow_factory() as uow:
            row = uow.connection.execute(
                """SELECT l.link_id,l.parent_run_id,l.parent_node_run_id,
                          n.aggregate_version
                     FROM subflow_links l
                     JOIN workflow_runs child ON child.run_id=l.child_run_id
                     JOIN node_runs n ON n.node_run_id=l.parent_node_run_id
                    WHERE l.status='running' AND n.status='waiting'
                      AND child.status IN ('succeeded','failed','cancelled')
                    ORDER BY l.created_at,l.link_id LIMIT 1"""
            ).fetchone()
            selected = None if row is None else dict(row)
        if selected is None:
            return False
        result = self.runtime_service.submit(CommandEnvelope.create(
            command_type="apply_subflow_result",
            aggregate_id=EntityId.parse(selected["parent_node_run_id"]),
            correlation_id=EntityId.parse(selected["parent_run_id"]),
            expected_version=AggregateVersion(int(selected["aggregate_version"])),
            idempotency_key=f"apply-subflow:{selected['link_id']}",
            actor="system:subflow-reconciler",
            payload={"link_id": selected["link_id"]}, issued_at=self.clock(),
        ))
        if result.disposition.value not in {"applied", "replayed"}:
            detail = "; ".join(item.message for item in result.diagnostics)
            raise RuntimeError(f"Subflow result apply rejected: {detail}")
        return True


class ForeachReconciler:
    """Advance one durable Foreach group through child Run completion."""

    def __init__(self, runtime_service, *, clock):
        self.runtime_service = runtime_service
        self.clock = clock

    def run_once(self) -> bool:
        with self.runtime_service.uow_factory() as uow:
            rows = uow.connection.execute(
                """SELECT g.group_id,g.run_id,g.aggregate_version,parent.status parent_status,
                          g.concurrency_limit,
                          SUM(CASE WHEN i.status='running' THEN 1 ELSE 0 END) active,
                          SUM(CASE WHEN i.status IN ('pending','ready') THEN 1 ELSE 0 END) pending,
                          SUM(CASE WHEN i.status IN ('pending','ready','running') THEN 1 ELSE 0 END) unfinished,
                          SUM(CASE WHEN i.status='running' AND child.status IN
                              ('succeeded','failed','cancelled') THEN 1 ELSE 0 END) terminal_children
                     FROM foreach_groups g
                     JOIN workflow_runs parent ON parent.run_id=g.run_id
                LEFT JOIN foreach_items i ON i.group_id=g.group_id
                LEFT JOIN workflow_runs child ON child.run_id=i.child_run_id
                    WHERE g.status='running'
                 GROUP BY g.group_id ORDER BY g.updated_at,g.group_id LIMIT 20"""
            ).fetchall()
            selected = None
            for row in rows:
                active = int(row["active"] or 0)
                pending = int(row["pending"] or 0)
                actionable = (
                    int(row["terminal_children"] or 0) > 0
                    or pending > 0 and active == 0
                       and row["parent_status"] in {"running", "waiting"}
                    or int(row["unfinished"] or 0) == 0
                )
                if actionable:
                    selected = dict(row)
                    break
        if selected is None:
            return False
        result = self.runtime_service.submit(CommandEnvelope.create(
            command_type="advance_foreach",
            aggregate_id=EntityId.parse(selected["group_id"]),
            correlation_id=EntityId.parse(selected["run_id"]),
            expected_version=AggregateVersion(int(selected["aggregate_version"])),
            idempotency_key=(
                f"advance-foreach:{selected['group_id']}:"
                f"{selected['aggregate_version']}"
            ),
            actor="system:foreach-reconciler", payload={}, issued_at=self.clock(),
        ))
        if result.disposition.value not in {"applied", "replayed"}:
            detail = "; ".join(item.message for item in result.diagnostics)
            raise RuntimeError(f"Foreach advance rejected: {detail}")
        return True
