from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import unittest

from orbit.workflow.domain.envelopes import EventEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import StoredEvent
from orbit.workflow.domain.serialization import canonical_json, definition_hash
from orbit.workflow.domain.upcasting import UpcasterRegistry, with_payload
from orbit.workflow.domain.versions import Revision
from orbit.workflow.persistence import EventVersionCatalog, UpcastingEventReader


FIXTURE = Path(__file__).parent / "fixtures/workflow_persistence/v1/golden-run.json"
GOLDEN_HASH = "sha256:b8e074a82052b553b14c4256427556a7987bdfd244d13e8da874d7dd917f1892"


class WorkflowPersistenceGoldenTests(unittest.TestCase):
    def test_golden_raw_upcast_state_cursor_and_hash(self) -> None:
        raw_bytes = FIXTURE.read_bytes()
        fixture = json.loads(raw_bytes)
        self.assertEqual(GOLDEN_HASH, definition_hash(fixture).value)
        registry = UpcasterRegistry()
        registry.register(
            "run_progressed", 1,
            lambda item: with_payload(item, {**dict(item.payload), "format": "v2"}, version=2),
        )
        registry.seal()
        reader = UpcastingEventReader(EventVersionCatalog({"run_progressed": 2}), registry)
        state = {"values": []}
        payloads = []
        for position, value in enumerate(fixture["raw_events"], 1):
            event = EventEnvelope(
                EntityId.parse(value["event_id"]), value["event_type"], Revision(value["event_version"]),
                EntityId.parse(value["aggregate_id"]), Revision(value["sequence"]),
                EntityId.parse(value["correlation_id"]), EntityId.parse(value["causation_id"]),
                datetime.fromisoformat(value["occurred_at"].replace("Z", "+00:00")), value["payload"],
            )
            upgraded = reader.read(StoredEvent(EntityId("run", "r1"), position, event))
            payloads.append(dict(upgraded.envelope.payload))
            state["values"].append(upgraded.envelope.payload["value"])
        self.assertEqual(fixture["expected_upcasted_payloads"], payloads)
        self.assertEqual(fixture["expected_state"], state)
        self.assertEqual(fixture["expected_cursor"], position)
        global_stream = fixture["cross_aggregate_global_stream"]
        self.assertEqual(
            list(range(1, fixture["expected_global_cursor"] + 1)),
            [item["global_position"] for item in global_stream],
        )
        self.assertEqual(
            ["run:r1", "node_run:n1", "attempt:a1"],
            [item["aggregate_id"] for item in global_stream],
        )
        self.assertEqual(raw_bytes, FIXTURE.read_bytes(), "replay must not rewrite raw fixture")
        self.assertEqual(canonical_json(fixture), canonical_json(json.loads(raw_bytes)))


if __name__ == "__main__":
    unittest.main()
