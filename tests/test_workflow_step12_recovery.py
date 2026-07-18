from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.human import HumanTaskKind
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.dsl import compile_source
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.recovery import RecoveryManager


NOW = datetime(2026, 7, 18, 1, tzinfo=timezone.utc)


class RecoveryApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recovery.db"
        source = {
            "dsl_version": "1.2",
            "metadata": {"id": "recovery", "name": "Recovery"},
            "nodes": [{"id": "done", "kind": "terminal"}],
            "edges": [], "entry": ["done"], "terminals": ["done"],
        }
        compiled = compile_source(
            json.dumps(source), InMemoryHandlerCatalog([]),
            InMemorySchemaCatalog({}), source_format="json",
        )
        SQLiteWorkflowVersionStore(self.path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        self.run_id = EntityId("run", "recovery")
        RuntimeApplicationService(self.path).submit(
            CommandEnvelope(
                EntityId("command", "start-recovery"), "start_run",
                self.run_id, self.run_id, AggregateVersion(0), "start",
                "test", NOW,
                {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
                 "definition_hash": compiled.definition_hash.value},
            )
        )
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                "UPDATE workflow_runs SET status='waiting' WHERE run_id=?",
                (str(self.run_id),),
            )
        self.human = HumanTaskService(self.path)

    def test_dry_run_does_not_expire_due_human_task(self):
        task_id, _ = self.human.create(
            self.run_id, HumanTaskKind.INPUT, {"question": "x"},
            actor="planner", now=NOW, deadline_at=NOW + timedelta(seconds=1),
        )
        report = RecoveryManager(self.path, human_service=self.human).scan(
            NOW + timedelta(seconds=2), apply=False,
        )
        self.assertEqual(["EXPIRED_HUMAN"], [item.code for item in report.findings])
        with connect_workflow_database(self.path, read_only=True) as connection:
            self.assertEqual(
                "waiting",
                connection.execute(
                    "SELECT status FROM human_tasks WHERE task_id=?", (str(task_id),)
                ).fetchone()[0],
            )

    def test_apply_consumes_exact_finding_with_expected_version_and_audit(self):
        task_id, _ = self.human.create(
            self.run_id, HumanTaskKind.INPUT, {"question": "x"},
            actor="planner", now=NOW, deadline_at=NOW + timedelta(seconds=1),
        )
        report = RecoveryManager(self.path, human_service=self.human).scan(
            NOW + timedelta(seconds=2), apply=True,
        )
        self.assertEqual(1, len(report.applied_action_ids))
        self.assertEqual((), report.failed_actions)
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT status,aggregate_version FROM human_tasks WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            self.assertEqual(("expired", 2), tuple(row))
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM audit_records WHERE action='human.expire' AND target_id=?",
                    (str(task_id),),
                ).fetchone()
            )

    def test_unknown_planner_creates_scoped_manual_takeover_once(self):
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                """INSERT INTO planner_attempts(
                       attempt_id,run_id,attempt_number,status,context_json,
                       context_hash,prompt_hash,capability_manifest_hash,model_id,
                       provider_id,request_fingerprint,raw_response,
                       raw_response_checksum,provider_request_id,usage_json,
                       proposal_id,error_json,lease_owner,lease_token_hash,
                       fencing_token,lease_expires_at,aggregate_version,
                       created_at,updated_at
                   ) VALUES (?,?,1,'unknown','{}',?,?,?,?,?, ?,NULL,NULL,NULL,NULL,
                             NULL,'{}',NULL,NULL,0,NULL,0,?,?)""",
                (
                    "planner_attempt:unknown", str(self.run_id),
                    "sha256:" + "1" * 64, "sha256:" + "2" * 64,
                    "sha256:" + "3" * 64, "model", "provider",
                    "sha256:" + "4" * 64, NOW.isoformat(), NOW.isoformat(),
                ),
            )
        manager = RecoveryManager(self.path, human_service=self.human)
        first = manager.scan(NOW, apply=True)
        second = manager.scan(NOW, apply=True)
        self.assertIn("UNKNOWN_PLANNER", [item.code for item in first.findings])
        self.assertEqual((), first.failed_actions)
        self.assertEqual((), second.failed_actions)
        with connect_workflow_database(self.path, read_only=True) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM human_tasks WHERE run_id=? AND kind='recovery'",
                (str(self.run_id),),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_pagination_cursor_is_stable(self):
        report = RecoveryManager(self.path).scan(NOW, limit=1)
        self.assertEqual(1, report.scanned_runs)
        self.assertEqual(str(self.run_id), report.next_cursor)
        tail = RecoveryManager(self.path).scan(
            NOW, after_run_id=report.next_cursor or "", limit=1,
        )
        self.assertEqual(0, tail.scanned_runs)


if __name__ == "__main__":
    unittest.main()
