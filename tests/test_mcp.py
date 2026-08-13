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
                {
                    "list_runs", "inspect_run", "start_run", "cancel_run",
                    "list_workflows", "get_run_result", "list_artifacts",
                    "read_artifact", "list_inbox", "request_human_task_token",
                    "submit_human_task", "generate_workflow",
                    "get_authoring_job", "claim_authoring_request",
                    "wait_authoring_request", "submit_authoring_response",
                    "runtime_status",
                },
                names,
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


class DiscoveryAndResultTests(ApiTestCase):
    """The two halves that made `start_run` usable on its own.

    Before these, an agent had to be told out of band which workflow_id it was
    allowed to run, and had no way to read what the run produced — it could
    start work and never see the answer.
    """

    def test_workflows_are_discoverable_before_starting_one(self) -> None:
        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(client, "list_workflows", {}, actor="reader"))

            entry = next(
                item for item in payload["workflows"]
                if item["workflow_id"] == "workflow:linear"
            )
            self.assertIn("goal_readiness", entry)
            self.assertIn("inputs", entry)
            # The graph and the handler bindings stay out: an agent choosing
            # what to run does not read a page of JSON to do it.
            self.assertNotIn("graph", entry)
            self.assertNotIn("definition", entry)

    def test_ready_only_filters_to_what_a_goal_can_start(self) -> None:
        with AsgiHarness(self.app) as client:
            payload = payload_of(
                tool(client, "list_workflows", {"ready_only": True}, actor="reader")
            )

            self.assertEqual(
                set(), {
                    item["goal_readiness"] for item in payload["workflows"]
                } - {"ready"},
            )

    def test_a_finished_run_reports_its_result(self) -> None:
        with AsgiHarness(self.app) as client:
            started = payload_of(tool(
                client, "start_run",
                {"workflow_id": "workflow:linear", "input": {"value": 0},
                 "idempotency_key": "mcp-result-1"},
                actor="writer",
            ))

            payload = payload_of(tool(
                client, "get_run_result", {"run_id": started["run_id"]},
                actor="reader",
            ))

            self.assertIn("state", payload)

    def test_artifacts_are_listable_and_scoped_to_a_run(self) -> None:
        with AsgiHarness(self.app) as client:
            payload = payload_of(
                tool(client, "list_artifacts", {"run_id": "run:nothing"}, actor="reader")
            )

            self.assertEqual([], payload["artifacts"])

    def test_an_artifact_nobody_may_see_reads_as_missing(self) -> None:
        """Not a different error from one that does not exist."""

        with AsgiHarness(self.app) as client:
            result = tool(
                client, "read_artifact", {"artifact_id": "artifact:none"},
                actor="reader",
            )

            self.assertTrue(result["result"]["isError"])
            self.assertIn("error", payload_of(result))


class OperationsToolTests(ApiTestCase):
    def test_runtime_status_counts_the_runtime_itself(self) -> None:
        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(client, "runtime_status", {}, actor="ops-reader"))

            self.assertEqual(
                {"jobs", "timers", "active_leases", "runs"}, set(payload)
            )

    def test_a_plain_reader_cannot_read_runtime_status(self) -> None:
        """Ops state is a separate scope over MCP exactly as it is over HTTP."""

        with AsgiHarness(self.app) as client:
            body = tool(client, "runtime_status", {}, actor="reader")

            self.assertEqual(-32001, body["error"]["code"])
            self.assertIn("runtime.ops.read", body["error"]["message"])

    def test_the_inbox_lists_what_is_waiting_on_a_person(self) -> None:
        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(client, "list_inbox", {}, actor="reader"))

            self.assertEqual([], payload["items"])
            self.assertEqual(0, payload["action_count"])


class AuthoringToolTests(ApiTestCase):
    """This composition wires no authoring agent, so the tools say so."""

    def test_generation_reports_that_it_is_not_configured(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "generate_workflow",
                {"prompt": "summarise a document", "idempotency_key": "g1"},
                actor="writer",
            )

            self.assertTrue(result["result"]["isError"])
            self.assertIn("not configured", payload_of(result)["error"])


