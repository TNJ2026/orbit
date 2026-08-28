"""Safe production wiring for Orbit's LangGraph execution runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Lock
from types import SimpleNamespace
from typing import Any
import uuid

from ..domain.deadlines import MIN_AGENT_DURATION_SECONDS
from ..domain.handlers import UnknownExternalResultError
from ..domain.durable_execution import ExecutionSafety
from ..domain.serialization import canonical_json
from ..data.secrets import assert_no_secret_values
from ..handlers import AgentHandler, ToolHandler, TransformHandler
from ..handlers.agent import AgentRequest
from ..handlers.context import ScopedSecretResolver
from .harness_subagent import HarnessSubagentHandler
from ..persistence.workflow_versions import SQLiteWorkflowVersionStore
from .artifacts import LangGraphArtifactStore
from .console import AttemptConsole, AttemptConsoleSink
from .compiler import (
    BoundHandler, HandlerOutcome, LangGraphHandlerRegistry, LangGraphRetryableError,
    LangGraphRunCancelled, LangGraphUnknownExternalResult,
)
from .service import (
    LangGraphWorkflowService, append_event, ensure_event_log,
)


def _transform(inputs: Mapping[str, Any], config: Mapping[str, Any], _context):
    operation = config.get("operation", "identity")
    if operation == "identity":
        return dict(inputs)
    if operation == "select_fields":
        fields = config.get("fields")
        if not isinstance(fields, (list, tuple)):
            raise ValueError("transform fields must be an array")
        return {key: inputs[key] for key in fields if key in inputs}
    if operation == "build_object":
        value = config.get("value")
        if not isinstance(value, Mapping):
            raise ValueError("transform value must be an object")
        return dict(value)
    raise ValueError(f"unsupported transform operation: {operation}")


class _CancelledRuns:
    """Which runs must not have work started for them, and nothing more.

    Read and written under the caller's own lock — the same one an adapter
    takes to claim an attempt — so a cancel either lands before the claim and
    stops it, or after it and finds an attempt to signal. There is no third
    order, which is the whole point: checking the run's status separately
    leaves a window where cancel is invisible to both.

    Held until the run is finished being driven, and released there. It was
    briefly a fixed number of recent cancellations, which is a guess about how
    long a race lasts and therefore wrong: a run cancelled during a Handler
    that takes minutes would have its mark evicted by enough other
    cancellations, and its next node would then be allowed to start. The
    engine's own check cannot cover that — by then the run is under way and
    has passed it. So the mark's life is the drive's life, and the entry is
    removed by name rather than by age.
    """

    def __init__(self) -> None:
        self._marked: set[str] = set()

    def add(self, run_id: str) -> None:
        self._marked.add(run_id)

    def discard(self, run_id: str) -> None:
        self._marked.discard(run_id)

    def __contains__(self, run_id: str) -> bool:
        return run_id in self._marked

    def __len__(self) -> int:
        return len(self._marked)


def _retryable(manifest, exc: Exception) -> Exception:
    """The same failure, said in the one way the engine can act on.

    Nothing raised this before. The DSL took a retry policy, the validator had
    rules for it, and the compiler carried timers, backoff and a per-generation
    budget for it — but no adapter ever asked for a retry, so a real Handler
    with `max_attempts: 3` was called once and the run failed. Every retry
    policy an author wrote was decoration.

    Whether to honour one is still the compiler's: a node without a policy
    gets its original failure re-raised. What is kept here is the narrower
    guarantee that an `unknown_on_lease_loss` Handler's failure is never even
    *described* as repeatable. Compilation already refuses to attach a retry
    policy to one, so this is not what stops the repeat — it stops a caller
    being handed a misleading exception on the way out.
    """

    if isinstance(exc, LangGraphRunCancelled):
        # Nothing to repeat: the run was cancelled, and a retry policy that
        # took this for a transient failure would schedule a timer that
        # re-entered a run somebody stopped.
        return exc
    if manifest.execution_safety is not ExecutionSafety.REPLAY_SAFE:
        return exc
    retryable = LangGraphRetryableError(f"{type(exc).__name__}: {exc}")
    retryable.__cause__ = exc
    return retryable


def _console_sink(console: AttemptConsole | None, context):
    """Where a Handler's process output goes, or nowhere.

    `None` is a real configuration — an embedder binding these adapters
    without the adapter's own database — and a Handler that cannot print is
    still a Handler that runs, so the absent case is silent rather than an
    error.
    """

    if console is None:
        return _Discarded()
    return AttemptConsoleSink(
        console, run_id=context.run_id, node_id=context.node_id,
        attempt_id=context.attempt_id,
    )


class _Discarded:
    def emit(self, _stream: str, _text: str) -> None:
        return None


class _HandlerAttemptJournal:
    """Prevent an external Handler effect from replaying after process loss."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.owner = uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            ensure_event_log(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS langgraph_handler_attempts("
                "attempt_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT NOT NULL,"
                "status TEXT NOT NULL,output_json TEXT,error TEXT,updated_at TEXT NOT NULL,"
                "handler_name TEXT NOT NULL DEFAULT '',execution_ref TEXT,"
                "execution_owner TEXT)"
            )
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(langgraph_handler_attempts)"
                )
            }
            if "handler_name" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_handler_attempts"
                    " ADD COLUMN handler_name TEXT NOT NULL DEFAULT ''"
                )
            if "execution_ref" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_handler_attempts ADD COLUMN execution_ref TEXT"
                )
            if "execution_owner" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_handler_attempts ADD COLUMN execution_owner TEXT"
                )

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def claim(
        self, context, *, handler_name: str = "", retry_failed: bool = False,
    ) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,output_json FROM langgraph_handler_attempts"
                " WHERE attempt_id=?", (context.attempt_id,),
            ).fetchone()
            if row is not None:
                if row["status"] == "failed" and retry_failed:
                    connection.execute(
                        "UPDATE langgraph_handler_attempts SET status='started',"
                        "error=NULL,updated_at=? WHERE attempt_id=? AND status='failed'",
                        (self._now(), context.attempt_id),
                    )
                    self._append(
                        connection, "started", context.run_id,
                        context.node_id, context.attempt_id,
                    )
                    connection.commit()
                    return None
                connection.commit()
                if row["status"] == "succeeded":
                    return json.loads(row["output_json"])
                raise LangGraphUnknownExternalResult(
                    f"Handler attempt {context.attempt_id} has {row['status']} outcome"
                )
            connection.execute(
                "INSERT INTO langgraph_handler_attempts"
                "(attempt_id,run_id,node_id,status,output_json,error,updated_at,"
                "handler_name) VALUES (?,?,?,'started',NULL,NULL,?,?)",
                (context.attempt_id, context.run_id, context.node_id, self._now(),
                 handler_name),
            )
            self._append(
                connection, "started", context.run_id,
                context.node_id, context.attempt_id,
            )
            connection.commit()
        return None

    def record_execution(self, attempt_id: str, execution_ref: str) -> None:
        if not isinstance(execution_ref, str) or not execution_ref.strip():
            raise ValueError("execution_ref is required")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE langgraph_handler_attempts SET execution_ref=?,"
                "execution_owner=?,updated_at=? WHERE attempt_id=? AND status='started'",
                (execution_ref, self.owner, self._now(), attempt_id),
            ).rowcount
            connection.commit()
        if changed != 1:
            raise RuntimeError("Agent execution could not be attached to its attempt")

    def stale_execution_ref(self, attempt_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status,execution_ref,execution_owner"
                " FROM langgraph_handler_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if (
            row is None or row["status"] != "started" or not row["execution_ref"]
            or row["execution_owner"] == self.owner
        ):
            return None
        return str(row["execution_ref"])

    def _append(
        self, connection, outcome: str, run_id: str, node_id: str, attempt_id: str,
    ) -> None:
        """One node event, in the transaction that recorded the attempt.

        The run log is one stream, so a subscriber sees a node start and a run
        settle in the order they happened rather than having to reconcile two.
        """

        append_event(
            connection, run_id, f"langgraph_node.{outcome}",
            occurred_at=self._now(), node_id=node_id, attempt_id=attempt_id,
        )

    def settle(
        self, attempt_id: str, status: str, *, output=None, error: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT run_id,node_id FROM langgraph_handler_attempts"
                " WHERE attempt_id=?", (attempt_id,),
            ).fetchone()
            connection.execute(
                "UPDATE langgraph_handler_attempts SET status=?,output_json=?,error=?,"
                "updated_at=? WHERE attempt_id=?",
                (
                    status,
                    None if output is None else canonical_json(output),
                    error,
                    self._now(),
                    attempt_id,
                ),
            )
            if attempt is not None:
                self._append(
                    connection, status, attempt["run_id"], attempt["node_id"],
                    attempt_id,
                )
            connection.commit()


