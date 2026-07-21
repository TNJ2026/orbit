"""Plan Definition, Runtime Overlay and Plan Diff — three separate answers.

The separation is the point, and it is enforced structurally rather than by
convention:

* **Definition** is what the plan *says*: nodes, handlers, edges. It is derived
  from the immutable plan record and is identical for every viewer, forever.
* **Overlay** is what this run *did*: per-node status, generation, attempts.
  It changes constantly and is meaningless without a plan version to hang on.
* **Diff** is what changed *between two plan versions*: added, removed and
  altered nodes.

Merging them is the failure mode this module exists to prevent. A single blob
of "node with a status" makes it impossible to tell a plan that was replanned
from a node that merely retried, and it invites a UI to render last run's
status against this version's graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database
from .graph_layout import graph_layout


class PlanNotFound(ValueError):
    """No such plan version for this run."""


def _plan_row(connection, run_id: str, version: int | None):
    if version is None:
        return connection.execute(
            "SELECT * FROM execution_plans WHERE run_id = ?"
            " ORDER BY plan_version DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    return connection.execute(
        "SELECT * FROM execution_plans WHERE run_id = ? AND plan_version = ?",
        (run_id, version),
    ).fetchone()


def _node_definition(node: Mapping[str, Any]) -> dict[str, Any]:
    """One node, definition fields only.

    The handler fingerprint is included deliberately: two plan versions whose
    nodes look identical but bind different handler builds are not the same
    plan, and a reader has no other way to see that.
    """

    return {
        "node_id": node["node_id"],
        "kind": node["kind"],
        "handler_name": node.get("handler_name"),
        "handler_version": node.get("handler_version"),
        "handler_manifest_fingerprint": node.get("handler_manifest_fingerprint"),
        "config": node.get("config") or {},
        "inputs": [port["id"] for port in node.get("inputs") or ()],
        "outputs": [port["id"] for port in node.get("outputs") or ()],
    }


def _definition_edges(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize linear 1.1 and static-graph 1.2 edges for read clients."""

    if plan.get("schema_version") == "1.2":
        return [
            {
                "edge_id": edge["edge_id"],
                "from": edge["source_node_id"],
                "to": edge["target_node_id"],
                "route": edge["route"],
                "priority": edge["priority"],
                "back_edge": bool(edge["back_edge"]),
                "policy_ref": edge.get("policy_ref"),
            }
            for edge in plan.get("edges") or ()
        ]
    successors = plan.get("successors") or {}
    return [
        {"from": source, "to": target}
        for source, target in sorted(successors.items())
        if target
    ]


