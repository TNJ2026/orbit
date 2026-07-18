"""M5 Gate: dev tooling is optional, and the kernel does not depend on it.

Two claims are tested here rather than argued: a non-development runtime never
loads git, and a development runtime that loads it still cannot be handed a
command by a workflow.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orbit.web.builtin_handlers import (
    BUILTIN_SCHEMAS, DEV_TOOL_MANIFEST, DEV_TOOL_WRITE_MANIFEST,
    TRANSFORM_MANIFEST, builtin_handlers, dev_tool_handlers,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.handlers.dev_tools import VerifyProfile


PROFILES = (VerifyProfile("unit", ("python", "-m", "unittest", "discover")),)


class DefaultCompositionTests(unittest.TestCase):
    def test_the_default_runtime_registers_no_development_tooling(self) -> None:
        names = {r.manifest.name for r in builtin_handlers()}
        self.assertEqual({"transform"}, names)

    def test_the_default_handler_needs_no_capabilities(self) -> None:
        self.assertEqual((), TRANSFORM_MANIFEST.capabilities)
        self.assertEqual((), TRANSFORM_MANIFEST.required_secrets)

    def test_the_builtin_schemas_are_self_contained(self) -> None:
        self.assertIn("schema://object/1.0", BUILTIN_SCHEMAS)


class DevToolCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, **overrides):
        return dev_tool_handlers(
            self.root, self.root / "state", verify_profiles=PROFILES, **overrides
        )

    def test_granting_everything_registers_both_handlers(self) -> None:
        registrations, names = self.build()
        self.assertEqual(
            {"dev_tool", "dev_tool_write"},
            {r.manifest.name for r in registrations},
        )
        self.assertIn("git.integrate", names)

    def test_read_only_grants_leave_out_the_writing_handler(self) -> None:
        registrations, names = self.build(
            allowed_capabilities=["workspace.read", "process.run"]
        )
        self.assertEqual({"dev_tool"}, {r.manifest.name for r in registrations})
        self.assertNotIn("git.integrate", names)

    def test_granting_nothing_registers_nothing(self) -> None:
        registrations, names = self.build(allowed_capabilities=[])
        self.assertEqual((), registrations)
        self.assertEqual((), names)

    def test_the_two_handlers_differ_only_in_execution_safety(self) -> None:
        """That difference is the whole point: a lost lease on a merge is
        unknown, a lost lease on `git status` is not."""

        self.assertEqual(ExecutionSafety.REPLAY_SAFE, DEV_TOOL_MANIFEST.execution_safety)
        self.assertEqual(
            ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
            DEV_TOOL_WRITE_MANIFEST.execution_safety,
        )
        self.assertEqual(DEV_TOOL_MANIFEST.config_schema, DEV_TOOL_WRITE_MANIFEST.config_schema)

    def test_a_workflow_selects_a_tool_and_cannot_describe_one(self) -> None:
        schema = DEV_TOOL_MANIFEST.config_schema
        self.assertEqual({"tool_name", "tool_version"}, set(schema["properties"]))
        for forbidden in ("command", "argv", "path", "cwd", "env", "shell"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, schema["properties"])

    def test_the_dev_handlers_declare_the_capabilities_they_use(self) -> None:
        for manifest in (DEV_TOOL_MANIFEST, DEV_TOOL_WRITE_MANIFEST):
            with self.subTest(manifest=manifest.name):
                self.assertIn("process.run", manifest.capabilities)
                self.assertEqual((), manifest.required_secrets)

    def test_a_sealed_registry_is_returned_ready_to_use(self) -> None:
        registrations, _names = self.build()
        handler = registrations[0].implementation
        self.assertTrue(
            handler.validate(
                DEV_TOOL_MANIFEST,
                {"tool_name": "git.status", "tool_version": "1.0.0"},
            ).valid
        )

    def test_a_read_only_tool_is_refused_through_the_writing_handler(self) -> None:
        """Safety is checked at validation, not left to run-time luck."""

        registrations, _names = self.build()
        writing = next(r for r in registrations if r.manifest.name == "dev_tool_write")
        result = writing.implementation.validate(
            DEV_TOOL_WRITE_MANIFEST,
            {"tool_name": "git.status", "tool_version": "1.0.0"},
        )
        self.assertFalse(result.valid)

    def test_an_unregistered_tool_name_fails_validation(self) -> None:
        registrations, _names = self.build()
        result = registrations[0].implementation.validate(
            DEV_TOOL_MANIFEST, {"tool_name": "rm", "tool_version": "1.0.0"}
        )
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
