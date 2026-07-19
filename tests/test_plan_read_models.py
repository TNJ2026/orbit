"""M4.B: Definition, Overlay and Diff stay three separate answers.

The regression these guard against is a single "node with a status" blob:
once definition and run state share a shape, nothing stops a client from
drawing one plan version's graph with another version's statuses.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.api.plan_read_models import PlanNotFound, PlanReadModelService
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


RUN = "run:demo"
NOW = "2026-07-18T00:00:00Z"


def plan_json(version, *, nodes, successors, handler_fingerprint="sha256:aaa"):
    return json.dumps(
        {
            "schema_version": "1.1",
            "plan_id": f"plan:v{version}",
            "plan_version": version,
            "run_id": RUN,
            "workflow_id": "workflow:demo",
            "workflow_version": 1,
            "entry_node_id": nodes[0]["node_id"],
            "terminal_node_id": nodes[-1]["node_id"],
            "ordered_node_ids": [node["node_id"] for node in nodes],
            "successors": successors,
            "nodes": [
                {
                    "node_id": node["node_id"],
                    "kind": node.get("kind", "action"),
                    "handler_name": node.get("handler_name", "transform"),
                    "handler_version": node.get("handler_version", "1.0.0"),
                    "handler_manifest_fingerprint": node.get(
                        "fingerprint", handler_fingerprint
                    ),
                    "config": node.get("config", {}),
                    "inputs": [{"id": "value"}],
                    "outputs": [{"id": "value"}],
                }
                for node in nodes
            ],
        },
        sort_keys=True,
    )


class PlanReadModelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()
        self.service = PlanReadModelService(self.db)
        self.run_id = EntityId.parse(RUN)
        self.insert_run()

    def insert_run(self) -> None:
        """Plans and node runs are foreign-keyed to a run, and a run to a
        published workflow version. Build the chain from the bottom."""

        with connect_workflow_database(self.db) as connection:
            connection.execute(
                "INSERT INTO workflow_definitions(workflow_id, name, created_at,"
                " created_by) VALUES (?,?,?,?)",
                ("workflow:demo", "Demo", NOW, "test"),
            )
            connection.execute(
                "INSERT INTO workflow_versions(workflow_id, version, definition_hash,"
                " dsl_version, ir_version, compiler_version, canonical_ir_json,"
                " source_format, source_text, catalog_fingerprint, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "workflow:demo", 1, "sha256:workflow", "1.0", "1.1", "1.0",
                    "{}", "json", None, "sha256:catalog", NOW, "test",
                ),
            )
            columns = [row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")]
            defaults = {
                "run_id": RUN, "workflow_id": "workflow:demo", "workflow_version": 1,
                "status": "running", "aggregate_version": 1,
                "created_at": NOW, "updated_at": NOW, "plan_version": 1,
                "definition_hash": "sha256:workflow", "correlation_id": RUN,
            }
            values = [defaults.get(name) for name in columns]
            connection.execute(
                f"INSERT INTO workflow_runs({', '.join(columns)})"
                f" VALUES ({', '.join('?' * len(columns))})",
                values,
            )
            connection.commit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def insert_plan(self, version, *, nodes, successors, **kwargs) -> None:
        with connect_workflow_database(self.db) as connection:
            connection.execute(
                "INSERT INTO execution_plans(plan_id, run_id, plan_version,"
                " workflow_id, workflow_version, plan_schema_version,"
                " canonical_plan_json, definition_hash, created_event_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"plan:v{version}", RUN, version, "workflow:demo", 1, "1.1",
                    plan_json(version, nodes=nodes, successors=successors, **kwargs),
                    f"sha256:plan{version}", f"event:{version}", NOW,
                ),
            )
            connection.commit()

    def insert_node_run(self, node_id, *, plan_version, status, generation=1, attempts=0):
        # Ids include the plan version: a replan produces a new node run for
        # the same node id, not a mutation of the old one.
        node_run_id = f"node_run:{node_id}:{plan_version}:{generation}"
        with connect_workflow_database(self.db) as connection:
            connection.execute(
                "INSERT INTO node_runs(node_run_id, run_id, node_id,"
                " source_plan_version, status, aggregate_version, created_at,"
                " updated_at, generation, activation_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    node_run_id, RUN, node_id, plan_version, status, 3, NOW, NOW,
                    generation, f"{node_id}:{generation}",
                ),
            )
            for index in range(attempts):
                connection.execute(
                    "INSERT INTO node_attempts(attempt_id, node_run_id, attempt_number,"
                    " status, aggregate_version, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        f"attempt:{node_run_id}:{index}", node_run_id, index + 1,
                        "succeeded", 1, NOW, NOW,
                    ),
                )
            connection.commit()

    def two_node_plan(self, version=1, second="transform"):
        self.insert_plan(
            version,
            nodes=[{"node_id": "collect"}, {"node_id": second}],
            successors={"collect": second, second: None},
        )


class DefinitionTests(PlanReadModelTestCase):
    def test_definition_describes_the_plan_as_authored(self) -> None:
        self.two_node_plan()
        definition = self.service.definition(self.run_id)
        self.assertEqual(1, definition["plan_version"])
        self.assertEqual(["collect", "transform"], [n["node_id"] for n in definition["nodes"]])
        self.assertEqual([{"from": "collect", "to": "transform"}], definition["edges"])
        self.assertEqual("collect", definition["entry_node_id"])

    def test_definition_carries_no_run_state(self) -> None:
        """The decisive assertion for the whole module."""

        self.two_node_plan()
        self.insert_node_run("collect", plan_version=1, status="succeeded")
        definition = self.service.definition(self.run_id)
        serialised = json.dumps(definition)
        for forbidden in ("status", "succeeded", "generation", "attempts", "node_run_id"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialised)

    def test_the_handler_fingerprint_is_part_of_the_definition(self) -> None:
        """Two plans with the same node names but different handler builds are
        not the same plan, and nothing else in the payload would show it."""

        self.two_node_plan()
        node = self.service.definition(self.run_id)["nodes"][0]
        self.assertEqual("sha256:aaa", node["handler_manifest_fingerprint"])

    def test_the_latest_version_is_the_default(self) -> None:
        self.two_node_plan(version=1)
        self.two_node_plan(version=2, second="review")
        self.assertEqual(2, self.service.definition(self.run_id)["plan_version"])
        self.assertEqual(
            1, self.service.definition(self.run_id, plan_version=1)["plan_version"]
        )

    def test_available_versions_are_advertised(self) -> None:
        self.two_node_plan(version=1)
        self.two_node_plan(version=2)
        self.assertEqual([1, 2], self.service.definition(self.run_id)["available_versions"])

    def test_an_unknown_run_is_reported_not_guessed(self) -> None:
        with self.assertRaises(PlanNotFound):
            self.service.definition(EntityId.parse("run:missing"))

    def test_an_unknown_version_is_reported(self) -> None:
        self.two_node_plan()
        with self.assertRaises(PlanNotFound):
            self.service.definition(self.run_id, plan_version=9)


class OverlayTests(PlanReadModelTestCase):
    def test_overlay_reports_status_per_node(self) -> None:
        self.two_node_plan()
        self.insert_node_run("collect", plan_version=1, status="succeeded", attempts=1)
        self.insert_node_run("transform", plan_version=1, status="running", attempts=2)
        overlay = self.service.overlay(self.run_id)
        by_id = {node["node_id"]: node for node in overlay["nodes"]}
        self.assertEqual("succeeded", by_id["collect"]["status"])
        self.assertEqual(2, by_id["transform"]["attempts"])

    def test_overlay_carries_no_definition_fields(self) -> None:
        self.two_node_plan()
        self.insert_node_run("collect", plan_version=1, status="succeeded")
        node = self.service.overlay(self.run_id)["nodes"][0]
        for forbidden in ("handler_name", "kind", "config", "inputs", "outputs"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, node)

    def test_overlay_is_stamped_with_the_plan_version_it_belongs_to(self) -> None:
        """Without this, a client cannot tell it is about to paint stale
        statuses onto a replanned graph."""

        self.two_node_plan(version=1)
        self.insert_node_run("collect", plan_version=1, status="succeeded")
        self.two_node_plan(version=2, second="review")
        self.insert_node_run("collect", plan_version=2, status="running")

        latest = self.service.overlay(self.run_id)
        self.assertEqual(2, latest["plan_version"])
        self.assertEqual("running", latest["nodes"][0]["status"])

        older = self.service.overlay(self.run_id, plan_version=1)
        self.assertEqual(1, older["plan_version"])
        self.assertEqual("succeeded", older["nodes"][0]["status"])

    def test_a_node_never_run_simply_has_no_overlay_entry(self) -> None:
        self.two_node_plan()
        self.insert_node_run("collect", plan_version=1, status="succeeded")
        overlay = self.service.overlay(self.run_id)
        self.assertEqual(["collect"], [node["node_id"] for node in overlay["nodes"]])

    def test_generation_and_attempts_are_reported_separately(self) -> None:
        """A retry is not a rework; conflating them loses the distinction."""

        self.two_node_plan()
        self.insert_node_run(
            "collect", plan_version=1, status="failed", generation=1, attempts=3
        )
        self.insert_node_run(
            "collect", plan_version=1, status="running", generation=2, attempts=1
        )
        entries = self.service.overlay(self.run_id)["nodes"]
        self.assertEqual([1, 2], [entry["generation"] for entry in entries])

    def test_overlay_offers_an_expected_version_for_commands(self) -> None:
        self.two_node_plan()
        self.insert_node_run("collect", plan_version=1, status="running")
        self.assertEqual(3, self.service.overlay(self.run_id)["nodes"][0]["expected_version"])

    def test_historical_overlay_replays_only_events_at_or_before_the_cursor(self) -> None:
        self.two_node_plan()
        self.insert_node_run("collect", plan_version=1, status="succeeded", attempts=1)
        node_run_id = "node_run:collect:1:1"
        with connect_workflow_database(self.db) as connection:
            events = (
                ("event:history-1", node_run_id, 1, "node_run_transitioned", {"node_id": "collect", "from": "pending", "to": "ready", "plan_version": 1}),
                ("event:history-2", "attempt:collect:1:1:0", 1, "attempt_transitioned", {"node_run_id": node_run_id, "from": "created", "to": "running"}),
                ("event:history-3", node_run_id, 2, "node_run_transitioned", {"node_id": "collect", "from": "ready", "to": "running"}),
                ("event:history-4", node_run_id, 3, "node_run_transitioned", {"node_id": "collect", "from": "running", "to": "succeeded"}),
            )
            positions = []
            for event_id, aggregate_id, sequence, event_type, payload in events:
                cursor = connection.execute(
                    "INSERT INTO run_events(event_id,run_id,aggregate_id,aggregate_sequence,"
                    "event_type,event_version,correlation_id,causation_id,occurred_at,payload_json)"
                    " VALUES (?,?,?,?,?,1,?,?,?,?)",
                    (event_id, RUN, aggregate_id, sequence, event_type, RUN,
                     "command:history", NOW, json.dumps(payload)),
                )
                positions.append(cursor.lastrowid)
            connection.commit()

        running = self.service.overlay(
            self.run_id, plan_version=1, as_of_global_position=positions[2]
        )
        self.assertEqual("running", running["nodes"][0]["status"])
        self.assertEqual(1, running["nodes"][0]["attempts"])
        self.assertEqual(positions[2], running["as_of_global_position"])
        current = self.service.overlay(self.run_id)
        self.assertEqual("succeeded", current["nodes"][0]["status"])

    def test_historical_overlay_rejects_a_future_cursor(self) -> None:
        self.two_node_plan()
        with self.assertRaisesRegex(ValueError, "beyond the Run event head"):
            self.service.overlay(self.run_id, as_of_global_position=1)


class DiffTests(PlanReadModelTestCase):
    def test_an_added_node_is_reported(self) -> None:
        self.insert_plan(
            1, nodes=[{"node_id": "collect"}], successors={"collect": None}
        )
        self.insert_plan(
            2,
            nodes=[{"node_id": "collect"}, {"node_id": "review"}],
            successors={"collect": "review", "review": None},
        )
        diff = self.service.diff(self.run_id, base_version=1, target_version=2)
        self.assertEqual(["review"], diff["added_nodes"])
        self.assertEqual([], diff["removed_nodes"])
        self.assertEqual([{"from": "collect", "to": "review"}], diff["added_edges"])
        self.assertFalse(diff["identical"])

    def test_a_removed_node_is_reported(self) -> None:
        self.insert_plan(
            1,
            nodes=[{"node_id": "collect"}, {"node_id": "review"}],
            successors={"collect": "review", "review": None},
        )
        self.insert_plan(2, nodes=[{"node_id": "collect"}], successors={"collect": None})
        diff = self.service.diff(self.run_id, base_version=1, target_version=2)
        self.assertEqual(["review"], diff["removed_nodes"])
        self.assertEqual([{"from": "collect", "to": "review"}], diff["removed_edges"])

    def test_a_changed_handler_build_is_a_change(self) -> None:
        self.insert_plan(
            1, nodes=[{"node_id": "collect"}], successors={"collect": None},
            handler_fingerprint="sha256:old",
        )
        self.insert_plan(
            2, nodes=[{"node_id": "collect"}], successors={"collect": None},
            handler_fingerprint="sha256:new",
        )
        diff = self.service.diff(self.run_id, base_version=1, target_version=2)
        self.assertEqual(
            [{"node_id": "collect", "fields": ["handler_manifest_fingerprint"]}],
            diff["changed_nodes"],
        )

    def test_identical_plans_are_reported_as_identical(self) -> None:
        self.two_node_plan(version=1)
        self.two_node_plan(version=2)
        diff = self.service.diff(self.run_id, base_version=1, target_version=2)
        self.assertTrue(diff["identical"])
        self.assertEqual([], diff["changed_nodes"])

    def test_a_diff_carries_no_run_state(self) -> None:
        self.two_node_plan(version=1)
        self.two_node_plan(version=2, second="review")
        self.insert_node_run("collect", plan_version=2, status="running")
        serialised = json.dumps(
            self.service.diff(self.run_id, base_version=1, target_version=2)
        )
        for forbidden in ("running", "node_run_id", "attempts"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialised)


if __name__ == "__main__":
    unittest.main()
