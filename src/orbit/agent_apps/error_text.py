"""What went wrong, said to whoever is holding the other end of the pipe.

The proxy's own failures reach a client as whatever text the exception carried:
`MCP endpoint is unavailable: [Errno 61] Connection refused`.  That is true, and
it is not the sentence the reader needs.  They need to know whether to start
something, wait, or go and look at a log.

Only the proxy's own transport failures are read here.  A JSON-RPC error that
Orbit itself returned is forwarded untouched: it is Orbit's answer to a
question, already worded for the caller, and a proxy that rewrote it would be
putting words in the Runtime's mouth.

Deliberately small.  The DeepSeek-Harness panel classifies fifty-one messages
because it can meet all of them; a stdio proxy sits on one socket and can meet
five.  Sharing one table across the two would mean sharing a language runtime,
and the duplication that avoids is five lines.
"""

from __future__ import annotations

import re
from typing import Sequence

# Matched in order.  The overlaps are real: a refused connection and a timeout
# are both "unavailable" to `urllib`, and only the first is worth telling
# someone to fix.
_READINGS: Sequence[tuple[re.Pattern[str], str]] = (
    (
        re.compile(r"Connection refused|ConnectionRefused|\[Errno 61\]", re.I),
        "Orbit is not listening on that address. Start it, or check the port in "
        "the App manifest.",
    ),
    (
        re.compile(r"timed out|timeout", re.I),
        "Orbit did not answer in time. It may still be working on the last "
        "request.",
    ),
    (
        re.compile(r"returned HTTP 40[13]", re.I),
        "Orbit refused that: this caller is not allowed to do it.",
    ),
    (
        re.compile(r"returned HTTP", re.I),
        "Orbit answered with an error status. It may be starting up or "
        "shutting down.",
    ),
    (
        re.compile(r"returned invalid JSON", re.I),
        "Orbit sent something this proxy could not read. This one is worth "
        "reporting.",
    ),
    (
        re.compile(r"does not declare an MCP endpoint", re.I),
        "This App's manifest has no MCP endpoint, so there is nothing to "
        "forward to.",
    ),
    (
        re.compile(r"unavailable", re.I),
        "Could not reach Orbit. It may have stopped, or never started.",
    ),
)

UNKNOWN = "The proxy failed in a way it does not recognise."


def reading(detail: str) -> str:
    """The sentence for one failure, or an honest admission.

    An unrecognised failure is not dressed up as a known one.  A wrong
    diagnosis sends somebody to fix the wrong thing, which is worse than being
    told that nobody knows — and the original text travels beside this either
    way, so nothing is lost by declining to guess.
    """

    for pattern, sentence in _READINGS:
        if pattern.search(detail):
            return sentence
    return UNKNOWN
