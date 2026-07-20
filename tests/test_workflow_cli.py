from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from orbit.__main__ import main
from orbit.workflow.artifacts import LocalCASBackend
from tests.test_workflow_dsl import VALID_DSL


class WorkflowCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.workflow = root / "workflow.json"
        self.catalog = root / "catalog.json"
        self.db = root / "workflow.db"
        self.workflow.write_text(json.dumps(VALID_DSL), encoding="utf-8")
        self.catalog.write_text(
            json.dumps(
                {
                    "handlers": [
                        {
                            "name": "collect",
                            "version": "1.2.0",
                            "node_kinds": ["action"],
                            "inputs": {},
                            "outputs": {"request": "example://request/1.0"},
                            "config_schema": {
                                "type": "object",
                                "additionalProperties": False,
                            },
                            "execution_safety": "replay_safe",
                            "resource_profile": {
                                "max_input_tokens": 0,
                                "max_output_tokens": 0,
                                "max_tool_calls": 0,
                                "max_duration_seconds": 60,
                                "max_cost_microunits": 0,
                                "cost_class": "free",
                            },
                            "result_schema_id": "example://request/1.0",
                        }
                    ],
                    "schemas": {"example://request/1.0": {"type": "object"}},
                    "extensions": [],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *arguments: str) -> str:
        output = StringIO()
        with patch("sys.argv", ["orbit", *arguments]), redirect_stdout(output):
            main()
        return output.getvalue()

    def test_validate_and_compile_use_same_canonical_result(self) -> None:
        validated = json.loads(
            self.run_cli(
                "workflow", "validate", str(self.workflow),
                "--catalog", str(self.catalog), "--json",
            )
        )
        compiled = json.loads(
            self.run_cli(
                "workflow", "compile", str(self.workflow),
                "--catalog", str(self.catalog),
            )
        )
        self.assertTrue(validated["valid"])
        self.assertEqual("workflow:approval_flow", compiled["workflow_id"])

    def test_publish_is_exposed_with_idempotent_output(self) -> None:
        arguments = (
            "workflow", "publish", str(self.workflow), "--catalog", str(self.catalog),
            "--db", str(self.db), "--expected-version", "0", "--json",
        )
        first = json.loads(self.run_cli(*arguments))
        second_arguments = list(arguments)
        second_arguments[second_arguments.index("0")] = "999"
        second = json.loads(self.run_cli(*second_arguments))
        self.assertEqual(first, second)
        self.assertEqual(1, first["version"])

    def test_validation_error_returns_exit_code_two_and_diagnostics(self) -> None:
        self.workflow.write_text('{"dsl_version":"9.0"}', encoding="utf-8")
        output = StringIO()
        with patch(
            "sys.argv",
            [
                "orbit", "workflow", "validate", str(self.workflow),
                "--catalog", str(self.catalog), "--json",
            ],
        ), redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(2, raised.exception.code)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["valid"])
        self.assertTrue(payload["diagnostics"])

    def test_serve_wires_the_configured_artifact_store(self) -> None:
        artifact_root = Path(self.temp_dir.name) / "custom-artifacts"
        with (
            patch("orbit.web.app.create_app") as create_app,
            patch("orbit.__main__.upsert_project"),
            patch("orbit.__main__.uvicorn.run"),
        ):
            output = self.run_cli(
                "serve", "--db", str(self.db),
                "--artifact-root", str(artifact_root),
                "--no-agent-discovery",
            )

        backend = create_app.call_args.kwargs["artifact_backend"]
        self.assertIsInstance(backend, LocalCASBackend)
        self.assertEqual(artifact_root.absolute(), backend.root)
        self.assertTrue((artifact_root / "staging").is_dir())
        self.assertTrue((artifact_root / "blobs" / "sha256").is_dir())
        self.assertIn(f"artifacts: {artifact_root.absolute()}", output)

    def test_serve_defaults_artifacts_beside_the_selected_database(self) -> None:
        with (
            patch("orbit.web.app.create_app") as create_app,
            patch("orbit.__main__.upsert_project"),
            patch("orbit.__main__.uvicorn.run"),
        ):
            self.run_cli(
                "serve", "--db", str(self.db), "--no-agent-discovery",
            )

        backend = create_app.call_args.kwargs["artifact_backend"]
        self.assertEqual(self.db.parent / "artifacts", backend.root)

    def test_serve_reports_an_unusable_artifact_root_without_a_traceback(self) -> None:
        invalid_root = Path(self.temp_dir.name) / "not-a-directory"
        invalid_root.write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(
            SystemExit, "cannot initialize Artifact store"
        ):
            self.run_cli(
                "serve", "--db", str(self.db),
                "--artifact-root", str(invalid_root),
                "--no-agent-discovery",
            )

    def test_serve_refuses_legacy_schema_before_creating_artifact_store(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        artifact_root = Path(self.temp_dir.name) / "must-not-exist"

        with self.assertRaisesRegex(SystemExit, "legacy engine tables"):
            self.run_cli(
                "serve", "--db", str(self.db),
                "--artifact-root", str(artifact_root),
                "--no-agent-discovery",
            )
        self.assertFalse(artifact_root.exists())


if __name__ == "__main__":
    unittest.main()
