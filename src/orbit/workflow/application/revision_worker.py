"""The background half of Agent workflow revision.

The editor enqueues a revision and returns immediately; something else has to
spend the model call. That used to live in the durable worker runtime next to
the job dispatcher, which put it behind the execution engine it never needed —
a revision job is claimed from the draft service, not from the job table, and
it outlived the engine's removal.

Two loops, both driven by `BackgroundLoop` in the composition root: one claims
and runs a queued job, the other fails jobs whose worker died holding a lease.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta


@dataclass
class InMemoryMetrics:
    """Counters with no exporter — enough to assert on in a test."""

    counters: dict[tuple[str, tuple], int] = field(default_factory=dict)
    observations: list[tuple[str, float, dict[str, str]]] = field(default_factory=list)

    def increment(self, name, value=1, **labels):
        key = (name, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name, value, **labels):
        self.observations.append((name, value, labels))


class RevisionDispatcher:
    """Claim and run one queued Agent workflow-revision job.

    The editor enqueues a prompt and returns; this loop is what actually
    spends the model call. Lease long enough to cover the CLI's own timeout,
    settle under the fence, and never hold a transaction across the call.
    """

    def __init__(
        self, service, *, worker_id="revision-1", clock, metrics=None,
        lease_seconds=360, agent_command=None, agent_commands=None,
        model_id=None,
    ):
        self.service = service
        self.worker_id = worker_id
        self.clock = clock
        self.metrics = metrics or InMemoryMetrics()
        self.agent_command = agent_command
        # The author may have named an Agent when queueing; the audit trail
        # should name the CLI that really ran, not the Runtime's default.
        self.agent_commands = dict(agent_commands or {})
        self.model_id = model_id
        if lease_seconds <= 0 or lease_seconds > 600:
            raise ValueError(
                "revision lease must be positive and at most ten minutes"
            )
        self.lease_ttl = timedelta(seconds=lease_seconds)

    def _increment(self, name):
        try: self.metrics.increment(name)
        except Exception: pass

    def run_once(self) -> bool:
        self._increment("revision_heartbeat")
        claimed = self.service.claim_revision(
            self.worker_id, self.clock(), lease_ttl=self.lease_ttl
        )
        if claimed is None:
            self._increment("revision_empty")
            return False
        job, token = claimed
        settled = self.service.execute_revision(
            job, token, clock=self.clock,
            agent_command=self.agent_commands.get(
                job.requested_agent, self.agent_command
            ),
            model_id=job.requested_agent or self.model_id,
        )
        self._increment(f"revision_{settled.status}")
        return True


class RevisionRecoveryScanner:
    """Fail revision jobs whose worker died holding the lease."""

    def __init__(self, service, *, clock):
        self.service = service
        self.clock = clock

    def run_once(self) -> bool:
        return bool(self.service.expire_revisions(self.clock()))