def _validate_secret_refs(inputs, context, manifest) -> None:
    for port in context.input_ports:
        if port.get("data_policy", {}).get("transport") != "secret_ref":
            continue
        reference = inputs.get(port["id"])
        if not isinstance(reference, Mapping):
            raise ValueError(f"secret_ref input {port['id']!r} must be an object")
        unknown = set(reference) - {"logical_name", "version", "provider_hint"}
        logical_name = reference.get("logical_name")
        if unknown or not isinstance(logical_name, str) or not logical_name:
            raise ValueError(f"secret_ref input {port['id']!r} is invalid")
        if logical_name not in manifest.required_secrets:
            raise ValueError(
                f"secret_ref input {port['id']!r} was not declared by Handler Manifest"
            )


def _agent_adapter(
    implementation: AgentHandler, manifest, journal, artifact_store, secret_values,
    console: AttemptConsole | None = None,
):
    active: dict[str, dict[str, str]] = {}
    active_lock = Lock()
    cancelled = _CancelledRuns()
    pruned_attempts: set[str] = set()

    def cancel_attempt(attempt_id: str) -> None:
        execution_ref = f"agent:{attempt_id}"
        request_cancel = getattr(implementation.client, "request_cancel", None)
        if request_cancel is not None:
            request_cancel(execution_ref)
        else:
            implementation.client.cancel(execution_ref)

    def clear_cancel_attempt(attempt_id: str) -> None:
        clear_cancel_request = getattr(
            implementation.client, "clear_cancel_request", None,
        )
        if clear_cancel_request is not None:
            clear_cancel_request(f"agent:{attempt_id}")

    def consume_pruned_locked(attempt_id: str) -> bool:
        if attempt_id not in pruned_attempts:
            return False
        pruned_attempts.discard(attempt_id)
        return True

    def consume_pruned(attempt_id: str) -> bool:
        with active_lock:
            return consume_pruned_locked(attempt_id)

    def pruned_outcome(context, artifacts):
        artifact_store.abandon(artifacts.produced_artifact_ids)
        journal.settle(context.attempt_id, "cancelled", error="winner_join_pruned")
        return HandlerOutcome(
            {port["id"]: None for port in context.output_ports}, route="cancel",
        )

    def invoke(inputs, config, context):
        _validate_secret_refs(inputs, context, manifest)
        recovery_ref = journal.stale_execution_ref(context.attempt_id)
        if recovery_ref is not None:
            try:
                implementation.client.recover(recovery_ref)
            except Exception:
                # Recovery is cleanup, not evidence that the external result is
                # known. `claim` below still prevents a replay and reports the
                # durable started attempt as unknown.
                pass
        replay = journal.claim(context, handler_name=manifest.name)
        if replay is not None:
            return replay
        configured_timeout = config.get("timeout_seconds")
        duration = manifest.resource_profile.max_duration_seconds
        if (
            isinstance(configured_timeout, int)
            and not isinstance(configured_timeout, bool)
        ):
            duration = min(
                duration, max(MIN_AGENT_DURATION_SECONDS, configured_timeout),
            )
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=duration
        )
        artifacts = artifact_store.access(
            run_id=context.run_id,
            node_id=context.node_id,
            attempt_id=context.attempt_id,
            output_ports=tuple(
                SimpleNamespace(
                    id=port["id"], schema_id=port["schema_id"],
                    data_policy=SimpleNamespace(
                        transport=SimpleNamespace(value=port["data_policy"]["transport"]),
                        max_size_bytes=port["data_policy"]["max_size_bytes"],
                        content_types=tuple(port["data_policy"]["content_types"]),
                    ),
                )
                for port in context.output_ports
            ),
            inputs=inputs,
            input_ports=context.input_ports,
            secret_values=secret_values.values(),
            actor=context.actor,
        )
        secrets = ScopedSecretResolver(
            tuple(manifest.required_secrets), secret_values,
        )
        request_context = SimpleNamespace(
            request=SimpleNamespace(
                attempt_id=context.attempt_id,
                # Carried so a Handler can give this run its own place to
                # work. Nodes in one run hand files to each other; attempts of
                # one node must land in the same directory.
                run_id=context.run_id,
                deadline=deadline,
                process_deadline=deadline,
                input=inputs,
                config=config,
                input_ports=context.input_ports,
                output_ports=context.output_ports,
            ),
            output=_console_sink(console, context),
            artifacts=artifacts,
            secrets=secrets,
            record_execution=lambda execution_ref: journal.record_execution(
                context.attempt_id, execution_ref,
            ),
        )
        if consume_pruned(context.attempt_id):
            return pruned_outcome(context, artifacts)
        try:
            with active_lock:
                if context.run_id in cancelled:
                    raise LangGraphRunCancelled(
                        f"run {context.run_id} was cancelled before this attempt"
                    )
                pruned_before_start = consume_pruned_locked(context.attempt_id)
                if not pruned_before_start:
                    active.setdefault(context.run_id, {})[
                        context.attempt_id
                    ] = context.node_id
            if pruned_before_start:
                return pruned_outcome(context, artifacts)
            try:
                response = implementation.client.execute(
                    AgentRequest(inputs, config, context.attempt_id), request_context
                )
            finally:
                with active_lock:
                    attempts = active.get(context.run_id)
                    if attempts is not None:
                        attempts.pop(context.attempt_id, None)
                        if not attempts:
                            active.pop(context.run_id, None)
                clear_cancel_attempt(context.attempt_id)
            if consume_pruned(context.attempt_id):
                return pruned_outcome(context, artifacts)
            if not isinstance(response.output, Mapping):
                raise ValueError("Agent output must be an object")
            output = dict(response.output)
            assert_no_secret_values(output, secret_values.values())
            referenced = {str(item) for item in response.artifact_refs}
            produced = set(artifacts.produced_artifact_ids)
            if referenced != produced:
                raise ValueError(
                    "Agent artifact_refs must exactly match produced Artifacts"
                )
            artifacts.commit()
            journal.settle(context.attempt_id, "succeeded", output=output)
            return output
        except UnknownExternalResultError as exc:
            if consume_pruned(context.attempt_id):
                return pruned_outcome(context, artifacts)
            artifact_store.abandon(artifacts.produced_artifact_ids)
            journal.settle(context.attempt_id, "unknown", error=str(exc))
            raise LangGraphUnknownExternalResult(str(exc)) from None
        except LangGraphRunCancelled:
            # No work started, so there is no outcome to record beyond the
            # attempt not having happened — and nothing to retry.
            journal.settle(
                context.attempt_id, "failed", error="LangGraphRunCancelled",
            )
            raise
        except Exception as exc:
            artifact_store.abandon(artifacts.produced_artifact_ids)
            journal.settle(
                context.attempt_id, "failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise _retryable(manifest, exc)

    def cancel_run(run_id: str) -> bool:
        with active_lock:
            cancelled.add(run_id)
            attempts = tuple(active.get(run_id, ()))
        for attempt_id in attempts:
            cancel_attempt(attempt_id)
            with active_lock:
                still_active = attempt_id in active.get(run_id, {})
            if not still_active:
                clear_cancel_attempt(attempt_id)
        return bool(attempts)

    def cancel_attempts(run_id: str, attempt_ids: frozenset[str]) -> bool:
        with active_lock:
            pruned_attempts.update(attempt_ids)
            attempts = tuple(
                attempt_id
                for attempt_id in active.get(run_id, {})
                if attempt_id in attempt_ids
            )
        for attempt_id in attempts:
            cancel_attempt(attempt_id)
            with active_lock:
                still_active = attempt_id in active.get(run_id, {})
            if not still_active:
                clear_cancel_attempt(attempt_id)
        return bool(attempts)

    def finish_run(run_id: str) -> None:
        """The run is no longer being driven, so its mark has no more work."""

        with active_lock:
            cancelled.discard(run_id)
            prefix = f"langgraph_attempt:{run_id}:"
            pruned_attempts.difference_update(
                item for item in tuple(pruned_attempts) if item.startswith(prefix)
            )

    return invoke, cancel_run, cancel_attempts, finish_run


def _tool_adapter(
    implementation: ToolHandler, manifest, journal, secret_values,
    console: AttemptConsole | None = None,
):
    active: dict[str, dict[str, Any]] = {}
    active_lock = Lock()
    cancelled = _CancelledRuns()
    pruned_attempts: set[str] = set()

    def consume_pruned_locked(attempt_id: str) -> bool:
        if attempt_id not in pruned_attempts:
            return False
        pruned_attempts.discard(attempt_id)
        return True

    def consume_pruned(attempt_id: str) -> bool:
        with active_lock:
            return consume_pruned_locked(attempt_id)

    def pruned_outcome(context):
        journal.settle(context.attempt_id, "cancelled", error="winner_join_pruned")
        return HandlerOutcome(
            {port["id"]: None for port in context.output_ports}, route="cancel",
        )

    def invoke(inputs, config, context):
        _validate_secret_refs(inputs, context, manifest)
        validation = implementation.validate(manifest, config)
        if not validation.valid:
            issue = validation.issues[0]
            raise ValueError(f"Tool Handler validation {issue.path}: {issue.message}")
        replay = journal.claim(
            context,
            handler_name=manifest.name,
            retry_failed=(
                manifest.execution_safety is ExecutionSafety.REPLAY_SAFE
            ),
        )
        if replay is not None:
            return replay
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=manifest.resource_profile.max_duration_seconds
        )
        request = SimpleNamespace(
            attempt_id=context.attempt_id, input=inputs, config=config,
            idempotency_key=context.attempt_id, deadline=deadline,
            process_deadline=deadline, actor=context.actor,
        )
        handler_context = SimpleNamespace(
            request=request,
            secrets=ScopedSecretResolver(
                tuple(manifest.required_secrets), secret_values,
            ),
            artifacts=None,
            output=_console_sink(console, context),
            clock=lambda: datetime.now(timezone.utc),
        )
        if consume_pruned(context.attempt_id):
            return pruned_outcome(context)
        try:
            with active_lock:
                if context.run_id in cancelled:
                    raise LangGraphRunCancelled(
                        f"run {context.run_id} was cancelled before this attempt"
                    )
            # `prepare` is where an external execution begins, so the check
            # above is the last moment at which refusing costs nothing — but
            # it cannot be the only one. The lock is released across `prepare`
            # on purpose, since holding it there would serialise every
            # attempt this adapter runs behind one external call. So a cancel
            # can land while `prepare` is in flight, and it would see no
            # active entry to signal: registering and re-reading the mark in
            # one acquisition is what closes that, and what is left over is
            # cancelled rather than executed.
            prepared = implementation.prepare(
                request, SimpleNamespace(request=request)
            )
            with active_lock:
                stranded = context.run_id in cancelled
                pruned_while_preparing = consume_pruned_locked(context.attempt_id)
                if not stranded and not pruned_while_preparing:
                    active[context.attempt_id] = {
                        "execution_ref": prepared.execution_ref,
                        "context": handler_context,
                        "run_id": context.run_id,
                        "node_id": context.node_id,
                    }
            if stranded:
                implementation.cancel(prepared.execution_ref, handler_context)
                raise LangGraphRunCancelled(
                    f"run {context.run_id} was cancelled while preparing"
                )
            if pruned_while_preparing:
                implementation.cancel(prepared.execution_ref, handler_context)
                return pruned_outcome(context)
            try:
                raw = implementation.execute(prepared, handler_context)
                result = implementation.normalize_result(raw, handler_context)
            finally:
                with active_lock:
                    active.pop(context.attempt_id, None)
            if consume_pruned(context.attempt_id):
                return pruned_outcome(context)
            if not isinstance(result.output, Mapping):
                raise ValueError("Tool output must be an object")
            output = dict(result.output)
            assert_no_secret_values(output, secret_values.values())
            journal.settle(context.attempt_id, "succeeded", output=output)
            return output
        except LangGraphRunCancelled:
            # No work started, so there is no outcome to record beyond the
            # attempt not having happened — and nothing to retry.
            journal.settle(
                context.attempt_id, "failed", error="LangGraphRunCancelled",
            )
            raise
        except Exception as exc:
            if consume_pruned(context.attempt_id):
                return pruned_outcome(context)
            if isinstance(implementation, HarnessSubagentHandler):
                if isinstance(exc, UnknownExternalResultError):
                    journal.settle(
                        context.attempt_id, "unknown", error=type(exc).__name__,
                    )
                    delegation_id = exc.failure.provider_request_id or context.attempt_id
                    raise LangGraphUnknownExternalResult(
                        "reconciliation_required: Harness delegation "
                        f"{delegation_id} outcome is unknown"
                    ) from None
                journal.settle(
                    context.attempt_id, "failed", error=type(exc).__name__,
                )
                raise
            if manifest.execution_safety is ExecutionSafety.UNKNOWN_ON_LEASE_LOSS:
                journal.settle(context.attempt_id, "unknown", error=type(exc).__name__)
                raise LangGraphUnknownExternalResult(
                    f"Tool attempt {context.attempt_id} outcome is unknown"
                ) from None
            journal.settle(context.attempt_id, "failed", error=type(exc).__name__)
            raise _retryable(manifest, exc)

    def cancel_run(run_id: str) -> bool:
        with active_lock:
            cancelled.add(run_id)
            entries = tuple(
                item for item in active.values() if item["run_id"] == run_id
            )
        for item in entries:
            implementation.cancel(item["execution_ref"], item["context"])
        return bool(entries)

    def cancel_attempts(run_id: str, attempt_ids: frozenset[str]) -> bool:
        with active_lock:
            pruned_attempts.update(attempt_ids)
            entries = tuple(
                (attempt_id, item) for attempt_id, item in active.items()
                if item["run_id"] == run_id and attempt_id in attempt_ids
            )
        for _attempt_id, item in entries:
            implementation.cancel(item["execution_ref"], item["context"])
        return bool(entries)

    def finish_run(run_id: str) -> None:
        """The run is no longer being driven, so its mark has no more work."""

        with active_lock:
            cancelled.discard(run_id)
            prefix = f"langgraph_attempt:{run_id}:"
            pruned_attempts.difference_update(
                item for item in tuple(pruned_attempts) if item.startswith(prefix)
            )

    return invoke, cancel_run, cancel_attempts, finish_run


