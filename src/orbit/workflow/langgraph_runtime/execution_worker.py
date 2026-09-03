"""Out-of-process Handler execution for a workspace Control Runtime."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import secrets
import threading
from typing import Any, Mapping, Sequence

from .compiler import (
    AcceptanceNotMet, BoundHandler, HandlerOutcome, LangGraphHandlerRegistry,
    LangGraphRetryableError, LangGraphRunCancelled,
    LangGraphUnknownExternalResult,
)


def _handler(registry: LangGraphHandlerRegistry, name: str) -> BoundHandler:
    # The worker is the registry's composition root. Looking up its sealed
    # entries here avoids manufacturing a fake workflow node merely to resolve
    # a name already authenticated by the parent-side registry.
    return registry._entries[name]  # noqa: SLF001


def _serve_worker(
    registrations: Sequence[Any], attempt_db_path: str, artifact_root: str,
    secret_values: Mapping[str, str], authkey: bytes, bootstrap,
) -> None:
    from .artifacts import LangGraphArtifactStore
    from .wiring import trusted_handlers

    store = LangGraphArtifactStore(attempt_db_path, artifact_root)
    registry = trusted_handlers(
        registrations,
        attempt_db_path=attempt_db_path,
        artifact_store=store,
        secret_values=secret_values,
    )
    listener = Listener(("127.0.0.1", 0), family="AF_INET", authkey=authkey)
    metadata = [
        {
            "name": item.name,
            "version": item.version,
            "manifest_fingerprint": item.manifest_fingerprint,
            "legacy_manifest_fingerprint": item.legacy_manifest_fingerprint,
            "supported_transports": tuple(item.supported_transports),
            "retry_safe": item.retry_safe,
            "capabilities": tuple(item.capabilities),
        }
        for item in registry._entries.values()  # noqa: SLF001
    ]
    bootstrap.send({"address": listener.address, "handlers": metadata, "pid": os.getpid()})
    bootstrap.close()
    stopping = threading.Event()

    def answer(connection) -> None:
        try:
            request = connection.recv()
            operation = request.get("operation")
            if operation == "shutdown":
                stopping.set()
                connection.send({"ok": True, "result": True})
                return
            item = _handler(registry, str(request["handler"]))
            if operation == "invoke":
                value = item.invoke(
                    request["inputs"], request["config"], request["context"],
                )
                if isinstance(value, HandlerOutcome):
                    payload = {"output": dict(value.output), "route": value.route}
                    connection.send({"ok": True, "kind": "outcome", "result": payload})
                else:
                    connection.send({"ok": True, "kind": "mapping", "result": dict(value)})
            elif operation == "cancel_run":
                value = False if item.cancel_run is None else item.cancel_run(request["run_id"])
                connection.send({"ok": True, "result": bool(value)})
            elif operation == "cancel_attempts":
                value = False if item.cancel_attempts is None else item.cancel_attempts(
                    request["run_id"], frozenset(request["attempt_ids"]),
                )
                connection.send({"ok": True, "result": bool(value)})
            elif operation == "finish_run":
                if item.finish_run is not None:
                    item.finish_run(request["run_id"])
                connection.send({"ok": True, "result": True})
            else:
                connection.send({"ok": False, "error_type": "ValueError", "error": "unknown operation"})
        except Exception as exc:  # delivered to the trusted control process
            try:
                connection.send({
                    "ok": False, "error_type": type(exc).__name__, "error": str(exc),
                })
            except (BrokenPipeError, EOFError, OSError):
                pass
        finally:
            connection.close()

    try:
        while not stopping.is_set():
            connection = listener.accept()
            threading.Thread(target=answer, args=(connection,), daemon=True).start()
    finally:
        listener.close()


@dataclass
class ExecutionWorkerController:
    process: Any
    address: Any
    authkey: bytes
    pid: int

    @property
    def alive(self) -> bool:
        return bool(self.process.is_alive())

    def request(self, payload: Mapping[str, Any]) -> Any:
        if not self.alive:
            raise LangGraphUnknownExternalResult("Execution Worker is unavailable")
        try:
            connection = Client(self.address, family="AF_INET", authkey=self.authkey)
            connection.send(dict(payload))
            response = connection.recv()
            connection.close()
        except (EOFError, OSError, BrokenPipeError) as exc:
            raise LangGraphUnknownExternalResult(
                "Execution Worker connection was lost; Handler outcome is unknown"
            ) from exc
        if response.get("ok"):
            return response
        error = str(response.get("error") or "Execution Worker failed")
        error_type = response.get("error_type")
        if error_type == "LangGraphRetryableError":
            raise LangGraphRetryableError(error)
        if error_type == "LangGraphRunCancelled":
            raise LangGraphRunCancelled(error)
        if error_type == "AcceptanceNotMet":
            # Kept as itself across the boundary: the driver settles the run
            # on this type, and a bare RuntimeError would reach it as a crash
            # and answer a caller with a blank 500 about a run whose reason is
            # written down.
            raise AcceptanceNotMet(error)
        if error_type in {"LangGraphUnknownExternalResult", "UnknownExternalResultError"}:
            raise LangGraphUnknownExternalResult(error)
        raise RuntimeError(f"Execution Worker {error_type}: {error}")

    def stop(self, timeout: float = 5.0) -> bool:
        if not self.alive:
            self.process.join(timeout=0)
            return True
        try:
            self.request({"operation": "shutdown"})
        except LangGraphUnknownExternalResult:
            pass
        # The accept loop may have entered its next blocking accept before the
        # shutdown handler set the event. One authenticated wake-up guarantees
        # it gets a chance to observe the mark and leave.
        if self.process.is_alive():
            try:
                connection = Client(self.address, family="AF_INET", authkey=self.authkey)
                connection.send({"operation": "shutdown"})
                connection.close()
            except (EOFError, OSError, BrokenPipeError):
                pass
        self.process.join(timeout=timeout)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=timeout)
        return not self.process.is_alive()


class ExecutionWorkerPool:
    """A bounded set of workers with attempt-stable dispatch."""

    def __init__(self, workers: Sequence[ExecutionWorkerController]) -> None:
        if not workers:
            raise ValueError("Execution Worker pool cannot be empty")
        self.workers = tuple(workers)
        self._lock = threading.Lock()
        self._next = 0
        self._attempt_workers: dict[str, ExecutionWorkerController] = {}

    @property
    def alive(self) -> bool:
        return all(worker.alive for worker in self.workers)

    @property
    def pid(self) -> int:
        return self.workers[0].pid

    @property
    def pids(self) -> tuple[int, ...]:
        return tuple(worker.pid for worker in self.workers)

    def _select(self, payload: Mapping[str, Any]) -> ExecutionWorkerController:
        operation = payload.get("operation")
        if operation == "invoke":
            attempt_id = str(getattr(payload.get("context"), "attempt_id", ""))
            with self._lock:
                worker = self._attempt_workers.get(attempt_id)
                if worker is None:
                    worker = self.workers[self._next % len(self.workers)]
                    self._next += 1
                    if attempt_id:
                        self._attempt_workers[attempt_id] = worker
                return worker
        return self.workers[0]

    def request(self, payload: Mapping[str, Any]) -> Any:
        operation = payload.get("operation")
        if operation in {"cancel_run", "cancel_attempts", "finish_run"}:
            responses = [worker.request(payload) for worker in self.workers if worker.alive]
            if operation == "finish_run":
                run_id = str(payload.get("run_id", ""))
                with self._lock:
                    for attempt_id in tuple(self._attempt_workers):
                        if attempt_id.startswith(f"langgraph_attempt:{run_id}:"):
                            self._attempt_workers.pop(attempt_id, None)
            return {
                "ok": True,
                "result": any(bool(item.get("result")) for item in responses),
            }
        return self._select(payload).request(payload)

    def stop(self, timeout: float = 5.0) -> bool:
        return all(worker.stop(timeout) for worker in self.workers)


def start_execution_worker(
    registrations: Sequence[Any], *, state_directory: Path | str,
    secret_values: Mapping[str, str] | None = None,
) -> tuple[LangGraphHandlerRegistry, ExecutionWorkerController]:
    """Start one authenticated worker and return parent-side Handler proxies."""

    state = Path(state_directory)
    state.mkdir(parents=True, exist_ok=True)
    methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in methods else "spawn")
    parent, child = context.Pipe(duplex=False)
    authkey = secrets.token_bytes(32)
    process = context.Process(
        target=_serve_worker,
        args=(
            tuple(registrations), str(state / "langgraph-runs.sqlite3"),
            str(state / "artifacts"), dict(secret_values or {}), authkey, child,
        ),
        name="orbit-execution-worker",
        daemon=True,
    )
    process.start()
    child.close()
    if not parent.poll(30):
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("Execution Worker did not become ready")
    bootstrap = parent.recv()
    parent.close()
    controller = ExecutionWorkerController(
        process, tuple(bootstrap["address"]), authkey, int(bootstrap["pid"]),
    )

    handlers = []
    for facts in bootstrap["handlers"]:
        name = facts["name"]

        def rpc(operation: str, *, selected=name, **payload):
            return controller.request({"operation": operation, "handler": selected, **payload})

        def invoke(inputs, config, execution_context, *, call=rpc):
            response = call(
                "invoke", inputs=dict(inputs), config=dict(config), context=execution_context,
            )
            if response.get("kind") == "outcome":
                value = response["result"]
                return HandlerOutcome(value["output"], route=value["route"])
            return response["result"]

        handlers.append(BoundHandler(
            name,
            facts["version"],
            facts["manifest_fingerprint"],
            invoke,
            cancel_run=lambda run_id, call=rpc: bool(call("cancel_run", run_id=run_id)["result"]),
            supported_transports=frozenset(facts["supported_transports"]),
            retry_safe=bool(facts["retry_safe"]),
            capabilities=frozenset(facts.get("capabilities", ())),
            finish_run=lambda run_id, call=rpc: call("finish_run", run_id=run_id),
            cancel_attempts=lambda run_id, attempts, call=rpc: bool(call(
                "cancel_attempts", run_id=run_id, attempt_ids=tuple(attempts),
            )["result"]),
            legacy_manifest_fingerprint=facts["legacy_manifest_fingerprint"],
        ))
    return LangGraphHandlerRegistry(handlers), controller


def start_execution_worker_pool(
    registrations: Sequence[Any], *, state_directory: Path | str,
    secret_values: Mapping[str, str] | None = None, worker_count: int = 1,
) -> tuple[LangGraphHandlerRegistry, ExecutionWorkerPool]:
    if not 1 <= worker_count <= 16:
        raise ValueError("execution worker count must be between 1 and 16")
    registries = []
    workers = []
    try:
        for _index in range(worker_count):
            registry, worker = start_execution_worker(
                registrations,
                state_directory=state_directory,
                secret_values=secret_values,
            )
            registries.append(registry)
            workers.append(worker)
    except Exception:
        for worker in workers:
            worker.stop()
        raise

    pool = ExecutionWorkerPool(workers)
    first = registries[0]
    handlers = []
    for original in first._entries.values():  # noqa: SLF001
        name = original.name

        def call(operation: str, *, selected=name, **payload):
            return pool.request({"operation": operation, "handler": selected, **payload})

        def invoke(inputs, config, execution_context, *, rpc=call):
            response = rpc(
                "invoke", inputs=dict(inputs), config=dict(config), context=execution_context,
            )
            if response.get("kind") == "outcome":
                value = response["result"]
                return HandlerOutcome(value["output"], route=value["route"])
            return response["result"]

        handlers.append(BoundHandler(
            name, original.version, original.manifest_fingerprint, invoke,
            cancel_run=lambda run_id, rpc=call: bool(rpc("cancel_run", run_id=run_id)["result"]),
            supported_transports=original.supported_transports,
            retry_safe=original.retry_safe,
            capabilities=original.capabilities,
            finish_run=lambda run_id, rpc=call: rpc("finish_run", run_id=run_id),
            cancel_attempts=lambda run_id, attempts, rpc=call: bool(rpc(
                "cancel_attempts", run_id=run_id, attempt_ids=tuple(attempts),
            )["result"]),
            legacy_manifest_fingerprint=original.legacy_manifest_fingerprint,
        ))
    return LangGraphHandlerRegistry(handlers), pool
