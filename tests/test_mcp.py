"""M3.E: the restored `/mcp` contract.

`external_integrations.json` records /mcp as `retain_and_rewrite` with a named
contract test list: initialize, discovery, read-only inspect, an authorized
write, an unauthorized call, and a version conflict. Each is a test here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from orbit.web.mcp import HARNESS_TOOL_NAMES, McpSessionRegistry
from orbit.web.mcp_app import ORBIT_WORKFLOWS_URI
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
    """The object the tool returned, read the way its consumers read it.

    `structuredContent`, not the text block. Both carried the whole payload
    for a while and this reached for whichever was easier; they are no longer
    the same thing for a tool bound to an App card, whose text block is a
    summary so a host does not print the answer twice. `integration-core`
    has always read this field (`gateway.ts`), so this is what the assertions
    below are about.
    """

    return result["result"]["structuredContent"]


class HandshakeTests(ApiTestCase):
    def test_private_agent_tool_backend_is_not_an_mcp_endpoint(self) -> None:
        with AsgiHarness(self.app) as client:
            listed = client.request(
                "POST", "/internal/v1/agent-tools", actor="reader",
                body={"operation": "list"},
            ).json()
            refused = client.request(
                "POST", "/internal/v1/agent-tools", actor="reader",
                body={"jsonrpc": "2.0", "method": "tools/list"},
            )

        self.assertIn("tools", listed["result"])
        self.assertEqual(400, refused.status_code)
        self.assertEqual("unknown operation", refused.json()["error"])

    def test_initialize_reports_protocol_and_server(self) -> None:
        with AsgiHarness(self.app) as client:
            body = rpc(client, "initialize", {}, actor="reader").json()
            self.assertEqual("2.0", body["jsonrpc"])
            self.assertEqual("orbit", body["result"]["serverInfo"]["name"])
            self.assertIn("tools", body["result"]["capabilities"])
            self.assertIn("resources", body["result"]["capabilities"])

    def test_dashboard_resource_is_discoverable_and_readable(self) -> None:
        with AsgiHarness(self.app) as client:
            listed = rpc(client, "resources/list", actor="reader").json()
            resources = listed["result"]["resources"]
            self.assertEqual(
                {
                    "ui://orbit/current-task-v30.html", "ui://orbit/workflows-v10.html",
                    "ui://orbit/workflow-authoring-v5.html", "ui://orbit/goal-run-v11.html",
                    "ui://orbit/goals-v5.html",
                },
                {resource["uri"] for resource in resources},
            )
            for resource in resources:
                self.assertEqual("text/html;profile=mcp-app", resource["mimeType"])
                read = rpc(
                    client, "resources/read", {"uri": resource["uri"]}, actor="reader",
                ).json()
                content = read["result"]["contents"][0]
                self.assertIn("'ui/initialize'", content["text"])
                self.assertIn("openPromptEditor", content["text"])
                self.assertIn("dispatchPrompt", content["text"])

    def test_the_catalogue_is_offered_twice_with_and_without_a_card(self) -> None:
        """One reading, two offers: look at this, or work something out.

        A host that mounts App cards prints the card *and* the JSON under it,
        so a tool that draws a card answers twice. Shortening every such text
        block is not the way out — the model reads it too, and with the names
        gone it goes and fetches each workflow separately, mounting a card for
        every one. The way out is to let the question choose: `list_workflows`
        is what a person asked to see, `inspect_workflows` is what the model
        reads to pick or filter, and only the first is bound to a resource.

        That pair is what makes the one summary in `SUMMARISED_TOOLS` safe: the
        listing's text block can stand down because the reading it carried is
        still offered, by name, one call away.

        The pair reads the same catalogue. Anything else would be two answers
        to one question, which is worse than two ways to ask it.
        """

        with AsgiHarness(self.app) as client:
            declared = {
                item["name"]: item
                for item in rpc(client, "tools/list", actor="reader")
                .json()["result"]["tools"]
            }
            carded = (declared["list_workflows"].get("_meta") or {}).get("ui")
            plain = (declared["inspect_workflows"].get("_meta") or {}).get("ui")
            self.assertEqual(ORBIT_WORKFLOWS_URI, carded["resourceUri"])
            self.assertIsNone(plain)

            # Each points a reader at the other, because a description is the
            # only thing choosing between them.
            self.assertIn(
                "inspect_workflows", declared["list_workflows"]["description"],
            )
            self.assertIn(
                "without opening", declared["inspect_workflows"]["description"],
            )

            self.assertEqual(
                payload_of(tool(client, "list_workflows", {}, actor="reader")),
                payload_of(tool(client, "inspect_workflows", {}, actor="reader")),
            )
            self.assertEqual(
                payload_of(
                    tool(client, "list_workflows", {"ready_only": True}, actor="reader")
                ),
                payload_of(
                    tool(client, "inspect_workflows", {"ready_only": True}, actor="reader")
                ),
            )

    def test_the_card_bound_listing_does_not_repeat_itself_as_text(self) -> None:
        """The card is the answer; the text beside it says so and stands down.

        A host that mounts App cards prints the card and the JSON under it.
        This is the second attempt at not answering twice — the first cut the
        text on every card-bound tool at once and the model, which reads that
        block, went and fetched each workflow separately. What is different
        now is that `inspect_workflows` exists and the summary names it, so
        there is somewhere to go for the values.

        One tool for now. Whether the model takes that route is being watched
        before this is offered to the other five.
        """

        with AsgiHarness(self.app) as client:
            carded = tool(client, "list_workflows", {}, actor="reader")["result"]
            plain = tool(client, "inspect_workflows", {}, actor="reader")["result"]

            # The data is untouched, and it is the same data.
            self.assertEqual(plain["structuredContent"], carded["structuredContent"])

            # Length first, so turning the summary off reads as "the text is
            # the payload again" rather than as a KeyError further down.
            text = carded["content"][0]["text"]
            self.assertLess(len(text), 300, f"text block is not a summary: {text[:80]}")
            summary = json.loads(text)
            self.assertEqual(
                len(carded["structuredContent"]["workflows"]),
                summary["shown_in_card"],
            )
            self.assertIn("inspect_workflows", summary["note"])

            # The twin still answers in full: it draws nothing, so there is
            # nothing beside it to repeat.
            self.assertEqual(
                plain["structuredContent"],
                json.loads(plain["content"][0]["text"]),
            )

    def test_resource_templates_are_an_empty_list_not_an_error(self) -> None:
        """Declaring `resources` is a promise to answer how they are addressed.

        Orbit's five are fixed `ui://` documents with nothing templated about
        them, so the honest answer is an empty list. METHOD_NOT_FOUND is what
        a host reads as a resource surface that does not work, and it takes
        the panels down with it — observed against WorkBuddy 5.4.2, which asks
        for this immediately after `resources/list`.
        """

        with AsgiHarness(self.app) as client:
            body = rpc(client, "resources/templates/list", actor="reader").json()
            self.assertNotIn("error", body)
            self.assertEqual([], body["result"]["resourceTemplates"])

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
                    "get_capabilities", "list_runs", "inspect_run", "start_run", "resume_run",
                    "open_orbit_dashboard", "open_orbit_goals",
                    "list_runtime_events", "get_run_steps", "get_run_graph",
                    "get_run_edges", "read_run_output",
                    "recover_run", "cancel_run", "replay_langgraph_run",
                    "list_workflows", "inspect_workflows",
                    "get_workflow_definition",
                    "inspect_workflow_definition", "delete_workflow", "list_agents",
                    "list_artifacts", "read_artifact",
                    "read_artifact_content",
                    "get_artifact_lineage", "collect_artifacts",
                    "generate_workflow", "modify_workflow", "get_authoring_job",
                    "list_authoring_jobs", "read_authoring_output",
                    "register_authoring_client",
                    "claim_authoring_request", "wait_authoring_request",
                    "submit_authoring_response",
                },
                names,
            )
            for item in tools:
                self.assertIn("inputSchema", item)
                self.assertEqual({"type": "object"}, item["outputSchema"])
                # The scope is an internal authorisation detail, not part of
                # the advertised tool contract.
                self.assertNotIn("scope", item)
            dashboard = next(
                item for item in tools if item["name"] == "open_orbit_dashboard"
            )
            self.assertEqual(
                "ui://orbit/current-task-v30.html",
                dashboard["_meta"]["ui"]["resourceUri"],
            )
            self.assertEqual(
                {
                    "open_orbit_dashboard": "ui://orbit/current-task-v30.html",
                    "open_orbit_goals": "ui://orbit/goals-v5.html",
                },
                {
                    item["name"]: item["_meta"]["ui"]["resourceUri"]
                    for item in tools if item["name"].startswith("open_orbit_")
                },
            )
            card_bindings = {
                item["name"]: item.get("_meta", {}).get("ui", {}).get("resourceUri")
                for item in tools
            }
            self.assertEqual("ui://orbit/workflows-v10.html", card_bindings["list_workflows"])
            self.assertEqual(
                "ui://orbit/workflows-v10.html",
                card_bindings["get_workflow_definition"],
            )
            self.assertIsNone(card_bindings["inspect_workflow_definition"])
            self.assertEqual("ui://orbit/workflow-authoring-v5.html", card_bindings["generate_workflow"])
            self.assertEqual("ui://orbit/goal-run-v11.html", card_bindings["start_run"])
            self.assertEqual("ui://orbit/goals-v5.html", card_bindings["open_orbit_goals"])

    def test_app_resources_request_borderless_host_chrome(self) -> None:
        with AsgiHarness(self.app) as client:
            listed = rpc(
                client, "resources/list", actor="reader",
            ).json()["result"]["resources"]
            detail = next(
                item for item in listed
                if item["uri"] == "ui://orbit/workflows-v10.html"
            )
            self.assertFalse(detail["_meta"]["ui"]["prefersBorder"])
            self.assertFalse(detail["_meta"]["openai/widgetPrefersBorder"])

            read = rpc(
                client, "resources/read",
                {"uri": "ui://orbit/workflows-v10.html"}, actor="reader",
            ).json()["result"]["contents"][0]
            self.assertFalse(read["_meta"]["ui"]["prefersBorder"])
            self.assertFalse(read["_meta"]["openai/widgetPrefersBorder"])

    def test_workbuddy_prompt_modes_distinguish_editable_and_direct_actions(self) -> None:
        with AsgiHarness(self.app) as client:
            workflows = rpc(
                client, "resources/read",
                {"uri": "ui://orbit/workflows-v10.html"}, actor="reader",
            ).json()["result"]["contents"][0]["text"]
            dashboard = rpc(
                client, "resources/read",
                {"uri": "ui://orbit/current-task-v30.html"}, actor="reader",
            ).json()["result"]["contents"][0]["text"]

            self.assertIn("dispatchPromptValue(`使用工作流", workflows)
            self.assertIn("hostProvidesPromptEditor()", workflows)
            self.assertIn("mode==='direct'||hostProvidesPromptEditor()", workflows)
            self.assertIn('data-prompt-mode="edit"', workflows)
            self.assertIn('data-prompt-mode="direct"', dashboard)
            self.assertIn("mode==='direct'||hostProvidesPromptEditor()", dashboard)
            for editable in (
                "action(t().createWorkflow,t().promptCreateWorkflow,'edit')",
                "action(t().handle,t().promptHandle(run),'edit',true)",
                'data-prompt="${esc(t().promptAddAgent)}" data-prompt-mode="edit"',
            ):
                self.assertIn(editable, dashboard)
            for direct in (
                "action(t().history,t().promptHistory,'direct')",
                "action(t().open,t().promptOpen,'direct')",
                "action(t().cancel,t().promptCancel(run.run_id),'direct')",
                "action(t().explain,t().promptExplain(run.run_id),'direct',true)",
            ):
                self.assertIn(direct, dashboard)

    def test_harness_profile_carries_the_writer_surface_but_not_the_ops_one(self) -> None:
        """What a Harness needs to ask for a Workflow, and to write one.

        The broker half used to be excluded outright: a Harness asked for
        Workflows and a forked Agent CLI wrote them. It is here now because the
        Runtime prefers a writer that is already connected over one it has to
        fork, and a client only counts as connected while it is *waiting* on
        the authoring queue. Without `wait_authoring_request` the Harness can
        never be on that queue, so the preference has nothing to prefer and
        every Workflow is written by a CLI nobody can watch.

        The registration tool is presence-only, while
        `claim_authoring_request` is the
        non-blocking variant, and a surface that offers both ways to take the
        same work invites a client that polls when it could be waiting — which
        is the thing that leaves gaps in its own presence.
        """

        from orbit.web.app import create_app
        from tests.test_api_v1 import SCHEMAS, transform_registration
        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE

        app = create_app(
            self.db.parent / "harness-profile.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE]),
            mcp_tool_profile="harness",
            langgraph_state_directory=self.db.parent / "harness-langgraph",
        )
        with AsgiHarness(app) as client:
            tools = rpc(client, "tools/list", actor="reader").json()["result"]["tools"]
        self.assertEqual(
            {
                "get_capabilities", "list_workflows", "inspect_workflows",
                "get_workflow_definition", "inspect_workflow_definition",
                "delete_workflow",
                "list_agents", "list_runs", "inspect_run",
                "generate_workflow", "modify_workflow", "get_authoring_job",
                "list_authoring_jobs", "read_authoring_output",
                "list_runtime_events", "get_run_steps", "get_run_graph",
                "get_run_edges", "read_run_output",
                "replay_langgraph_run", "start_run", "resume_run",
                "cancel_run", "list_artifacts", "read_artifact",
                "read_artifact_content",
                "get_artifact_lineage",
                "register_authoring_client", "wait_authoring_request",
                "submit_authoring_response",
            },
            {item["name"] for item in tools},
        )
        self.assertNotIn(
            "claim_authoring_request", {item["name"] for item in tools},
            "one way onto the queue, and it is the one that waits",
        )

    def test_capability_handshake_identifies_profile_and_protocol(self) -> None:
        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(
                client, "get_capabilities", {}, actor="reader",
            ))
        self.assertEqual("orbit-harness/1", payload["integration_protocol"])
        self.assertEqual("full", payload["tool_profile"])
        self.assertIn("langgraph_run/1", payload["event_schemas"])


class ToolCallTests(ApiTestCase):
    def _start(self, client, key="mcp-1"):
        result = tool(
            client, "start_run",
            {"workflow_id": "workflow:linear", "input": {"value": 0},
             "idempotency_key": key},
            actor="writer",
        )
        return payload_of(result)

    def test_list_runs_shows_the_Workspace_whoever_is_asking(self) -> None:
        """A Runtime serves one Workspace, and that is the whole of visibility.

        This asserted that a second actor saw none of it, while the panel asked
        for `owner=workspace` and saw all of it, and Orbit's own UI could not
        ask at all — two rules over one database, and a UI that showed
        twenty-five of thirty-five Runs with no sign the rest existed.
        """

        with AsgiHarness(self.app) as client:
            self._start(client, key="mine")
            mine = payload_of(tool(client, "list_runs", {}, actor="writer"))["runs"]
            others = payload_of(tool(client, "list_runs", {}, actor="reader"))["runs"]

        self.assertEqual(1, len(mine))
        self.assertEqual(
            [run["run_id"] for run in mine], [run["run_id"] for run in others],
            "a second actor is not a second Workspace",
        )

    def test_list_agents_reports_identity_and_attempt_totals_only(self) -> None:
        """The Agents surface, mirroring the HTTP catalog it stands in for.

        Identity and aggregate outcomes only. A caller that could read a Handler's config schema or its
        required secrets from here would be reading the makings of a command
        this Runtime exists to keep it from composing.
        """

        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(client, "list_agents", {}, actor="reader"))

        self.assertIn("agents", payload)
        for agent in payload["agents"]:
            self.assertTrue(agent["name"].startswith("agent."))
            self.assertEqual(
                {"name", "version", "node_kinds", "attempt_count", "failed_count"},
                set(agent),
            )
            self.assertEqual(0, agent["attempt_count"])
            self.assertEqual(0, agent["failed_count"])

    def test_list_agents_answers_none_rather_than_refusing_without_a_registry(self) -> None:
        """An unsealed registry is a startup state, not a caller's problem."""

        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(client, "list_agents", {}, actor="reader"))

        self.assertEqual([], payload["agents"])

    def test_read_only_tool_works_for_a_reader(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(client, "list_runs", {}, actor="reader")
            self.assertFalse(result["result"]["isError"])
            self.assertEqual([], payload_of(result)["runs"])

    def test_dashboard_tool_returns_the_initial_current_task_projection(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(client, "open_orbit_dashboard", {}, actor="reader")

        payload = payload_of(result)
        self.assertEqual([], payload["runs"])

    def test_write_tool_starts_a_run(self) -> None:
        with AsgiHarness(self.app) as client:
            started = self._start(client)
            self.assertTrue(started["run_id"].startswith("langgraph_run:"))

    def test_a_run_may_be_cancelled_by_someone_who_did_not_start_it(self) -> None:
        """Acting on a Run is bounded by the Workspace, as looking at it is.

        The panel is a view of the Workspace and offers each Run the commands
        that Run advertises. While `cancel` filtered by owner, pressing one on
        a Run from another Session got "not found" for something plainly on
        screen — and a Run whose Session had ended could not be answered by
        anyone, which is how an approval sat in a panel for ten days.
        """

        with AsgiHarness(self.app) as client:
            started = self._start(client, key="started-by-writer")
            answer = tool(client, "cancel_run", {
                "run_id": started["run_id"],
                "expected_version": started["revision"],
                "idempotency_key": "cancelled-by-another",
            }, actor="second-writer")

        payload = payload_of(answer)
        # Either it cancelled, or the Run had already moved on and this is a
        # revision conflict. What it must never be again is unfindable.
        self.assertNotIn("not found", json.dumps(payload, ensure_ascii=False))
        if not answer["result"]["isError"]:
            self.assertEqual("cancelled", payload["status"])

    def test_start_exposes_goal_wait_and_the_public_run_projection(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "start_run",
                {
                    "workflow_id": "workflow:linear",
                    "input": {"value": 0},
                    "goal": "check the login flow",
                    "wait": False,
                    "idempotency_key": "mcp-goal-wait",
                },
                actor="writer",
            )
            started = payload_of(result)
            self.assertEqual(started, result["result"]["structuredContent"])
            self.assertEqual("check the login flow", started["goal"])
            # The request beside the label given to it. A goal is routinely a
            # summary of the inputs rather than a copy of them, so a reader
            # given only the goal cannot see what was actually asked for.
            self.assertEqual({"value": 0}, started["inputs"])
            self.assertEqual(
                {
                    "goal", "inputs", "artifact_count", "workflow_id",
                    "workflow_version", "template_id", "agent_binding", "status",
                    "revision", "result", "interrupts", "error", "created_at",
                    "updated_at", "allowed_commands", "run_id",
                },
                set(started),
            )
            self.assertNotIn("owner_actor", started)
            self.assertIn(
                "langgraph_run.cancel",
                {item["command"] for item in started["allowed_commands"]},
            )

    def test_wait_must_be_boolean(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "start_run",
                {
                    "workflow_id": "workflow:linear", "wait": "false",
                    "idempotency_key": "bad-wait",
                },
                actor="writer",
            )
            self.assertTrue(result["result"]["isError"])
            self.assertIn("wait must be", payload_of(result)["error"])

    def test_deferred_start_rejects_bad_inputs_before_creating_a_run(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "start_run",
                {
                    "workflow_id": "workflow:linear", "input": {"nope": 1},
                    "wait": False, "idempotency_key": "bad-deferred-input",
                },
                actor="writer",
            )
            self.assertTrue(result["result"]["isError"])
            self.assertIn("unknown workflow inputs", payload_of(result)["error"])
            self.assertEqual(
                [], payload_of(tool(client, "list_runs", {}, actor="writer"))["runs"],
            )

    def test_repeating_the_key_replays_instead_of_duplicating(self) -> None:
        with AsgiHarness(self.app) as client:
            first = self._start(client, key="same")
            second = self._start(client, key="same")
            self.assertEqual(first["run_id"], second["run_id"])

    def test_inspect_answers_why(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(client, key="inspect")["run_id"]
            # A run is scoped to the actor who started it.
            body = payload_of(tool(client, "inspect_run", {"run_id": run_id}, actor="writer"))
            self.assertEqual(run_id, body["run_id"])
            self.assertIn("status", body)

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

    def test_a_workflow_id_without_its_kind_prefix_still_starts(self) -> None:
        """The id an Agent passes back is the id it was shown, minus `workflow:`.

        Every surface reports the namespaced form, and a model reads the kind
        as a label rather than part of the value. This used to fail as a
        missing *version*, which is the one thing that was not wrong.
        """

        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(
                client, "start_run",
                {"workflow_id": "linear", "input": {"value": 0},
                 "idempotency_key": "bare-id"},
                actor="writer",
            ))
        self.assertEqual("workflow:linear", payload["workflow_id"])

    def test_an_unknown_workflow_is_not_reported_as_a_missing_version(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "start_run",
                {"workflow_id": "workflow:nope", "workflow_version": 2,
                 "idempotency_key": "gone"},
                actor="writer",
            )
        self.assertTrue(result["result"]["isError"])
        self.assertIn("workflow not found", payload_of(result)["error"])

    def test_a_missing_argument_is_reported_to_the_caller(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(client, "start_run", {"workflow_id": "workflow:linear"}, actor="writer")
            self.assertTrue(result["result"]["isError"])

    def test_the_harness_profile_carries_what_the_Host_actually_calls(self) -> None:
        """A tool in the profile is the Host's only way to reach the Runtime.

        The Host's authoring loop parks on the queue with
        `wait_authoring_request` and answers with `submit_authoring_response`.
        Standing on that queue is what makes it a writer Orbit prefers over
        forking an Agent CLI — being connected is not enough, waiting is what
        counts. A profile that omits the wait leaves the Host permanently off
        the queue, and the preference silently never fires.

        The delegation tools that were already here are a different mechanism
        with confusingly similar names: `claim_delegation` is about a person
        confirming what an external Agent did, not about writing a Workflow.
        """

        for name in ("wait_authoring_request", "submit_authoring_response"):
            self.assertIn(name, HARNESS_TOOL_NAMES, name)

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
            # How big a thing this is: one integer, so a listing can size a
            # workflow up without asking for the graph that would say it.
            self.assertEqual(4, entry["node_count"])
            # The graph and the handler bindings stay out: an agent choosing
            # what to run does not read a page of JSON to do it.
            self.assertNotIn("graph", entry)
            self.assertNotIn("definition", entry)

    def publish_reversed_workflow(self) -> None:
        """A workflow authored bottom-up: terminal first, entry last.

        Nothing stops an author — or a generating Agent — from writing the
        nodes in any order, and the store keeps the order it was given. A
        reader meeting the steps in that order meets the end before the
        beginning, which is what the definition tool is here to prevent.
        """

        from orbit.workflow.dsl import compile_source
        from orbit.workflow.catalogs import (
            InMemoryHandlerCatalog, InMemorySchemaCatalog,
        )
        from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
        from tests.test_api_v1 import SCHEMAS, transform_registration

        dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "reversed", "name": "Written bottom-up"},
            "nodes": [
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                },
                {
                    "id": "second", "kind": "action",
                    "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                    "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                    "handler": {"name": "transform", "version": "1.0.0"},
                },
                {
                    "id": "first", "kind": "action",
                    "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                    "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                    "handler": {"name": "transform", "version": "1.0.0"},
                },
            ],
            "edges": [
                {
                    "id": "a", "from": {"node": "first", "port": "value"},
                    "to": {"node": "second", "port": "value"},
                },
                {
                    "id": "b", "from": {"node": "second", "port": "value"},
                    "to": {"node": "done", "port": "value"},
                },
            ],
            "entry": ["first"], "terminals": ["done"],
        }
        registration = transform_registration()
        compiled = compile_source(
            json.dumps(dsl), InMemoryHandlerCatalog([registration.manifest]),
            InMemorySchemaCatalog(SCHEMAS), source_format="json",
        )
        SQLiteWorkflowVersionStore(self.db).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=json.dumps(dsl), actor="mcp-test",
        )

    def test_a_workflow_reads_as_its_steps_in_the_order_they_happen(self) -> None:
        """What `list_workflows` deliberately leaves out, asked for one at a time."""

        self.publish_reversed_workflow()
        with AsgiHarness(self.app) as client:
            payload = payload_of(tool(
                client, "get_workflow_definition",
                {"workflow_id": "workflow:reversed"}, actor="reader",
            ))

        self.assertEqual(
            [("first", "action"), ("second", "action"), ("done", "terminal")],
            [(node["node_id"], node["kind"]) for node in payload["nodes"]],
        )
        self.assertEqual("workflow:reversed", payload["workflow_id"])
        self.assertEqual("Written bottom-up", payload["name"])
        self.assertEqual(1, payload["latest_version"])
        self.assertEqual("structured", payload["input_mode"])
        self.assertEqual(["value"], [item["id"] for item in payload["inputs"]])
        self.assertIn("goal_readiness", payload)
        self.assertIn("goal_binding", payload)
        self.assertEqual(
            [("first", "second"), ("second", "done")],
            [(edge["from"], edge["to"]) for edge in payload["graph"]["edges"]],
        )
        self.assertEqual("outline", payload["graph"]["layout"]["mode"])
        self.assertEqual("transform", payload["nodes"][0]["handler"])
        # A step that runs nothing says so rather than naming a handler it
        # does not have.
        self.assertIsNone(payload["nodes"][2]["handler"])

    def test_a_missing_workflow_is_an_error_not_an_empty_definition(self) -> None:
        with AsgiHarness(self.app) as client:
            body = tool(
                client, "get_workflow_definition",
                {"workflow_id": "workflow:not-here"}, actor="reader",
            )
        self.assertTrue(body["result"]["isError"])

    def test_delete_requires_write_scope_version_and_is_idempotent(self) -> None:
        self.publish_reversed_workflow()
        arguments = {
            "workflow_id": "workflow:reversed", "expected_version": 1,
            "idempotency_key": "delete-reversed",
        }
        with AsgiHarness(self.app) as client:
            refused = tool(client, "delete_workflow", arguments, actor="reader")
            first = payload_of(tool(
                client, "delete_workflow", arguments, actor="writer",
            ))
            repeated = payload_of(tool(
                client, "delete_workflow", arguments, actor="writer",
            ))
            listed = payload_of(tool(client, "list_workflows", {}, actor="reader"))

        self.assertEqual(-32001, refused["error"]["code"])
        self.assertEqual(first, repeated)
        self.assertTrue(first["deleted"])
        self.assertNotIn(
            "workflow:reversed", {item["workflow_id"] for item in listed["workflows"]},
        )

    def test_delete_refuses_a_workflow_its_own_authoring_is_still_writing(self) -> None:
        """`/api/v1` has always refused this; this door went straight through.

        An Agent could delete the Workflow its own `modify` job was part-way
        into. The job then failed on a retired id — which is the outcome the
        job service re-checks for as a rare restart-gap, made the ordinary
        result of using this tool.
        """

        from datetime import datetime, timedelta, timezone

        from orbit.workflow.persistence.database import connect_workflow_database

        self.publish_reversed_workflow()
        later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with connect_workflow_database(self.db) as connection:
            connection.execute(
                "INSERT INTO workflow_authoring_jobs(job_id,job_type,actor,"
                "workflow_id,prompt,mode,status,idempotency_key,deadline_at,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "authoring_job:busy", "modify", "writer", "workflow:reversed",
                    "make it shorter", "modify", "running", "busy-key", later,
                    later, later,
                ),
            )
            connection.commit()

        with AsgiHarness(self.app) as client:
            refused = tool(client, "delete_workflow", {
                "workflow_id": "workflow:reversed", "expected_version": 1,
                "idempotency_key": "delete-while-writing",
            }, actor="writer")
            listed = payload_of(tool(client, "inspect_workflows", {}, actor="reader"))

        self.assertTrue(refused["result"]["isError"], refused)
        self.assertIn("authoring is still active", json.dumps(payload_of(refused)))
        self.assertIn(
            "workflow:reversed", {item["workflow_id"] for item in listed["workflows"]},
        )

    def test_a_deleted_workflow_still_reads_for_the_runs_that_carry_it(self) -> None:
        """Deleting retires an id; it does not retract the Runs holding it.

        Those Runs go on executing and being read, and a reader drawing one has
        the workflow id and nothing else to name it by. While the point read
        refused a deleted id, every such Run was drawn as `workflow:wf_…` — an
        execution target that reads as missing beside work that plainly is not.
        So the catalogue stops offering it, because a catalogue is an offer to
        start something, and the read by id keeps answering and says which it
        is answering about.
        """

        self.publish_reversed_workflow()
        with AsgiHarness(self.app) as client:
            before = payload_of(tool(
                client, "inspect_workflow_definition",
                {"workflow_id": "workflow:reversed"}, actor="reader",
            ))
            tool(client, "delete_workflow", {
                "workflow_id": "workflow:reversed", "expected_version": 1,
                "idempotency_key": "delete-then-read",
            }, actor="writer")
            after = payload_of(tool(
                client, "inspect_workflow_definition",
                {"workflow_id": "workflow:reversed"}, actor="reader",
            ))
            catalogue = payload_of(tool(client, "inspect_workflows", {}, actor="reader"))
            started = tool(client, "start_run", {
                "workflow_id": "workflow:reversed", "input": {"value": 0},
                "idempotency_key": "start-deleted",
            }, actor="writer")

        self.assertFalse(before["archived"])
        self.assertTrue(after["archived"])
        # The same reading as before, name and steps included: this is what a
        # Run's row needs in order to say anything but the id.
        self.assertEqual(before["name"], after["name"])
        self.assertEqual(before["nodes"], after["nodes"])
        self.assertNotIn(
            "workflow:reversed",
            {item["workflow_id"] for item in catalogue["workflows"]},
        )
        # Readable is not startable, and the refusal says which of the two.
        self.assertTrue(started["result"]["isError"])
        self.assertIn("deleted", payload_of(started)["error"])

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
                client, "inspect_run", {"run_id": started["run_id"]},
                actor="writer",
            ))

            self.assertEqual("completed", payload["status"])
            self.assertEqual([], payload["interrupts"])

    def test_harness_can_incrementally_read_its_events_and_steps(self) -> None:
        with AsgiHarness(self.app) as client:
            started = payload_of(tool(
                client, "start_run",
                {"workflow_id": "workflow:linear", "input": {"value": 0},
                 "idempotency_key": "mcp-events-1"}, actor="author",
            ))
            events = payload_of(tool(
                client, "list_runtime_events",
                {"after_position": 0}, actor="author",
            ))
            steps = payload_of(tool(
                client, "get_run_steps", {"run_id": started["run_id"]},
                actor="author",
            ))
            graph = payload_of(tool(
                client, "get_run_graph", {"run_id": started["run_id"]},
                actor="author",
            ))
            edges = payload_of(tool(
                client, "get_run_edges", {"run_id": started["run_id"]},
                actor="author",
            ))
            output = payload_of(tool(
                client, "read_run_output",
                {"run_id": started["run_id"], "after": 0}, actor="author",
            ))

        self.assertTrue(events["events"])
        self.assertEqual(events["events"][-1]["position"], events["next_position"])
        self.assertTrue(steps["steps"])
        self.assertIn("nodes", graph["graph"])
        self.assertTrue(edges["edges"])
        self.assertEqual(0, output["after"])
        self.assertFalse(output["has_more"])

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

    def test_artifact_content_is_bounded_and_base64_encoded(self) -> None:
        from orbit.workflow.domain.data import PortTransport

        store = self.app.state.langgraph_service.artifacts
        policy = SimpleNamespace(
            transport=PortTransport.ARTIFACT_REF,
            content_types=("text/plain",), max_size_bytes=100,
        )
        port = SimpleNamespace(id="document", schema_id="text/1", data_policy=policy)
        access = store.access(
            run_id="run:artifact-content", node_id="write", attempt_id="attempt:1",
            output_ports=(port,), inputs={}, actor="reader",
        )
        artifact_id = access.write(
            name="document", content=b"hello", content_type="text/plain",
            filename="hello.txt",
        )
        store.commit(access.produced_artifact_ids)

        with AsgiHarness(self.app) as client:
            content = payload_of(tool(
                client, "read_artifact_content", {"artifact_id": artifact_id},
                actor="reader",
            ))
            too_small = tool(
                client, "read_artifact_content",
                {"artifact_id": artifact_id, "max_bytes": 4}, actor="reader",
            )

        self.assertEqual("base64", content["encoding"])
        self.assertEqual("aGVsbG8=", content["content"])
        self.assertTrue(too_small["result"]["isError"])


