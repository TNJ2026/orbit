"""The single production composition root.

Everything the Runtime needs is wired here and nowhere else: the database, the
kernel, the handler registry, the durable worker pool, the timer dispatcher and
the recovery scanner. Background components are owned by Starlette's lifespan,
so a shutdown that leaves a worker (or a worker's child process) running is a
test failure rather than a thing to notice in production.

This module deliberately contains no state machine, no routing decision, no
planner policy and no SQL.
"""

from __future__ import annotations

import asyncio
from collections import ChainMap
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute

from ..workflow.application.durable_runtime_service import (
    DurableRuntimeApplicationService,
)
from ..workflow.application.handler_runtime_service import HandlerRuntimeBuilder
from ..workflow.persistence.attempt_output import attempt_output_sink_factory
from ..workflow.application.plan_service import PlanService
from ..workflow.catalogs import InMemorySchemaCatalog
from ..workflow.persistence.database import connect_workflow_database
from ..workflow.persistence.migrations import migrate_workflow_database
from ..workflow.worker.runtime import (
    ForeachReconciler, PlannerDispatcher, PlannerProposalReconciler,
    RevisionDispatcher, RevisionRecoveryScanner,
    SubflowReconciler,
    TimerDispatcher, WorkerRuntime,
)
from .schema_guard import MixedSchemaError, assert_runtime_schema


DEFAULT_WORKER_COUNT = 2
DEFAULT_POLL_SECONDS = 0.5
DEFAULT_SHUTDOWN_SECONDS = 10.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BackgroundLoop:
    """A single-step component driven on its own thread.

    The loop owns no business logic — it calls `run_once()` and reports the
    last error so `/health/ready` can surface a component that is failing
    instead of letting it die quietly.
    """

    name: str
    step: Callable[[], bool]
    poll_seconds: float = DEFAULT_POLL_SECONDS
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    error_count: int = 0
    last_error: str | None = None
    iterations: int = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"{self.name} already started")
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self.step()
                with self._lock:
                    self.iterations += 1
            except Exception as exc:  # noqa: BLE001 - surfaced through health
                did_work = False
                with self._lock:
                    self.error_count += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
            # Only idle when there was nothing to do, so a busy queue drains at
            # full speed instead of one item per poll interval.
            if not did_work:
                self._stop.wait(self.poll_seconds)

    def request_stop(self) -> None:
        """Ask the loop to finish its current step and exit."""

        self._stop.set()

    def join(self, timeout: float = DEFAULT_SHUTDOWN_SECONDS) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_SECONDS) -> bool:
        self.request_stop()
        return self.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "alive": self.alive,
                "iterations": self.iterations,
                "error_count": self.error_count,
                "last_error": self.last_error,
            }


@dataclass(frozen=True)
class HandlerRegistration:
    """One trusted handler to register before the registry is sealed."""

    manifest: Any
    implementation: Any
    implementation_id: str


