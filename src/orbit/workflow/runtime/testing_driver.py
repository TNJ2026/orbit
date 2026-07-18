"""Test-only in-memory handler driver; all state changes still use Commands."""

from __future__ import annotations

from datetime import datetime, timezone

from ..domain.envelopes import CommandEnvelope
from ..domain.ids import EntityId
from ..domain.versions import AggregateVersion
from .events import derived_id


class InMemoryExecutionDriver:
    def __init__(self, service, handlers, *, clock=None) -> None:
        self.service = service
        self.handlers = handlers
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run_ready_nodes(self, run_id: EntityId) -> None:
        while True:
            run = self.service.get_run(run_id)
            if run.status.value != "running":
                return
            with self.service.uow_factory() as uow:
                ready = [
                    item for item in uow.node_runs.list_by_run(run_id)
                    if item.status.value == "ready"
                ]
            if not ready:
                return
            node = ready[0]
            now = self.clock()
            start = CommandEnvelope(
                derived_id("command", node.node_run_id, "start"), "start_attempt",
                node.node_run_id, run_id, node.aggregate_version,
                f"start:{node.node_run_id}", "memory-driver", now, {},
            )
            started = self.service.submit(start)
            attempt_id = EntityId.parse(started.summary["attempt_id"])
            with self.service.uow_factory() as uow:
                input_event = next(
                    item for item in reversed(uow.events.read_stream(node.node_run_id))
                    if item.envelope.event_type == "node_input_prepared"
                )
            handler = self.handlers[node.node_id]
            try:
                output = handler(dict(input_event.envelope.payload["input"]))
                command = CommandEnvelope(
                    derived_id("command", attempt_id, "complete"), "complete_attempt",
                    attempt_id, run_id, AggregateVersion(2),
                    f"complete:{attempt_id}", "memory-driver", now, {"output": output},
                )
            except Exception as exc:
                command = CommandEnvelope(
                    derived_id("command", attempt_id, "fail"), "fail_attempt",
                    attempt_id, run_id, AggregateVersion(2),
                    f"fail:{attempt_id}", "memory-driver", now,
                    {"error": {
                        "code": "handler_permanent", "category": "permanent_error",
                        "message": str(exc), "source": "memory-driver", "details": {},
                        "cause": None,
                    }},
                )
            self.service.submit(command)
