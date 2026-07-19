"""Single-step durable worker and timer loops with injected execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event
from typing import Any, Callable

from ..domain.handlers import HandlerResultStatus
from ..domain.serialization import to_primitive
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


class PlannerDispatcher:
    """Claim and execute one durable Planner attempt."""

    def __init__(
        self, service, *, worker_id="planner-1", clock, metrics=None,
        lease_margin_seconds=60,
    ):
        self.service = service
        self.worker_id = worker_id
        self.clock = clock
        self.metrics = metrics or InMemoryMetrics()
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
        result = self.service.execute_claimed(
            claimed, self.clock(), clock=self.clock
        )
        status_object = getattr(result, "status", None)
        status = getattr(status_object, "value", status_object)
        if status == "unknown" and getattr(result, "raw_response", None) is not None:
            self._increment("planner_unknown_preserved")
        else:
            self._increment("planner_completed")
        return True
