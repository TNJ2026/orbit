"""Bounded P8 projections for dynamic Runtime facts.

Raw Planner responses, lease credentials and Foreach payloads never leak from
summary endpoints. Item values live behind the API's sensitive-data scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database
from .dto import CursorError, decode_cursor, encode_cursor


class DynamicReadModelService:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @staticmethod
    def _cursor(raw: str | None, *, kind: str, run_id: str) -> dict[str, Any]:
        value = decode_cursor(raw)
        if value and (value.get("kind") != kind or value.get("run_id") != run_id):
            raise CursorError("cursor does not match this dynamic projection")
        return value

    @staticmethod
    def _json(value):
        return None if value is None else json.loads(value)

    @staticmethod
    def _action_summary(value):
        action = json.loads(value)
        arguments = action.get("arguments", {})
        summary = {
            "kind": action.get("kind"),
            "argument_keys": sorted(arguments),
        }
        if action.get("kind") == "dispatch" and isinstance(arguments.get("handler"), str):
            summary["handler"] = arguments["handler"]
        return summary

    @staticmethod
    def _require_run(connection, run_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"run not found: {run_id}")

    def planner_decisions(self, run_id: EntityId, *, cursor=None, limit=50):
        run = str(run_id)
        state = self._cursor(cursor, kind="planner", run_id=run)
        clause, parameters = "", [run]
        if state:
            clause = "AND (a.attempt_number>? OR (a.attempt_number=? AND a.attempt_id>?))"
            parameters.extend((state["number"], state["number"], state["id"]))
        with connect_workflow_database(self.path, read_only=True) as connection:
            self._require_run(connection, run)
            rows = connection.execute(
                """SELECT a.attempt_id,a.attempt_number,a.status,a.model_id,a.provider_id,
                          a.usage_json,a.error_json,a.created_at,a.updated_at,
                          p.proposal_id,p.status AS proposal_status,p.base_plan_version,
                          p.action_json,p.reason,p.validation_json,
                          x.patch_id,x.status AS patch_status,x.result_plan_version,
                          x.reason AS patch_reason,d.allowed,d.requires_approval,
                          d.rule_set_version,d.reasons_json
                     FROM planner_attempts a
                LEFT JOIN planner_proposals p ON p.attempt_id=a.attempt_id
                LEFT JOIN plan_patches x ON x.proposal_id=p.proposal_id
                LEFT JOIN policy_decisions d ON d.patch_id=x.patch_id
                    AND d.created_at=(SELECT MAX(d2.created_at) FROM policy_decisions d2
                                      WHERE d2.patch_id=x.patch_id)
                    WHERE a.run_id=? """ + clause +
                " ORDER BY a.attempt_number,a.attempt_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{
            "attempt_id": row["attempt_id"],
            "attempt_number": int(row["attempt_number"]),
            "status": row["status"],
            "model_id": row["model_id"],
            "provider_id": row["provider_id"],
            "usage": self._json(row["usage_json"]),
            "error": self._json(row["error_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "proposal": None if row["proposal_id"] is None else {
                "proposal_id": row["proposal_id"],
                "status": row["proposal_status"],
                "base_plan_version": int(row["base_plan_version"]),
                "action": self._action_summary(row["action_json"]),
                "reason": row["reason"],
                "validation": self._json(row["validation_json"]),
            },
            "patch": None if row["patch_id"] is None else {
                "patch_id": row["patch_id"],
                "status": row["patch_status"],
                "result_plan_version": row["result_plan_version"],
                "reason": row["patch_reason"],
            },
            "policy": None if row["allowed"] is None else {
                "allowed": bool(row["allowed"]),
                "requires_approval": bool(row["requires_approval"]),
                "rule_set_version": row["rule_set_version"],
                "reasons": self._json(row["reasons_json"]),
            },
        } for row in rows]
        next_cursor = None
        if more:
            last = rows[-1]
            next_cursor = encode_cursor({
                "kind": "planner", "run_id": run,
                "number": int(last["attempt_number"]), "id": last["attempt_id"],
            })
        return items, next_cursor

    def foreach_groups(self, run_id: EntityId, *, cursor=None, limit=50):
        run = str(run_id)
        state = self._cursor(cursor, kind="foreach-groups", run_id=run)
        clause, parameters = "", [run]
        if state:
            clause = "AND g.group_id>?"
            parameters.append(state["id"])
        with connect_workflow_database(self.path, read_only=True) as connection:
            self._require_run(connection, run)
            rows = connection.execute(
                """SELECT g.*,
                          SUM(CASE WHEN i.status='succeeded' THEN 1 ELSE 0 END) succeeded,
                          SUM(CASE WHEN i.status IN ('failed','unknown') THEN 1 ELSE 0 END) failed,
                          SUM(CASE WHEN i.status='running' THEN 1 ELSE 0 END) running,
                          SUM(CASE WHEN i.status IN ('pending','ready') THEN 1 ELSE 0 END) pending
                     FROM foreach_groups g LEFT JOIN foreach_items i ON i.group_id=g.group_id
                    WHERE g.run_id=? """ + clause +
                " GROUP BY g.group_id ORDER BY g.group_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{
            "group_id": row["group_id"], "node_run_id": row["node_run_id"],
            "plan_version": int(row["plan_version"]), "status": row["status"],
            "failure_policy": row["failure_policy"],
            "concurrency_limit": int(row["concurrency_limit"]),
            "item_count": int(row["item_count"]),
            "counts": {key: int(row[key] or 0) for key in ("pending", "running", "succeeded", "failed")},
            "aggregate_checksum": row["aggregate_checksum"],
            "expected_version": int(row["aggregate_version"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        } for row in rows]
        next_cursor = None if not more else encode_cursor({
            "kind": "foreach-groups", "run_id": run, "id": rows[-1]["group_id"],
        })
        return items, next_cursor

    def foreach_items(self, run_id: EntityId, group_id: EntityId, *, cursor=None, limit=50):
        run, group = str(run_id), str(group_id)
        state = self._cursor(cursor, kind=f"foreach-items:{group}", run_id=run)
        clause, parameters = "", [run, group]
        if state:
            clause = "AND (item_index>? OR (item_index=? AND item_id>?))"
            parameters.extend((state["index"], state["index"], state["id"]))
        with connect_workflow_database(self.path, read_only=True) as connection:
            self._require_run(connection, run)
            owner = connection.execute(
                "SELECT 1 FROM foreach_groups WHERE group_id=? AND run_id=?", (group, run)
            ).fetchone()
            if owner is None:
                raise ValueError("Foreach group not found")
            rows = connection.execute(
                "SELECT * FROM foreach_items WHERE run_id=? AND group_id=? " + clause +
                " ORDER BY item_index,item_id LIMIT ?", (*parameters, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{
            "item_id": row["item_id"], "item_key": row["item_key"],
            "item_index": int(row["item_index"]), "status": row["status"],
            "child_run_id": row["child_run_id"],
            "input": self._json(row["input_json"]),
            "output": self._json(row["output_json"]),
            "error": self._json(row["error_json"]),
            "retry_count": int(row["retry_count"]),
            "expected_version": int(row["aggregate_version"]),
            "updated_at": row["updated_at"],
        } for row in rows]
        next_cursor = None
        if more:
            last = rows[-1]
            next_cursor = encode_cursor({
                "kind": f"foreach-items:{group}", "run_id": run,
                "index": int(last["item_index"]), "id": last["item_id"],
            })
        return items, next_cursor

    def subflows(self, run_id: EntityId, *, cursor=None, limit=50):
        run = str(run_id)
        state = self._cursor(cursor, kind="subflows", run_id=run)
        clause, parameters = "", [run, run]
        if state:
            clause = "AND link_id>?"
            parameters.append(state["id"])
        with connect_workflow_database(self.path, read_only=True) as connection:
            self._require_run(connection, run)
            rows = connection.execute(
                "SELECT * FROM subflow_links WHERE (parent_run_id=? OR child_run_id=?) "
                + clause + " ORDER BY link_id LIMIT ?", (*parameters, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{
            "link_id": row["link_id"], "parent_run_id": row["parent_run_id"],
            "child_run_id": row["child_run_id"],
            "parent_node_run_id": row["parent_node_run_id"],
            "workflow_id": row["workflow_id"],
            "workflow_version": int(row["workflow_version"]),
            "status": row["status"], "recursion_depth": int(row["recursion_depth"]),
            "propagation": self._json(row["propagation_policy_json"]),
            "expected_version": int(row["aggregate_version"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        } for row in rows]
        next_cursor = None if not more else encode_cursor({
            "kind": "subflows", "run_id": run, "id": rows[-1]["link_id"],
        })
        return items, next_cursor
