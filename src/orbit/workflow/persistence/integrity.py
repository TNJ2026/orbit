"""Read-only consistency audit for the workflow persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ..domain.ids import EntityId
from .database import connect_workflow_database
from .snapshots import snapshot_checksum, snapshot_record_from_row


@dataclass(frozen=True)
class IntegrityIssue:
    code: str
    message: str
    entity_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.entity_id is not None:
            result["entity_id"] = self.entity_id
        return result


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    issues: tuple[IntegrityIssue, ...]
    checked_events: int
    checked_snapshots: int
    migration_versions: tuple[int, ...] = ()
    table_counts: tuple[tuple[str, int], ...] = ()
    indexes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked_events": self.checked_events,
            "checked_snapshots": self.checked_snapshots,
            "issues": [item.to_dict() for item in self.issues],
            "migration_versions": list(self.migration_versions),
            "table_counts": dict(self.table_counts),
            "indexes": list(self.indexes),
        }


def check_database(path: Path | str, *, run_id: EntityId | None = None) -> IntegrityReport:
    connection = connect_workflow_database(path, read_only=True)
    issues: list[IntegrityIssue] = []
    predicate = "" if run_id is None else " WHERE run_id = ?"
    parameters = () if run_id is None else (str(run_id),)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            issues.append(IntegrityIssue("SQLITE_INTEGRITY", str(integrity)))
        for row in connection.execute("PRAGMA foreign_key_check").fetchall():
            issues.append(
                IntegrityIssue(
                    "FOREIGN_KEY", f"{row[0]} row {row[1]} references {row[2]}"
                )
            )
        run_sql = """
            SELECT r.run_id, r.definition_hash AS run_hash,
                   v.definition_hash AS version_hash
            FROM workflow_runs r
            LEFT JOIN workflow_versions v
              ON v.workflow_id = r.workflow_id AND v.version = r.workflow_version
        """
        run_params: tuple[object, ...] = ()
        if run_id is not None:
            run_sql += " WHERE r.run_id = ?"
            run_params = (str(run_id),)
        for row in connection.execute(run_sql, run_params).fetchall():
            if row["version_hash"] is None or row["run_hash"] != row["version_hash"]:
                issues.append(
                    IntegrityIssue(
                        "WORKFLOW_VERSION_BINDING",
                        "run definition hash does not match its workflow version",
                        row["run_id"],
                    )
                )
        events = connection.execute(
            f"SELECT * FROM run_events{predicate} ORDER BY aggregate_id, aggregate_sequence",
            parameters,
        ).fetchall()
        previous: dict[str, int] = {}
        event_ids = {row["event_id"] for row in events}
        for row in events:
            aggregate = row["aggregate_id"]
            expected = previous.get(aggregate, 0) + 1
            if row["aggregate_sequence"] != expected:
                issues.append(
                    IntegrityIssue(
                        "EVENT_SEQUENCE_GAP",
                        f"expected {expected}, got {row['aggregate_sequence']}",
                        aggregate,
                    )
                )
            previous[aggregate] = row["aggregate_sequence"]
            try:
                json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                issues.append(
                    IntegrityIssue("INVALID_EVENT_JSON", "payload is not JSON", row["event_id"])
                )
        projection_tables = (
            ("workflow_runs", "run_id"),
            ("node_runs", "node_run_id"),
            ("node_attempts", "attempt_id"),
            ("branch_tokens", "token_id"),
            ("jobs", "job_id"),
            ("job_leases", "lease_id"),
            ("durable_timers", "timer_id"),
            ("join_groups", "join_group_id"),
            ("graph_control_counters", "counter_id"),
            ("planner_attempts", "attempt_id"),
        )
        for table, column in projection_tables:
            sql = f"SELECT {column}, aggregate_version FROM {table}"
            for row in connection.execute(sql).fetchall():
                expected = previous.get(row[column], 0)
                if row["aggregate_version"] != expected:
                    issues.append(
                        IntegrityIssue(
                            "PROJECTION_VERSION_MISMATCH",
                            f"projection {row['aggregate_version']} != stream {expected}",
                            row[column],
                        )
                    )
        receipts_sql = "SELECT * FROM command_receipts"
        if run_id is not None:
            receipts_sql += " WHERE run_id = ?"
        causation_by_event = {row["event_id"]: row["causation_id"] for row in events}
        for row in connection.execute(receipts_sql, parameters).fetchall():
            try:
                results = json.loads(row["result_event_ids_json"])
            except (TypeError, json.JSONDecodeError):
                results = []
                issues.append(
                    IntegrityIssue("INVALID_RECEIPT_JSON", "result ids are not JSON", row["command_id"])
                )
            missing = [item for item in results if item not in event_ids]
            if missing:
                issues.append(
                    IntegrityIssue(
                        "RECEIPT_EVENT_MISSING",
                        f"missing event ids: {', '.join(missing)}",
                        row["command_id"],
                    )
                )
            wrong_cause = [
                item for item in results
                if item in causation_by_event and causation_by_event[item] != row["command_id"]
            ]
            if wrong_cause:
                issues.append(
                    IntegrityIssue(
                        "RECEIPT_CAUSATION_MISMATCH",
                        f"events not caused by command: {', '.join(wrong_cause)}",
                        row["command_id"],
                    )
                )
        snapshots = connection.execute(
            f"SELECT * FROM run_snapshots{predicate} ORDER BY run_id, snapshot_sequence",
            parameters,
        ).fetchall()
        run_heads = {
            row["run_id"]: row["head"]
            for row in connection.execute(
                "SELECT run_id, COALESCE(MAX(global_position), 0) AS head FROM run_events GROUP BY run_id"
            ).fetchall()
        }
        for row in snapshots:
            try:
                snapshot = snapshot_record_from_row(row)
                if snapshot.checksum != snapshot_checksum(snapshot):
                    raise ValueError("checksum mismatch")
                if snapshot.last_global_position > run_heads.get(row["run_id"], 0):
                    raise ValueError("cursor beyond event stream")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                issues.append(
                    IntegrityIssue("SNAPSHOT_CORRUPT", str(exc), row["snapshot_id"])
                )
        for row in connection.execute(
            """SELECT p.patch_id FROM plan_patches p LEFT JOIN execution_plans e
               ON e.run_id=p.run_id AND e.plan_version=p.result_plan_version
               WHERE p.status='committed' AND e.plan_id IS NULL"""
        ):
            issues.append(IntegrityIssue("PLAN_VERSION_MISSING", "committed Patch has no immutable PlanVersion", row["patch_id"]))
        for row in connection.execute(
            """SELECT a.run_id,a.reserved_microunits,a.consumed_microunits,
                      COALESCE((SELECT SUM(r.reserved_microunits) FROM budget_reservations r WHERE r.run_id=a.run_id AND r.status='active'),0) AS expected_reserved,
                      COALESCE((SELECT SUM(l.amount_microunits) FROM budget_ledger_entries l WHERE l.run_id=a.run_id AND l.kind='usage'),0) AS expected_consumed
               FROM budget_accounts a"""
        ):
            if row["reserved_microunits"] != row["expected_reserved"] or row["consumed_microunits"] != row["expected_consumed"]:
                issues.append(IntegrityIssue("BUDGET_LEDGER_MISMATCH", "Budget Account does not match ledger facts", row["run_id"]))
        for row in connection.execute(
            """SELECT g.group_id,g.item_count,COUNT(i.item_id) actual FROM foreach_groups g
               LEFT JOIN foreach_items i ON i.group_id=g.group_id GROUP BY g.group_id HAVING actual != g.item_count"""
        ):
            issues.append(IntegrityIssue("FOREACH_ITEM_COUNT", "Foreach item count does not match Group", row["group_id"]))
        for row in connection.execute(
            """SELECT s.link_id FROM subflow_links s LEFT JOIN workflow_runs r ON r.run_id=s.child_run_id
               WHERE r.run_id IS NULL"""
        ):
            issues.append(IntegrityIssue("SUBFLOW_CHILD_MISSING", "Subflow child Run is missing", row["link_id"]))
        tables = (
            "workflow_runs", "execution_plans", "node_runs", "node_attempts",
            "run_events", "run_snapshots", "branch_tokens", "command_receipts",
            "jobs", "job_leases", "durable_timers",
            "join_groups", "graph_control_counters",
            "planner_attempts", "planner_proposals",
            "plan_patches", "policy_decisions", "human_tasks",
            "budget_accounts", "budget_reservations", "budget_ledger_entries",
            "human_task_participants", "foreach_groups", "foreach_items",
            "subflow_links", "security_capabilities", "artifact_acl",
            "audit_records", "api_command_receipts",
        )
        counts = tuple(
            (table, int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
            for table in tables
        )
        migrations = tuple(
            row[0] for row in connection.execute(
                "SELECT version FROM workflow_schema_migrations ORDER BY version"
            ).fetchall()
        )
        indexes = tuple(
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        return IntegrityReport(
            not issues, tuple(issues), len(events), len(snapshots), migrations, counts, indexes
        )
    finally:
        connection.close()