class ClientWrittenWorkflowTests(unittest.TestCase):
    """Generation answered by the connected client instead of a forked CLI.

    A connected App parks its prompt and waits; these two tools are
    the whole exchange, so what matters here is that a client can claim a
    prompt and that the document it writes settles the job it belongs to.
    """

    def build(self):
        import tempfile
        import time
        from pathlib import Path

        from orbit.web.app import create_app
        from orbit.workflow.authoring import ExternalAuthoringBroker
        from tests.test_api_v1 import SCHEMAS, transform_registration
        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        broker = ExternalAuthoringBroker()
        app = create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE]),
            workflow_generators=broker.generators(),
            # No CLI here, and no App has reported itself yet. The broker is
            # the unnamed fallback that keeps authoring wired until one does.
            workflow_generator=broker,
            authoring_broker=broker,
        )
        return app, broker, time

    def test_a_client_claims_the_prompt_and_answers_it(self) -> None:
        app, _broker, time = self.build()
        document = {
            "dsl_version": "1.3",
            "metadata": {"id": "written", "name": "Written by the client"},
            "nodes": [
                {
                    "id": "transform", "kind": "action", "label": "Transform",
                    "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                    "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                    "handler": {"name": "transform", "version": "1.0.0"},
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                },
            ],
            "edges": [{
                "id": "finish", "from": {"node": "transform", "port": "value"},
                "to": {"node": "done", "port": "value"},
            }],
            "entry": ["transform"], "terminals": ["done"],
            "result": {"node": "transform", "port": "value"},
        }

        with AsgiHarness(app) as client:
            # Reporting itself is what makes this App a name an author can
            # pick; nothing is addressable before it has polled once.
            tool(client, "claim_authoring_request", {"client": "cursor"}, actor="writer")
            started = payload_of(tool(
                client, "generate_workflow",
                {
                    "prompt": "double a number", "idempotency_key": "g1",
                    "agent": "cursor",
                },
                actor="writer",
            ))

            deadline = time.monotonic() + 10.0
            request = None
            while request is None and time.monotonic() < deadline:
                request = payload_of(tool(
                    client, "claim_authoring_request", {"client": "cursor"},
                    actor="writer",
                ))["request"]
                if request is None:
                    time.sleep(0.02)
            self.assertIsNotNone(request, "the job never parked a prompt")
            self.assertEqual(started["job_id"], request["job_id"])
            self.assertIn("double a number", request["prompt"])

            # A second poll hands out nothing — one prompt is one client's
            # work — but still names it, so a client that lost the id can
            # answer instead of leaving the job parked until its deadline.
            second = payload_of(tool(
                client, "claim_authoring_request", {"client": "cursor"},
                actor="writer",
            ))
            self.assertIsNone(second["request"])
            self.assertEqual(
                [request["request_id"]],
                [item["request_id"] for item in second["waiting"]],
            )

            accepted = payload_of(tool(
                client, "submit_authoring_response",
                {"request_id": request["request_id"], "dsl": document},
                actor="writer",
            ))
            self.assertTrue(accepted["accepted"])

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                job = payload_of(tool(
                    client, "get_authoring_job",
                    {"job_id": started["job_id"]}, actor="writer",
                ))
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual("done", job["status"])
            self.assertEqual("Written by the client", job["result"]["name"])

    def test_answering_a_request_nobody_parked_is_refused(self) -> None:
        app, _broker, _time = self.build()
        with AsgiHarness(app) as client:
            result = tool(
                client, "submit_authoring_response",
                {"request_id": "authoring_request:nope", "dsl": "{}"},
                actor="writer",
            )
            self.assertTrue(result["result"]["isError"])

    def test_an_idle_runtime_reports_nothing_to_claim(self) -> None:
        app, _broker, _time = self.build()
        with AsgiHarness(app) as client:
            payload = payload_of(tool(
                client, "claim_authoring_request", {}, actor="writer",
            ))
            self.assertIsNone(payload["request"])
            self.assertEqual([], payload["waiting"])
            self.assertEqual([], payload["clients"])

    def test_polling_apps_become_names_an_author_can_pick(self) -> None:
        app, _broker, _time = self.build()
        with AsgiHarness(app) as client:
            def offered():
                body = client.get("/api/v1/capabilities", actor="writer").json()
                return body["data"]["capabilities"]["workflow_generation"]["agents"]

            self.assertEqual([], offered())
            payload = payload_of(tool(
                client, "claim_authoring_request", {"client": "cursor"},
                actor="writer",
            ))
            self.assertEqual(["cursor"], payload["clients"])
            # The capability list is read again per request, so an App that
            # just connected is immediately somewhere work can be sent.
            self.assertEqual(["cursor"], offered())

    def test_an_unaddressable_app_is_refused_when_the_job_is_created(self) -> None:
        app, _broker, _time = self.build()
        with AsgiHarness(app) as client:
            result = tool(
                client, "generate_workflow",
                {
                    "prompt": "anything", "idempotency_key": "g9",
                    "agent": "not-here",
                },
                actor="writer",
            )
            self.assertTrue(result["result"]["isError"])
            self.assertIn("unknown generation agent", payload_of(result)["error"])


