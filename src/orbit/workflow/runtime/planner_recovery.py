"""Recovery scanner for durable Planner projections without provider calls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlannerRecoveryReport:
    parsed_responses: int
    expired_unknown: int
    diagnostics: tuple[str, ...] = ()


class PlannerRecoveryScanner:
    def __init__(self, service) -> None: self.service = service

    def scan_once(self, now, *, limit=100):
        parsed = expired = 0; diagnostics = []
        with self.service.uow_factory() as uow:
            run_ids = sorted({item.run_id for item in uow.planner_attempts.list_expired(now, limit=limit)}, key=str)
            expired_items = tuple(item for run_id in run_ids for item in uow.planner_attempts.list_by_run(run_id) if item.status.value == "running" and item.lease_expires_at <= now)[:limit]
            # Response-received is already durable and can be parsed without a model call.
            response_items = uow.planner_attempts.list_ready_to_parse(limit=limit)
        for item in expired_items:
            self.service.expire_attempt(item.attempt_id, now); expired += 1
        for item in response_items[:limit]:
            self.service.parse_response(item.attempt_id, now); parsed += 1
        return PlannerRecoveryReport(parsed, expired, tuple(diagnostics))
