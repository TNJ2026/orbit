from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orbit.background_agent import BackgroundAgentWorker
from orbit.background_agent_adapter import run_codex


class BackgroundAgentWorkerTests(unittest.TestCase):
    def test_cli_keeps_subcommand_and_child_command_separate(self) -> None:
        from orbit.__main__ import build_parser

        args = build_parser().parse_args([
            "agent-worker", "--command", "agent adapter", "--pool", "coding",
        ])
        self.assertEqual("agent-worker", args.command)
        self.assertEqual("agent adapter", args.agent_command)
        self.assertEqual(["coding"], args.pool)

    def test_cli_defaults_to_the_builtin_codex_backend(self) -> None:
        from orbit.__main__ import build_parser

        args = build_parser().parse_args(["agent-worker"])
        self.assertEqual("codex", args.backend)
        self.assertIsNone(args.agent_command)

    def test_hub_can_enable_the_builtin_worker_from_environment(self) -> None:
        from orbit.__main__ import build_parser

        with mock.patch.dict(
            "os.environ", {"ORBIT_BACKGROUND_AGENT_BACKEND": "codex"}, clear=False,
        ):
            args = build_parser().parse_args(["hub", "serve"])
        self.assertEqual("codex", args.background_agent_backend)
        self.assertIsNone(args.background_agent_command)

    def test_builtin_codex_backend_uses_structured_ephemeral_execution(self) -> None:
        def execute(command, **_kwargs):
            answer = Path(command[command.index("--output-last-message") + 1])
            answer.write_text(
                '{"result_json":"{\\"answer\\":\\"done\\"}"}', encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=execute) as invoked:
            result = run_codex(
                {"input": {"task": "work"}, "config": {"effects": "write"}},
                executable="/tools/codex",
            )

        command = invoked.call_args.args[0]
        self.assertEqual({"answer": "done"}, result)
        self.assertIn("--ephemeral", command)
        self.assertEqual("workspace-write", command[command.index("--sandbox") + 1])
        self.assertIn("--output-schema", command)

    def test_one_claim_runs_in_workspace_and_completes_with_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            responses = [
                {
                    "workspace_id": "workspace:one",
                    "workspace_path": temporary,
                    "delegation": {
                        "delegation_id": "app:one",
                        "request": {"input": {"task": "hello"}},
                    },
                },
                {"delegation": {"status": "succeeded"}},
            ]
            command = (
                "python -c 'import json,sys; request=json.load(sys.stdin); "
                "print(json.dumps({\"answer\": request[\"input\"][\"task\"]}))'"
            )
            worker = BackgroundAgentWorker(command, worker_id="machine-worker")
            with mock.patch(
                "orbit.background_agent._post", side_effect=responses,
            ) as post:
                self.assertTrue(worker.run_once())

            self.assertEqual("claim", post.call_args_list[0].args[1])
            self.assertEqual("complete", post.call_args_list[-1].args[1])
            completion = post.call_args_list[-1].args[2]
            self.assertEqual({"answer": "hello"}, completion["result"])
            self.assertEqual("workspace:one", completion["workspace_id"])

    def test_empty_queue_does_not_launch_a_child(self) -> None:
        worker = BackgroundAgentWorker("false", worker_id="machine-worker")
        with mock.patch(
            "orbit.background_agent._post",
            return_value={"workspace_id": None, "delegation": None},
        ):
            self.assertFalse(worker.run_once())


if __name__ == "__main__":
    unittest.main()
