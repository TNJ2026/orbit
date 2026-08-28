"""Generation performed by connected MCP clients instead of a spawned CLI.

`TrustedCliDslGenerator` forks an Agent CLI and reads its stdout. This is the
same contract with the process removed: the prompt is parked, an already
connected MCP client claims it, writes the DSL itself, and hands the text back.
Everything downstream is unchanged — the answer still goes through the same
compile funnel, the same bounded retry rounds, and the same publisher.

The trust rule is untouched, because the direction of trust never changes:
nothing here takes a command from a caller. A client may only *answer* a prompt
the Runtime wrote, and its answer is text that must survive the compiler like
any other model's.

More than one App may be connected, so a client names itself when it polls and
the author may address one by name. That name is a routing label, not a
credential: on a loopback Runtime every caller is the same actor, and a client
could call itself anything. It decides where work goes, never what a caller is
allowed to do — the scope check upstream is what does that.

The cost of removing the child process is that nothing starts the work. A CLI
is spawned and runs; a parked prompt sits until somebody claims it. Two things
end a wait nobody answers: a claim that goes silent loses its lease and the
request returns to the queue, and the job's own deadline ends the job.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable, Collection, Iterator, Mapping

from .generator import (
    MAX_RESPONSE_BYTES, AuthoringUnavailableError, AuthoringUnknownResultError,
    active_scope,
)

# An App is offered under the name it registers, with nothing added. The name
# used to be decorated `app:<client>` so a client could not take, or be
# mistaken for, the name of a discovered CLI — but that made the *kind* of
# Agent part of its address, which an author has no reason to care about and
# which forced every reader to know the convention. A collision is refused at
# registration instead, the way an operator-configured writer's already is:
# two writers answering to one name means an author cannot be told truthfully
# which one wrote their workflow.
# A prompt nobody ever claims must not hold its job thread for ever. Jobs carry
# a deadline of their own and normally end this wait long before the cap; the
# cap exists for a Runtime configured without one.
DEFAULT_MAX_WAIT_SECONDS = 3600.0
# How long a claim holds a request. Generous enough to write a whole document,
# short enough that a client which died releases its work well inside the job
# deadline that would otherwise be the only way out.
DEFAULT_LEASE_SECONDS = 300.0
# How long after its last poll a client is still considered connected, and so
# still offered as somewhere an author can send work. Matched to the authoring
# deadline: an App silent for longer than a whole generation could have taken
# is not somewhere to send work. Shorter values look tidier and are wrong —
# reading a menu, choosing an App and describing a workflow is a minutes-long
# human act, and an entry that expires underneath it turns a legitimate choice
# into "unknown generation agent" at the moment of submitting.
DEFAULT_PRESENCE_SECONDS = 600.0
# Long enough that a slow client is not a cancellation, short enough that a
# stop request is not left waiting on a full response.
_POLL_SECONDS = 0.2
# Client names are addresses that appear in a UI menu and in job records.
_CLIENT_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")
# Private delivery addresses are callable generator keys but are not Agents a
# person can choose from a menu. Apps use them to distinguish two live
# conversations that share one public client identity.
_PRIVATE_ROUTE_PREFIX = "route."


class ReservedClientNameError(ValueError):
    """An App asked for a name that already answers for another writer."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"the name {name!r} is already registered by another Agent; "
            "register under a different one"
        )
        self.name = name


class UnknownAuthoringRequestError(LookupError):
    """The named request was never parked, or has already been answered."""


def client_agent_name(client: str) -> str:
    """The name an App is offered under: the one it registered."""

    return client


def normalise_client(client: Any) -> str | None:
    """The client name in an argument, or None when the caller gave none."""

    if client is None:
        return None
    name = str(client).strip()
    if not name:
        return None
    if not _CLIENT_NAME.match(name):
        raise ValueError(
            "a client name must start with a letter and use only letters, "
            "digits, dot, dash or underscore"
        )
    return name


