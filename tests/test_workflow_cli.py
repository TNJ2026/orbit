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
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_serve_enables_langgraph_by_default(self) -> None:
        with (
            patch("orbit.web.app.create_app") as create_app,
            patch("orbit.__main__.upsert_project"),
            patch("orbit.__main__.uvicorn.run"),
        ):
            self.run_cli(
                "serve", "--db", str(self.db), "--no-agent-discovery",
            )

        self.assertEqual(
            self.db.parent,
            create_app.call_args.kwargs["langgraph_state_directory"],
        )

        self.assertEqual(
            "single-agent", create_app.call_args.kwargs["workflow_ui_mode"]
        )
        # An explicit --db is self-contained, in every command alike. A
        # sibling file only this command knew about is what made a Workflow
        # published with `orbit workflow publish --db X` invisible in the UI
        # served from the same X.
        self.assertEqual(
            self.db, create_app.call_args.kwargs["workflow_db_path"],
        )

    def test_serve_can_select_the_multi_agent_ui(self) -> None:
        with (
            patch("orbit.web.app.create_app") as create_app,
            patch("orbit.__main__.upsert_project"),
            patch("orbit.__main__.uvicorn.run"),
        ):
            self.run_cli(
                "serve", "--db", str(self.db), "--no-agent-discovery",
                "--ui-mode", "multi-agent",
            )

        self.assertEqual(
            "multi-agent", create_app.call_args.kwargs["workflow_ui_mode"]
        )
        self.assertEqual(
            self.db, create_app.call_args.kwargs["workflow_db_path"],
        )

    def test_mcp_runs_in_the_same_mode_serve_does(self) -> None:
        """One flag, one meaning, on both surfaces.

        `--ui-mode` used to pick which library `orbit mcp` addressed and
        nothing else, so a Workflow the UI could start — because single-Agent
        mode rebinds its Agent steps to the installed Agent — was refused over
        stdio for naming an Agent that is not installed. Both surfaces read
        the same catalog; they have to run it the same way too.
        """

        for mode in ("single-agent", "multi-agent"):
            with self.subTest(mode=mode):
                with (
                    patch("orbit.web.app.create_app") as create_app,
                    patch("orbit.web.mcp.serve_stdio"),
                ):
                    self.run_cli(
                        "mcp", "--db", str(self.db), "--no-agent-discovery",
                        "--ui-mode", mode,
                    )

                self.assertEqual(
                    mode, create_app.call_args.kwargs["workflow_ui_mode"],
                )

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


