"""Local delivery adapter for one-time HumanTask submission tokens."""

from __future__ import annotations

from threading import Lock


class InMemoryHumanTaskDelivery:
    """Hold a token only until one named participant takes it."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tokens: dict[tuple[str, str], str] = {}

    def deliver(self, task_id, participants, token: str) -> None:
        with self._lock:
            for actor in participants:
                self._tokens[(str(task_id), actor)] = token

    def take(self, task_id, actor: str) -> str | None:
        with self._lock:
            return self._tokens.pop((str(task_id), actor), None)
