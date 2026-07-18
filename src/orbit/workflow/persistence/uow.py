"""Explicit SQLite transaction boundary for deterministic runtime writes."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from ..domain.persistence import DatabaseBusyError, PersistenceError
from .database import connect_workflow_database


FaultHook = Callable[[str], None]


class SQLiteUnitOfWork:
    def __init__(self, path: Path | str, *, fault_hook: FaultHook | None = None) -> None:
        self.path = Path(path)
        self.fault_hook = fault_hook
        self.connection: sqlite3.Connection | None = None
        self.committed = False
        self.events = None
        self.receipts = None
        self.runs = None
        self.plans = None
        self.node_runs = None
        self.attempts = None
        self.tokens = None
        self.snapshots = None
        self.jobs = None
        self.leases = None
        self.timers = None
        self.values = None
        self.value_links = None
        self.artifacts = None
        self.artifact_links = None
        self.joins = None
        self.counters = None
        self.planner_attempts = None
        self.planner_proposals = None

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def __enter__(self) -> SQLiteUnitOfWork:
        if self.connection is not None:
            raise RuntimeError("UnitOfWork cannot be re-entered")
        self.connection = connect_workflow_database(self.path)
        try:
            self._fault("before_begin")
            self.connection.execute("BEGIN IMMEDIATE")
            self._fault("after_begin")
        except sqlite3.OperationalError as exc:
            self.connection.close()
            self.connection = None
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise DatabaseBusyError(str(exc)) from None
            raise PersistenceError(str(exc)) from None
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            self.connection.close()
            self.connection = None
            raise
        from .event_store import SQLiteEventStore
        from .receipts import SQLiteCommandReceiptStore
        from .snapshots import SQLiteSnapshotStore
        from .durable import SQLiteJobRepository, SQLiteLeaseRepository, SQLiteTimerRepository
        from .data import (
            SQLiteArtifactLinkRepository, SQLiteArtifactRepository,
            SQLiteValueLinkRepository, SQLiteValueRepository,
        )
        from .graph import SQLiteControlCounterRepository, SQLiteJoinGroupRepository
        from .planner import SQLitePlannerAttemptRepository, SQLitePlannerProposalRepository
        from .repositories import (
            SQLiteAttemptRepository,
            SQLiteBranchTokenRepository,
            SQLiteExecutionPlanRepository,
            SQLiteNodeRunRepository,
            SQLiteWorkflowRunRepository,
        )

        self.events = SQLiteEventStore(self.connection, fault_hook=self.fault_hook)
        self.receipts = SQLiteCommandReceiptStore(
            self.connection, fault_hook=self.fault_hook
        )
        arguments = (self.connection, self.events)
        keywords = {"fault_hook": self.fault_hook}
        self.runs = SQLiteWorkflowRunRepository(*arguments, **keywords)
        self.plans = SQLiteExecutionPlanRepository(*arguments, **keywords)
        self.node_runs = SQLiteNodeRunRepository(*arguments, **keywords)
        self.attempts = SQLiteAttemptRepository(*arguments, **keywords)
        self.tokens = SQLiteBranchTokenRepository(*arguments, **keywords)
        self.snapshots = SQLiteSnapshotStore(
            self.connection, fault_hook=self.fault_hook
        )
        self.jobs = SQLiteJobRepository(*arguments, **keywords)
        self.leases = SQLiteLeaseRepository(*arguments, **keywords)
        self.timers = SQLiteTimerRepository(*arguments, **keywords)
        self.values = SQLiteValueRepository(self.connection, **keywords)
        self.value_links = SQLiteValueLinkRepository(self.connection, **keywords)
        self.artifacts = SQLiteArtifactRepository(self.connection, **keywords)
        self.artifact_links = SQLiteArtifactLinkRepository(self.connection, **keywords)
        self.joins = SQLiteJoinGroupRepository(*arguments, **keywords)
        self.counters = SQLiteControlCounterRepository(*arguments, **keywords)
        self.planner_attempts = SQLitePlannerAttemptRepository(*arguments, **keywords)
        self.planner_proposals = SQLitePlannerProposalRepository(*arguments, **keywords)
        return self

    def commit(self) -> None:
        connection = self._require_connection()
        if self.committed:
            raise RuntimeError("UnitOfWork already committed")
        self._fault("before_commit")
        connection.commit()
        self.committed = True
        self._fault("after_commit")

    def rollback(self) -> None:
        if self.connection is not None and self.connection.in_transaction:
            self.connection.rollback()

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("UnitOfWork is not active")
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self.connection is not None
        try:
            if exc_type is not None or not self.committed:
                self.rollback()
        finally:
            self.connection.close()
            self.connection = None
            self.events = None
            self.receipts = None
            self.runs = None
            self.plans = None
            self.node_runs = None
            self.attempts = None
            self.tokens = None
            self.snapshots = None
            self.jobs = None
            self.leases = None
            self.timers = None
            self.values = None
            self.value_links = None
            self.artifacts = None
            self.artifact_links = None
            self.joins = None
            self.counters = None
            self.planner_attempts = None
            self.planner_proposals = None


class SQLiteReadSession:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.connection = connect_workflow_database(self.path, read_only=True)
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self.connection is not None
        self.connection.close()
        self.connection = None