class RuntimeComposition:
    """Owns the wired Runtime and the lifecycle of its background components."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        handlers: Sequence[HandlerRegistration] = (),
        schemas: Mapping[str, Any] | None = None,
        secret_values: Mapping[str, str] | None = None,
        worker_count: int = DEFAULT_WORKER_COUNT,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], datetime] = utc_now,
        artifact_backend: Any = None,
        planner_service: Any = None,
        human_delivery: Any = None,
        draft_service: Any = None,
        revision_agent_command: str | None = None,
        revision_agent_commands: Mapping[str, str] | None = None,
        revision_model_id: str | None = None,
        workflow_db_path: Path | str | None = None,
        langgraph_service: Any = None,
        legacy_execution: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.workflow_db_path = Path(workflow_db_path or db_path)
        self.clock = clock
        self.worker_count = max(1, int(worker_count))
        self.poll_seconds = poll_seconds
        self.planner_service = planner_service
        # Set when a reviser is wired; the revision loops key off it.
        self.draft_service = draft_service
        self.revision_agent_command = revision_agent_command
        self.revision_agent_commands = dict(revision_agent_commands or {})
        self.revision_model_id = revision_model_id
        self.langgraph_service = langgraph_service
        self.legacy_execution = bool(legacy_execution)
        if human_delivery is None:
            from ..workflow.application.human_delivery import (
                InMemoryHumanTaskDelivery,
            )
            human_delivery = InMemoryHumanTaskDelivery()
        self.human_delivery = human_delivery

        # A file carrying legacy tables is refused before anything is wired:
        # continuing would mean serving a database whose semantics are half
        # owned by an engine that no longer exists.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        assert_runtime_schema(self.db_path)
        connection = connect_workflow_database(self.db_path)
        try:
            migrate_workflow_database(connection)
        finally:
            connection.close()
        self.tables = assert_runtime_schema(self.db_path)

        from ..workflow.application.budget_service import BudgetService
        self.budget_service = BudgetService(self.db_path)

        self.schema_catalog = InMemorySchemaCatalog(dict(schemas or {}))
        builder = HandlerRuntimeBuilder(
            self.schema_catalog, secret_values=dict(secret_values or {}),
            output_sink_factory=attempt_output_sink_factory(
                self.db_path, clock=self.clock
            ),
        )
        for registration in handlers:
            builder.register(
                registration.manifest,
                registration.implementation,
                implementation_id=registration.implementation_id,
            )
        # Sealing before any worker starts is what makes "the plan's exact
        # handler version" a runtime guarantee rather than a convention.
        self.handler_executor = builder.build()
        self.handler_registry = builder.registry
        self.handler_summary = builder.summary()

        self.service = DurableRuntimeApplicationService(
            self.db_path,
            execution_registry=self.handler_registry,
            artifact_backend=artifact_backend,
            human_task_delivery=self.human_delivery.deliver,
            planner_service=planner_service,
            budget_service=self.budget_service,
            workflow_db_path=self.workflow_db_path,
        )
        # The executor is built before the service (it seals the registry the
        # service binds to), so the Artifact capability is attached now that the
        # service exists. Without this the adapter was defined but never reached
        # a running Handler, and every artifact write hit the rejecting default.
        self.handler_executor.artifact_access_factory = self.service.build_artifact_access

        self.loops: list[BackgroundLoop] = []
        self._workers: list[WorkerRuntime] = []
        self._started = False

    # -- background components -------------------------------------------

    def _build_loops(self) -> list[BackgroundLoop]:
        loops: list[BackgroundLoop] = []
        for index in range(self.worker_count if self.legacy_execution else 0):
            # Each worker gets its own runtime object, so an execution_ref and
            # its cancellation token are never shared between concurrent jobs.
            # The HandlerExecutor is passed directly: WorkerRuntime detects an
            # `execute` attribute and takes the production path that builds a
            # typed ExecutorRequest and runs the LeaseSupervisor. Wrapping it in
            # a callable would silently select the legacy compatibility path and
            # drop lease renewal.
            worker = WorkerRuntime(
                self.service,
                self.handler_executor,
                worker_id=f"worker-{index + 1}",
                clock=self.clock,
            )
            self._workers.append(worker)
            loops.append(
                BackgroundLoop(worker.worker_id, worker.run_once, self.poll_seconds)
            )

        if self.legacy_execution:
            timer = TimerDispatcher(self.service, worker_id="timer-1", clock=self.clock)
            loops.append(BackgroundLoop("timer-1", timer.run_once, self.poll_seconds))
        if callable(getattr(self.langgraph_service, "recover_due", None)):
            loops.append(BackgroundLoop(
                "langgraph-timer", lambda: bool(
                    self.langgraph_service.recover_due(limit=100)
                ), self.poll_seconds,
            ))

        if self.legacy_execution:
            loops.append(
                BackgroundLoop("recovery", self._recovery_pass, max(self.poll_seconds, 5.0))
            )
            subflows = SubflowReconciler(self.service, clock=self.clock)
            loops.append(BackgroundLoop(
                "subflow-reconciler", subflows.run_once, self.poll_seconds,
            ))
            foreach = ForeachReconciler(self.service, clock=self.clock)
            loops.append(BackgroundLoop(
                "foreach-reconciler", foreach.run_once, self.poll_seconds,
            ))
        if self.legacy_execution and self.planner_service is not None:
            planner = PlannerDispatcher(
                self.planner_service, worker_id="planner-1", clock=self.clock,
                budget_service=self.budget_service,
            )
            loops.append(
                BackgroundLoop("planner-1", planner.run_once, self.poll_seconds)
            )
            reconciler = PlannerProposalReconciler(
                self.planner_service, self.service, clock=self.clock,
                execution_registry=self.handler_registry,
                plan_service_factory=lambda **options: PlanService(
                    self.db_path, **options
                ),
            )
            loops.append(BackgroundLoop(
                "planner-proposals", reconciler.run_once, self.poll_seconds
            ))
            loops.append(
                BackgroundLoop(
                    "planner-recovery", self._planner_recovery_pass,
                    max(self.poll_seconds, 5.0),
                )
            )
        # Agent workflow revisions are durable jobs: the editor enqueues, this
        # loop spends the model call, and a recovery pass fails jobs whose
        # worker died mid-flight.
        if getattr(self.draft_service, "reviser", None) is not None:
            revisions = RevisionDispatcher(
                self.draft_service, worker_id="revision-1", clock=self.clock,
                agent_command=self.revision_agent_command,
                agent_commands=self.revision_agent_commands,
                model_id=self.revision_model_id,
            )
            loops.append(BackgroundLoop(
                "revision-1", revisions.run_once, self.poll_seconds,
            ))
            revision_recovery = RevisionRecoveryScanner(
                self.draft_service, clock=self.clock,
            )
            loops.append(BackgroundLoop(
                "revision-recovery", revision_recovery.run_once,
                max(self.poll_seconds, 5.0),
            ))
        return loops

    def _recovery_pass(self) -> bool:
        report = self.service.durable_recovery.scan_once(self.clock())
        return bool(
            report.expired_leases
            or report.expired_timer_leases
            or report.materialized_jobs
        )

    def _planner_recovery_pass(self) -> bool:
        now = self.clock()
        report = self.planner_service.recovery.scan_once(now)
        settled = self.budget_service.reconcile_planner_reservations(
            actor="planner-recovery", now=now,
        )
        return bool(report.parsed_responses or report.expired_unknown or settled)

    def start(self) -> None:
        if self._started:
            return
        self.loops = self._build_loops()
        for loop in self.loops:
            loop.start()
        self._started = True

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_SECONDS) -> list[str]:
        """Stop every loop; returns the names that did not exit in time.

        Order matters. A worker inside a minutes-long Agent call cannot notice
        a stop flag, so waiting on it first only burns the shutdown budget and
        then kills the process with the Handler still running — the lease then
        expires unrenewed and the attempt ends `unknown_external_result` with
        nothing recorded. Cancelling first gives that Handler the chance to
        stop and report while the process is still alive to write it down.
        """

        for loop in self.loops:
            loop.request_stop()
        for worker in self._workers:
            try:
                worker.cancel_current()
            except Exception:
                pass
        stragglers = [loop.name for loop in self.loops if not loop.join(timeout)]
        self._started = False
        return stragglers

    # -- health -----------------------------------------------------------

    def liveness(self) -> dict[str, Any]:
        return {"status": "live"}

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        checks: dict[str, Any] = {}

        try:
            tables = assert_runtime_schema(self.db_path)
            checks["database"] = {"ok": True, "tables": len(tables)}
        except (MixedSchemaError, OSError) as exc:
            checks["database"] = {"ok": False, "error": str(exc)}

        try:
            with connect_workflow_database(self.db_path, read_only=True) as connection:
                versions = [
                    row[0] for row in connection.execute(
                        "SELECT version FROM workflow_schema_migrations ORDER BY version"
                    )
                ]
            checks["migrations"] = {"ok": bool(versions), "applied": versions}
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            checks["migrations"] = {"ok": False, "error": str(exc)}

        checks["handlers"] = {
            "ok": self.handler_registry.sealed,
            "sealed": self.handler_registry.sealed,
            "count": len(self.handler_summary.handlers),
        }

        components = [loop.status() for loop in self.loops]
        checks["components"] = {
            "ok": bool(components) and all(item["alive"] for item in components),
            "detail": components,
        }

        ready = all(item.get("ok") for item in checks.values())
        return ready, checks


class _LiveCapabilities(Mapping):
    """Composition facts, with the few that outlive composition read again.

    Nearly every capability is settled when the app is built and never moves.
    Generation is the exception: an Agent App connects over MCP and becomes a
    name an author may send work to, then disconnects and stops being one. A
    dict captured at build time would advertise a client that has gone and hide
    one that has arrived, so those entries are computed on read instead.
    """

    def __init__(
        self,
        static: Mapping[str, Any],
        live: Mapping[str, Callable[[], Any]],
    ) -> None:
        self._static, self._live = static, live

    def __getitem__(self, key: str) -> Any:
        recompute = self._live.get(key)
        return self._static[key] if recompute is None else recompute()

    def __iter__(self):
        return iter(self._static)

    def __len__(self) -> int:
        return len(self._static)


def create_app(
    db_path: Path | str,
    *,
    handlers: Sequence[HandlerRegistration] = (),
    schemas: Mapping[str, Any] | None = None,
    secret_values: Mapping[str, str] | None = None,
    worker_count: int = DEFAULT_WORKER_COUNT,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    clock: Callable[[], datetime] = utc_now,
    artifact_backend: Any = None,
    human_delivery: Any = None,
    extra_routes: Sequence[Route | Mount] = (),
    authenticator: Callable[[Any], str | None] | None = None,
    authorizer: Any = None,
    rate_limiter: Any = None,
    unlimited_actors: Sequence[str] = (),
    token_exempt_actors: Sequence[str] = (),
    operator_actors: Sequence[str] = (),
    serve_ui: bool = False,
    discover_agents: bool = False,
    agent_capabilities: Sequence[str] | None = None,
    workflow_generator: Callable[[str], str] | None = None,
    workflow_generators: Mapping[str, Callable[[str], str]] | None = None,
    structured_agents: Mapping[str, Any] | None = None,
    authoring_broker: Any = None,
    single_goal_mode: bool = True,
    workflow_db_path: Path | str | None = None,
    shutdown_request: Callable[[], None] | None = None,
    langgraph_service: Any = None,
    langgraph_state_directory: Path | str | None = None,
    legacy_execution: bool = True,
    workflow_ui_mode: str = "multi-agent",
) -> Starlette:
    """Build the Runtime application.

    `extra_routes` is the seam for protocol adapters (`/api/v1` in M3, `/mcp`
    in M3): they mount alongside health, and get the composition through
    `app.state.runtime` rather than by importing anything from the old engine.
    """

    if workflow_ui_mode not in {"single-agent", "multi-agent"}:
        raise ValueError("workflow_ui_mode must be single-agent or multi-agent")

    # Discovery runs *before* the composition, because the composition seals
    # the handler registry in its constructor. Registering afterwards is not
    # merely late — it is impossible, and that is how discovered agents ended
    # up visible in the catalog and uncallable from a workflow.
    agent_catalog: Sequence[Mapping[str, Any]] = ()
    # Named generators an author may choose between. Discovery fills this
    # below; an explicit mapping (tests, embedders) wins outright. Held rather
    # than copied, so an embedder may supply a mapping that changes — which is
    # what a set of connected Agent Apps is.
    generation_agents: Mapping[str, Any] = (
        {} if workflow_generators is None else workflow_generators
    )
    registrations = list(handlers)
    planner_service = None
    if discover_agents:
        from ..workflow.catalogs.agent_discovery import (
            catalog_entries, discover_agent_clis,
        )

        from .builtin_handlers import agent_handlers, planner_provider_from_agents

        discovered = discover_agent_clis()
        agent_catalog = catalog_entries(discovered)
        invokable_agents = tuple(
            agent for agent in discovered if agent.spec.runtime_compatible
        )
        agent_registrations, _names = agent_handlers(
            invokable_agents, allowed_capabilities=agent_capabilities
        )
        registrations.extend(agent_registrations)

        # The planner rides on the same discovery pass and the same trust
        # rule: its command is the resolved executable, chosen here, never
        # supplied by a request. No discovered CLI simply means no planner —
        # the Runtime runs fine without one.
        planner_provider = planner_provider_from_agents(invokable_agents)
        if planner_provider is not None:
            from ..workflow.application.planner_service import (
                PlannerApplicationService,
            )

            planner_service = PlannerApplicationService(
                db_path, provider=planner_provider
            )

        # Workflow generation rides the same discovery result and the same
        # trust rule. Every discovered CLI gets a generator so the author can
        # name one per request; the first stays the default for callers that
        # do not care. An explicit `workflow_generator` (tests, embedders)
        # takes precedence below.
        if invokable_agents:
            from ..workflow.authoring import TrustedCliDslGenerator

            if not generation_agents:
                generation_agents = {
                    agent.name: TrustedCliDslGenerator(
                        (agent.executable_path, *agent.spec.invocation.args),
                        prompt_flag=agent.spec.invocation.prompt_flag,
                        prompt_positional=agent.spec.invocation.prompt_positional,
                    )
                    for agent in invokable_agents
                }
            if workflow_generator is None:
                workflow_generator = generation_agents.get(
                    invokable_agents[0].name, next(iter(generation_agents.values()))
                )

        # An author may also route generation to an MCP client that is already
        # connected — an Agent App operating this Runtime rather than a fresh
        # CLI it forks. These names are layered *under* the discovered CLIs so
        # a client can never shadow one, and so none of them becomes the
        # default by accident: a forked CLI runs, while a parked prompt only
        # waits, and nobody may ever come for it. An explicitly injected
        # mapping is left exactly as the embedder wrote it.
        if not workflow_generators:
            from ..workflow.authoring import ExternalAuthoringBroker

            if authoring_broker is None:
                authoring_broker = ExternalAuthoringBroker()
            # A ChainMap rather than a merged dict: which Apps are connected
            # changes while the Runtime runs, so the set of names an author may
            # pick from has to be read, not remembered.
            generation_agents = ChainMap(
                generation_agents, authoring_broker.generators()
            )
            if workflow_generator is None:
                # No CLI to fall back on, and no App has connected yet — but
                # one may, so authoring is wired rather than declared
                # unavailable for the life of the process. The broker itself is
                # the unnamed fallback; the names in the menu are the ones Apps
                # report for themselves.
                workflow_generator = authoring_broker

    if structured_agents:
        # Operator-configured names, so they sit *over* discovery rather than
        # under it like a connected App does — an operator who named one meant
        # it. A collision is refused outright instead: two different writers
        # answering to one name means an author cannot be told truthfully which
        # one wrote their workflow, and being told the wrong one is worse than
        # an error.
        from ..workflow.authoring.structured import structured_generators

        clash = sorted(set(structured_agents) & set(generation_agents))
        if clash:
            raise ValueError(
                "structured agent names collide with discovered ones: "
                + ", ".join(clash)
            )
        built = structured_generators(tuple(structured_agents.items()))
        generation_agents = ChainMap(dict(built), generation_agents)
        if workflow_generator is None:
            workflow_generator = built[sorted(built)[0]]

    if langgraph_state_directory is not None:
        if langgraph_service is not None:
            raise ValueError(
                "provide langgraph_service or langgraph_state_directory, not both"
            )
        from ..workflow.langgraph_runtime import build_service

        langgraph_service = build_service(
            workflow_db_path or db_path,
            registrations,
            state_directory=langgraph_state_directory,
            secret_values=secret_values,
        )

    composition = RuntimeComposition(
        db_path,
        handlers=registrations,
        schemas=schemas,
        secret_values=secret_values,
        worker_count=worker_count,
        poll_seconds=poll_seconds,
        clock=clock,
        artifact_backend=artifact_backend,
        planner_service=planner_service,
        human_delivery=human_delivery,
        workflow_db_path=workflow_db_path,
        langgraph_service=langgraph_service,
        legacy_execution=legacy_execution,
    )
    if legacy_execution and composition.workflow_db_path != composition.db_path:
        from ..workflow.persistence.workflow_versions import merge_workflow_library

        merge_workflow_library(composition.db_path, composition.workflow_db_path)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        if langgraph_service is not None:
            # A process may stop after LangGraph wrote a checkpoint but before
            # the adapter settled its run metadata. Recover those bounded,
            # explicitly opted-in runs before accepting new work.
            failures: list[dict[str, str]] = []

            def record(run_id: str, exc: Exception) -> None:
                failures.append(
                    {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
                )

            try:
                # In a worker thread: recovery replays whole workflows
                # synchronously, so running it here would mean the server
                # accepts no connection until every left-over run has finished.
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: langgraph_service.recover_running(on_error=record)
                )
            except Exception as exc:  # noqa: BLE001 - startup outlives recovery
                # Per-run failures are already isolated inside recover_running;
                # reaching here means recovery itself could not run at all. The
                # Runtime still starts: refusing to serve anything because some
                # older run cannot be replayed is a worse outage than the one
                # being reported.
                record("*", exc)
            if failures:
                # Surfaced rather than swallowed, like shutdown_stragglers
                # below: a run that cannot be recovered is a thing an operator
                # has to see, not a thing to discover from a silent status.
                app.state.startup_recovery_failures = failures
        composition.start()
        try:
            yield
        finally:
            stragglers = composition.stop()
            if stragglers:
                # Surfaced rather than swallowed: a loop that will not stop is
                # a bug, and hiding it here is how zombie workers survive a
                # restart.
                app.state.shutdown_stragglers = stragglers

    async def health_live(_request: Request) -> JSONResponse:
        return JSONResponse(composition.liveness())

    async def health_ready(_request: Request) -> JSONResponse:
        ready, checks = composition.readiness()
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    from ..workflow.application.authoring_job_service import AuthoringJobService
    from .api_v1 import authoring_timeout_seconds, build_api_v1
    from .mcp import build_mcp_dispatcher, mcp_routes

    # Composition facts for /api/v1/capabilities: what this deployment can do,
    # with a reason when it cannot. The UI renders "service not provided" from
    # these instead of probing endpoints for 404s (delivery plan API-7).
    # An injected mapping without discovery still needs one default, and it has
    # to be settled before capabilities are declared or the Runtime would
    # report generation unavailable while holding generators.
    if workflow_generator is None and generation_agents:
        workflow_generator = next(iter(generation_agents.values()))

    # Which name in the list the Runtime actually falls back to. The list is
    # sorted for display, so the default is not simply its first entry; it is
    # settled above and identified here by identity, whichever path set it.
    def default_generation_agent() -> str | None:
        return next(
            (
                name for name, generator in generation_agents.items()
                if generator is workflow_generator
            ),
            None,
        )

    def generation_facts() -> dict[str, Any]:
        # Recomputed per request: a connected client is a name an author may
        # pick, and clients arrive and leave while the Runtime runs.
        return {
            "available": True, "agents": sorted(generation_agents),
            "default_agent": default_generation_agent(),
        }

    capabilities = {
        "static_graph": {"available": True},
        "human_tasks": {"available": True},
        "artifacts": (
            {"available": True}
            if artifact_backend is not None
            else {"available": False, "reason": "artifact_store_not_configured"}
        ),
        "planner_dispatcher": (
            {"available": True}
            if planner_service is not None
            else {
                "available": False,
                "reason": (
                    "no_planner_provider" if discover_agents
                    else "agent_discovery_disabled"
                ),
            }
        ),
        "planner": (
            {"available": True}
            if planner_service is not None
            else {
                "available": False,
                "reason": (
                    "no_planner_provider" if discover_agents
                    else "agent_discovery_disabled"
                ),
            }
        ),
        "dynamic_plan_patch": (
            {"available": True}
            if planner_service is not None
            and composition.handler_summary.handler_count > 0
            else {
                "available": False,
                "reason": (
                    "planner_not_configured"
                    if planner_service is None else "no_registered_handlers"
                ),
            }
        ),
        "agent_handlers": {
            "available": bool(agent_catalog),
            "agents": sorted(
                str(item["agent"]) for item in agent_catalog
                if "agent" in item and "agent.invoke" in item.get("capabilities", ())
            ),
            **({} if agent_catalog else {"reason": "no_discovered_agents"}),
        },
        "foreach": {"available": True},
        "subflow": {"available": True},
        "history_overlay": {"available": True},
        "langgraph_workflows": (
            {"available": True, "api": "/api/v1/langgraph-runs"}
            if langgraph_service is not None
            else {"available": False, "reason": "service_not_configured"}
        ),
        "workflow_generation": (
            # The names an author may pick from. Empty means this Runtime has
            # exactly one way to write DSL and the choice is not offered.
            generation_facts()
            if workflow_generator is not None
            else {
                "available": False,
                "reason": (
                    "no_generation_agent" if discover_agents
                    else "agent_discovery_disabled"
                ),
            }
        ),
        "workflow_editing": generation_facts(),
    }
    if workflow_generator is not None:
        # The two generation entries are the only ones that can change after
        # startup, so they are the only ones read again per request.
        capabilities = _LiveCapabilities(capabilities, {
            "workflow_generation": generation_facts,
            "workflow_editing": generation_facts,
        })

    # Authoring shares the sealed registry's manifests and the composition's
    # schema catalog, so a generated draft can only reference what a published
    # workflow could. Publishing goes through the same definition service the
    # CLI uses — one validation path, two entrances.
    from ..workflow.application.workflows import (
        WorkflowCatalogs, WorkflowDefinitionService,
    )
    from ..workflow.catalogs import InMemoryHandlerCatalog
    from ..workflow.catalogs.extensions import InMemoryExtensionRegistry
    from ..workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore

    manifests = [entry.manifest for entry in composition.handler_registry.entries()]
    authoring_catalogs = WorkflowCatalogs(
        InMemoryHandlerCatalog(manifests),
        composition.schema_catalog,
        InMemoryExtensionRegistry(),
    )
    workflow_publisher = WorkflowDefinitionService(
        authoring_catalogs, SQLiteWorkflowVersionStore(composition.workflow_db_path)
    )
    template_service = None
    if (
        workflow_ui_mode == "single-agent"
        and langgraph_service is not None
        and authoring_broker is not None
    ):
        from ..workflow.templates import SingleAgentTemplateService

        template_service = SingleAgentTemplateService(
            workflow_publisher, manifests, langgraph_service,
            authoring_broker.clients,
        )
    from ..workflow.application.workflow_draft_service import (
        WorkflowDraftApplicationService,
    )

    # Authoring is built first so the draft service can borrow its reviser:
    # editing a workflow means describing the change to the same agent that
    # generates one from scratch.
    authoring_service = None
    if workflow_generator is not None:
        from ..workflow.authoring import WorkflowAuthoringService

        authoring_service = WorkflowAuthoringService(
            authoring_catalogs.handlers,
            composition.schema_catalog,
            workflow_generator,
            generators=generation_agents,
            handler_facts=[
                {
                    "name": manifest.name, "version": manifest.version,
                    "node_kinds": list(manifest.node_kinds),
                    "inputs": dict(manifest.inputs),
                    "outputs": dict(manifest.outputs),
                    "config_schema": dict(manifest.config_schema),
                }
                for manifest in manifests
            ],
            # Discovered Agent generation serves the simplified Goal UI and
            # must produce a directly runnable prompt ingress. Explicitly
            # injected generators are an embedding seam and may intentionally
            # target a different input contract.
            require_goal_binding=discover_agents,
        )

    draft_service = WorkflowDraftApplicationService(
        composition.db_path, workflow_publisher,
        reviser=authoring_service.revise if authoring_service is not None else None,
        workflow_db_path=composition.workflow_db_path,
    )
    # Hand the service to the composition before lifespan startup builds its
    # loops, so the revision dispatcher and its recovery pass are supervised
    # like every other background component.
    composition.draft_service = draft_service
    generator_command = getattr(workflow_generator, "command", None)
    composition.revision_agent_command = (
        generator_command[0] if generator_command else None
    )
    # The dispatcher records which CLI actually ran a job, so it needs the
    # command behind each name the author could have chosen.
    composition.revision_agent_commands = {
        name: generator.command[0]
        for name, generator in generation_agents.items()
        if getattr(generator, "command", None)
    }

    operational_config = {
        "worker_count": worker_count,
        "poll_seconds": poll_seconds,
    }
    # One AuthoringJobService for the whole process. It owns in-flight jobs —
    # their cancel scopes, their deadline timers, and the recovery that
    # restarts queued work at startup — so a second instance would run every
    # queued job a second time and cancel what the first had started. A job
    # dispatched over MCP and one started from the UI are the same job, in the
    # same list, cancellable from either.
    authoring_jobs = (
        AuthoringJobService(
            composition.db_path, authoring_service, workflow_publisher,
            workflow_db_path=composition.workflow_db_path,
            timeout_seconds=authoring_timeout_seconds(operational_config),
            clock=composition.clock,
        )
        if authoring_service is not None and workflow_publisher is not None
        else None
    )

    # The MCP surface is a second protocol over the same application services
    # and the same identity, not a second implementation. Built here rather
    # than inside the route factory so `orbit mcp` can carry this very
    # dispatcher over stdio instead of standing up its own services against a
    # database this process already has open.
    mcp_dispatch = build_mcp_dispatcher(
        composition.db_path, composition.service,
        workflow_db_path=composition.workflow_db_path,
        authorizer=authorizer,
        single_goal_mode=single_goal_mode,
        schema_catalog=composition.schema_catalog,
        artifact_backend=artifact_backend,
        authoring_jobs=authoring_jobs,
        authoring_broker=authoring_broker,
        langgraph_service=langgraph_service,
        legacy_execution=legacy_execution,
    )

    routes: list[Route | Mount | WebSocketRoute] = [
        Route("/health/live", health_live, methods=["GET"]),
        Route("/health/ready", health_ready, methods=["GET"]),
        *build_api_v1(
            composition.db_path, composition.service,
            workflow_db_path=composition.workflow_db_path,
            authenticator=authenticator, authorizer=authorizer,
            rate_limiter=rate_limiter,
            unlimited_actors=unlimited_actors,
            token_exempt_actors=token_exempt_actors,
            operator_actors=operator_actors,
            agent_catalog=agent_catalog,
            capabilities=capabilities,
            schema_catalog=composition.schema_catalog,
            artifact_backend=artifact_backend,
            authoring_service=authoring_service,
            workflow_publisher=workflow_publisher,
            draft_service=draft_service,
            single_goal_mode=single_goal_mode,
            operational_config=operational_config,
            authoring_jobs=authoring_jobs,
            shutdown_request=shutdown_request,
            langgraph_service=langgraph_service,
            legacy_execution=legacy_execution,
            workflow_ui_mode=workflow_ui_mode,
            template_service=template_service,
        ),
        # The MCP surface is a second protocol over the same application
        # services and the same identity, not a second implementation.
        *mcp_routes(mcp_dispatch, authenticator=authenticator),
    ]

    # Durable notifications for Agent Apps. Frames are hints only: consumers
    # re-read HTTP/MCP state and execute only server-issued allowed_commands.
    from .runtime_events import runtime_event_routes

    routes.extend(runtime_event_routes(
        db_path, authenticator=authenticator, authorizer=authorizer,
    ))

    if authoring_broker is not None:
        # The push half of client-written generation. Claiming stays the only
        # way work changes hands; this only removes the wait for a poll.
        from .authoring_events import authoring_event_routes

        routes.extend(
            authoring_event_routes(authoring_broker, authenticator=authenticator)
        )

    if serve_ui:
        # The modular UI is static files only: it holds no server-side session
        # and no mock adapter, and reaches the runtime exclusively through
        # /api/v1. Mounting it here is the whole integration.
        from importlib import resources

        from starlette.staticfiles import StaticFiles

        class RevalidatedStaticFiles(StaticFiles):
            """Serve the UI with `cache-control: no-cache`.

            The UI is ES modules that import each other by fixed path. Without
            an explicit directive a browser applies heuristic freshness per
            file, so one changed module can load beside a cached copy of its
            neighbour — the import then fails and the page renders nothing at
            all. `no-cache` still revalidates with the ETag, so an unchanged
            file costs a 304 rather than a re-download; what it removes is the
            chance of a half-old module graph.
            """

            def file_response(self, *args, **kwargs):
                response = super().file_response(*args, **kwargs)
                response.headers["cache-control"] = "no-cache"
                return response

        ui_root = resources.files("orbit").joinpath("static/workflow-ui")
        routes.append(
            Mount(
                "/ui",
                app=RevalidatedStaticFiles(directory=str(ui_root), html=True),
                name="ui",
            )
        )

        # The graph editor is a separate surface with a build step of its own,
        # mounted beside the hand-written UI rather than inside it. Keeping
        # them apart is what lets the editor use a framework without the rest
        # of the UI acquiring one.
        #
        # Served in both modes, because the API it lives on is: the workflow
        # catalog is not a property of which authoring UI was chosen. Absent
        # only from a source checkout that has not run the build, and a missing
        # directory must not stop the Runtime.
        editor_root = resources.files("orbit").joinpath("static/workflow-editor")
        if editor_root.joinpath("index.html").is_file():
            routes.append(
                Mount(
                    "/editor",
                    app=RevalidatedStaticFiles(
                        directory=str(editor_root), html=True
                    ),
                    name="editor",
                )
            )

    routes.extend(extra_routes)
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.runtime = composition
    # None when discovery is off or found nothing; adapters must treat the
    # planner as optional rather than assume it.
    app.state.planner = composition.planner_service
    # `orbit mcp` reaches the tools through this instead of over its own HTTP
    # connection: same dispatcher, same services, one transport removed.
    app.state.mcp_dispatch = mcp_dispatch
    # Kept separate from the Orbit composition by design (ADR 002). Embedders
    # may operate the optional adapter without implying it is the default
    # event-sourced Runtime.
    app.state.langgraph_service = langgraph_service
    return app