class AuthoringToolTests(ApiTestCase):
    """This composition wires no authoring agent, so the tools say so."""

    def test_an_unconfigured_runtime_lists_no_authoring_jobs(self) -> None:
        with AsgiHarness(self.app) as client:
            result = payload_of(tool(
                client, "list_authoring_jobs", {"limit": 5}, actor="reader",
            ))
        self.assertEqual([], result["jobs"])

    def test_authoring_console_requires_sensitive_read_scope(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "read_authoring_output",
                {"job_id": "authoring_job:none"}, actor="reader",
            )
        self.assertEqual(-32001, result["error"]["code"])

    def test_generation_reports_that_it_is_not_configured(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "generate_workflow",
                {"prompt": "summarise a document", "idempotency_key": "g1"},
                actor="writer",
            )

            self.assertTrue(result["result"]["isError"])
            self.assertIn("not configured", payload_of(result)["error"])

    def test_modification_reports_that_it_is_not_configured(self) -> None:
        with AsgiHarness(self.app) as client:
            result = tool(
                client, "modify_workflow",
                {"workflow_id": "workflow:one", "prompt": "add approval", "idempotency_key": "m1"},
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

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        broker = ExternalAuthoringBroker()
        app = create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
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

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
            poll_seconds=0.02,
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

    def run_stdio(self, *messages, actor="writer", actor_prefix=None):
        import io

        from orbit.web.mcp import serve_stdio

        sink = io.StringIO()
        serve_stdio(
            self.app.state.mcp_dispatch, actor,
            stdin=io.StringIO("".join(f"{item}\n" for item in messages)),
            stdout=sink,
            actor_prefix=actor_prefix,
        )
        return [json.loads(line) for line in sink.getvalue().splitlines()]

    def test_a_session_handshakes_and_lists_the_same_tools(self) -> None:
        responses = self.run_stdio(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
        )

        self.assertEqual(2, len(responses))
        self.assertEqual("orbit", responses[0]["result"]["serverInfo"]["name"])
        self.assertEqual(35, len(responses[1]["result"]["tools"]))

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

        payload = responses[0]["result"]["structuredContent"]
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
            '"params":{"name":"start_run","arguments":'
            '{"workflow_id":"workflow:linear","idempotency_key":"stdio-1"}}}',
            actor="reader",
        )

        self.assertEqual(-32001, responses[0]["error"]["code"])

    def test_trusted_stdio_metadata_can_select_a_scoped_actor(self) -> None:
        responses = self.run_stdio(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"list_runs","arguments":{},"_meta":'
            '{"orbit/actor":"reader"}}}',
            actor="writer", actor_prefix="reader",
        )
        self.assertNotIn("error", responses[0])

    def test_stdio_metadata_cannot_escape_its_actor_prefix(self) -> None:
        responses = self.run_stdio(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"list_runs","arguments":{},"_meta":'
            '{"orbit/actor":"local"}}}',
            actor_prefix="harness:session:",
        )
        self.assertEqual(-32001, responses[0]["error"]["code"])


