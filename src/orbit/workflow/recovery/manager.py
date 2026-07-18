"""Paged startup recovery with finding-specific, audited Apply actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic

from ..domain.human import HumanTaskKind
from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database


@dataclass(frozen=True)
class RecoveryFinding:
    code: str
    entity_id: str
    run_id: str
    expected_version: int
    safe_to_apply: bool
    details: str

    @property
    def action_id(self) -> str:
        return f"{self.code}:{self.entity_id}:{self.expected_version}"


@dataclass(frozen=True)
class RecoveryReport:
    scanned_runs: int
    findings: tuple[RecoveryFinding, ...]
    next_cursor: str | None
    deadline_reached: bool
    applied_action_ids: tuple[str, ...] = ()
    failed_actions: tuple[tuple[str, str], ...] = ()


class RecoveryManager:
    def __init__(
        self,
        path: Path | str,
        *,
        durable_service=None,
        planner_service=None,
        human_service=None,
        foreach_service=None,
    ) -> None:
        self.path = Path(path)
        self.durable = durable_service
        self.planner = planner_service
        self.human = human_service
        self.foreach = foreach_service

    def scan(
        self,
        now: datetime,
        *,
        after_run_id: str = "",
        limit: int = 100,
        deadline_seconds: float = 5,
        apply: bool = False,
    ) -> RecoveryReport:
        if limit < 1 or limit > 1000:
            raise ValueError("Recovery limit must be between 1 and 1000")
        started = monotonic()
        findings: list[RecoveryFinding] = []
        with connect_workflow_database(self.path, read_only=True) as connection:
            runs = connection.execute(
                """SELECT run_id, status FROM workflow_runs
                   WHERE run_id > ? AND status NOT IN ('succeeded','failed','cancelled')
                   ORDER BY run_id LIMIT ?""",
                (after_run_id, limit),
            ).fetchall()
            for run in runs:
                findings.extend(self._find_for_run(connection, run["run_id"], now))
                if monotonic() - started >= deadline_seconds:
                    break

        applied: list[str] = []
        failed: list[tuple[str, str]] = []
        if apply:
            for finding in findings:
                try:
                    self._apply_finding(finding, now)
                    applied.append(finding.action_id)
                except Exception as exc:
                    failed.append((finding.action_id, type(exc).__name__))

        cursor = None if not runs or len(runs) < limit else runs[-1]["run_id"]
        return RecoveryReport(
            len(runs),
            tuple(findings),
            cursor,
            monotonic() - started >= deadline_seconds,
            tuple(applied),
            tuple(failed),
        )

    @staticmethod
    def _find_for_run(connection, run_id: str, now: datetime) -> list[RecoveryFinding]:
        findings: list[RecoveryFinding] = []
        specifications = (
            (
                "UNKNOWN_ATTEMPT",
                """SELECT a.attempt_id AS id, a.aggregate_version AS version
                   FROM node_attempts a JOIN node_runs n ON n.node_run_id=a.node_run_id
                   WHERE n.run_id=? AND a.status='unknown_external_result'""",
                (run_id,),
                False,
            ),
            (
                "UNKNOWN_PLANNER",
                """SELECT attempt_id AS id, aggregate_version AS version
                   FROM planner_attempts WHERE run_id=? AND status='unknown'""",
                (run_id,),
                False,
            ),
            (
                "EXPIRED_HUMAN",
                """SELECT task_id AS id, aggregate_version AS version
                   FROM human_tasks
                   WHERE run_id=? AND status IN ('waiting','claimed')
                     AND deadline_at IS NOT NULL AND deadline_at<=?""",
                (run_id, now.isoformat()),
                True,
            ),
            (
                "ORPHAN_FOREACH",
                """SELECT group_id AS id, aggregate_version AS version
                   FROM foreach_groups g
                   WHERE run_id=? AND status='running'
                     AND NOT EXISTS(
                       SELECT 1 FROM foreach_items i WHERE i.group_id=g.group_id
                         AND i.status IN ('pending','ready','running'))""",
                (run_id,),
                True,
            ),
            (
                "ORPHAN_SUBFLOW",
                """SELECT link_id AS id, aggregate_version AS version
                   FROM subflow_links s
                   WHERE parent_run_id=? AND status IN ('starting','running')
                     AND NOT EXISTS(
                       SELECT 1 FROM workflow_runs r WHERE r.run_id=s.child_run_id)""",
                (run_id,),
                False,
            ),
        )
        for code, sql, parameters, safe in specifications:
            for row in connection.execute(sql, parameters):
                findings.append(
                    RecoveryFinding(
                        code,
                        row["id"],
                        run_id,
                        row["version"],
                        safe,
                        "derived from durable projection",
                    )
                )
        return findings

    def _apply_finding(self, finding: RecoveryFinding, now: datetime) -> None:
        actor = "system:recovery"
        if finding.code == "EXPIRED_HUMAN":
            if self.human is None:
                raise RuntimeError("HumanTask service is unavailable")
            self.human.expire(
                EntityId.parse(finding.entity_id),
                expected_version=finding.expected_version,
                actor=actor,
                now=now,
            )
            return
        if finding.code == "ORPHAN_FOREACH":
            if self.foreach is None:
                raise RuntimeError("Foreach service is unavailable")
            self.foreach.aggregate(
                EntityId.parse(finding.entity_id), actor=actor, now=now
            )
            return
        if not finding.safe_to_apply:
            self._create_manual_takeover(finding, now)
            return
        raise RuntimeError(f"no recovery command for {finding.code}")

    def _create_manual_takeover(
        self, finding: RecoveryFinding, now: datetime
    ) -> None:
        if self.human is None:
            raise RuntimeError("HumanTask service is unavailable")
        try:
            self.human.create(
                EntityId.parse(finding.run_id),
                HumanTaskKind.RECOVERY,
                {
                    "finding_code": finding.code,
                    "entity_id": finding.entity_id,
                    "expected_version": finding.expected_version,
                    "allowed_actions": ["new_attempt", "compensate", "terminate"],
                },
                actor="system:recovery",
                now=now,
            )
        except ValueError as exc:
            if "already exists" not in str(exc):
                raise

