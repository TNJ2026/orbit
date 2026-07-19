"""P0 contract freeze for the Runtime UI plan's B2/B3 (delivery plan §3.2).

These goldens pin the *target* DTO shapes — InboxItem 2.0, RunSummary 2.0 and
the run query — before their projections exist. They validate frozen schemas
against curated fixtures only; endpoint goldens arrive with API-1/API-3. The
point is that P2/P5 build against a reviewed contract instead of whatever
shape the first implementation happened to emit.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

FIXTURES = Path(__file__).parent / "fixtures" / "ui_contracts" / "v2"

SCHEMA_FILES = (
    "allowed-command.schema.json",
    "inbox-item.schema.json",
    "run-summary.schema.json",
    "run-query.schema.json",
)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def validator(schema_name: str) -> Draft202012Validator:
    # inbox-item refers to allowed-command by relative file name; resolve every
    # schema through one registry so the reference stays a plain file name.
    resources = [
        (name, Resource.from_contents(load(name))) for name in SCHEMA_FILES
    ]
    registry = Registry().with_resources(resources)
    return Draft202012Validator(load(schema_name), registry=registry)


class FrozenSchemaTests(unittest.TestCase):
    def test_every_schema_is_itself_valid(self) -> None:
        for name in SCHEMA_FILES:
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(load(name))

    def test_valid_samples_pass(self) -> None:
        samples = load("samples.json")
        cases = (
            ("inbox-item.schema.json", samples["inbox_item_valid"]),
            ("run-summary.schema.json", samples["run_summary_valid"]),
            ("run-query.schema.json", samples["run_query_valid"]),
        )
        for schema_name, values in cases:
            checker = validator(schema_name)
            for index, value in enumerate(values):
                with self.subTest(schema=schema_name, sample=index):
                    errors = sorted(checker.iter_errors(value), key=str)
                    self.assertEqual([], errors, f"sample {index}: {errors}")

    def test_invalid_samples_are_rejected_for_the_stated_reason(self) -> None:
        samples = load("samples.json")
        cases = (
            ("inbox-item.schema.json", samples["inbox_item_invalid"]),
            ("run-summary.schema.json", samples["run_summary_invalid"]),
            ("run-query.schema.json", samples["run_query_invalid"]),
        )
        for schema_name, values in cases:
            checker = validator(schema_name)
            for value in values:
                reason = value.pop("_reason")
                with self.subTest(schema=schema_name, reason=reason):
                    self.assertTrue(
                        any(checker.iter_errors(value)),
                        f"expected rejection: {reason}",
                    )

    def test_inbox_commands_may_only_target_the_versioned_api(self) -> None:
        """The '^/api/v1/' pattern is the frozen no-arbitrary-URL rule."""
        checker = validator("allowed-command.schema.json")
        command = {
            "command": "x", "label": "x", "method": "POST",
            "href": "/api/legacy/x", "target_aggregate_id": "run:r",
            "expected_version": 0, "payload_schema": "x/1.0",
            "confirmation": "explicit",
        }
        self.assertTrue(any(checker.iter_errors(command)))
        command["href"] = "/api/v1/runs/run:r/cancel"
        self.assertEqual([], list(checker.iter_errors(command)))

    def test_wait_reason_vocabulary_matches_the_graph_summary(self) -> None:
        """RunSummary 2.0 must not invent waiting words the runtime never emits.

        The only addition over the graph summary's vocabulary is budget_wait,
        which the aggregated inbox introduces (plan B2/B3).
        """
        from orbit.workflow.application import runtime_service

        source = Path(runtime_service.__file__).read_text(encoding="utf-8")
        produced = {
            "human_wait", "unknown_wait", "retry_wait", "join_wait",
            "timer_wait", "stalled",
        }
        for word in produced:
            self.assertIn(f'"{word}"', source)
        frozen = set(
            load("run-summary.schema.json")["properties"]["wait_reason"]["enum"]
        )
        self.assertEqual(produced | {"budget_wait", None}, frozen)


if __name__ == "__main__":
    unittest.main()
