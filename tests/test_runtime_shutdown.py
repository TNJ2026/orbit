from __future__ import annotations

import asyncio
import tempfile
import unittest

from orbit.web.api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, Authorizer,
)
from orbit.web.app import create_app
from tests.test_web_composition import AsgiHarness


class RuntimeShutdownTests(unittest.TestCase):
    def _app(self, db_path, stopped):
        scopes = {
            "operator": (READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE),
            "viewer": (READ_SCOPE,),
        }
        return create_app(
            db_path,
            worker_count=1,
            poll_seconds=0.01,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: scopes.get(actor, ())),
            operator_actors=("operator",),
            shutdown_request=lambda: stopped.append(True),
        )

    def test_capabilities_advertise_shutdown_only_to_operator(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stopped = []
            with AsgiHarness(self._app(f"{root}/runtime.db", stopped)) as client:
                operator = client.get(
                    "/api/v1/capabilities", actor="operator",
                ).json()["data"]
                viewer = client.get(
                    "/api/v1/capabilities", actor="viewer",
                ).json()["data"]

                command = operator["runtime"]["allowed_commands"][0]
                self.assertEqual("runtime.shutdown", command["command"])
                self.assertEqual("/api/v1/runtime/shutdown", command["href"])
                self.assertEqual([], viewer["runtime"]["allowed_commands"])

    def test_shutdown_is_idempotent_and_runs_after_response(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stopped = []
            with AsgiHarness(self._app(f"{root}/runtime.db", stopped)) as client:
                response = client.post(
                    "/api/v1/runtime/shutdown",
                    actor="operator",
                    key="stop-runtime-once",
                    body={"expected_version": 0},
                )
                self.assertEqual(200, response.status_code)
                self.assertEqual("stopping", response.json()["data"]["status"])
                self.assertEqual([], stopped)
                client._loop.run_until_complete(asyncio.sleep(0.06))
                self.assertEqual([True], stopped)

    def test_shutdown_requires_command_headers_and_operator_scope(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stopped = []
            with AsgiHarness(self._app(f"{root}/runtime.db", stopped)) as client:
                missing_key = client.post(
                    "/api/v1/runtime/shutdown",
                    actor="operator",
                    body={"expected_version": 0},
                )
                denied = client.post(
                    "/api/v1/runtime/shutdown",
                    actor="viewer",
                    key="viewer-stop",
                    body={"expected_version": 0},
                )
                self.assertEqual(400, missing_key.status_code)
                self.assertEqual(403, denied.status_code)
                self.assertEqual([], stopped)


if __name__ == "__main__":
    unittest.main()
