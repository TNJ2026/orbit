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
from typing import Any, Callable, Iterator, Mapping

from .generator import (
    MAX_RESPONSE_BYTES, AuthoringUnavailableError, AuthoringUnknownResultError,
    active_scope,
)

# How a client's own queue is named among the generation agents. `app:cursor`
# reads as an address, and the prefix keeps a client from taking — or being
# mistaken for — the name of a discovered CLI.
CLIENT_AGENT_PREFIX = "app:"
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


class UnknownAuthoringRequestError(LookupError):
    """The named request was never parked, or has already been answered."""


def client_agent_name(client: str) -> str:
    return f"{CLIENT_AGENT_PREFIX}{client}"


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

    def __init__(self, pending: _Pending) -> None:
        self._pending = pending

    def cancel(self, *, grace_seconds: float | None = None) -> None:
        # There is no process to be gentle with: a waiting thread stops now,
        # and `grace_seconds` is accepted only to match the handle contract.
        self._pending.cancelled = True
        self._pending.event.set()


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
        return [client_agent_name(name) for name in self._broker.clients()]

    def __getitem__(self, key: str) -> Callable[[str], str]:
        if key in self._names():
            return self._broker.generator_for(key[len(CLIENT_AGENT_PREFIX):])
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
    ) -> None:
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._pending: dict[str, _Pending] = {}
        # When each client was last heard from. Presence is observed, never
        # declared: a client that stopped polling stopped being somewhere work
        # can be sent, whatever it said when it arrived.
        self._seen: dict[str, float] = {}
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
        """Clients heard from recently enough to still be worth addressing."""

        cutoff = self.clock() - self.presence_seconds
        with self._lock:
            return sorted(
                name for name, seen in self._seen.items() if seen > cutoff
            )

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
        handle = _CancelHandle(entry)
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

    # -- the client side ----------------------------------------------------

    def claim(self, *, actor: str, client: str | None = None) -> dict[str, Any] | None:
        """Take the oldest request this client may have, or None when idle.

        A claim is a lease, not a lock: it records who was handed the prompt so
        a stopped request can say whether anybody had started on it, and it
        lapses so a silent client cannot strand the work.
        """

        name = normalise_client(client)
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