class SharedAuthoringServiceTests(unittest.TestCase):
    """MCP and `/api/v1` must dispatch into one AuthoringJobService.

    The service owns in-flight jobs: their cancel scopes, their deadline
    timers, and the recovery that restarts queued work at construction. A
    second instance against the same database therefore runs every queued job
    a second time — two Agent CLI calls for one job — and fails the jobs the
    first instance had just started.
    """

    def test_one_service_serves_both_protocols(self) -> None:
        import tempfile
        from pathlib import Path

        import orbit.workflow.application.authoring_job_service as service_module
        from orbit.web.app import create_app
        from orbit.workflow.application.authoring_job_service import (
            AuthoringJobService,
        )
        from tests.test_api_v1 import SCHEMAS, transform_registration

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        constructed: list[object] = []

        class Counting(AuthoringJobService):
            def __init__(self, *args, **kwargs):
                constructed.append(self)
                super().__init__(*args, **kwargs)

        service_module.AuthoringJobService = Counting
        self.addCleanup(
            setattr, service_module, "AuthoringJobService", AuthoringJobService,
        )

        # A generator is what makes the runtime wire authoring at all; it is
        # never called here, only its presence matters.
        app = create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            workflow_generator=lambda _prompt: "{}",
        )

        self.assertIsNotNone(app.state.mcp_dispatch)
        self.assertEqual(
            1, len(constructed),
            "each extra instance recovers and re-runs every queued job",
        )


class StdioTransportTests(ApiTestCase):
    """The second transport must not be a second implementation.

    Everything below goes through the same dispatcher the HTTP endpoint uses;
    what is being tested is the framing — one JSON object per line, a
    notification answered by silence, and a bad line that does not end the
    session.
    """

    def run_stdio(self, *messages, actor="writer"):
        import io

        from orbit.web.mcp import serve_stdio

        sink = io.StringIO()
        serve_stdio(
            self.app.state.mcp_dispatch, actor,
            stdin=io.StringIO("".join(f"{item}\n" for item in messages)),
            stdout=sink,
        )
        return [json.loads(line) for line in sink.getvalue().splitlines()]

    def test_a_session_handshakes_and_lists_the_same_tools(self) -> None:
        responses = self.run_stdio(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
        )

        self.assertEqual(2, len(responses))
        self.assertEqual("orbit", responses[0]["result"]["serverInfo"]["name"])
        self.assertEqual(17, len(responses[1]["result"]["tools"]))

    def test_a_notification_produces_no_line_at_all(self) -> None:
        """There is no 202 on this transport; silence is the whole answer."""

        responses = self.run_stdio(
            '{"jsonrpc":"2.0","method":"notifications/initialized"}',
            '{"jsonrpc":"2.0","id":1,"method":"ping"}',
        )

        self.assertEqual([1], [item["id"] for item in responses])

    def test_a_tool_call_reaches_the_same_services(self) -> None:
        responses = self.run_stdio(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"list_workflows","arguments":{}}}'
        )

        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(
            ["workflow:linear"],
            [item["workflow_id"] for item in payload["workflows"]],
        )

    def test_a_malformed_line_is_answered_without_ending_the_session(self) -> None:
        responses = self.run_stdio(
            "not json at all",
            '{"jsonrpc":"2.0","id":7,"method":"ping"}',
        )

        self.assertEqual(-32700, responses[0]["error"]["code"])
        self.assertEqual(7, responses[1]["id"])

    def test_the_actor_is_the_one_the_caller_was_started_as(self) -> None:
        """No connection to authenticate: scope still decides."""

        responses = self.run_stdio(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"runtime_status","arguments":{}}}',
            actor="reader",
        )

        self.assertEqual(-32001, responses[0]["error"]["code"])


if __name__ == "__main__":
    unittest.main()
