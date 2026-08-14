"""Frozen shapes the UI is built against.

These began as a forward contract: DTOs agreed before their projections
existed, so the implementation would be written to a reviewed shape rather
than the shape the first attempt happened to emit. Three of them — InboxItem,
RunSummary and the run query — were pinning projections of the event-sourced
engine, and went when it did. What is left describes shapes the Runtime still
serves.

Curated samples say what the schema means, including what it must reject. The
served payload says the server still means it: a schema validated only against
fixtures written beside it agrees with itself for ever.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

FIXTURES = Path(__file__).parent / "fixtures" / "ui_contracts" / "v2"

SCHEMA_FILES = (
    "allowed-command.schema.json",
    "workflow-draft.schema.json",
)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def validator(schema_name: str) -> Draft202012Validator:
    # A schema may refer to another by relative file name; resolving every one
    # through a single registry keeps the reference a plain name.
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
            ("workflow-draft.schema.json", samples["workflow_draft_valid"]),
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
            ("workflow-draft.schema.json", samples["workflow_draft_invalid"]),
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


class ServedPayloadTests(unittest.TestCase):
    """The shape as the Runtime actually emits it, not only as described."""

    def test_the_commands_a_catalog_advertises_match_the_frozen_shape(self) -> None:
        from orbit.web.api_v1 import (
            Authorizer, OPS_READ_SCOPE, READ_SCOPE, WRITE_SCOPE,
        )
        from orbit.web.app import create_app
        from tests.test_web_composition import (
            AsgiHarness, SCHEMAS, publish_linear_workflow, transform_registration,
        )

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        database = Path(temp.name) / "runtime.db"
        app = create_app(
            database,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: "author",
            authorizer=Authorizer(
                lambda _actor: [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE]
            ),
            langgraph_state_directory=Path(temp.name) / "langgraph",
        )
        publish_linear_workflow(database)
        with AsgiHarness(app) as client:
            catalog = client.get(
                "/api/v1/workflows", actor="author",
            ).json()["data"]["workflows"]

        commands = [
            command for entry in catalog
            for command in entry.get("allowed_commands", ())
        ]
        self.assertTrue(commands, "the catalog advertised no commands to check")
        checker = validator("allowed-command.schema.json")
        for command in commands:
            with self.subTest(command=command.get("command")):
                errors = sorted(checker.iter_errors(command), key=str)
                self.assertEqual([], errors, f"{command}: {errors}")


if __name__ == "__main__":
    unittest.main()
