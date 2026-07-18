"""M3.E: the restored `/mcp` contract.

`external_integrations.json` records /mcp as `retain_and_rewrite` with a named
contract test list: initialize, discovery, read-only inspect, an authorized
write, an unauthorized call, and a version conflict. Each is a test here.
"""

from __future__ import annotations

import json
import unittest

from tests.test_api_v1 import ApiTestCase
from tests.test_web_composition import AsgiHarness


def rpc(client, method, params=None, *, actor=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.request("POST", "/mcp", actor=actor, body=body)


def tool(client, name, arguments, *, actor):
    response = rpc(
        client, "tools/call", {"name": name, "arguments": arguments}, actor=actor
    )
    return response.json()


def payload_of(result):
    """Unwrap the MCP text content back into the object the tool returned."""

    return json.loads(result["result"]["content"][0]["text"])


class HandshakeTests(ApiTestCase):
    def test_initialize_reports_protocol_and_server(self) -> None:
        with AsgiHarness(self.app) as client:
            body = rpc(client, "initialize", {}, actor="reader").json()
            self.assertEqual("2.0", body["jsonrpc"])
            self.assertEqual("orbit", body["result"]["serverInfo"]["name"])
            self.assertIn("tools", body["result"]["capabilities"])

    def test_a_notification_gets_no_response_body(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.request(
                "POST", "/mcp", actor="reader",
                body={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            self.assertEqual(202, response.status_code)

    def test_unknown_method_is_a_protocol_error(self) -> None:
        with AsgiHarness(self.app) as client:
            body = rpc(client, "does/not/exist", actor="reader").json()
            self.assertEqual(-32601, body["error"]["code"])

    def test_malformed_body_does_not_crash_the_endpoint(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.request(
                "POST", "/mcp", actor="reader", headers={"content-type": "application/json"}
            )
            self.assertEqual(200, response.status_code)
            self.assertIn("error", response.json())


class DiscoveryTests(ApiTestCase):
    def test_tools_are_discoverable_with_schemas(self) -> None:
        with AsgiHarness(self.app) as client:
            tools = rpc(client, "tools/list", actor="reader").json()["result"]["tools"]
            names = {item["name"] for item in tools}
            self.assertEqual(
                {"list_runs", "inspect_run", "start_run", "cancel_run"}, names
            )
            for item in tools:
                self.assertIn("inputSchema", item)
                # The scope is an internal authorisation detail, not part of
                # the advertised tool contract.
                self.assertNotIn("scope", item)


class ToolCallTests(ApiTestCase):
    def _start(self, client, key="mcp-1"):
        result = tool(
            client, "start_run",
            {"workflow_id": "workflow:linear", "input": {"value": 0},
             "idempotency_key": key},
            actor="writer",
        )
        return payload_of(result)

    def test_read_only_tool_works_for_a_reader(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(client, "list_runs", {}, actor="reader")
            self.assertFalse(result["result"]["isError"])
            self.assertEqual([], payload_of(result)["runs"])

    def test_write_tool_starts_a_run(self) -> None:
        with AsgiHarness(self.app) as client:
            started = self._start(client)
            self.assertTrue(started["run_id"].startswith("run:"))
            self.assertFalse(started["replayed"])

    def test_repeating_the_key_replays_instead_of_duplicating(self) -> None:
        with AsgiHarness(self.app) as client:
            first = self._start(client, key="same")
            second = self._start(client, key="same")
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertTrue(second["replayed"])

    def test_inspect_answers_why(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(client, key="inspect")["run_id"]
            body = payload_of(tool(client, "inspect_run", {"run_id": run_id}, actor="reader"))
            self.assertEqual(run_id, body["summary"]["run_id"])
            self.assertIn("responsibilities", body)

    def test_cancel_with_a_stale_version_is_a_tool_error_not_a_crash(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(client, key="cancel")["run_id"]
            result = tool(
                client, "cancel_run",
                {"run_id": run_id, "expected_version": 999, "idempotency_key": "c"},
                actor="writer",
            )
            self.assertTrue(result["result"]["isError"])
            self.assertIn("error", payload_of(result))

    def test_a_missing_argument_is_reported_to_the_caller(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(client, "start_run", {"workflow_id": "workflow:linear"}, actor="writer")
            self.assertTrue(result["result"]["isError"])

    def test_unknown_tool_is_an_invalid_params_error(self) -> None:
        with AsgiHarness(self.app) as client:
            body = tool(client, "no_such_tool", {}, actor="writer")
            self.assertEqual(-32602, body["error"]["code"])


class McpAuthorizationTests(ApiTestCase):
    def test_anonymous_tool_calls_are_refused(self) -> None:
        with AsgiHarness(self.app) as client:
            body = tool(client, "list_runs", {}, actor=None)
            self.assertEqual(-32001, body["error"]["code"])

    def test_a_reader_cannot_start_a_run(self) -> None:
        with AsgiHarness(self.app) as client:
            body = tool(
                client, "start_run",
                {"workflow_id": "workflow:linear", "idempotency_key": "k"},
                actor="reader",
            )
            self.assertEqual(-32001, body["error"]["code"])
            self.assertIn("runtime.write", body["error"]["message"])

    def test_discovery_stays_open_but_reveals_no_state(self) -> None:
        """An unauthenticated client may learn the tool names, nothing more."""

        with AsgiHarness(self.app) as client:
            listed = rpc(client, "tools/list").json()
            self.assertIn("tools", listed["result"])
            self.assertEqual(-32001, tool(client, "list_runs", {}, actor=None)["error"]["code"])


if __name__ == "__main__":
    unittest.main()
