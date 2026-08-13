"""Generation answered by a connected MCP client instead of a forked CLI.

The broker removes the child process from authoring, so the two facts worth
pinning are the ones the process used to provide for free: a job still settles
on the answer it is given, and a job that is stopped while a prompt is parked
still ends — with an honest verdict about whether anybody had started on it.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from orbit.workflow.authoring import (
    AuthoringUnavailableError, AuthoringUnknownResultError, CancelScope,
    ExternalAuthoringBroker, UnknownAuthoringRequestError, WorkflowAuthoringService,
    cancellable,
)
from orbit.workflow.authoring.external import ReservedClientNameError
from orbit.workflow.application.authoring_job_service import AuthoringJobService

from tests.test_workflow_authoring_jobs import AuthoringJobTestCase, dsl


class ReservedNameTests(unittest.TestCase):
    """An App registers under the name it chooses, and collisions are refused.

    The name used to be decorated `app:<client>`, which made collision
    impossible by making the *kind* of Agent part of its address — something
    an author has no reason to care about, and which every reader of a name
    had to know. Refusing is the same answer an operator-configured writer
    already gets: two writers answering to one name means an author cannot be
    told truthfully which one wrote their workflow.
    """

    def broker(self, *reserved):
        return ExternalAuthoringBroker(reserved_names=lambda: set(reserved))

    def test_a_name_another_agent_answers_to_is_refused(self) -> None:
        broker = self.broker("codex", "claude")
        with self.assertRaises(ReservedClientNameError) as caught:
            broker.claim(actor="local", client="codex")
        self.assertEqual("codex", caught.exception.name)
        self.assertIn("register under a different one", str(caught.exception))
        # Refused means not registered: it must not become addressable.
        self.assertEqual([], broker.clients())

    def test_a_free_name_is_taken_as_given(self) -> None:
        broker = self.broker("codex")
        broker.claim(actor="local", client="cursor")
        self.assertEqual(["cursor"], broker.clients())
        self.assertEqual(["cursor"], sorted(broker.generators()))

    def test_a_registered_client_keeps_polling_without_re_checking(self) -> None:
        """The reserved set is read at registration, not on every claim.

        A CLI discovered *after* an App took the name would otherwise start
        refusing a client mid-session, which is not a thing the App can act
        on and not a moment to lose its queue.
        """

        reserved: set[str] = set()
        broker = ExternalAuthoringBroker(reserved_names=lambda: set(reserved))
        broker.claim(actor="local", client="cursor")
        reserved.add("cursor")
        self.assertIsNone(broker.claim(actor="local", client="cursor"))
        self.assertEqual(["cursor"], broker.clients())


class BrokerJobTests(AuthoringJobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.broker = ExternalAuthoringBroker()
        # Polling is how an App reports itself, and reporting itself is what
        # makes `cursor` a name an author can pick. Nothing is addressable
        # before its App has been heard from.
        self.broker.claim(actor="local", client="cursor")

    def authoring(self) -> WorkflowAuthoringService:
        return WorkflowAuthoringService(
            self.catalogs.handlers, self.catalogs.schemas, self.broker,
            generators=self.broker.generators(),
            handler_facts=[{
                "name": "transform", "version": "1.0.0", "kinds": ["action"],
                "inputs": {"value": "example://integer/1.0"},
                "outputs": {"value": "example://integer/1.0"},
            }],
        )

    def claimed(self, *, actor="local", client="cursor", timeout=10.0):
        """Wait for the job thread to park its prompt, then take it."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            request = self.broker.claim(actor=actor, client=client)
            if request is not None:
                return request
            time.sleep(0.02)
        self.fail("no generation request was ever parked")

    def test_work_addressed_to_one_app_is_never_handed_to_another(self) -> None:
        # `cursor` has to have been seen before an author can address it.
        self.broker.claim(actor="local", client="cursor")
        jobs = self.service()
        created = jobs.create(
            actor="author", prompt="Research", idempotency_key="g1",
            agent="cursor",
        )
        deadline = time.monotonic() + 10.0
        while not self.broker.pending() and time.monotonic() < deadline:
            time.sleep(0.02)

        # Another App polling sees nothing: the author picked one by name, and
        # quietly substituting a different one would make the choice a lie.
        self.assertIsNone(self.broker.claim(actor="local", client="zed"))
        self.assertIsNone(self.broker.claim(actor="local"))

        request = self.claimed(client="cursor")
        self.assertEqual("cursor", request["addressed_to"])
        self.broker.respond(request["request_id"], dsl(), actor="cursor")
        self.assertEqual("done", self.settled(jobs, created["job_id"])["status"])

    def test_an_app_that_never_polled_cannot_be_addressed(self) -> None:
        jobs = self.service()
        with self.assertRaises(Exception) as caught:
            jobs.create(
                actor="author", prompt="Research", idempotency_key="g1",
                agent="never-here",
            )
        # Refused at creation rather than minutes later when the job runs.
        self.assertIn("unknown generation agent", str(caught.exception))

    def test_a_client_written_document_is_compiled_and_published(self) -> None:
        jobs = self.service()
        created = jobs.create(
            actor="author", prompt="Research a topic", idempotency_key="g1",
            agent="cursor",
        )
        request = self.claimed()
        # The client is handed the same prompt a CLI would have received.
        self.assertIn("INSTRUCTION-BEGIN", request["prompt"])
        self.assertIn("Research a topic", request["prompt"])
        self.assertEqual(created["job_id"], request["job_id"])

        self.broker.respond(request["request_id"], dsl(), actor="client")
        job = self.settled(jobs, created["job_id"])
        self.assertEqual("done", job["status"])
        self.assertEqual(1, job["attempts"])

    def test_the_console_narrates_an_exchange_that_has_no_child_process(self) -> None:
        jobs = self.service()
        created = jobs.create(
            actor="author", prompt="Research a topic", idempotency_key="g1",
            agent="cursor",
        )
        request = self.claimed()
        self.broker.respond(request["request_id"], dsl(), actor="client")
        self.settled(jobs, created["job_id"])

        chunks, _cursor = jobs.output(created["job_id"])
        printed = {
            "stdout": "".join(c["text"] for c in chunks if c["stream"] == "stdout"),
            "stderr": "".join(c["text"] for c in chunks if c["stream"] == "stderr"),
        }
        # What was asked, who took it, and what came back — the account a
        # forked CLI gives for free by printing to its pipes.
        self.assertIn("waiting for cursor", printed["stderr"])
        self.assertIn("INSTRUCTION-BEGIN", printed["stderr"])
        self.assertIn("Research a topic", printed["stderr"])
        self.assertIn(f"{request['request_id']} claimed by cursor", printed["stderr"])
        self.assertIn('"dsl_version": "1.3"', printed["stdout"])

    def test_a_rejected_document_comes_back_as_another_request(self) -> None:
        jobs = self.service()
        created = jobs.create(
            actor="author", prompt="Research", idempotency_key="g1",
            agent="cursor",
        )
        first = self.claimed()
        self.broker.respond(first["request_id"], "not a workflow", actor="client")

        # The compiler's refusal is fed back the same way it is to a CLI: a
        # fresh prompt for the same job, carrying what went wrong.
        second = self.claimed()
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(created["job_id"], second["job_id"])
        self.broker.respond(second["request_id"], json.dumps(dsl()), actor="client")
        job = self.settled(jobs, created["job_id"])
        self.assertEqual("done", job["status"])
        self.assertEqual(2, job["attempts"])

    def test_cancelling_a_job_releases_the_prompt_nobody_claimed(self) -> None:
        jobs = self.service()
        created = jobs.create(
            actor="author", prompt="Research", idempotency_key="g1",
            agent="cursor",
        )
        deadline = time.monotonic() + 10.0
        while not self.broker.pending() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.broker.pending())

        jobs.cancel(created["job_id"], actor="author")
        job = self.settled(jobs, created["job_id"])
        self.assertEqual("cancelled", job["status"])
        # The waiting thread let go of the request rather than leaking it.
        deadline = time.monotonic() + 10.0
        while self.broker.pending() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([], self.broker.pending())


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = ExternalAuthoringBroker()

    def park(self, scope: CancelScope | None = None):
        """Run one parked generation on its own thread and report the outcome."""

        outcome: dict = {}

        def run() -> None:
            try:
                if scope is None:
                    outcome["text"] = self.broker("write me a workflow")
                else:
                    with cancellable(scope):
                        outcome["text"] = self.broker("write me a workflow")
            except Exception as exc:  # noqa: BLE001 - the outcome under test
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not self.broker.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        return thread, outcome

    def test_an_idle_broker_hands_out_nothing(self) -> None:
        self.assertIsNone(self.broker.claim(actor="client"))
        self.assertEqual([], self.broker.pending())

    def test_polling_is_what_makes_an_app_addressable(self) -> None:
        self.assertEqual([], self.broker.clients())
        self.broker.claim(actor="local", client="cursor")
        self.broker.claim(actor="local", client="zed")
        self.assertEqual(["cursor", "zed"], self.broker.clients())
        # The generation agent names follow the clients, so an author is only
        # offered Apps that are actually there.
        self.assertEqual(
            ["cursor", "zed"], sorted(self.broker.generators()),
        )

    def test_waiting_client_is_addressable_and_claims_new_work(self) -> None:
        result = {}

        def wait() -> None:
            result["request"] = self.broker.wait_claim(
                actor="local", client="chatgpt", timeout_seconds=2,
            )

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        deadline = time.monotonic() + 2
        while "chatgpt" not in self.broker.clients() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("chatgpt", self.broker.clients())
        generation, outcome = self.park()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        request = result["request"]
        self.assertIsNotNone(request)
        self.assertEqual("write me a workflow", request["prompt"])
        self.broker.respond(request["request_id"], "{}", actor="chatgpt")
        generation.join(timeout=2)
        self.assertEqual("{}", outcome["text"])

    def test_wait_timeout_removes_client_presence(self) -> None:
        self.assertIsNone(self.broker.wait_claim(
            actor="local", client="chatgpt", timeout_seconds=0.01,
        ))
        self.assertEqual([], self.broker.clients())

    def test_an_app_that_stopped_polling_stops_being_offered(self) -> None:
        now = [1000.0]
        broker = ExternalAuthoringBroker(
            presence_seconds=60.0, clock=lambda: now[0],
        )
        broker.claim(actor="local", client="cursor")
        now[0] += 61.0
        self.assertEqual([], broker.clients())
        self.assertEqual([], sorted(broker.generators()))

    def test_a_name_that_could_not_be_an_address_is_refused(self) -> None:
        for name in ("has space", "9leading", "semi;colon", "x" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.broker.claim(actor="local", client=name)

    def test_a_lapsed_claim_returns_the_work_to_the_queue(self) -> None:
        now = [1000.0]
        broker = ExternalAuthoringBroker(lease_seconds=30.0, clock=lambda: now[0])
        self.broker = broker
        thread, outcome = self.park()
        first = broker.claim(actor="local", client="cursor")
        self.assertEqual(30.0, first["lease_seconds"])
        self.assertIsNone(broker.claim(actor="local", client="zed"))

        # `cursor` went quiet. The prompt is nobody's work again, and the App
        # the author still has open can pick it up.
        now[0] += 31.0
        second = broker.claim(actor="local", client="zed")
        self.assertEqual(first["request_id"], second["request_id"])
        broker.respond(second["request_id"], "{}", actor="zed")
        thread.join(timeout=10.0)
        self.assertEqual("{}", outcome["text"])

        # The App that lost the lease is refused rather than published twice.
        with self.assertRaises(UnknownAuthoringRequestError):
            broker.respond(second["request_id"], "{}", actor="cursor")

    def test_addressed_work_is_taken_before_shared_work(self) -> None:
        shared_thread, _ = self.park()
        mine = threading.Thread(
            target=lambda: self.broker.generator_for("cursor")("addressed to me"),
            daemon=True,
        )
        mine.start()
        deadline = time.monotonic() + 10.0
        while len(self.broker.pending()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        # The shared request was parked first, but work asked for by name wins:
        # leaving it behind to take anybody's work would be perverse.
        claimed = self.broker.claim(actor="local", client="cursor")
        self.assertEqual("cursor", claimed["addressed_to"])
        self.broker.respond(claimed["request_id"], "{}", actor="cursor")
        mine.join(timeout=10.0)
        self.broker.respond(
            self.broker.claim(actor="local", client="cursor")["request_id"],
            "{}", actor="cursor",
        )
        shared_thread.join(timeout=10.0)

    def test_an_unknown_request_id_is_refused(self) -> None:
        with self.assertRaises(UnknownAuthoringRequestError):
            self.broker.respond("authoring_request:nope", "{}", actor="client")

    def test_an_object_answer_is_carried_back_as_its_text(self) -> None:
        thread, outcome = self.park()
        request = self.broker.claim(actor="client")
        self.broker.respond(request["request_id"], {"dsl_version": "1.3"}, actor="client")
        thread.join(timeout=10.0)
        self.assertEqual('{"dsl_version": "1.3"}', outcome["text"])

    def test_an_empty_answer_is_refused_rather_than_compiled(self) -> None:
        thread, outcome = self.park()
        request = self.broker.claim(actor="client")
        with self.assertRaises(ValueError):
            self.broker.respond(request["request_id"], "   ", actor="client")
        # The request is still open, so the client can try again.
        self.assertEqual(1, len(self.broker.pending()))
        self.broker.respond(request["request_id"], "{}", actor="client")
        thread.join(timeout=10.0)
        self.assertEqual("{}", outcome["text"])

    def test_stopping_an_unclaimed_request_spent_nothing(self) -> None:
        scope = CancelScope()
        thread, outcome = self.park(scope)
        scope.cancel()
        thread.join(timeout=10.0)
        self.assertIsInstance(outcome["error"], AuthoringUnavailableError)

    def test_stopping_a_claimed_request_leaves_an_unknown_result(self) -> None:
        scope = CancelScope()
        thread, outcome = self.park(scope)
        self.broker.claim(actor="client")
        scope.cancel()
        thread.join(timeout=10.0)
        # Somebody had the prompt and may already have called a model, so the
        # only honest verdict is that nobody knows what it did.
        self.assertIsInstance(outcome["error"], AuthoringUnknownResultError)

    def test_a_request_parked_after_a_cancellation_stops_immediately(self) -> None:
        # Cancellation is racy by nature: the scope is asked to stop before
        # there is anything to stop, and must remember it.
        scope = CancelScope()
        scope.cancel()
        with cancellable(scope), self.assertRaises(AuthoringUnavailableError):
            self.broker("write me a workflow")
        self.assertEqual([], self.broker.pending())


class SubscriptionTests(unittest.TestCase):
    """Being told there is work, instead of asking on a timer."""

    def setUp(self) -> None:
        self.broker = ExternalAuthoringBroker()
        self.seen: dict[str, list] = {}

    def subscribe(self, client: str) -> int:
        events = self.seen.setdefault(client, [])
        return self.broker.subscribe(client, events.append)

    def park(self, target: str | None):
        thread = threading.Thread(
            target=lambda: (
                self.broker(" prompt ") if target is None
                else self.broker.generator_for(target)(" prompt ")
            ),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10.0
        while not self.broker.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        return thread

    def drain(self, client: str) -> None:
        """Answer whatever is parked so no test leaves a thread waiting."""

        request = self.broker.claim(actor="local", client=client)
        if request is not None:
            self.broker.respond(request["request_id"], "{}", actor=client)

    def test_a_connection_is_presence_no_timeout_can_be_wrong_about(self) -> None:
        now = [1000.0]
        broker = ExternalAuthoringBroker(presence_seconds=60.0, clock=lambda: now[0])
        token = broker.subscribe("cursor", lambda _event: None)
        now[0] += 6000.0
        # It never polled, and it is still here. A poll timeout was only ever
        # approximating the thing a live connection states outright.
        self.assertEqual(["cursor"], broker.clients())
        self.assertEqual(["cursor"], sorted(broker.generators()))

        broker.unsubscribe(token)
        self.assertEqual([], broker.clients())

    def test_a_closed_stream_outranks_a_poll_that_came_before_it(self) -> None:
        broker = ExternalAuthoringBroker()
        broker.claim(actor="local", client="cursor")
        token = broker.subscribe("cursor", lambda _event: None)
        self.assertEqual(["cursor"], broker.clients())

        # The socket is gone, and that is newer than the poll before it. Left
        # to the presence timeout, an App that both streams and claims would
        # linger in the menu for exactly the interval the stream was meant to
        # make unnecessary.
        broker.unsubscribe(token)
        self.assertEqual([], broker.clients())
        # Going back to polling is not punished: the next poll re-registers.
        broker.claim(actor="local", client="cursor")
        self.assertEqual(["cursor"], broker.clients())

    def test_one_of_two_streams_closing_leaves_the_app_connected(self) -> None:
        broker = ExternalAuthoringBroker()
        first = broker.subscribe("cursor", lambda _event: None)
        broker.subscribe("cursor", lambda _event: None)
        broker.unsubscribe(first)
        self.assertEqual(["cursor"], broker.clients())

    def test_parking_work_tells_the_app_it_is_addressed_to(self) -> None:
        self.subscribe("cursor")
        self.subscribe("zed")
        thread = self.park("cursor")

        self.assertEqual(1, len(self.seen["cursor"]))
        event = self.seen["cursor"][0]
        self.assertEqual("request_parked", event["type"])
        self.assertEqual("cursor", event["addressed_to"])
        self.assertEqual(
            event["request_id"], self.broker.pending()[0]["request_id"],
        )
        # An App is woken for work it could take, never for somebody else's.
        self.assertEqual([], self.seen["zed"])

        self.drain("cursor")
        thread.join(timeout=10.0)

    def test_unaddressed_work_wakes_everybody_who_could_take_it(self) -> None:
        self.subscribe("cursor")
        self.subscribe("zed")
        thread = self.park(None)

        self.assertEqual(1, len(self.seen["cursor"]))
        self.assertEqual(1, len(self.seen["zed"]))
        self.assertIsNone(self.seen["zed"][0]["addressed_to"])

        self.drain("zed")
        thread.join(timeout=10.0)

    def test_work_back_on_offer_is_announced_like_it_was_the_first_time(self) -> None:
        now = [1000.0]
        broker = ExternalAuthoringBroker(lease_seconds=30.0, clock=lambda: now[0])
        self.broker = broker
        events: list = []
        broker.subscribe("cursor", events.append)
        thread = self.park("cursor")
        broker.claim(actor="local", client="cursor")

        now[0] += 31.0
        broker.pending()  # any read sweeps lapsed leases
        self.assertEqual(
            ["request_parked", "request_released"],
            [event["type"] for event in events],
        )
        self.assertEqual("cursor", events[1]["unanswered_by"])

        self.drain("cursor")
        thread.join(timeout=10.0)

    def test_a_sink_that_fails_never_reaches_the_job(self) -> None:
        def explode(_event):
            raise RuntimeError("this client's socket is gone")

        self.broker.subscribe("cursor", explode)
        self.subscribe("zed")
        # Notification is a shortcut to claiming, so a broken sink costs its
        # own client promptness and costs the job nothing at all.
        thread = self.park(None)
        self.assertEqual(1, len(self.seen["zed"]))

        self.drain("zed")
        thread.join(timeout=10.0)

    def test_a_nameless_subscriber_is_refused(self) -> None:
        for name in (None, "", "has space"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.broker.subscribe(name, lambda _event: None)


if __name__ == "__main__":
    unittest.main()