def trusted_handlers(
    registrations: Sequence[Any], *, attempt_db_path: Path | str | None = None,
    artifact_store: LangGraphArtifactStore | None = None,
    secret_values: Mapping[str, str] | None = None,
) -> LangGraphHandlerRegistry:
    """Expose only reviewed adapters, never arbitrary Orbit NodeHandlers."""

    if artifact_store is None and attempt_db_path is not None:
        attempt_path = Path(attempt_db_path)
        artifact_store = LangGraphArtifactStore(
            attempt_path, attempt_path.parent / "artifacts",
        )
    # Beside the attempts it belongs to. Not in the same transaction as
    # anything: a console is an observation, and a slow or failing write of it
    # must not reach the attempt it is describing.
    console = None if attempt_db_path is None else AttemptConsole(attempt_db_path)
    handlers: list[BoundHandler] = []
    secret_values = dict(secret_values or {})
    for registration in registrations:
        manifest = registration.manifest
        if isinstance(registration.implementation, TransformHandler):
            handlers.append(BoundHandler(
                manifest.name,
                manifest.version,
                manifest.fingerprint,
                _transform,
                retry_safe=True,
                legacy_manifest_fingerprint=manifest.legacy_fingerprint,
            ))
        elif (
            isinstance(registration.implementation, AgentHandler)
            and attempt_db_path is not None
            and artifact_store is not None
        ):
            journal = _HandlerAttemptJournal(attempt_db_path)
            invoke, cancel_run, cancel_attempts, finish_run = _agent_adapter(
                registration.implementation, manifest, journal, artifact_store,
                secret_values, console,
            )
            handlers.append(BoundHandler(
                manifest.name,
                manifest.version,
                manifest.fingerprint,
                invoke,
                cancel_run=cancel_run,
                supported_transports=frozenset({
                    "inline", "artifact_ref", "secret_ref",
                }),
                finish_run=finish_run,
                cancel_attempts=cancel_attempts,
                legacy_manifest_fingerprint=manifest.legacy_fingerprint,
            ))
        elif (
            isinstance(registration.implementation, (ToolHandler, HarnessSubagentHandler))
            and attempt_db_path is not None
        ):
            journal = _HandlerAttemptJournal(attempt_db_path)
            invoke, cancel_run, cancel_attempts, finish_run = _tool_adapter(
                registration.implementation, manifest, journal, secret_values,
                console,
            )
            handlers.append(BoundHandler(
                manifest.name,
                manifest.version,
                manifest.fingerprint,
                invoke,
                cancel_run=cancel_run,
                supported_transports=frozenset({"inline", "secret_ref"}),
                retry_safe=(
                    manifest.execution_safety is ExecutionSafety.REPLAY_SAFE
                ),
                finish_run=finish_run,
                cancel_attempts=cancel_attempts,
                legacy_manifest_fingerprint=manifest.legacy_fingerprint,
            ))
    return LangGraphHandlerRegistry(handlers)


def build_service(
    workflow_db_path: Path | str,
    registrations: Sequence[Any],
    *,
    state_directory: Path | str,
    secret_values: Mapping[str, str] | None = None,
    single_goal: bool = False,
    rebind: Any = None,
) -> LangGraphWorkflowService:
    """Build the isolated service and its two adapter-owned databases."""

    state = Path(state_directory)
    artifact_store = LangGraphArtifactStore(
        state / "langgraph-runs.sqlite3", state / "artifacts",
    )
    return LangGraphWorkflowService(
        SQLiteWorkflowVersionStore(workflow_db_path),
        trusted_handlers(
            registrations,
            attempt_db_path=state / "langgraph-runs.sqlite3",
            artifact_store=artifact_store,
            secret_values=secret_values,
        ),
        run_db_path=state / "langgraph-runs.sqlite3",
        checkpoint_db_path=state / "langgraph-checkpoints.sqlite3",
        artifact_store=artifact_store,
        console=AttemptConsole(state / "langgraph-runs.sqlite3"),
        single_goal=single_goal,
        rebind=rebind,
    )
