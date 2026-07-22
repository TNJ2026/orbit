"""Published Workflow catalog projections for the Runtime UI.

The immutable WorkflowIR is the authority. In particular, run ingress is the
input shape of every entry node: that is the exact set the Runtime kernel
validates when it schedules a new run. The UI must not infer it from a handler
catalog or from a previous run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..domain.serialization import to_primitive
from ..persistence.database import connect_workflow_database
from .graph_layout import graph_layout


class WorkflowCatalogReadModelService:
    def __init__(self, path: Path | str, schema_catalog) -> None:
        self.path = Path(path)
        self.schemas = schema_catalog

    @staticmethod
    def _summary(ir: Mapping[str, Any]) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for node in ir.get("nodes") or ():
            kind = str(node["kind"])
            kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "node_count": len(ir.get("nodes") or ()),
            "edge_count": len(ir.get("edges") or ()),
            "entry": list(ir.get("entry") or ()),
            "terminals": list(ir.get("terminals") or ()),
            "node_kinds": kinds,
        }

    def _inputs(self, ir: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
        by_id = {node["id"]: node for node in ir.get("nodes") or ()}
        shapes = [
            list(by_id[node_id].get("inputs") or ())
            for node_id in ir.get("entry") or ()
            if node_id in by_id
        ]
        # The kernel sends one input object to every entry node. Different
        # entry shapes cannot be honestly represented as one generated form;
        # retain the raw JSON escape hatch and let the server validate it.
        if not shapes or any(shape != shapes[0] for shape in shapes[1:]):
            return [], "json"
        ports = []
        structured = True
        for port in shapes[0]:
            schema = self.schemas.get(port["schema_id"])
            policy = port.get("data_policy") or {}
            if schema is None or policy.get("transport", "inline") != "inline":
                structured = False
            ports.append({
                "id": port["id"],
                "schema_id": port["schema_id"],
                "required": bool(port.get("required", True)),
                "has_default": bool(port.get("has_default", False)),
                "default": port.get("default"),
                "description": port.get("description") or "",
                "schema": None if schema is None else to_primitive(schema),
                "transport": policy.get("transport", "inline"),
            })
        return ports, "structured" if structured else "json"

    @staticmethod
    def _goal_binding(
        ir: Mapping[str, Any], inputs: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        """Project the conventional Agent ingress as an explicit UI fact.

        The browser must not guess from a port called ``prompt``.  Orbit owns
        the built-in ``agent.*`` handler contract, so the catalog can safely
        advertise when a single object input accepts the Run goal envelope.
        """

        entries = list(ir.get("entry") or ())
        if len(entries) != 1 or len(inputs) != 1:
            return None
        node = next(
            (item for item in ir.get("nodes") or () if item.get("id") == entries[0]),
            None,
        )
        handler = None if node is None else node.get("handler")
        port = inputs[0]
        schema = port.get("schema") or {}
        if (
            not isinstance(handler, Mapping)
            or not str(handler.get("name", "")).startswith("agent.")
            or port.get("id") != "prompt"
            or schema.get("type") != "object"
            or port.get("transport") != "inline"
        ):
            return None
        return {
            "source": "run.goal",
            "node_id": entries[0],
            "input_id": "prompt",
            "property": "goal",
            "value_shape": "object",
        }

    @staticmethod
    def _graph(ir: Mapping[str, Any]) -> dict[str, Any]:
        """The definition as a drawable graph, in the plan's vocabulary.

        The IR names an edge's ends ``source_node``/``target_node``; a run's
        plan calls them ``from``/``to``. One renderer draws both pictures, so
        the catalog speaks the plan's dialect rather than making the browser
        translate.
        """

        nodes = [
            {
                "node_id": node["id"],
                "kind": node["kind"],
                "handler_name": (node.get("handler") or {}).get("name"),
                "handler_version": (node.get("handler") or {}).get("version"),
            }
            for node in ir.get("nodes") or ()
        ]
        edges = [
            {
                "edge_id": edge["id"],
                "from": edge["source_node"],
                "to": edge["target_node"],
                "route": edge.get("route", "success"),
                "priority": edge.get("priority", 0),
                "back_edge": bool(edge.get("back_edge", False)),
            }
            for edge in ir.get("edges") or ()
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "entry": list(ir.get("entry") or ()),
            "terminals": list(ir.get("terminals") or ()),
            "layout": graph_layout([node["node_id"] for node in nodes], edges),
        }

    def _entry(self, row, *, include_definition: bool) -> dict[str, Any]:
        ir = json.loads(row["canonical_ir_json"])
        inputs, input_mode = self._inputs(ir)
        goal_binding = self._goal_binding(ir, inputs)
        item = {
            "workflow_id": row["workflow_id"],
            "name": ir["name"],
            "description": ir.get("description") or "",
            "labels": dict(ir.get("labels") or {}),
            "latest_version": int(row["version"]),
            "definition_hash": row["definition_hash"],
            "created_at": row["created_at"],
            "input_mode": input_mode,
            "inputs": inputs,
            "goal_binding": goal_binding,
            "summary": self._summary(ir),
        }
        if include_definition:
            item["definition"] = ir
            item["graph"] = self._graph(ir)
        return item

    def list(self) -> list[dict[str, Any]]:
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                """SELECT current.* FROM workflow_versions current
                   WHERE version = (
                     SELECT MAX(version) FROM workflow_versions
                     WHERE workflow_id = current.workflow_id
                   )
                   ORDER BY workflow_id"""
            ).fetchall()
            # How recently a definition was actually used is a fact about runs,
            # not about the definition. A catalog of dozens is ordered by it far
            # more often than by workflow_id, so the projection carries it.
            usage = {
                row["workflow_id"]: row
                for row in connection.execute(
                    "SELECT workflow_id, MAX(created_at) AS last_run_at,"
                    " COUNT(*) AS run_count FROM workflow_runs GROUP BY workflow_id"
                ).fetchall()
            }
        entries = []
        for row in rows:
            item = self._entry(row, include_definition=False)
            used = usage.get(row["workflow_id"])
            item["last_run_at"] = None if used is None else used["last_run_at"]
            item["run_count"] = 0 if used is None else int(used["run_count"])
            entries.append(item)
        return entries

    def detail(self, workflow_id: str, version: int | None = None) -> dict[str, Any]:
        with connect_workflow_database(self.path, read_only=True) as connection:
            version_rows = connection.execute(
                "SELECT version, definition_hash, created_at, created_by, source_text "
                "FROM workflow_versions WHERE workflow_id = ? ORDER BY version DESC",
                (workflow_id,),
            ).fetchall()
            if version is None:
                row = connection.execute(
                    "SELECT * FROM workflow_versions WHERE workflow_id = ?"
                    " ORDER BY version DESC LIMIT 1",
                    (workflow_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM workflow_versions"
                    " WHERE workflow_id = ? AND version = ?",
                    (workflow_id, version),
                ).fetchone()
        if row is None:
            raise ValueError(f"workflow version not found: {workflow_id}")
        item = self._entry(row, include_definition=True)
        item["selected_version"] = int(row["version"])
        item["latest_version"] = int(version_rows[0]["version"])
        item["versions"] = [
            {
                "version": int(value["version"]),
                "definition_hash": value["definition_hash"],
                "created_at": value["created_at"],
                "created_by": value["created_by"],
                "source_available": value["source_text"] is not None,
            }
            for value in version_rows
        ]
        # The author-facing source, distinct from canonical IR (editor plan
        # §7). Early versions published without source degrade to
        # source_available=false — viewable and runnable, never "editable".
        item["source"] = row["source_text"]
        item["source_format"] = row["source_format"]
        item["source_available"] = row["source_text"] is not None
        return item