class HubDoorIdentityTests(unittest.TestCase):
    """The Session that acted is recorded, whichever door it came through.

    A Runtime the Hub launched serves `serve_mcp=False`, so the Hub reaches it
    at `/internal/v1/agent-tools` and forwards the actor header there. While
    only `/mcp` accepted that header, every call routed through the Hub
    resolved to the single loopback operator: one name on every Run, every
    cancellation and every authoring job in a Hub-managed Workspace.
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from orbit.web.app import create_app
        from orbit.web.local_identity import (
            local_authorizer, loopback_scoped_mcp_authenticator,
        )
        from tests.test_api_v1 import SCHEMAS, transform_registration
        from tests.test_web_composition import publish_linear_workflow

        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "langgraph"
        db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            # The production pair, which is the point of this test.
            authenticator=lambda request: loopback_scoped_mcp_authenticator(
                request, trusted_prefix="harness:session:",
            ),
            authorizer=local_authorizer(trusted_prefix="harness:session:"),
            single_goal_mode=False,
            langgraph_state_directory=self.state,
        )
        publish_linear_workflow(db)

    def call(self, client, name, arguments, *, actor):
        return client.request(
            "POST", "/internal/v1/agent-tools", actor=actor,
            body={"operation": "call", "name": name, "arguments": arguments},
        ).json()

    def owners(self) -> set[str]:
        import sqlite3

        with sqlite3.connect(self.state / "langgraph-runs.sqlite3") as runs:
            return {row[0] for row in runs.execute(
                "SELECT DISTINCT owner_actor FROM langgraph_runs"
            )}

    def test_a_run_started_through_the_hub_door_belongs_to_its_session(self) -> None:
        session = "harness:session:abc-123"
        with AsgiHarness(self.app) as client:
            answer = self.call(client, "start_run", {
                "workflow_id": "workflow:linear", "input": {"value": 0},
                "idempotency_key": "through-the-hub",
            }, actor=session)

        self.assertNotIn("protocol_error", answer, answer)
        self.assertEqual({session}, self.owners())

    def test_that_door_still_refuses_a_name_outside_the_prefix(self) -> None:
        with AsgiHarness(self.app) as client:
            answer = self.call(client, "list_runs", {}, actor="attacker")

        # No identity at all, so no scopes: the tool is refused rather than
        # served under the loopback operator's name.
        self.assertIn("protocol_error", answer, answer)


class McpSessionRegistryTests(unittest.TestCase):
    """Presence is observed, never declared: silent clients age out."""

    def test_initialize_records_who_the_client_is(self) -> None:
        registry = McpSessionRegistry(clock=lambda: 1000.0)
        registry.observe("agent", "initialize", {
            "clientInfo": {"name": "agent-reach", "version": "1.2.3"},
            "protocolVersion": "2025-06-18",
        })

        (session,) = registry.sessions()
        self.assertEqual("agent", session["session_id"])
        self.assertEqual("agent", session["actor"])
        self.assertEqual(
            {"name": "agent-reach", "version": "1.2.3"}, session["client"],
        )
        self.assertEqual("2025-06-18", session["protocol_version"])
        self.assertEqual(1, session["requests"])
        self.assertTrue(session["connected"])

    def test_a_silent_client_ages_out(self) -> None:
        ticks = [1000.0]
        registry = McpSessionRegistry(
            presence_seconds=30, clock=lambda: ticks[0],
        )
        registry.observe("agent", "ping", {})

        self.assertTrue(registry.sessions()[0]["connected"])
        ticks[0] += 31
        (session,) = registry.sessions()
        self.assertFalse(session["connected"])
        # Aging out is a verdict on the record, not a deletion: the operator
        # still sees who was here and when they went quiet.
        self.assertEqual("ping", session["last_method"])

    def test_anonymous_messages_share_one_session(self) -> None:
        registry = McpSessionRegistry(clock=lambda: 1000.0)
        registry.observe(None, "initialize", {})
        registry.observe(None, "tools/list", {})

        (session,) = registry.sessions()
        self.assertEqual("anonymous", session["session_id"])
        self.assertIsNone(session["actor"])
        self.assertEqual(2, session["requests"])


class McpSessionEndpointTests(ApiTestCase):
    def sessions(self, client, actor="reader"):
        response = client.request("GET", "/api/v1/mcp/sessions", actor=actor)
        self.assertEqual(200, response.status_code)
        return response.json()["data"]

    def test_no_traffic_reports_no_sessions(self) -> None:
        with AsgiHarness(self.app) as client:
            data = self.sessions(client)
            self.assertEqual([], data["sessions"])
            self.assertIn("presence_seconds", data)

    def test_initialize_and_calls_make_a_connected_session(self) -> None:
        with AsgiHarness(self.app) as client:
            rpc(client, "initialize", {
                "clientInfo": {"name": "agent-reach", "version": "1.2.3"},
                "protocolVersion": "2025-06-18",
            }, actor="reader")
            tool(client, "list_workflows", {}, actor="reader")

            (session,) = self.sessions(client)["sessions"]
            self.assertEqual("reader", session["session_id"])
            self.assertEqual(
                {"name": "agent-reach", "version": "1.2.3"}, session["client"],
            )
            self.assertEqual("tools/call", session["last_method"])
            self.assertEqual(2, session["requests"])
            self.assertTrue(session["connected"])
            self.assertIn("connected_at", session)
            self.assertIn("last_seen", session)

    def test_anonymous_handshake_is_visible_as_anonymous(self) -> None:
        with AsgiHarness(self.app) as client:
            rpc(client, "initialize", {})

            (session,) = self.sessions(client)["sessions"]
            self.assertEqual("anonymous", session["session_id"])
            self.assertIsNone(session["actor"])
            self.assertTrue(session["connected"])

    def test_the_endpoint_requires_credentials(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.request("GET", "/api/v1/mcp/sessions")
            self.assertEqual(401, response.status_code)
            response = client.request(
                "GET", "/api/v1/mcp/sessions", actor="nobody",
            )
            self.assertEqual(403, response.status_code)


if __name__ == "__main__":
    unittest.main()


class WorkspaceIsTheBoundaryTests(ApiTestCase):
    """One rule for visibility, and both transports read it from one place.

    They disagreed for as long as both existed: `/mcp` offered `owner` and
    `/api/v1` did not, so what had happened in a Workspace depended on which
    door you came through.
    """

    def test_the_choice_is_gone_from_the_tool_surface(self) -> None:
        with AsgiHarness(self.app) as client:
            tools = rpc(client, "tools/list", actor="reader").json()["result"]["tools"]
        offered = [
            tool["name"] for tool in tools
            if "owner" in (tool["inputSchema"].get("properties") or {})
        ]
        self.assertEqual(
            [], offered,
            "a parameter that changes nothing reads as a control somebody relies on",
        )

    def test_both_transports_answer_from_the_same_rule(self) -> None:
        import inspect

        from orbit.web import api_v1, mcp, run_visibility

        self.assertIsNone(run_visibility.reading_actor("anybody"))
        # Read from the shared module rather than each deciding for itself,
        # which is the arrangement that let them drift in the first place.
        for module in (mcp, api_v1.langgraph_runs):
            source = inspect.getsource(module)
            self.assertIn("run_visibility import reading_actor", source, module.__name__)
            self.assertNotIn("def reading_actor", source, module.__name__)
            self.assertNotIn("def reading_owner", source, module.__name__)
