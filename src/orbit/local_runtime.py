"""Single-process runtime optimized for one local Orbit project."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .runner import run_queued_job
from .settings import read_settings
from .store import Store


class LocalRuntime:
    """Own background maintenance and embedded workers with clean shutdown."""

    def __init__(
        self,
        store: Store,
        project_root: str | None,
        *,
        run_worker: bool,
        worker_concurrency: int,
        scheduler_tick: Callable[[Store, str | None], Any],
        maintenance: list[tuple[str, float, Callable[[Store, str | None], Any]]],
        hub_sweep: tuple[float, Callable[[Store, str | None], Any]],
        goal_verify: tuple[float, Callable[[Store, str | None], Any]],
        record_error: Callable[[str, Exception], None],
    ):
        self.store = store
        self.project_root = project_root
        self.run_worker = run_worker
        self.worker_concurrency = max(1, int(worker_concurrency))
        self.scheduler_tick = scheduler_tick
        self.maintenance = maintenance
        self.hub_sweep = hub_sweep
        self.goal_verify = goal_verify
        self.record_error = record_error
        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._capacity = threading.Condition()
        self._active_workers = 0
        self._started = False
        self._stopped = False

    def _start_thread(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def start(self) -> None:
        with self._state_lock:
            if self._started or self._stopped:
                return
            self._started = True
        if self.run_worker:
            reaped = self.store.reap_stale_runs()
            if reaped:
                print(f"reaped {reaped} stale running task_runs at startup", flush=True)
            # Apply results left at the runner/scheduler boundary by a prior
            # version before switching this process to the direct local path.
            try:
                self.scheduler_tick(self.store, self.project_root)
            except Exception as exc:
                self.record_error("scheduler_startup", exc)
            self._start_thread("workflow-maintenance", self._maintenance_loop)
            self._start_thread("hub-inspect-sweep", self._hub_loop)
            self._start_thread("goal-verify-sweep", self._goal_verify_loop)
            for index in range(self.worker_concurrency):
                self._start_thread(
                    f"runner-worker-{index}",
                    lambda index=index: self._worker_loop(index),
                )
        else:
            self._start_thread("workflow-maintenance", self._maintenance_loop)
            self._start_thread("workflow-scheduler", self._scheduler_loop)
            self._start_thread("hub-inspect-sweep", self._hub_loop)
            self._start_thread("goal-verify-sweep", self._goal_verify_loop)

    def _maintenance_loop(self) -> None:
        due = {name: time.monotonic() + interval for name, interval, _ in self.maintenance}
        while not self.stop_event.wait(0.5):
            now = time.monotonic()
            for name, interval, callback in self.maintenance:
                if now < due[name]:
                    continue
                due[name] = now + interval
                try:
                    callback(self.store, self.project_root)
                except Exception as exc:
                    self.record_error(name, exc)

    def _periodic_loop(
        self,
        component: str,
        interval: float,
        callback: Callable[[Store, str | None], Any],
    ) -> None:
        while not self.stop_event.wait(max(0.1, float(interval))):
            try:
                callback(self.store, self.project_root)
            except Exception as exc:
                self.record_error(component, exc)

    def _scheduler_loop(self) -> None:
        self._periodic_loop("scheduler", 1.0, self.scheduler_tick)

    def _hub_loop(self) -> None:
        self._periodic_loop("hub_inspect", *self.hub_sweep)

    def _goal_verify_loop(self) -> None:
        self._periodic_loop("goal_verify", *self.goal_verify)

    def _acquire_capacity(self) -> bool:
        with self._capacity:
            while not self.stop_event.is_set():
                configured = int(
                    read_settings(self.project_root).get("max_concurrent_tasks", 1)
                )
                limit = max(1, min(self.worker_concurrency, configured))
                if self._active_workers < limit:
                    self._active_workers += 1
                    return True
                self._capacity.wait(timeout=1.0)
        return False

    def _release_capacity(self) -> None:
        with self._capacity:
            self._active_workers = max(0, self._active_workers - 1)
            self._capacity.notify_all()

    def _worker_loop(self, index: int) -> None:
        generation = self.store.run_job_generation()
        name = f"serve-local-{index}"
        while not self.stop_event.is_set():
            if not self._acquire_capacity():
                return
            try:
                result = run_queued_job(
                    self.store,
                    self.project_root,
                    runner_name=name,
                    lease_seconds=3600,
                    apply_inline=True,
                    renew_lease=False,
                )
            except Exception as exc:
                self.record_error("embedded_runner", exc)
                result = None
            finally:
                self._release_capacity()
            if result is None:
                generation = self.store.wait_for_run_job(
                    generation, self.stop_event, timeout=30.0
                )

    def stop(self) -> None:
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
        self.stop_event.set()
        self.store.wake_run_job_waiters()
        with self._capacity:
            self._capacity.notify_all()
        if self.run_worker:
            for run in self.store.list_running_task_runs():
                try:
                    self.store.request_run_kill(
                        int(run["id"]), "local runtime shutting down"
                    )
                except Exception:
                    pass
        deadline = time.monotonic() + 15.0
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # Do not close underneath a long-running verification callback that
        # ignored shutdown; the daemon thread will disappear with the process.
        if not any(thread.is_alive() for thread in self._threads):
            self.store.close()
