"""Paged read models backing the HTTP and MCP adapters.

These queries answer product questions ("what is waiting on me?") rather than
exposing tables. Everything is paged, and the run list resolves its waiting
reason in one pass instead of asking the diagnostics service per row.
"""

from __future__ import annotations

import json
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
RUN_QUERY_STATUSES = {
    "pending": ("created",),
    "running": ("running",),
    "waiting": ("waiting", "waiting_for_budget", "budget_exhausted"),
    "succeeded": ("succeeded",),
    "failed": ("failed",),
    "cancelled": ("cancelled",),
}
RESPONSIBILITY_FILTERS = frozenset({"human", "budget", "unknown", "recovery"})

# One query per responsibility kind. Kept as data so the set is auditable and
# a new kind cannot be added without appearing here.
RESPONSIBILITY_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "human",
        "SELECT run_id, task_id AS id, status, kind AS detail, aggregate_version"
        " FROM human_tasks WHERE run_id IN ({run_ids})"
        " AND status IN ('waiting','claimed')",
        "Human task",
    ),
    (
        "job",
        "SELECT run_id, job_id AS id, status, job_kind AS detail, aggregate_version"
        " FROM jobs WHERE run_id IN ({run_ids})"
        " AND status IN ('ready','leased','running','retry_wait')",
        "Job",
    ),
    (
        "timer",
        "SELECT run_id, timer_id AS id, status, purpose AS detail, aggregate_version"
        " FROM durable_timers WHERE run_id IN ({run_ids})"
        " AND status IN ('scheduled','leased')",
        "Timer",
    ),
    (
        "planner",
        "SELECT run_id, attempt_id AS id, status, provider_id AS detail, aggregate_version"
        " FROM planner_attempts WHERE run_id IN ({run_ids})"
        " AND status IN ('requested','running','response_received','unknown')",
        "Planner",
    ),
    (
        "foreach",
        "SELECT run_id, group_id AS id, status, failure_policy AS detail, aggregate_version"
        " FROM foreach_groups WHERE run_id IN ({run_ids})"
        " AND status IN ('pending','running')",
        "Foreach group",
    ),
    (
        "subflow",
        "SELECT parent_run_id AS run_id, link_id AS id, status,"
        " child_run_id AS detail, aggregate_version"
        " FROM subflow_links WHERE parent_run_id IN ({run_ids})"
        " AND status IN ('starting','running','unknown')",
        "Subflow",
    ),
    # A NodeRun the Runtime could not settle is waiting on a person, not on
    # itself. It carries its NodeRun because running the step again is an act
    # on the NodeRun, not on the attempt that stayed unknown. A run that has
    # already ended is nobody's responsibility: its history records that the
    # result stayed unknown, and there is nothing left to decide.
    (
        "unknown",
        "SELECT n.run_id, a.attempt_id AS id, a.status, n.node_id AS detail,"
        " a.aggregate_version, n.node_run_id,"
        " n.aggregate_version AS node_version"
        " FROM node_attempts a JOIN node_runs n ON n.node_run_id=a.node_run_id"
        " JOIN workflow_runs r ON r.run_id=n.run_id"
        " WHERE n.run_id IN ({run_ids})"
        " AND a.status='unknown_external_result'"
        " AND n.status IN ('pending','ready','running','waiting')"
        " AND r.status NOT IN ('succeeded','failed','cancelled')",
        "Unknown result",
    ),
)