class PlanReadModelService:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- definition -------------------------------------------------------

    def definition(
        self, run_id: EntityId, *, plan_version: int | None = None
    ) -> dict[str, Any]:
        """The plan as authored. No run state appears in this projection."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            row = _plan_row(connection, str(run_id), plan_version)
            if row is None:
                raise PlanNotFound(f"no plan for {run_id}")
            versions = [
                int(item["plan_version"])
                for item in connection.execute(
                    "SELECT plan_version FROM execution_plans WHERE run_id = ?"
                    " ORDER BY plan_version",
                    (str(run_id),),
                )
            ]

        plan = json.loads(row["canonical_plan_json"])
        edges = _definition_edges(plan)
        return {
            "run_id": str(run_id),
            "plan_id": row["plan_id"],
            "plan_version": int(row["plan_version"]),
            "plan_schema_version": row["plan_schema_version"],
            "workflow_id": row["workflow_id"],
            "workflow_version": int(row["workflow_version"]),
            "definition_hash": row["definition_hash"],
            "entry_node_id": plan.get("entry_node_id"),
            "terminal_node_id": plan.get("terminal_node_id"),
            "entry_node_ids": plan.get("entry_node_ids") or [plan.get("entry_node_id")],
            "terminal_node_ids": plan.get("terminal_node_ids") or [plan.get("terminal_node_id")],
            "nodes": [_node_definition(node) for node in plan.get("nodes") or ()],
            "edges": edges,
            "available_versions": versions,
        }

    # -- overlay ----------------------------------------------------------

    def overlay(
        self, run_id: EntityId, *, plan_version: int | None = None,
        as_of_global_position: int | None = None,
    ) -> dict[str, Any]:
        """What happened to each node, keyed by node id.

        Carries no definition fields. A caller that wants both fetches both and
        joins on `node_id`, which forces it to notice when it is looking at a
        different plan version than the one it drew.
        """

        if as_of_global_position is not None and as_of_global_position < 0:
            raise ValueError("as_of_global_position must be non-negative")
        with connect_workflow_database(self.path, read_only=True) as connection:
            plan = _plan_row(connection, str(run_id), plan_version)
            if plan is None:
                raise PlanNotFound(f"no plan for {run_id}")
            resolved = int(plan["plan_version"])
            if as_of_global_position is not None:
                return self._historical_overlay(
                    connection, run_id, resolved, as_of_global_position
                )
            rows = connection.execute(
                "SELECT node_id, node_run_id, status, generation, aggregate_version,"
                " updated_at FROM node_runs"
                " WHERE run_id = ? AND source_plan_version = ?"
                " ORDER BY node_id, generation",
                (str(run_id), resolved),
            ).fetchall()
            attempts = {
                row["node_id"]: int(row["attempts"])
                for row in connection.execute(
                    "SELECT n.node_id AS node_id, COUNT(a.attempt_id) AS attempts"
                    " FROM node_runs n LEFT JOIN node_attempts a"
                    "   ON a.node_run_id = n.node_run_id"
                    " WHERE n.run_id = ? AND n.source_plan_version = ?"
                    " GROUP BY n.node_id",
                    (str(run_id), resolved),
                )
            }

        return {
            "run_id": str(run_id),
            "plan_version": resolved,
            "as_of_global_position": None,
            "nodes": [
                {
                    "node_id": row["node_id"],
                    "node_run_id": row["node_run_id"],
                    "status": row["status"],
                    # A retry bumps the attempt count; a rework bumps the
                    # generation. Reporting both is what lets a reader tell
                    # "tried again" from "sent back".
                    "generation": int(row["generation"]),
                    "attempts": attempts.get(row["node_id"], 0),
                    "expected_version": int(row["aggregate_version"]),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ],
        }

    @staticmethod
    def _historical_overlay(connection, run_id, plan_version, position):
        """Project node/attempt facts from the event log at one stable cursor.

        Node identity metadata is immutable and may be read from ``node_runs``;
        status, version and attempt count are reconstructed solely from events
        at or before the requested global position.
        """

        head = connection.execute(
            "SELECT COALESCE(MAX(global_position), 0) FROM run_events WHERE run_id=?",
            (str(run_id),),
        ).fetchone()[0]
        if position > int(head):
            raise ValueError("as_of_global_position is beyond the Run event head")
        metadata = {
            row["node_run_id"]: row
            for row in connection.execute(
                "SELECT node_run_id,node_id,source_plan_version,generation"
                " FROM node_runs WHERE run_id=?",
                (str(run_id),),
            )
        }
        nodes: dict[str, dict[str, Any]] = {}
        attempts: dict[str, set[str]] = {}
        for event in connection.execute(
            "SELECT global_position,aggregate_id,aggregate_sequence,event_type,"
            " occurred_at,payload_json FROM run_events"
            " WHERE run_id=? AND global_position<=? ORDER BY global_position",
            (str(run_id), position),
        ):
            payload = json.loads(event["payload_json"])
            if event["event_type"] == "node_run_transitioned":
                item = metadata.get(event["aggregate_id"])
                if item is None or int(item["source_plan_version"]) != plan_version:
                    continue
                nodes[event["aggregate_id"]] = {
                    "node_id": item["node_id"],
                    "node_run_id": event["aggregate_id"],
                    "status": payload["to"],
                    "generation": int(item["generation"]),
                    "attempts": 0,
                    "expected_version": int(event["aggregate_sequence"]),
                    "updated_at": event["occurred_at"],
                }
            elif event["event_type"] == "attempt_transitioned":
                node_run_id = payload.get("node_run_id")
                if node_run_id in metadata:
                    attempts.setdefault(node_run_id, set()).add(event["aggregate_id"])
        for node_run_id, entry in nodes.items():
            entry["attempts"] = len(attempts.get(node_run_id, ()))
        return {
            "run_id": str(run_id),
            "plan_version": plan_version,
            "as_of_global_position": position,
            "event_head": int(head),
            "nodes": sorted(
                nodes.values(), key=lambda item: (item["node_id"], item["generation"])
            ),
        }

    # -- diff -------------------------------------------------------------

    def diff(
        self, run_id: EntityId, *, base_version: int, target_version: int
    ) -> dict[str, Any]:
        """What changed between two plan versions of the same run."""

        base = self.definition(run_id, plan_version=base_version)
        target = self.definition(run_id, plan_version=target_version)

        base_nodes = {node["node_id"]: node for node in base["nodes"]}
        target_nodes = {node["node_id"]: node for node in target["nodes"]}
        changed = [
            {
                "node_id": node_id,
                "fields": sorted(
                    field
                    for field in base_nodes[node_id]
                    if base_nodes[node_id][field] != target_nodes[node_id][field]
                ),
            }
            for node_id in sorted(set(base_nodes) & set(target_nodes))
            if base_nodes[node_id] != target_nodes[node_id]
        ]
        base_edges = {(edge["from"], edge["to"]) for edge in base["edges"]}
        target_edges = {(edge["from"], edge["to"]) for edge in target["edges"]}

        return {
            "run_id": str(run_id),
            "base_version": base["plan_version"],
            "target_version": target["plan_version"],
            "added_nodes": sorted(set(target_nodes) - set(base_nodes)),
            "removed_nodes": sorted(set(base_nodes) - set(target_nodes)),
            "changed_nodes": changed,
            "added_edges": [
                {"from": source, "to": target_id}
                for source, target_id in sorted(target_edges - base_edges)
            ],
            "removed_edges": [
                {"from": source, "to": target_id}
                for source, target_id in sorted(base_edges - target_edges)
            ],
            "identical": (
                base_nodes == target_nodes and base_edges == target_edges
            ),
        }

    # -- graph ------------------------------------------------------------

    def graph(
        self, run_id: EntityId, *, plan_version: int | None = None
    ) -> dict[str, Any]:
        """Static definition plus explicit current projection facts.

        The two halves stay separate in the DTO. In particular, this method
        does not infer branch or join state by replaying the timeline.
        """

        definition = self.definition(run_id, plan_version=plan_version)
        overlay = self.overlay(run_id, plan_version=definition["plan_version"])
        with connect_workflow_database(self.path, read_only=True) as connection:
            run = connection.execute(
                "SELECT aggregate_version FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            tokens = connection.execute(
                "SELECT token_id, source_node_run_id, status, aggregate_version,"
                " scope_json, created_at, updated_at FROM branch_tokens"
                " WHERE run_id = ? ORDER BY created_at, token_id",
                (str(run_id),),
            ).fetchall()
            joins = connection.execute(
                "SELECT join_group_id, node_id, generation, policy_json,"
                " participant_edge_ids_json, status, decision_json,"
                " aggregate_version, created_at, updated_at FROM join_groups"
                " WHERE run_id = ? ORDER BY join_group_id",
                (str(run_id),),
            ).fetchall()
            counters = connection.execute(
                "SELECT counter_id, policy_id, scope_key, value, limit_value,"
                " aggregate_version, updated_at FROM graph_control_counters"
                " WHERE run_id = ? ORDER BY counter_id",
                (str(run_id),),
            ).fetchall()

        plan_version_value = definition["plan_version"]
        branch_tokens = []
        for row in tokens:
            scope = json.loads(row["scope_json"])
            if scope.get("plan_version", plan_version_value) != plan_version_value:
                continue
            branch_tokens.append({
                "token_id": row["token_id"],
                "source_node_run_id": row["source_node_run_id"],
                "edge_id": scope.get("edge_id"),
                "target_node_id": scope.get("target_node_id"),
                "target_generation": scope.get("target_generation"),
                "branch_group": scope.get("branch_group"),
                "status": row["status"],
                "expected_version": int(row["aggregate_version"]),
                "updated_at": row["updated_at"],
            })
        join_groups = [
            {
                "join_group_id": row["join_group_id"],
                "node_id": row["node_id"],
                "generation": int(row["generation"]),
                "policy": json.loads(row["policy_json"]),
                "participant_edge_ids": json.loads(row["participant_edge_ids_json"]),
                "status": row["status"],
                "decision": None if row["decision_json"] is None else json.loads(row["decision_json"]),
                "expected_version": int(row["aggregate_version"]),
                "updated_at": row["updated_at"],
            }
            for row in joins
        ]
        control_counters = [
            {
                "counter_id": row["counter_id"],
                "policy_id": row["policy_id"],
                "scope_key": row["scope_key"],
                "value": int(row["value"]),
                "limit": int(row["limit_value"]),
                "expected_version": int(row["aggregate_version"]),
                "updated_at": row["updated_at"],
            }
            for row in counters
        ]
        versions = [
            int(run["aggregate_version"]),
            *(node["expected_version"] for node in overlay["nodes"]),
            *(item["expected_version"] for item in branch_tokens),
            *(item["expected_version"] for item in join_groups),
            *(item["expected_version"] for item in control_counters),
        ]
        nodes = definition["nodes"]
        edges = definition["edges"]
        return {
            "run_id": str(run_id),
            "plan_version": plan_version_value,
            "projection_version": max(versions),
            "definition": {
                "scope": "immutable",
                "plan_schema_version": definition["plan_schema_version"],
                "definition_hash": definition["definition_hash"],
                "nodes": nodes,
                "edges": edges,
                "layout": graph_layout([node["node_id"] for node in nodes], edges),
            },
            "runtime_overlay": {
                "scope": "current",
                "plan_version": overlay["plan_version"],
                "nodes": overlay["nodes"],
                "branch_tokens": branch_tokens,
                "join_groups": join_groups,
                "control_counters": control_counters,
            },
        }
