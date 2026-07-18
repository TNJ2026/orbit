"""Contract-test helpers for detecting impure Event reducers."""

from __future__ import annotations

import ast
from contextlib import ExitStack
from contextlib import contextmanager
import inspect
import textwrap
from unittest import mock

from .domain.replay import Reducer, StateT, replay_events
from .domain.envelopes import EventEnvelope


class SideEffectDetected(AssertionError):
    pass


_FORBIDDEN_ROOTS = {
    "datetime",
    "open",
    "os",
    "pathlib",
    "random",
    "requests",
    "socket",
    "subprocess",
    "time",
    "urllib",
    "uuid",
}

_PATCH_TARGETS = (
    "builtins.open",
    "socket.socket",
    "subprocess.Popen",
    "subprocess.run",
    "urllib.request.urlopen",
    "time.time",
    "time.monotonic",
    "random.random",
    "uuid.uuid4",
)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def assert_reducer_source_is_pure(reducer: Reducer) -> None:
    """Reject obvious clock, randomness, filesystem, process, and network use."""

    try:
        source = textwrap.dedent(inspect.getsource(reducer))
    except (OSError, TypeError):
        return
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            root = _root_name(node.func)
            if root in _FORBIDDEN_ROOTS:
                raise SideEffectDetected(
                    f"reducer contains forbidden side-effect source: {root}"
                )


def guarded_replay(
    initial_state: StateT,
    events: list[EventEnvelope],
    reducer: Reducer[StateT],
) -> StateT:
    """Replay while actively blocking common nondeterministic side effects."""

    assert_reducer_source_is_pure(reducer)

    with side_effect_guard():
        return replay_events(initial_state, events, reducer)


@contextmanager
def side_effect_guard():
    """Actively block common external calls around replay or upcasting."""

    def blocked(*args, **kwargs):
        raise SideEffectDetected("external side effect attempted during replay")

    with ExitStack() as stack:
        for target in _PATCH_TARGETS:
            stack.enter_context(mock.patch(target, side_effect=blocked))
        yield
