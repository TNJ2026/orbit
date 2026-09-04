from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orbit.background_agent import BackgroundAgentWorker


class BackgroundAgentWorkerTests(unittest.TestCase):
    def test_cli_keeps_subcommand_and_child_command_separate(self) -> None:
        from orbit.__main__ import build_parser

        args = build_parser().parse_args([
            "agent-worker", "--command", "agent adapter", "--pool", "coding",
        ])
        self.assertEqual("agent-worker", args.command)
        self.assertEqual("agent adapter", args.agent_command)
        self.assertEqual(["coding"], args.pool)

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
