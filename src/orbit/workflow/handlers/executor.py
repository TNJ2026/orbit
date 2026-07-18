"""Registry-backed ExecutorPort implementation for NodeHandler lifecycle."""

from __future__ import annotations

from dataclasses import replace
from threading import Lock
from typing import Mapping

from jsonschema import Draft202012Validator

from ..domain.errors import ErrorInfo
from ..domain.handler_context import HandlerContext, PrepareContext
from ..domain.handlers import (
    HandlerExecutionError, HandlerPermanentError, HandlerResult,
    HandlerResultStatus, HandlerValidationError,
)
from ..domain.serialization import canonical_json, to_primitive
from ..data.secrets import assert_no_secret_values
from .context import NullTracer, RejectingArtifactWriter, ScopedSecretResolver, utc_now
from .registry import ExecutionRegistry
from .usage import InMemoryUsageReporter, UsageConflictError


def _schema_error(schema, value, label: str) -> None:
    errors = sorted(
        Draft202012Validator(to_primitive(schema)).iter_errors(to_primitive(value)),
        key=lambda item: tuple(str(part) for part in item.path),
    )
    if errors:
        path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in errors[0].path)
        raise HandlerValidationError(f"{label} {path}: {errors[0].message}")


class HandlerExecutor:
    def __init__(
        self,
        registry: ExecutionRegistry,
        schema_catalog,
        *,
        secret_values: Mapping[str, str] | None = None,
        artifact_writer=None,
        artifact_access_factory=None,
        logger=None,
        tracer=None,
        clock=utc_now,
        usage_reporter_factory=InMemoryUsageReporter,
    ) -> None:
        if not registry.sealed:
            raise RuntimeError("HandlerExecutor requires a sealed ExecutionRegistry")
        self.registry = registry
        self.schemas = schema_catalog
        self.secret_values = dict(secret_values or {})
        self.artifact_writer = artifact_writer or RejectingArtifactWriter()
        self.artifact_access_factory = artifact_access_factory
        self.logger = logger or (lambda message, fields: None)
        self.tracer = tracer or NullTracer()
        self.clock = clock
        self.usage_reporter_factory = usage_reporter_factory
        self._current = {}
        self._current_lock = Lock()

    def execute(self, request, cancellation_token) -> HandlerResult:
        entry = self.registry.resolve(
            request.handler_name,
            request.handler_version,
            expected_manifest_fingerprint=request.handler_manifest_fingerprint,
        )
        manifest = entry.manifest
        resolver = ScopedSecretResolver(manifest.required_secrets, self.secret_values)
        reporter = self.usage_reporter_factory()
        artifacts = (
            self.artifact_access_factory(request)
            if self.artifact_access_factory is not None else self.artifact_writer
        )
        context = HandlerContext(
            request, resolver, artifacts, reporter,
            cancellation_token, self.logger, self.tracer, self.clock,
        )
        try:
            _schema_error(manifest.config_schema, request.config, "config")
            validation = entry.implementation.validate(manifest, request.config)
            if not validation.valid:
                first = validation.issues[0]
                raise HandlerValidationError(
                    f"handler validation {first.path}: {first.message}"
                )
            self._validate_input(manifest, request.input)
            prepared = entry.implementation.prepare(request, PrepareContext(request))
            cancellation_token.raise_if_cancelled()
            with self._current_lock:
                self._current[request.attempt_id] = (
                    entry.implementation, prepared.execution_ref, context
                )
            raw = entry.implementation.execute(prepared, context)
            result = entry.implementation.normalize_result(raw, context)
            result = self._finalize_usage(result, reporter, request.attempt_id)
            if result.status is HandlerResultStatus.SUCCEEDED:
                assert_no_secret_values(result.output, self.secret_values.values())
                if len(canonical_json(result.output).encode("utf-8")) > 1_048_576:
                    raise HandlerValidationError("output exceeds 1 MiB inline limit")
                schema = self.schemas.get(manifest.result_schema_id)
                if schema is None:
                    raise HandlerValidationError(
                        f"result schema is not registered: {manifest.result_schema_id}"
                    )
                _schema_error(schema, result.output, "output")
            return result
        except HandlerExecutionError as exc:
            failure = exc.failure
            error = failure.error
            redacted_message = resolver.redact(error.message)
            redacted_details = resolver.redact_data(error.details)
            redacted_cause = resolver.redact_data(error.cause)
            if (
                redacted_message != error.message
                or redacted_details != error.details
                or redacted_cause != error.cause
            ):
                error = ErrorInfo(
                    error.code, error.category, redacted_message, error.source,
                    redacted_details, redacted_cause,
                )
                failure = replace(failure, error=error)
            return self._finalize_usage(
                failure.to_result(), reporter, request.attempt_id
            )
        except UsageConflictError as exc:
            return HandlerPermanentError(
                "handler returned invalid cumulative usage",
                details={"reason": type(exc).__name__},
            ).failure.to_result()
        except Exception as exc:
            return HandlerPermanentError(
                f"handler raised {type(exc).__name__}"
            ).failure.to_result()
        finally:
            with self._current_lock:
                self._current.pop(request.attempt_id, None)

    def cancel_current(self, attempt_id=None) -> bool:
        with self._current_lock:
            if attempt_id is None:
                current = next(iter(self._current.values())) if len(self._current) == 1 else None
            else:
                current = self._current.get(attempt_id)
        if current is None:
            return False
        implementation, execution_ref, context = current
        if execution_ref is None:
            return False
        implementation.cancel(execution_ref, context)
        return True

    def _validate_input(self, manifest, value) -> None:
        if not manifest.inputs:
            return
        if not isinstance(value, Mapping):
            raise HandlerValidationError("handler input must be an object")
        for port, schema_id in manifest.inputs.items():
            if port not in value:
                raise HandlerValidationError(f"input port is missing: {port}")
            schema = self.schemas.get(schema_id)
            if schema is None:
                raise HandlerValidationError(f"input schema is not registered: {schema_id}")
            _schema_error(schema, value[port], f"input.{port}")

    @staticmethod
    def _finalize_usage(result, reporter, attempt_id):
        before = reporter.latest(attempt_id)
        if result.usage is not None:
            if result.usage.attempt_id != attempt_id:
                raise UsageConflictError("final usage belongs to a different Attempt")
            if before is not None and result.usage.sequence.value < before.sequence.value:
                raise UsageConflictError("final usage is older than streamed usage")
            reporter.report(result.usage)
        latest = reporter.latest(attempt_id)
        if result.usage is None and latest is not None:
            return replace(result, usage=latest, usage_incomplete=True)
        return result