class ReadModelService:
    """Read-only projections. Never mutates, never returns raw table rows."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- helpers ----------------------------------------------------------

    def _budget(self, connection, run_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT total_microunits, reserved_microunits, consumed_microunits,"
            " aggregate_version FROM budget_accounts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            # The account's own version. A budget command targets
            # budget_account:<run>, so advertising the run's version here would
            # hand the client a number that belongs to a different aggregate.
            "aggregate_version": row["aggregate_version"],
            "total": row["total_microunits"],
            "reserved": row["reserved_microunits"],
            "consumed": row["consumed_microunits"],
        }

    def _responsibility_rows(self, connection, run_id: str) -> list[dict[str, Any]]:
        return self._responsibilities_for_runs(connection, (run_id,))[run_id]

    def _responsibilities_for_runs(
        self, connection, run_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        grouped = {run_id: [] for run_id in run_ids}
        if not run_ids:
            return grouped
        placeholders = ",".join("?" for _ in run_ids)
        for kind, sql, label in RESPONSIBILITY_QUERIES:
            for row in connection.execute(sql.format(run_ids=placeholders), tuple(run_ids)):
                columns = set(row.keys())
                grouped[row["run_id"]].append({
                    "kind": kind,
                    "id": row["id"],
                    "status": row["status"],
                    "detail": row["detail"],
                    "label": f"{label}: {row['detail']}" if row["detail"] else label,
                    "aggregate_version": row["aggregate_version"],
                    # Carried only by the kinds whose command acts on something
                    # other than the row itself.
                    **({"node_run_id": row["node_run_id"]} if "node_run_id" in columns else {}),
                    **({"node_version": row["node_version"]} if "node_version" in columns else {}),
                })
        # Human first: those are the ones a person can actually act on.
        order = {kind: index for index, (kind, _, _) in enumerate(RESPONSIBILITY_QUERIES)}
        for found in grouped.values():
            found.sort(key=lambda item: (order[item["kind"]], str(item["id"])))
        return grouped

    @staticmethod
    def _node_labels(connection, run_row) -> dict[str, str]:
        """Every step's reader-facing name, from the Run's own definition.

        The label is read from the immutable Workflow version this Run is bound
        to, never from whatever the catalog serves now: a Run shows the flow it
        actually executed. Nodes without a label — older definitions, or ones a
        dynamic patch added — are simply absent, and the caller says nothing
        rather than showing an internal id.
        """

        row = connection.execute(
            "SELECT canonical_ir_json FROM workflow_versions"
            " WHERE workflow_id = ? AND version = ?",
            (run_row["workflow_id"], run_row["workflow_version"]),
        ).fetchone()
        if row is None:
            return {}
        labels = {}
        for node in json.loads(row["canonical_ir_json"]).get("nodes", ()):
            label = node.get("label")
            if isinstance(label, str) and label.strip():
                labels[str(node["id"])] = label.strip()
        return labels

    def _artifact_counts_for_runs(
        self, connection, run_ids: Sequence[str], *, actor: str | None
    ) -> dict[str, int]:
        """How many Artifacts of each Run this actor may see.

        Counted through `artifact_acl`, exactly like the Artifact catalog: a
        list that said "has files" about files the reader cannot open would be
        a permission leak dressed up as a convenience.
        """

        if not run_ids or actor is None:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        rows = connection.execute(
            "SELECT a.run_id AS run_id, COUNT(DISTINCT a.artifact_id) AS total"
            " FROM artifacts a JOIN artifact_acl acl"
            " ON acl.artifact_id = a.artifact_id"
            f" WHERE a.run_id IN ({placeholders}) AND a.status = 'committed'"
            " AND acl.subject = ? AND acl.permission = 'read'"
            " GROUP BY a.run_id",
            (*run_ids, actor),
        ).fetchall()
        return {row["run_id"]: int(row["total"]) for row in rows}

    def _budgets_for_runs(
        self, connection, run_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        rows = connection.execute(
            "SELECT run_id, total_microunits, reserved_microunits,"
            " consumed_microunits, aggregate_version FROM budget_accounts"
            f" WHERE run_id IN ({placeholders})",
            tuple(run_ids),
        ).fetchall()
        return {
            row["run_id"]: {
                "aggregate_version": row["aggregate_version"],
                "total": row["total_microunits"],
                "reserved": row["reserved_microunits"],
                "consumed": row["consumed_microunits"],
            }
            for row in rows
        }

    @staticmethod
    def _summary_responsibilities(
        rows: Sequence[Mapping[str, Any]], budget: Mapping[str, Any] | None
    ) -> list[dict[str, Any]]:
        result = [dict(item) for item in rows]
        if budget is not None and budget["consumed"] >= budget["total"] > 0:
            result.append({
                "kind": "budget", "id": "budget", "status": "blocked",
                "detail": None, "label": "Budget exhausted",
                "aggregate_version": budget["aggregate_version"],
            })
        return result

    # -- run list ---------------------------------------------------------

    def list_runs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        active_only: bool = False,
        q: str = "",
        status: str | None = None,
        responsibility: str | None = None,
        terminal_only: bool = False,
        can_act: bool = False,
        actor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        state = decode_cursor(cursor)
        q = q.strip().lower()
        if len(q) > 200:
            raise ValueError("q must be at most 200 characters")
        if status is not None and status not in RUN_QUERY_STATUSES:
            raise ValueError("status is not valid")
        if responsibility is not None and responsibility not in RESPONSIBILITY_FILTERS:
            raise ValueError("responsibility is not valid")
        query_key = {
            "q": q, "status": status, "responsibility": responsibility,
            "active": bool(active_only), "terminal": bool(terminal_only),
            "can_act": bool(can_act),
        }
        if state and state.get("query") != query_key:
            raise ValueError("cursor does not match this run query")

        clauses = ["1 = 1"]
        params: list[Any] = []
        if active_only:
            placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
            clauses.append(f"wr.status IN ({placeholders})")
            params.extend(ACTIVE_RUN_STATUSES)
        if terminal_only:
            clauses.append("wr.status IN ('succeeded','failed','cancelled')")
        if status is not None:
            statuses = RUN_QUERY_STATUSES[status]
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"wr.status IN ({placeholders})")
            params.extend(statuses)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(LOWER(COALESCE(wr.display_name, wr.run_id)) LIKE ? ESCAPE '\\'"
                " OR LOWER(wr.run_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(wr.workflow_id) LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern, pattern))
        if responsibility == "human":
            clauses.append(
                "EXISTS (SELECT 1 FROM human_tasks h WHERE h.run_id=wr.run_id"
                " AND h.status IN ('waiting','claimed'))"
            )
        elif responsibility == "budget":
            clauses.append(
                "EXISTS (SELECT 1 FROM budget_accounts b WHERE b.run_id=wr.run_id"
                " AND b.total_microunits > 0"
                " AND b.consumed_microunits >= b.total_microunits)"
            )
        elif responsibility == "unknown":
            clauses.append(
                "(EXISTS (SELECT 1 FROM node_attempts a JOIN node_runs nr"
                " ON nr.node_run_id=a.node_run_id WHERE nr.run_id=wr.run_id"
                " AND a.status='unknown_external_result')"
                " OR EXISTS (SELECT 1 FROM planner_attempts p WHERE p.run_id=wr.run_id"
                " AND p.status='unknown')"
                " OR EXISTS (SELECT 1 FROM subflow_links s WHERE s.parent_run_id=wr.run_id"
                " AND s.status='unknown'))"
            )
        elif responsibility == "recovery":
            # Recovery findings are not yet a durable responsibility projection
            # (API-3/P5). Accept the frozen filter without inventing results.
            clauses.append("0 = 1")

        action_expression = "0"
        if can_act:
            action_expression = (
                "CASE WHEN EXISTS (SELECT 1 FROM human_tasks ah"
                " WHERE ah.run_id=wr.run_id AND ah.status IN ('waiting','claimed'))"
                " OR EXISTS (SELECT 1 FROM budget_accounts ab"
                " WHERE ab.run_id=wr.run_id AND ab.total_microunits > 0"
                " AND ab.consumed_microunits >= ab.total_microunits)"
                " THEN 1 ELSE 0 END"
            )
        cursor_clause = ""
        cursor_params: list[Any] = []
        if state:
            cursor_clause = (
                "WHERE (actor_action < ? OR (actor_action = ? AND updated_at < ?)"
                " OR (actor_action = ? AND updated_at = ? AND run_id > ?))"
            )
            cursor_params = [
                int(state["actor_action"]), int(state["actor_action"]),
                str(state["updated_at"]), int(state["actor_action"]),
                str(state["updated_at"]), str(state["run_id"]),
            ]
        sql = (
            "WITH candidates AS (SELECT wr.run_id, wr.display_name, wr.goal,"
            " wr.workflow_id, wr.workflow_version, wr.status, wr.aggregate_version,"
            f" wr.created_at, wr.updated_at, {action_expression} AS actor_action"
            " FROM workflow_runs wr"
            f" WHERE {' AND '.join(clauses)}) SELECT * FROM candidates {cursor_clause}"
            " ORDER BY actor_action DESC, updated_at DESC, run_id ASC LIMIT ?"
        )
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                sql, (*params, *cursor_params, limit + 1)
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            run_ids = tuple(row["run_id"] for row in rows)
            responsibilities = self._responsibilities_for_runs(connection, run_ids)
            budgets = self._budgets_for_runs(connection, run_ids)
            artifact_counts = self._artifact_counts_for_runs(
                connection, run_ids, actor=actor
            )
            summaries = []
            for row in rows:
                run_id = row["run_id"]
                budget = budgets.get(run_id)
                projected = run_summary(
                        dict(row),
                        self._summary_responsibilities(
                            responsibilities.get(run_id, ()), budget
                        ),
                        budget,
                        can_act=can_act,
                    )
                # A Goal list says whether a row has anything to show. Asking
                # per row turned one page into fifty requests, so the count
                # travels with the page it belongs to.
                projected["artifact_count"] = artifact_counts.get(run_id, 0)
                summaries.append(projected)
        next_cursor = (
            encode_cursor({
                "query": query_key,
                "actor_action": rows[-1]["actor_action"],
                "updated_at": rows[-1]["updated_at"],
                "run_id": rows[-1]["run_id"],
            }) if has_more else None
        )
        return summaries, next_cursor

    def dashboard(self, *, can_act: bool = False) -> dict[str, Any]:
        action_expression = "0"
        if can_act:
            action_expression = (
                "SUM(CASE WHEN EXISTS (SELECT 1 FROM human_tasks h"
                " WHERE h.run_id=workflow_runs.run_id"
                " AND h.status IN ('waiting','claimed'))"
                " OR EXISTS (SELECT 1 FROM budget_accounts b"
                " WHERE b.run_id=workflow_runs.run_id AND b.total_microunits > 0"
                " AND b.consumed_microunits >= b.total_microunits)"
                " THEN 1 ELSE 0 END)"
            )
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN status IN ('created','running') THEN 1 ELSE 0 END) AS active,"
                " SUM(CASE WHEN status IN ('waiting','waiting_for_budget','budget_exhausted')"
                " THEN 1 ELSE 0 END) AS waiting,"
                " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,"
                f" {action_expression} AS attention FROM workflow_runs"
            ).fetchone()
            active_row = connection.execute(
                "SELECT run_id FROM workflow_runs WHERE run_id=correlation_id"
                " AND status IN ('created','running','waiting','waiting_for_budget',"
                " 'budget_exhausted') ORDER BY updated_at DESC,run_id LIMIT 1"
            ).fetchone()
        recent, _ = self.list_runs(limit=5, can_act=can_act)
        active_goal = (
            None if active_row is None
            else self.run_summary(EntityId.parse(active_row["run_id"]), can_act=can_act)
        )
        return {
            "counts": {
                key: int(row[key] or 0)
                for key in ("total", "active", "waiting", "failed", "succeeded")
            },
            "attention_count": int(row["attention"] or 0),
            "active_goal": active_goal,
            "recent_runs": recent,
        }

    # -- one run ----------------------------------------------------------

    def run_summary(self, run_id: EntityId, *, can_act: bool = False) -> dict[str, Any]:
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT run_id, display_name, goal, workflow_id, workflow_version,"
                " status, aggregate_version,"
                " created_at, updated_at, definition_hash, correlation_id"
                " FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"run not found: {run_id}")
            budget = self._budget(connection, str(run_id))
            summary = run_summary(
                dict(row),
                self._summary_responsibilities(
                    self._responsibility_rows(connection, str(run_id)), budget
                ),
                budget,
                can_act=can_act,
            )
            summary["definition_hash"] = row["definition_hash"]
            summary["correlation_id"] = row["correlation_id"]
            plan = connection.execute(
                "SELECT plan_version AS version, canonical_plan_json"
                " FROM execution_plans WHERE run_id = ?"
                " ORDER BY plan_version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
            summary["plan_version"] = plan["version"] if plan else None
            summary["current_step"] = None
            if row["status"] == "running" and plan is not None:
                definition = json.loads(plan["canonical_plan_json"])
                order = {
                    node_id: index
                    for index, node_id in enumerate(
                        definition.get("ordered_node_ids") or ()
                    )
                }
                running = connection.execute(
                    "SELECT n.node_run_id, n.node_id, n.updated_at,"
                    " COUNT(a.attempt_id) AS attempts"
                    " FROM node_runs n LEFT JOIN node_attempts a"
                    " ON a.node_run_id=n.node_run_id"
                    " WHERE n.run_id=? AND n.source_plan_version=?"
                    " AND n.status='running'"
                    " GROUP BY n.node_run_id, n.node_id, n.updated_at",
                    (str(run_id), int(plan["version"])),
                ).fetchall()
                if running:
                    selected = sorted(
                        running,
                        key=lambda candidate: (
                            candidate["updated_at"],
                            -order.get(candidate["node_id"], 1_000_000),
                        ),
                        reverse=True,
                    )[0]
                    plan_node = next(
                        (
                            item for item in definition.get("nodes", ())
                            if item.get("node_id") == selected["node_id"]
                        ),
                        {},
                    )
                    summary["current_step"] = {
                        "label": self._node_labels(connection, row).get(
                            selected["node_id"]
                        ),
                        "agent": plan_node.get("handler_name"),
                        "state": (
                            "retrying" if int(selected["attempts"]) > 1
                            else "running"
                        ),
                        "node_id": selected["node_id"],
                        "node_run_id": selected["node_run_id"],
                        **self._liveness(connection, selected["node_run_id"]),
                    }
        return summary

    @staticmethod
    def _liveness(connection, node_run_id: str) -> dict[str, Any]:
        """Evidence that a running step is still alive, rather than merely open.

        "Running" is a status; it survives a Handler that stopped talking, a
        worker that died and a lease nobody renews. Two facts distinguish a
        step that is working from one that is only recorded as working: when
        its process last printed something, and how long its lease is still
        good for. Both are real rows — neither is inferred from the status.

        The lease's expiry is reported rather than a derived "last renewed at":
        the renewal interval is a worker setting that can change, so
        subtracting it from the expiry would produce a timestamp that looks
        precise and is guessed. How long the lease still has left is the fact
        the database actually holds, and it answers the same question.
        """

        output = connection.execute(
            "SELECT created_at FROM attempt_output WHERE node_run_id = ?"
            " ORDER BY chunk_id DESC LIMIT 1",
            (node_run_id,),
        ).fetchone()
        lease = connection.execute(
            "SELECT l.expires_at, l.renewal_revision, l.worker_id"
            " FROM job_leases l JOIN jobs j ON j.job_id = l.job_id"
            " WHERE j.node_run_id = ? AND l.status = 'active'"
            " ORDER BY l.fencing_token DESC LIMIT 1",
            (node_run_id,),
        ).fetchone()
        return {
            "last_output_at": output["created_at"] if output else None,
            "lease_expires_at": lease["expires_at"] if lease else None,
            # How many times the lease has been renewed. A step that has been
            # running for minutes with zero renewals is a step whose supervisor
            # is not doing its job, and no other field says so.
            "lease_renewals": lease["renewal_revision"] if lease else None,
            "worker_id": lease["worker_id"] if lease else None,
        }

    def _human_task_authority(self, connection, actor: str | None) -> dict[str, bool]:
        """Which HumanTasks this actor may actually answer.

        Token authority is per task and deliberately not a scope: a participant,
        the assignee, the claimer or the creating actor. The Inbox has always
        applied it; a run page that did not would offer an Approve button the
        server refuses — which is exactly how people learn to distrust the UI.
        """

        rows = connection.execute(
            "SELECT h.task_id, h.assignee, h.claimed_by, h.actor,"
            " EXISTS(SELECT 1 FROM human_task_participants p"
            "        WHERE p.task_id=h.task_id AND p.actor=?) AS participant"
            " FROM human_tasks h WHERE h.status IN ('waiting','claimed')",
            (actor or "",),
        ).fetchall()
        return {
            row["task_id"]: bool(row["participant"])
            or actor in {row["assignee"], row["claimed_by"], row["actor"]}
            for row in rows
        }

    def responsibilities(
        self, run_id: EntityId, *, command_factory=None, actor: str | None = None
    ) -> list[dict[str, Any]]:
        """Waiting items for a run, each carrying its authorised commands."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = self._responsibility_rows(connection, str(run_id))
            budget = self._budget(connection, str(run_id))
            human_authority = self._human_task_authority(connection, actor)
            run = connection.execute(
                "SELECT status, aggregate_version,"
                " COALESCE((SELECT MAX(e.aggregate_sequence) FROM run_events e"
                " WHERE e.aggregate_id=workflow_runs.run_id), 0) AS command_version"
                " FROM workflow_runs WHERE run_id = ?",
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
                node_run_id=row.get("node_run_id"),
                allowed_commands=(
                    ()
                    if row["kind"] == "human"
                    and not human_authority.get(str(row["id"]), False)
                    else tuple(factory(
                        row, run_id=str(run_id), run_version=run["command_version"],
                    ))
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
                expected_version=budget["aggregate_version"],
                allowed_commands=tuple(
                    factory(
                        {"kind": "budget", "id": str(run_id), "status": "blocked",
                         "aggregate_version": budget["aggregate_version"],
                         "detail": None},
                        run_id=str(run_id), run_version=run["command_version"],
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
                "payload": json.loads(row["payload_json"]),
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
        errors = [
            {
                "position": row["global_position"],
                "event_id": row["event_id"],
                "aggregate_id": row["aggregate_id"],
                "type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        next_cursor = (
            encode_cursor({"position": errors[-1]["position"]})
            if len(errors) == limit else None
        )
        return errors, next_cursor

    def data(
        self, run_id: EntityId, *, cursor: str | None = None, limit: int = 50,
        actor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Inline Values and committed Artifact metadata for one Run.

        Blob keys are intentionally excluded. Reading Artifact contents is a
        separate capability; the HTTP adapter still requires its sensitive
        read scope because inline Values may contain user data.
        """

        after = str(decode_cursor(cursor).get("data_id", ""))
        with connect_workflow_database(self.path, read_only=True) as connection:
            if connection.execute(
                "SELECT 1 FROM workflow_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone() is None:
                raise ValueError(f"run not found: {run_id}")
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT value_id AS data_id, 'value' AS kind, owner_kind,
                           owner_id, port_id, schema_id, data_json, checksum,
                           size_bytes, NULL AS content_type, NULL AS visibility,
                           'committed' AS status, created_at
                    FROM "values" WHERE run_id = ? AND value_id > ?
                    UNION ALL
                    SELECT artifact_id AS data_id, 'artifact' AS kind,
                           producer_type AS owner_kind, producer_id AS owner_id,
                           output_port_id AS port_id, schema_id, NULL AS data_json,
                           checksum, size_bytes, content_type, visibility, status,
                           created_at
                    FROM artifacts a
                    WHERE run_id = ? AND artifact_id > ? AND status = 'committed'
                      AND (? IS NULL OR EXISTS (
                        SELECT 1 FROM artifact_acl acl
                        WHERE acl.artifact_id=a.artifact_id
                          AND acl.subject=? AND acl.permission='read'
                      ))
                ) ORDER BY data_id LIMIT ?
                """,
                (str(run_id), after, str(run_id), after, actor, actor, limit),
            ).fetchall()
        items = [
            {
                "data_id": row["data_id"],
                "kind": row["kind"],
                "owner_kind": row["owner_kind"],
                "owner_id": row["owner_id"],
                "port_id": row["port_id"],
                "schema_id": row["schema_id"],
                "value": None if row["data_json"] is None else json.loads(row["data_json"]),
                "checksum": row["checksum"],
                "size_bytes": row["size_bytes"],
                "content_type": row["content_type"],
                "visibility": row["visibility"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        next_cursor = (
            encode_cursor({"data_id": items[-1]["data_id"]})
            if len(items) == limit else None
        )
        return items, next_cursor

    def outcome(
        self, run_id: EntityId, *, actor: str | None, content_visible: bool,
    ) -> dict[str, Any]:
        """Project the declared Goal result without asking clients to guess."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            run = connection.execute(
                "SELECT status FROM workflow_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if run is None:
                raise ValueError(f"run not found: {run_id}")
            status = str(run["status"])
            if status not in {"succeeded", "failed", "cancelled"}:
                return {"state": "pending", "kind": None}
            if status != "succeeded":
                return {
                    "state": "missing", "kind": None,
                    "reason": "run_cancelled" if status == "cancelled" else "run_failed",
                }

            row = connection.execute(
                """
                SELECT canonical_plan_json FROM execution_plans
                WHERE run_id = ? ORDER BY plan_version DESC LIMIT 1
                """,
                (str(run_id),),
            ).fetchone()
            if row is None:
                return {"state": "unavailable_legacy", "kind": None}
            plan = json.loads(row["canonical_plan_json"])
            result = plan.get("result")
            if result is None and plan.get("schema_version") == "1.2":
                terminals = plan.get("terminal_node_ids") or []
                incoming = [
                    edge for edge in plan.get("edges", [])
                    if len(terminals) == 1
                    and edge.get("target_node_id") == terminals[0]
                    and edge.get("route") == "success"
                ]
                if len(incoming) == 1 and incoming[0].get("source_port"):
                    result = {
                        "node_id": incoming[0]["source_node_id"],
                        "output_port_id": incoming[0]["source_port"],
                    }
            if result is None:
                return {"state": "unavailable_legacy", "kind": None}

            parameters = (str(run_id), result["node_id"], result["output_port_id"])
            value = connection.execute(
                """
                SELECT v.data_json
                FROM node_runs nr
                JOIN node_attempts a ON a.node_run_id = nr.node_run_id
                JOIN "values" v
                  ON v.owner_kind = 'attempt_output' AND v.owner_id = a.attempt_id
                WHERE nr.run_id = ? AND nr.node_id = ? AND v.port_id = ?
                  AND a.status = 'succeeded'
                ORDER BY a.attempt_number DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
            if value is not None:
                payload = json.loads(value["data_json"])
                projected: dict[str, Any] = {
                    "state": "available",
                    "kind": "text" if isinstance(payload, str) else "json",
                    "content_visible": content_visible,
                }
                if content_visible:
                    projected["value"] = payload
                return projected

            artifact = connection.execute(
                """
                SELECT a.artifact_id, a.content_type, a.size_bytes
                FROM node_runs nr
                JOIN node_attempts na ON na.node_run_id = nr.node_run_id
                JOIN artifacts a ON a.producer_type = 'attempt'
                  AND a.producer_id = na.attempt_id
                WHERE nr.run_id = ? AND nr.node_id = ? AND a.output_port_id = ?
                  AND na.status = 'succeeded' AND a.status = 'committed'
                  AND (? IS NULL OR EXISTS (
                    SELECT 1 FROM artifact_acl acl
                    WHERE acl.artifact_id = a.artifact_id
                      AND acl.subject = ? AND acl.permission = 'read'
                  ))
                ORDER BY na.attempt_number DESC LIMIT 1
                """,
                (*parameters, actor, actor),
            ).fetchone()
            if artifact is not None:
                return {
                    "state": "available", "kind": "artifact",
                    "content_visible": content_visible,
                    "artifact_id": artifact["artifact_id"],
                    "content_type": artifact["content_type"],
                    "size_bytes": artifact["size_bytes"],
                }
            return {"state": "missing", "kind": None, "reason": "port_empty"}

    def lineage(
        self, run_id: EntityId, data_id: EntityId, *, actor: str | None = None
    ) -> dict[str, Any]:
        """Lineage edges for a Value or committed Artifact, scoped to its Run."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            if data_id.kind == "artifact":
                item = connection.execute(
                    "SELECT artifact_id AS data_id, output_port_id AS port_id,"
                    " producer_type AS owner_kind, producer_id AS owner_id"
                    " FROM artifacts WHERE artifact_id = ? AND run_id = ?"
                    " AND status = 'committed' AND (? IS NULL OR EXISTS ("
                    " SELECT 1 FROM artifact_acl acl WHERE acl.artifact_id=artifacts.artifact_id"
                    " AND acl.subject=? AND acl.permission='read'))",
                    (str(data_id), str(run_id), actor, actor),
                ).fetchone()
                rows = connection.execute(
                    "SELECT link_id, link_type, target_id, created_at"
                    " FROM artifact_links WHERE artifact_id = ? AND run_id = ?"
                    " ORDER BY link_id",
                    (str(data_id), str(run_id)),
                ).fetchall()
                links = [
                    {
                        "link_id": row["link_id"], "type": row["link_type"],
                        "source_id": str(data_id), "target_id": row["target_id"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            elif data_id.kind == "value":
                item = connection.execute(
                    "SELECT value_id AS data_id, port_id, owner_kind, owner_id"
                    " FROM \"values\" WHERE value_id = ? AND run_id = ?",
                    (str(data_id), str(run_id)),
                ).fetchone()
                rows = connection.execute(
                    "SELECT link_id, link_type, source_value_id, target_value_id,"
                    " created_at FROM value_links WHERE run_id = ? AND"
                    " (source_value_id = ? OR target_value_id = ?) ORDER BY link_id",
                    (str(run_id), str(data_id), str(data_id)),
                ).fetchall()
                links = [
                    {
                        "link_id": row["link_id"], "type": row["link_type"],
                        "source_id": row["source_value_id"],
                        "target_id": row["target_value_id"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            else:
                raise ValueError("lineage requires a value or artifact id")
        if item is None:
            raise ValueError(f"data not found in run: {data_id}")
        return {
            "data_id": item["data_id"], "kind": data_id.kind,
            "owner_kind": item["owner_kind"], "owner_id": item["owner_id"],
            "port_id": item["port_id"], "links": links,
        }

    # -- inbox ------------------------------------------------------------

    def inbox(
        self, *, cursor: str | None = None, limit: int = 50, command_factory=None,
        actor: str | None = None, recovery_findings: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Actor-shaped Human, Budget, Unknown and Recovery responsibilities.

        All kinds enter one stable lexical order and therefore share one
        cursor.  The caller may inject Recovery findings from the live scanner;
        keeping their final DTO construction here ensures the badge and the
        Inbox page consume exactly the same projection.
        """

        after = str(decode_cursor(cursor).get("item_id", ""))
        factory = command_factory or default_allowed_commands
        with connect_workflow_database(self.path, read_only=True) as connection:
            human_rows = connection.execute(
                "SELECT h.*, COALESCE((SELECT MAX(e.aggregate_sequence)"
                " FROM run_events e WHERE e.aggregate_id=r.run_id), 0) AS run_version,"
                " (SELECT COUNT(*) FROM human_task_participants p"
                "   WHERE p.task_id=h.task_id) AS participant_count,"
                " (SELECT COUNT(*) FROM human_task_participants p"
                "   WHERE p.task_id=h.task_id AND p.decision IS NOT NULL"
                "     AND p.decision!='withdraw') AS submitted_count,"
                " (SELECT COUNT(*) FROM human_task_participants p"
                "   WHERE p.task_id=h.task_id AND p.actor=?) AS actor_participant"
                " FROM human_tasks h JOIN workflow_runs r ON r.run_id=h.run_id"
                " WHERE h.status IN ('waiting','claimed')",
                (actor or "",),
            ).fetchall()
            budget_rows = connection.execute(
                "SELECT b.*, COALESCE((SELECT MAX(e.aggregate_sequence)"
                " FROM run_events e WHERE e.aggregate_id=r.run_id), 0) AS run_version"
                " FROM budget_accounts b JOIN workflow_runs r ON r.run_id=b.run_id"
                " WHERE b.total_microunits>0"
                " AND b.consumed_microunits>=b.total_microunits"
                " AND r.status NOT IN ('succeeded','failed','cancelled')"
            ).fetchall()
            # A node attempt carries its NodeRun with it: retrying is an act on
            # the NodeRun, and only a NodeRun the graph still holds open is
            # worth an operator's attention — a cancelled or superseded one has
            # already been answered.
            unknown_rows = connection.execute(
                "SELECT 'attempt' AS source, a.attempt_id AS id, n.run_id,"
                " a.status, a.aggregate_version, COALESCE((SELECT MAX(e.aggregate_sequence)"
                " FROM run_events e WHERE e.aggregate_id=r.run_id), 0) AS run_version,"
                " n.node_run_id, n.node_id, n.aggregate_version AS node_version"
                " FROM node_attempts a JOIN node_runs n ON n.node_run_id=a.node_run_id"
                " JOIN workflow_runs r ON r.run_id=n.run_id"
                " WHERE a.status='unknown_external_result'"
                " AND n.status IN ('pending','ready','running','waiting')"
                " AND r.status NOT IN ('succeeded','failed','cancelled')"
                " UNION ALL SELECT 'planner', p.attempt_id, p.run_id, p.status,"
                " p.aggregate_version, COALESCE((SELECT MAX(e.aggregate_sequence)"
                " FROM run_events e WHERE e.aggregate_id=r.run_id), 0),"
                " NULL, NULL, NULL"
                " FROM planner_attempts p JOIN workflow_runs r ON r.run_id=p.run_id"
                " WHERE p.status='unknown'"
                " UNION ALL SELECT 'subflow', s.link_id, s.parent_run_id, s.status,"
                " s.aggregate_version, COALESCE((SELECT MAX(e.aggregate_sequence)"
                " FROM run_events e WHERE e.aggregate_id=r.run_id), 0),"
                " NULL, NULL, NULL"
                " FROM subflow_links s JOIN workflow_runs r ON r.run_id=s.parent_run_id"
                " WHERE s.status='unknown'"
            ).fetchall()

        items: list[dict[str, Any]] = []

        def commands(record, run_id, run_version, *, permitted=True):
            if not permitted:
                return []
            return [item.to_dict() for item in factory(
                record, run_id=run_id, run_version=run_version
            )]

        for row in human_rows:
            record = {
                "kind": "human", "id": row["task_id"], "status": row["status"],
                "detail": row["kind"], "aggregate_version": row["aggregate_version"],
            }
            # Participant, assignee, claimer and creator are the exact token
            # authority used by HumanTaskService. Runtime write scope alone is
            # deliberately insufficient.
            permitted = (
                bool(row["actor_participant"])
                or actor in {row["assignee"], row["claimed_by"], row["actor"]}
            )
            allowed = commands(record, row["run_id"], row["run_version"], permitted=permitted)
            items.append({
                "item_id": f"human:{row['task_id']}", "task_id": row["task_id"],
                "kind": "human", "run_id": row["run_id"], "status": row["status"],
                "label": f"Human task: {row['kind']}", "detail": row["kind"],
                "expected_version": row["aggregate_version"],
                "deadline_at": row["deadline_at"],
                "quorum": {
                    "kind": "count" if row["quorum_kind"] == "n_of_m" else row["quorum_kind"],
                    "count": row["quorum_count"],
                    "submitted": row["submitted_count"],
                },
                "allowed_commands": allowed, "requires_actor_action": bool(allowed),
            })
        for row in budget_rows:
            record = {"kind": "budget", "id": row["run_id"], "status": "blocked",
                      "detail": None, "aggregate_version": row["aggregate_version"]}
            allowed = commands(record, row["run_id"], row["run_version"])
            items.append({
                "item_id": f"budget:{row['run_id']}", "task_id": None,
                "kind": "budget", "run_id": row["run_id"], "status": "exhausted",
                "label": "Budget exhausted", "detail": None,
                "expected_version": row["aggregate_version"], "deadline_at": None,
                "quorum": None,
                "allowed_commands": allowed, "requires_actor_action": bool(allowed),
            })
        for row in unknown_rows:
            record = {
                "kind": "unknown", "id": row["id"], "status": row["status"],
                "detail": row["source"],
                "aggregate_version": row["aggregate_version"],
                "node_run_id": row["node_run_id"], "node_id": row["node_id"],
                "node_version": row["node_version"],
            }
            allowed = commands(record, row["run_id"], row["run_version"])
            label = (
                f"Unknown result: {row['node_id']}" if row["node_id"]
                else f"Unknown result: {row['source']}"
            )
            items.append({
                "item_id": f"unknown:{row['id']}", "task_id": None,
                "kind": "unknown", "run_id": row["run_id"], "status": "unknown",
                "label": label, "detail": row["source"],
                "node_run_id": row["node_run_id"], "node_id": row["node_id"],
                "expected_version": row["aggregate_version"], "deadline_at": None,
                "quorum": None,
                "allowed_commands": allowed, "requires_actor_action": bool(allowed),
            })
        for finding in recovery_findings:
            allowed = list(finding.get("allowed_commands") or ())
            items.append({
                "item_id": f"recovery:{finding['action_id']}", "task_id": None,
                "kind": "recovery", "run_id": finding["run_id"],
                "status": "needs_attention",
                "label": finding["code"].replace("_", " ").title(),
                "detail": finding.get("details"),
                "expected_version": finding["expected_version"], "deadline_at": None,
                "quorum": None,
                "allowed_commands": allowed, "requires_actor_action": bool(allowed),
            })

        ordered = sorted(
            (item for item in items if item["item_id"] > after),
            key=lambda item: item["item_id"],
        )
        page = ordered[:limit]
        next_cursor = (
            encode_cursor({"item_id": page[-1]["item_id"]})
            if len(ordered) > limit else None
        )
        return page, next_cursor


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
        task_target = str(task_id) if str(task_id).startswith("human_task:") else f"human_task:{task_id}"
        decisions = (
            (AllowedCommand(
                "human.submit.provide_input", "Provide input", "POST",
                f"/api/v1/human-tasks/{task_id}/submit",
                task_target, version, "human-submit/1.0",
            ),)
            if row.get("detail") == "input" else (
            AllowedCommand(
                "human.submit.approve", "Approve", "POST",
                f"/api/v1/human-tasks/{task_id}/submit",
                task_target, version, "human-submit/1.0",
            ),
            AllowedCommand(
                "human.submit.reject", "Reject", "POST",
                f"/api/v1/human-tasks/{task_id}/submit",
                task_target, version, "human-submit/1.0",
            ),
        ))
        return (*decisions,
            # Retrieval surface for the one-time submission token. The kernel
            # keeps only the hash and the delivery adapter is process-local, so
            # without this command a restart would leave the task answerable by
            # no one. It is not rendered as an inbox button; the submit dialog
            # invokes it to fill its token field.
            AllowedCommand(
                "human.token", "Get token", "POST",
                f"/api/v1/human-tasks/{task_id}/token",
                task_target, version, "human-token/1.0",
            ),
            # Abandoning the run is a third, distinct answer. Rejecting an
            # approval decides the task and lets the workflow carry on down its
            # rejection path; without this a run parked on a person could only
            # be answered, never called off.
            AllowedCommand(
                "run.cancel", "Cancel run", "POST",
                f"/api/v1/runs/{run_id}/cancel",
                run_id, run_version, "run-cancel/1.0",
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
    # A node parked on an unknown external result is the one case an operator
    # can resolve: they know whether the Agent already acted, so they may run
    # the node again. The Runtime never decides that on its own.
    if kind == "unknown" and row.get("node_run_id"):
        node_run_id = str(row["node_run_id"])
        return (
            AllowedCommand(
                "unknown.accept", "Accept captured result", "POST",
                f"/api/v1/runs/{run_id}/node-runs/{node_run_id}"
                f"/accept/{row['id']}",
                node_run_id, int(row["node_version"]),
                "unknown-accept/1.0",
            ),
            AllowedCommand(
                "node.retry", "Run this step again", "POST",
                f"/api/v1/runs/{run_id}/node-runs/{node_run_id}/retry",
                node_run_id, int(row["node_version"]), "node-retry/1.0",
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