class _Pending:
    __slots__ = (
        "request_id", "prompt", "job_id", "event", "response", "claimed_by",
        "cancelled", "sink", "target", "lease_until",
    )

    def __init__(
        self, request_id: str, prompt: str, job_id: str | None, sink=None,
        target: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.prompt = prompt
        self.job_id = job_id
        self.event = threading.Event()
        self.response: str | None = None
        self.claimed_by: str | None = None
        self.cancelled = False
        # Which client this request is addressed to, or None for the shared
        # queue. An addressed request is never handed to anybody else: the
        # author picked that App, and quietly substituting another would make
        # the choice a lie.
        self.target = target
        self.lease_until: float | None = None
        # The job's console. A forked CLI fills it with what the child printed;
        # a parked request has no child, so it narrates the exchange instead —
        # otherwise a job routed to a client is a blank screen for minutes.
        self.sink = sink

    def emit(self, stream: str, text: str) -> None:
        if self.sink is None:
            return
        try:
            self.sink(stream, text)
        except Exception:  # noqa: BLE001 - observation, never the work
            pass


class _CancelHandle:
    """What a `CancelScope` cancels when the "child" is a waiting thread."""

    def __init__(
        self, pending: _Pending, notify: Callable[[_Pending], None] | None = None,
    ) -> None:
        self._pending = pending
        self._notify = notify
        self._lock = threading.Lock()
        self._notified = False

    def cancel(self, *, grace_seconds: float | None = None) -> None:
        # There is no process to be gentle with: a waiting thread stops now,
        # and `grace_seconds` is accepted only to match the handle contract.
        self._pending.cancelled = True
        self._pending.event.set()
        with self._lock:
            notify = None if self._notified else self._notify
            self._notified = True
        if notify is not None:
            notify(self._pending)


class _GeneratorView(Mapping):
    """The generation agents this broker offers, recomputed on every read.

    Every name here is one an App reported for itself; there is no standing
    entry for "whichever client turns up", because a name in this menu is what
    the job records as its author and a placeholder would credit the work to
    nobody. Clients connect and go away while the Runtime runs, so the set
    cannot be settled at composition either. The registry that is sealed at
    startup is the *handler* registry; which Agent writes a draft has always
    been a per-request choice.
    """

    def __init__(self, broker: ExternalAuthoringBroker) -> None:
        self._broker = broker

    def _names(self) -> list[str]:
        return [
            client_agent_name(name) for name in self._broker.clients()
            if not name.startswith(_PRIVATE_ROUTE_PREFIX)
        ]

    def __getitem__(self, key: str) -> Callable[[str], str]:
        # A private route is deliberately absent from iteration (and therefore
        # from authoring Agent menus), but an explicit request may address it.
        # Mapping.__contains__ delegates here, so AuthoringService.ensure_agent
        # still verifies that the route is live before accepting a Job.
        if key in self._broker.clients():
            return self._broker.generator_for(key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())


class ExternalAuthoringBroker:
    """Parks generation prompts for MCP clients, and carries answers back.

    One instance per process, shared by the generators that park prompts and
    the MCP tools that serve them. Requests are handed out oldest first, so two
    rounds of the same job cannot be answered out of order by one client.
    """

    def __init__(
        self,
        *,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        presence_seconds: float = DEFAULT_PRESENCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        reserved_names: Callable[[], Collection[str]] | None = None,
    ) -> None:
        # Names an App may not register under, read at registration rather
        # than captured: an operator's writers are configured at boot and a
        # caller may hold this broker before that.
        self._reserved_names = reserved_names or (lambda: ())
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._pending: dict[str, _Pending] = {}
        # When each client was last polled from. Presence is observed, never
        # declared: a client that stopped polling stopped being somewhere work
        # can be sent, whatever it said when it arrived.
        self._seen: dict[str, float] = {}
        # Clients holding an open event stream, by subscription token. A live
        # connection is a better answer to "is this App still there" than any
        # timeout over a poll: it is the fact the timeout was approximating.
        self._subscribers: dict[int, tuple[str, Callable[[Mapping[str, Any]], None]]] = {}
        self._next_token = 1
        self.max_wait_seconds = float(max_wait_seconds)
        self.lease_seconds = float(lease_seconds)
        self.presence_seconds = float(presence_seconds)
        self.clock = clock

    # -- the composition side -----------------------------------------------

    def generators(self) -> Mapping[str, Callable[[str], str]]:
        """A live mapping of the generation agent names this broker serves."""

        return _GeneratorView(self)

    def generator_for(self, client: str) -> Callable[[str], str]:
        """A generator that parks its prompts for one named client only."""

        name = normalise_client(client)
        if name is None:
            raise ValueError("a client name is required")

        def generate(prompt: str) -> str:
            return self._park(prompt, target=name)

        return generate

    def clients(self) -> list[str]:
        """Clients still worth addressing: connected, or recently polled.

        A client holding an event stream is present for as long as it holds it
        — no timeout can be wrong about that. Polling remains a second way in,
        because a client that cannot open a stream is still a client, and it
        is the only one the presence timeout applies to.
        """

        cutoff = self.clock() - self.presence_seconds
        with self._lock:
            polled = {
                name for name, seen in self._seen.items() if seen > cutoff
            }
            connected = {name for name, _deliver in self._subscribers.values()}
        return sorted(polled | connected)

    def touch(self, client: str) -> str:
        """Mark one delivery address present without claiming queued work."""

        name = normalise_client(client)
        if name is None:
            raise ValueError("a client name is required")
        # Resolve the dynamic registry before taking the broker lock.  The
        # registry may include this broker's live generator view, whose
        # iteration calls ``clients()`` and therefore takes the same lock.
        # Calling it from inside the critical section deadlocks registration
        # exactly when an App uses a name also considered by Agent discovery.
        reserved = {str(item) for item in self._reserved_names()}
        with self._lock:
            if name not in self._seen and name in reserved:
                raise ReservedClientNameError(name)
            self._seen[name] = self.clock()
        return name

    # -- the event stream ---------------------------------------------------

    def subscribe(
        self, client: str, deliver: Callable[[Mapping[str, Any]], None],
    ) -> int:
        """Register a client's event sink, and report it present until it goes.

        `deliver` is called from whichever thread parked or released the work —
        a Job's own thread, not an event loop — so an implementation that has
        to reach one must hand the event over itself.
        """

        name = normalise_client(client)
        if name is None:
            raise ValueError("a client name is required")
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subscribers[token] = (name, deliver)
        return token

    def unsubscribe(self, token: int) -> None:
        """Drop a client's event sink, and with it any older claim to presence.

        A socket closing is newer evidence than a poll the same client made
        before it — "gone" beats "was here a minute ago". Without this, any
        client that both streams and claims stays in the menu for the whole
        presence timeout after it disappears, which is the timeout the stream
        was supposed to make unnecessary. A client that goes back to polling
        re-registers on its very next poll.
        """

        with self._lock:
            gone = self._subscribers.pop(token, None)
            still_connected = {name for name, _sink in self._subscribers.values()}
            if gone is not None and gone[0] not in still_connected:
                self._seen.pop(gone[0], None)

    def _publish(self, event: Mapping[str, Any], *, target: str | None) -> None:
        """Tell the clients this event concerns, and nobody else.

        A notification is a shortcut to claiming, never a substitute for it:
        every fact in it can be re-read from `claim`, so a sink that drops or
        raises costs latency and nothing more.
        """

        with self._lock:
            sinks = [
                deliver for name, deliver in self._subscribers.values()
                if target is None or name == target
            ]
        for deliver in sinks:
            try:
                deliver(event)
            except Exception:  # noqa: BLE001 - observation, never the work
                pass

    # -- the generator side -------------------------------------------------

    def __call__(self, prompt: str) -> str:
        """Park `prompt` for whichever client comes for it, and block.

        This is the fallback the Runtime uses when an author named no Agent at
        all, so that a Runtime with no CLI installed still writes workflows
        once an App connects. It is deliberately not a *name* anybody can pick:
        addressed work records the App that wrote it, and a standing
        "whoever turns up" entry would credit the job to a placeholder.

        Signature-compatible with `TrustedCliDslGenerator.__call__`, which is
        the whole point: the authoring service cannot tell the two apart.
        """

        return self._park(prompt, target=None)

    def _park(self, prompt: str, *, target: str | None) -> str:
        scope = active_scope()
        entry = _Pending(
            f"authoring_request:{uuid.uuid4()}", prompt,
            getattr(scope, "job_id", None),
            getattr(scope, "on_output", None),
            target=target,
        )
        with self._lock:
            self._pending[entry.request_id] = entry
            self._order.append(entry.request_id)
        # The prompt is what a CLI would have been handed on stdin and never
        # printed. Here it is the only thing anybody can act on, so the console
        # shows it in full rather than a summary of it.
        addressed = "any connected client" if target is None else target
        entry.emit("stderr", (
            f"waiting for {addressed} to claim {entry.request_id}\n"
            f"--- prompt ---\n{prompt}\n--- end of prompt ---\n"
        ))
        # Told, rather than waited for. Without this a client learns about the
        # work on its next poll, so the interval it polls at is the latency —
        # and an idle Runtime pays for that interval all day.
        self._publish({
            "type": "request_parked", "request_id": entry.request_id,
            "job_id": entry.job_id, "addressed_to": target,
        }, target=target)
        def notify_cancelled(pending: _Pending) -> None:
            self._publish({
                "type": "request_cancelled",
                "request_id": pending.request_id,
                "job_id": pending.job_id,
                "addressed_to": pending.target,
                "claimed_by": pending.claimed_by,
                "reason": "cancelled_by_operator",
            }, target=pending.claimed_by or pending.target)

        handle = _CancelHandle(entry, notify_cancelled)
        # Registering before the wait, not after, so a cancellation that
        # arrives during setup is remembered by the scope and applied here.
        if scope is not None:
            scope.attach(handle)
        try:
            waited = 0.0
            while not entry.event.wait(_POLL_SECONDS):
                self._expire_leases()
                waited += _POLL_SECONDS
                if waited >= self.max_wait_seconds:
                    entry.cancelled = True
                    break
        finally:
            if scope is not None:
                scope.detach()
            self._discard(entry.request_id)
        if entry.cancelled or entry.response is None:
            entry.emit("stderr", f"{entry.request_id} stopped before it was answered\n")
            if entry.claimed_by is None:
                # Never handed out: no client was asked for anything, so
                # nothing was spent and nothing happened.
                raise AuthoringUnavailableError(
                    "no MCP client claimed this generation request"
                    + ("" if target is None else f" addressed to {target!r}")
                )
            # Claimed and then silenced. The client may already have called a
            # model, so this is unresolved rather than failed.
            raise AuthoringUnknownResultError(
                f"generation request was claimed by {entry.claimed_by!r} and "
                "stopped before it answered"
            )
        return entry.response

    def _discard(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)
            if request_id in self._order:
                self._order.remove(request_id)

    def _expire_leases(self) -> None:
        """Return work whose claimant went silent to the queue it came from.

        Without this a client that claimed and then died would hold a prompt
        until the job's deadline, and the App the author actually has open
        could never be offered it.
        """

        now = self.clock()
        lapsed = []
        with self._lock:
            for entry in self._pending.values():
                if (
                    entry.lease_until is not None
                    and entry.lease_until <= now
                    and entry.response is None
                    and not entry.event.is_set()
                ):
                    lapsed.append((entry, entry.claimed_by))
                    entry.claimed_by = None
                    entry.lease_until = None
        for entry, previous in lapsed:
            entry.emit("stderr", (
                f"{entry.request_id} was not answered by {previous}; "
                "it is waiting to be claimed again\n"
            ))
            # Back on offer, so whoever could take it is told the same way
            # they were told about it the first time.
            self._publish({
                "type": "request_released", "request_id": entry.request_id,
                "job_id": entry.job_id, "addressed_to": entry.target,
                "unanswered_by": previous,
            }, target=entry.target)

    # -- the client side ----------------------------------------------------

    def claim(self, *, actor: str, client: str | None = None) -> dict[str, Any] | None:
        """Take the oldest request this client may have, or None when idle.

        A claim is a lease, not a lock: it records who was handed the prompt so
        a stopped request can say whether anybody had started on it, and it
        lapses so a silent client cannot strand the work.
        """

        name = normalise_client(client)
        if name is not None and name not in self._seen:
            # Refused rather than decorated. An App used to be offered as
            # `app:<name>` so it could not collide; it is offered under the
            # name it chose now, and a name already answered by a discovered
            # CLI or an operator's writer is refused — two writers answering
            # to one name means an author cannot be told truthfully which one
            # wrote their workflow, and being told the wrong one is worse than
            # an error.
            if name in {str(item) for item in self._reserved_names()}:
                raise ReservedClientNameError(name)
        self._expire_leases()
        now = self.clock()
        with self._lock:
            if name is not None:
                self._seen[name] = now
            entries = [
                entry for request_id in self._order
                if (entry := self._pending.get(request_id)) is not None
                and entry.claimed_by is None
            ]
            # Work addressed to this client first: it was asked for by name,
            # and leaving it behind to take shared work would be perverse.
            claimed = next(
                (entry for entry in entries if name is not None and entry.target == name),
                None,
            ) or next((entry for entry in entries if entry.target is None), None)
            if claimed is not None:
                claimed.claimed_by = name or actor
                claimed.lease_until = now + self.lease_seconds
        if claimed is None:
            return None
        # Outside the lock: the console write talks to SQLite, and holding the
        # broker's lock across it would stall every other claim and answer.
        claimed.emit("stderr", f"{claimed.request_id} claimed by {claimed.claimed_by}\n")
        return {
            "request_id": claimed.request_id,
            "job_id": claimed.job_id,
            "addressed_to": claimed.target,
            "lease_seconds": self.lease_seconds,
            "prompt": claimed.prompt,
        }

    def wait_claim(
        self, *, actor: str, client: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        """Stay addressable until this client can claim work or times out."""

        name = normalise_client(client)
        if name is None:
            raise ValueError("a client name is required")
        timeout = float(timeout_seconds)
        if timeout < 0:
            raise ValueError("timeout_seconds must not be negative")

        claimed = self.claim(actor=actor, client=name)
        if claimed is not None or timeout == 0:
            return claimed

        wake = threading.Event()
        token = self.subscribe(name, lambda _event: wake.set())
        deadline = time.monotonic() + timeout
        try:
            while True:
                claimed = self.claim(actor=actor, client=name)
                if claimed is not None:
                    return claimed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                wake.wait(remaining)
                wake.clear()
        finally:
            self.unsubscribe(token)

    def pending(self) -> list[dict[str, Any]]:
        self._expire_leases()
        with self._lock:
            return [
                {
                    "request_id": entry.request_id, "job_id": entry.job_id,
                    "addressed_to": entry.target, "claimed_by": entry.claimed_by,
                }
                for request_id in self._order
                if (entry := self._pending.get(request_id)) is not None
            ]

    def respond(self, request_id: str, dsl: Any, *, actor: str) -> dict[str, Any]:
        """Hand a written DSL document back to the waiting job.

        Any client holding the id may answer, including one whose lease has
        lapsed. The first answer settles the request and every later one is
        refused, so a takeover after a lapse cannot produce two published
        drafts from one prompt.
        """

        text = dsl if isinstance(dsl, str) else json.dumps(dsl, ensure_ascii=False)
        if not text.strip():
            raise ValueError("a generation response cannot be empty")
        if len(text.encode()) > MAX_RESPONSE_BYTES:
            raise ValueError(
                f"generation response exceeded {MAX_RESPONSE_BYTES} bytes"
            )
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is None or entry.event.is_set():
                raise UnknownAuthoringRequestError(
                    "no generation request is waiting for this id"
                )
            if entry.claimed_by is None:
                entry.claimed_by = actor
            entry.response = text
        # The answer goes to the console on stdout, exactly where a forked CLI
        # would have printed it, so both kinds of job read the same way.
        entry.emit("stdout", text if text.endswith("\n") else text + "\n")
        entry.event.set()
        return {"request_id": request_id, "job_id": entry.job_id, "accepted": True}
