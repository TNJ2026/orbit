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

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ..workflow.application.durable_runtime_service import (
    DurableRuntimeApplicationService,
)
from ..workflow.application.handler_runtime_service import HandlerRuntimeBuilder
from ..workflow.catalogs import InMemorySchemaCatalog
from ..workflow.persistence.database import connect_workflow_database
from ..workflow.persistence.migrations import migrate_workflow_database
from ..workflow.worker.runtime import PlannerDispatcher, TimerDispatcher, WorkerRuntime
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

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_SECONDS) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

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
    ) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self.worker_count = max(1, int(worker_count))
        self.poll_seconds = poll_seconds
        self.planner_service = planner_service
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

        self.schema_catalog = InMemorySchemaCatalog(dict(schemas or {}))
        builder = HandlerRuntimeBuilder(
            self.schema_catalog, secret_values=dict(secret_values or {})
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
        )

        self.loops: list[BackgroundLoop] = []
        self._workers: list[WorkerRuntime] = []
        self._started = False

    # -- background components -------------------------------------------

    def _build_loops(self) -> list[BackgroundLoop]:
        loops: list[BackgroundLoop] = []
        for index in range(self.worker_count):
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

        timer = TimerDispatcher(self.service, worker_id="timer-1", clock=self.clock)
        loops.append(BackgroundLoop("timer-1", timer.run_once, self.poll_seconds))

        loops.append(
            BackgroundLoop("recovery", self._recovery_pass, max(self.poll_seconds, 5.0))
        )
        if self.planner_service is not None:
            planner = PlannerDispatcher(
                self.planner_service, worker_id="planner-1", clock=self.clock
            )
            loops.append(
                BackgroundLoop("planner-1", planner.run_once, self.poll_seconds)
            )
            loops.append(
                BackgroundLoop(
                    "planner-recovery", self._planner_recovery_pass,
                    max(self.poll_seconds, 5.0),
                )
            )
        return loops

    def _recovery_pass(self) -> bool:
        report = self.service.durable_recovery.scan_once(self.clock())
        return bool(
            report.expired_leases
            or report.expired_timer_leases
            or report.materialized_jobs
        )

    def _planner_recovery_pass(self) -> bool:
        report = self.planner_service.recovery.scan_once(self.clock())
        return bool(report.parsed_responses or report.expired_unknown)

    def start(self) -> None:
        if self._started:
            return
        self.loops = self._build_loops()
        for loop in self.loops:
            loop.start()
        self._started = True

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_SECONDS) -> list[str]:
        """Stop every loop; returns the names that did not exit in time."""

        stragglers = [loop.name for loop in self.loops if not loop.stop(timeout)]
        # Any handler subprocess still running belongs to a cancelled job.
        for worker in self._workers:
            try:
                worker.cancel_current()
            except Exception:
                pass
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
    serve_ui: bool = False,
    discover_agents: bool = False,
    agent_capabilities: Sequence[str] | None = None,
) -> Starlette:
    """Build the Runtime application.

    `extra_routes` is the seam for protocol adapters (`/api/v1` in M3, `/mcp`
    in M3): they mount alongside health, and get the composition through
    `app.state.runtime` rather than by importing anything from the old engine.
    """

    # Discovery runs *before* the composition, because the composition seals
    # the handler registry in its constructor. Registering afterwards is not
    # merely late — it is impossible, and that is how discovered agents ended
    # up visible in the catalog and uncallable from a workflow.
    agent_catalog: Sequence[Mapping[str, Any]] = ()
    registrations = list(handlers)
    planner_service = None
    if discover_agents:
        from ..workflow.catalogs.agent_discovery import (
            catalog_entries, discover_agent_clis,
        )

        from .builtin_handlers import agent_handlers, planner_provider_from_agents

        discovered = discover_agent_clis()
        agent_catalog = catalog_entries(discovered)
        agent_registrations, _names = agent_handlers(
            discovered, allowed_capabilities=agent_capabilities
        )
        registrations.extend(agent_registrations)

        # The planner rides on the same discovery pass and the same trust
        # rule: its command is the resolved executable, chosen here, never
        # supplied by a request. No discovered CLI simply means no planner —
        # the Runtime runs fine without one.
        planner_provider = planner_provider_from_agents(discovered)
        if planner_provider is not None:
            from ..workflow.application.planner_service import (
                PlannerApplicationService,
            )

            planner_service = PlannerApplicationService(
                db_path, provider=planner_provider
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
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
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

    from .api_v1 import build_api_v1
    from .mcp import build_mcp

    routes: list[Route | Mount] = [
        Route("/health/live", health_live, methods=["GET"]),
        Route("/health/ready", health_ready, methods=["GET"]),
        *build_api_v1(
            composition.db_path, composition.service,
            authenticator=authenticator, authorizer=authorizer,
            agent_catalog=agent_catalog,
        ),
        # The MCP surface is a second protocol over the same application
        # services and the same identity, not a second implementation.
        *build_mcp(
            composition.db_path, composition.service,
            authenticator=authenticator, authorizer=authorizer,
        ),
    ]

    if serve_ui:
        # The modular UI is static files only: it holds no server-side session
        # and no mock adapter, and reaches the runtime exclusively through
        # /api/v1. Mounting it here is the whole integration.
        from importlib import resources

        from starlette.staticfiles import StaticFiles

        ui_root = resources.files("orbit").joinpath("static/workflow-ui")
        routes.append(
            Mount("/ui", app=StaticFiles(directory=str(ui_root), html=True), name="ui")
        )

    routes.extend(extra_routes)
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.runtime = composition
    # None when discovery is off or found nothing; adapters must treat the
    # planner as optional rather than assume it.
    app.state.planner = composition.planner_service
    return app
