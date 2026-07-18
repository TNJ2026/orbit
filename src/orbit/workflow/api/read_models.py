"""Paged read models backing the HTTP and MCP adapters.

These queries answer product questions ("what is waiting on me?") rather than
exposing tables. Everything is paged, and the run list resolves its waiting
reason in one pass instead of asking the diagnostics service per row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database
from .dto import (
    AllowedCommand,
    Responsibility,
    budget_summary,
    decode_cursor,
    encode_cursor,
    run_summary,
)


ACTIVE_RUN_STATUSES = ("created", "running", "waiting", "waiting_for_budget", "budget_exhausted")

# One query per responsibility kind. Kept as data so the set is auditable and
# a new kind cannot be added without appearing here.
RESPONSIBILITY_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "human",
        "SELECT task_id AS id, status, kind AS detail, aggregate_version"
        " FROM human_tasks WHERE run_id = ? AND status IN ('waiting','claimed')",
        "Human task",
    ),
    (
        "job",
        "SELECT job_id AS id, status, job_kind AS detail, aggregate_version"
        " FROM jobs WHERE run_id = ? AND status IN ('ready','leased','running','retry_wait')",
        "Job",
    ),
    (
        "timer",
        "SELECT timer_id AS id, status, purpose AS detail, aggregate_version"
        " FROM durable_timers WHERE run_id = ? AND status IN ('scheduled','leased')",
        "Timer",
    ),
    (
        "planner",
        "SELECT attempt_id AS id, status, provider_id AS detail, aggregate_version"
        " FROM planner_attempts WHERE run_id = ?"
        " AND status IN ('requested','running','response_received','unknown')",
        "Planner",
    ),
    (
        "foreach",
        "SELECT group_id AS id, status, failure_policy AS detail, aggregate_version"
        " FROM foreach_groups WHERE run_id = ? AND status IN ('pending','running')",
        "Foreach group",
    ),
    (
        "subflow",
        "SELECT link_id AS id, status, child_run_id AS detail, aggregate_version"
        " FROM subflow_links WHERE parent_run_id = ?"
        " AND status IN ('starting','running','unknown')",
        "Subflow",
    ),
)


class ReadModelService:
    """Read-only projections. Never mutates, never returns raw table rows."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- helpers ----------------------------------------------------------

    def _budget(self, connection, run_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT total_microunits, reserved_microunits, consumed_microunits"
            " FROM budget_accounts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "total": row["total_microunits"],
            "reserved": row["reserved_microunits"],
            "consumed": row["consumed_microunits"],
        }

    def _responsibility_rows(self, connection, run_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for kind, sql, label in RESPONSIBILITY_QUERIES:
            for row in connection.execute(sql, (run_id,)):
                found.append({
                    "kind": kind,
                    "id": row["id"],
                    "status": row["status"],
                    "detail": row["detail"],
                    "label": f"{label}: {row['detail']}" if row["detail"] else label,
                    "aggregate_version": row["aggregate_version"],
                })
        # Human first: those are the ones a person can actually act on.
        order = {kind: index for index, (kind, _, _) in enumerate(RESPONSIBILITY_QUERIES)}
        found.sort(key=lambda item: (order[item["kind"]], str(item["id"])))
        return found

    # -- run list ---------------------------------------------------------

    def list_runs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        active_only: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        state = decode_cursor(cursor)
        after = str(state.get("run_id", ""))
        clauses = ["run_id > ?"]
        params: list[Any] = [after]
        if active_only:
            placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(ACTIVE_RUN_STATUSES)
        sql = (
            "SELECT run_id, workflow_id, workflow_version, status, aggregate_version,"
            " created_at, updated_at FROM workflow_runs"
            f" WHERE {' AND '.join(clauses)} ORDER BY run_id LIMIT ?"
        )
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(sql, (*params, limit)).fetchall()
            summaries = []
            for row in rows:
                run_id = row["run_id"]
                summaries.append(
                    run_summary(
                        dict(row),
                        self._responsibility_rows(connection, run_id),
                        self._budget(connection, run_id),
                    )
                )
        next_cursor = (
            encode_cursor({"run_id": rows[-1]["run_id"]}) if len(rows) == limit else None
        )
        return summaries, next_cursor

    # -- one run ----------------------------------------------------------

    def run_summary(self, run_id: EntityId) -> dict[str, Any]:
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT run_id, workflow_id, workflow_version, status, aggregate_version,"
                " created_at, updated_at, definition_hash, correlation_id"
                " FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"run not found: {run_id}")
            summary = run_summary(
                dict(row),
                self._responsibility_rows(connection, str(run_id)),
                self._budget(connection, str(run_id)),
            )
            summary["definition_hash"] = row["definition_hash"]
            summary["correlation_id"] = row["correlation_id"]
            plan = connection.execute(
                "SELECT MAX(plan_version) AS version FROM execution_plans WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            summary["plan_version"] = plan["version"] if plan else None
        return summary

    def responsibilities(
        self, run_id: EntityId, *, command_factory=None
    ) -> list[dict[str, Any]]:
        """Waiting items for a run, each carrying its authorised commands."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = self._responsibility_rows(connection, str(run_id))
            budget = self._budget(connection, str(run_id))
            run = connection.execute(
                "SELECT status, aggregate_version FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run is None:
                raise ValueError(f"run not found: {run_id}")

        factory = command_factory or default_allowed_commands
        result = []
        for row in rows:
            # Entity ids already carry their kind ("job:abc"), so only prefix
            # the ones that do not.
            identifier = str(row["id"])
            responsibility = Responsibility(
                responsibility_id=(
                    identifier if ":" in identifier else f"{row['kind']}:{identifier}"
                ),
                kind=row["kind"],
                label=row["label"],
                status=row["status"],
                detail=row["detail"],
                expected_version=row["aggregate_version"],
                allowed_commands=tuple(
                    factory(row, run_id=str(run_id), run_version=run["aggregate_version"])
                ),
            )
            result.append(responsibility.to_dict())

        if budget is not None and budget["consumed"] >= budget["total"] > 0:
            exhausted = Responsibility(
                responsibility_id=f"budget:{run_id}",
                kind="budget",
                label="Budget exhausted",
                status="blocked",
                detail=None,
                expected_version=run["aggregate_version"],
                allowed_commands=tuple(
                    factory(
                        {"kind": "budget", "id": str(run_id), "status": "blocked",
                         "aggregate_version": run["aggregate_version"], "detail": None},
                        run_id=str(run_id), run_version=run["aggregate_version"],
                    )
                ),
            )
            result.append(exhausted.to_dict())
        return result

    def timeline(
        self, run_id: EntityId, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        after = int(decode_cursor(cursor).get("position", 0))
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT global_position, event_id, aggregate_id, event_type,"
                " aggregate_sequence, correlation_id, causation_id, occurred_at,"
                " payload_json FROM run_events"
                " WHERE run_id = ? AND global_position > ?"
                " ORDER BY global_position LIMIT ?",
                (str(run_id), after, limit),
            ).fetchall()
        import json as _json

        events = [
            {
                "position": row["global_position"],
                "event_id": row["event_id"],
                "aggregate_id": row["aggregate_id"],
                "type": row["event_type"],
                "sequence": row["aggregate_sequence"],
                "correlation_id": row["correlation_id"],
                "causation_id": row["causation_id"],
                "occurred_at": row["occurred_at"],
                "payload": _json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        next_cursor = (
            encode_cursor({"position": events[-1]["position"]})
            if len(events) == limit else None
        )
        return events, next_cursor

    def errors(
        self, run_id: EntityId, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Complete error projection — not a filter over one timeline page."""

        after = int(decode_cursor(cursor).get("position", 0))
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT global_position, event_id, aggregate_id, event_type,"
                " occurred_at, payload_json FROM run_events"
                " WHERE run_id = ? AND global_position > ?"
                "   AND (event_type LIKE '%failed%' OR event_type LIKE '%rejected%'"
                "        OR event_type LIKE '%unknown%')"
                " ORDER BY global_position LIMIT ?",
                (str(run_id), after, limit),
            ).fetchall()
        import json as _json

        errors = [
            {
                "position": row["global_position"],
                "event_id": row["event_id"],
                "aggregate_id": row["aggregate_id"],
                "type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": _json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        next_cursor = (
            encode_cursor({"position": errors[-1]["position"]})
            if len(errors) == limit else None
        )
        return errors, next_cursor

    # -- inbox ------------------------------------------------------------

    def inbox(
        self, *, cursor: str | None = None, limit: int = 50, command_factory=None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Everything waiting on a person, across every run."""

        after = str(decode_cursor(cursor).get("task_id", ""))
        factory = command_factory or default_allowed_commands
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT task_id, run_id, kind, status, aggregate_version"
                " FROM human_tasks WHERE status IN ('waiting','claimed') AND task_id > ?"
                " ORDER BY task_id LIMIT ?",
                (after, limit),
            ).fetchall()
            items = []
            for row in rows:
                record = {
                    "kind": "human", "id": row["task_id"], "status": row["status"],
                    "detail": row["kind"], "aggregate_version": row["aggregate_version"],
                }
                items.append({
                    "item_id": f"human:{row['task_id']}",
                    # The bare id as well: it is what the caller puts in the
                    # /human-tasks/{task_id}/... path, and making the UI strip
                    # a prefix off item_id would make that coupling implicit.
                    "task_id": row["task_id"],
                    "kind": "human",
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "label": f"Human task: {row['kind']}",
                    "expected_version": row["aggregate_version"],
                    "allowed_commands": [
                        command.to_dict()
                        for command in factory(
                            record, run_id=row["run_id"],
                            run_version=row["aggregate_version"],
                        )
                    ],
                })
        next_cursor = (
            encode_cursor({"task_id": rows[-1]["task_id"]}) if len(rows) == limit else None
        )
        return items, next_cursor


def default_allowed_commands(
    row: Mapping[str, Any], *, run_id: str, run_version: int
) -> Sequence[AllowedCommand]:
    """Commands the server authorises for one responsibility.

    Centralised here rather than in the client: a UI that derives buttons from
    a status is a UI that can offer an action the server will refuse.
    """

    kind = row["kind"]
    version = int(row["aggregate_version"])
    if kind == "human":
        task_id = row["id"]
        return (
            AllowedCommand(
                "human.submit.approve", "Approve", "POST",
                f"/api/v1/human-tasks/{task_id}/submit",
                f"human_task:{task_id}", version, "human-submit/1.0",
            ),
            AllowedCommand(
                "human.submit.reject", "Reject", "POST",
                f"/api/v1/human-tasks/{task_id}/submit",
                f"human_task:{task_id}", version, "human-submit/1.0",
            ),
        )
    if kind == "budget":
        return (
            AllowedCommand(
                "budget.add", "Add budget", "POST",
                f"/api/v1/runs/{run_id}/budget",
                f"budget_account:{run_id}", version, "budget-add/1.0",
            ),
            AllowedCommand(
                "run.cancel", "Cancel run", "POST",
                f"/api/v1/runs/{run_id}/cancel",
                run_id, run_version, "run-cancel/1.0",
            ),
        )
    # Jobs, timers, planner attempts, foreach groups and subflows progress on
    # their own; the only human lever is cancelling the run.
    return (
        AllowedCommand(
            "run.cancel", "Cancel run", "POST",
            f"/api/v1/runs/{run_id}/cancel", run_id, run_version, "run-cancel/1.0",
        ),
    )