class WorkflowInventoryCliTests(unittest.TestCase):
    """Which published Workflows can start a Goal.

    The UI withholds `run.start` from anything that cannot run from a single
    Goal. An operator is entitled to that list before their users discover it,
    so the report exists and is read-only.
    """

    def setUp(self) -> None:
        from orbit.workflow.application.workflows import (
            WorkflowCatalogs, WorkflowDefinitionService,
        )
        from orbit.workflow.catalogs import (
            InMemoryHandlerCatalog, InMemorySchemaCatalog,
        )
        from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
        from orbit.workflow.persistence.database import connect_workflow_database
        from orbit.workflow.persistence.migrations import migrate_workflow_database
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_workflow_authoring_jobs import MANIFEST, dsl

        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_dir.cleanup)
        self.db = Path(self.temp_dir.name) / "runtime.db"
        with connect_workflow_database(self.db) as connection:
            migrate_workflow_database(connection)
        definitions = WorkflowDefinitionService(
            WorkflowCatalogs(
                InMemoryHandlerCatalog([MANIFEST]),
                InMemorySchemaCatalog({
                    "example://integer/1.0": {"type": "integer"},
                    "schema://object/1.0": {"type": "object"},
                }),
                InMemoryExtensionRegistry(),
            ),
            SQLiteWorkflowVersionStore(self.db),
        )
        definitions.publish_workflow(
            json.dumps(dsl()), source_name="<test>", source_format="json",
            expected_latest_version=0, actor="author",
        )
        # This is the conventional Agent ingress accepted by the Runtime: the
        # CLI inventory must resolve its built-in object schema just as the UI
        # does, rather than reporting a missing goal binding from an empty
        # schema catalog.
        with connect_workflow_database(self.db) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_versions"
                " WHERE workflow_id='workflow:research'"
            ).fetchone()
            research = json.loads(row["canonical_ir_json"])
            research["nodes"][0]["handler"]["name"] = "agent.codex"
            prompt = research["nodes"][0]["inputs"][0]
            prompt["id"] = "prompt"
            prompt["schema_id"] = "schema://object/1.0"
            connection.execute(
                "INSERT INTO workflow_versions(workflow_id,version,definition_hash,"
                "dsl_version,ir_version,compiler_version,canonical_ir_json,"
                "source_format,source_text,catalog_fingerprint,created_at,created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["workflow_id"], int(row["version"]) + 1,
                    row["definition_hash"] + "-goal-binding", row["dsl_version"],
                    row["ir_version"], row["compiler_version"], json.dumps(research),
                    row["source_format"], row["source_text"],
                    row["catalog_fingerprint"], row["created_at"], row["created_by"],
                ),
            )
            connection.commit()
        legacy = dsl()
        legacy["dsl_version"] = "1.2"
        legacy["metadata"] = {"id": "legacy", "name": "Legacy"}
        legacy.pop("result")
        definitions.publish_workflow(
            json.dumps(legacy), source_name="<test>", source_format="json",
            expected_latest_version=0, actor="author",
        )

    def run_cli(self, *arguments: str) -> str:
        output = StringIO()
        with patch("sys.argv", ["orbit", *arguments]), redirect_stdout(output):
            main()
        return output.getvalue()

    def report(self) -> dict:
        return json.loads(
            self.run_cli("workflow", "inventory", "--db", str(self.db), "--json")
        )

    def test_every_published_workflow_lands_in_exactly_one_bucket(self) -> None:
        report = self.report()
        buckets = report["workflows"]
        self.assertEqual(
            {"ready", "needs_upgrade", "needs_migration"}, set(buckets),
        )
        listed = [
            item["workflow_id"] for group in buckets.values() for item in group
        ]
        self.assertEqual(sorted(listed), sorted(set(listed)))
        self.assertEqual({"workflow:legacy", "workflow:research"}, set(listed))
        self.assertEqual(
            {key: len(value) for key, value in buckets.items()}, report["counts"],
        )
        self.assertEqual(
            ["workflow:research"],
            [item["workflow_id"] for item in buckets["ready"]],
        )

    def test_a_workflow_that_cannot_start_a_goal_carries_its_reason(self) -> None:
        entries = [
            item for group in self.report()["workflows"].values() for item in group
            if item["workflow_id"] == "workflow:legacy"
        ]
        self.assertEqual(1, len(entries))
        self.assertIsNotNone(entries[0]["reason"])

    def test_a_workflow_without_source_needs_an_operator_not_a_prompt(self) -> None:
        from orbit.workflow.persistence.database import connect_workflow_database

        # A published version is immutable, so the source-less case is written
        # as a fresh version — which is how it exists in the wild: published by
        # an older build that never stored the author's text.
        with connect_workflow_database(self.db) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id='workflow:legacy'"
            ).fetchone()
            connection.execute(
                "INSERT INTO workflow_versions(workflow_id,version,definition_hash,"
                "dsl_version,ir_version,compiler_version,canonical_ir_json,"
                "source_format,source_text,catalog_fingerprint,created_at,created_by)"
                " VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?)",
                (
                    row["workflow_id"], int(row["version"]) + 1,
                    row["definition_hash"] + "-next", row["dsl_version"],
                    row["ir_version"], row["compiler_version"],
                    row["canonical_ir_json"], row["source_format"],
                    row["catalog_fingerprint"], row["created_at"], row["created_by"],
                ),
            )
            connection.commit()
        buckets = self.report()["workflows"]
        self.assertEqual(
            ["workflow:legacy"],
            [item["workflow_id"] for item in buckets["needs_migration"]],
        )
        self.assertFalse(buckets["needs_migration"][0]["source_available"])

    def test_serving_reports_goal_readiness(self) -> None:
        """The operator hears it at boot, not from a confused user later."""

        with (
            patch("orbit.web.app.create_app"),
            patch("orbit.__main__.upsert_project"),
            patch("orbit.__main__.uvicorn.run"),
        ):
            output = self.run_cli(
                "serve", "--db", str(self.db), "--no-agent-discovery",
            )

        self.assertIn("goal readiness:", output)
        self.assertIn("workflow:legacy", output)
        self.assertIn("orbit workflow inventory", output)

    def test_a_readiness_survey_that_fails_does_not_stop_the_server(self) -> None:
        """A report is information; refusing to boot over it would be worse."""

        with (
            patch("orbit.web.app.create_app"),
            patch("orbit.__main__.upsert_project"),
            patch("orbit.__main__.uvicorn.run") as run,
            patch(
                "orbit.__main__._goal_readiness_buckets",
                side_effect=RuntimeError("projection is rebuilding"),
            ),
        ):
            output = self.run_cli(
                "serve", "--db", str(self.db),
                "--no-agent-discovery",
            )

        self.assertIn("could not survey workflow goal readiness", output)
        self.assertTrue(run.called)

    def test_the_report_reads_and_never_writes(self) -> None:
        # Content, not bytes. The database is in WAL mode, so the bytes of the
        # main file depend on when a checkpoint happened to run — which made
        # this assertion pass or fail according to what else the process had
        # done first, rather than according to whether the report wrote.
        before = self._contents()
        self.run_cli("workflow", "inventory", "--db", str(self.db))
        self.assertEqual(before, self._contents())

    def _contents(self) -> list[str]:
        """Every row and every schema object, as text."""

        with sqlite3.connect(self.db) as connection:
            return list(connection.iterdump())

    def test_the_human_report_names_each_bucket_and_every_workflow(self) -> None:
        printed = self.run_cli("workflow", "inventory", "--db", str(self.db))
        for expected in (
            "ready", "needs upgrade", "needs migration",
            "workflow:legacy", "workflow:research",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, printed)

    def test_a_missing_database_is_named_rather_than_stack_traced(self) -> None:
        missing = Path(self.temp_dir.name) / "absent.db"
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("workflow", "inventory", "--db", str(missing))
        self.assertIn(str(missing), str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class WorkflowLibraryResolutionTests(unittest.TestCase):
    """One rule for which library a command addresses, for every command.

    Each surface used to resolve it for itself. `orbit serve` defaults to
    `--ui-mode single-agent` and so read `single-agent-library.db`, while
    `orbit mcp`, `orbit workflow inventory|publish` and `orbit run start`
    resolved `library.db` — a default install where a Workflow published from
    the command line never appeared in the UI, and one authored in the UI
    could not be started from the command line. Nothing failed; the two
    catalogs simply had nothing to do with each other.
    """

    def parse(self, *arguments: str):
        from orbit.__main__ import build_parser

        return build_parser().parse_args(list(arguments))

    def resolved(self, *arguments: str) -> str:
        from orbit.__main__ import _workflow_db_path

        args = self.parse(*arguments)
        return _workflow_db_path(args.db, args.ui_mode)

    def test_every_default_surface_resolves_the_same_library_for_a_mode(self) -> None:
        from orbit.platform.projects import workflow_library_path

        commands = (
            ("serve",),
            ("mcp",),
            ("workflow", "inventory"),
                    )
        for mode in ("single-agent", "multi-agent"):
            expected = str(workflow_library_path(mode))
            for command in commands:
                with self.subTest(command=command, mode=mode):
                    self.assertEqual(
                        expected, self.resolved(*command, "--ui-mode", mode)
                    )

    def test_the_default_mode_is_the_same_everywhere(self) -> None:
        """Agreeing only when the operator names the mode is not agreeing."""

        defaults = {
            self.parse(*command).ui_mode
            for command in (
                ("serve",), ("mcp",), ("workflow", "inventory"),
                            )
        }
        self.assertEqual({"single-agent"}, defaults)

    def test_an_explicit_database_is_self_contained_in_every_command(self) -> None:
        """No sibling file that only one command knows the name of."""

        for command in (("serve",), ("mcp",), ("workflow", "inventory")):
            for mode in ("single-agent", "multi-agent"):
                with self.subTest(command=command, mode=mode):
                    self.assertEqual(
                        "/tmp/named.db",
                        self.resolved(
                            *command, "--db", "/tmp/named.db", "--ui-mode", mode
                        ),
                    )
