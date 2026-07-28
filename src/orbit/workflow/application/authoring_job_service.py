"""Persistent product-level Workflow generation and modification jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from pathlib import Path
from threading import Lock, Thread, Timer
from typing import Any, Mapping

from ..authoring import (
    AuthoringFailedError, AuthoringUnknownResultError, CancelScope, cancellable,
)
from ..persistence.authoring_output import SQLiteAuthoringOutputStore
from ..persistence.control import audit as persist_audit
from ..persistence.database import connect_workflow_database


ACTIVE = ("queued", "running")
# How long a cancelled Agent CLI gets to exit before it is force-killed.
CANCEL_GRACE_SECONDS = 5.0
# Error codes that mean "the Agent may have done something we cannot see".
# They are never a plain failure: a retry could pay for the same call twice.
UNKNOWN_RESULT_CODES = frozenset({
    "unknown_external_result", "AuthoringUnknownResultError",
})


class AuthoringJobConflict(ValueError):
    def __init__(self, code: str, job: Mapping[str, Any]) -> None:
        self.code, self.job = code, dict(job)
        super().__init__(code)


class AuthoringJobService:
    def __init__(
        self, path, authoring, publisher, *, workflow_db_path=None,
        timeout_seconds=None, clock=None,
        cancel_grace_seconds=CANCEL_GRACE_SECONDS,
    ):
        self.path = Path(path)
        self.workflow_path = Path(workflow_db_path or path)
        self.authoring, self.publisher = authoring, publisher
        self.timeout_seconds = (
            None if timeout_seconds is None else max(30, int(timeout_seconds))
        )
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Jobs in flight in *this* process, so a cancellation can reach the
        # Agent CLI rather than only marking a row. Another process's jobs are
        # not here, which is what the deadline and restart recovery are for.
        self._scopes: dict[str, CancelScope] = {}
        self._deadline_timers: dict[str, Timer] = {}
        self._scope_lock = Lock()
        # What the Agent CLI prints while it writes: an observation of a child
        # process, kept so a job that thinks for a minute is not a black box.
        self._output = SQLiteAuthoringOutputStore(self.path)
        self._recover()

    def output(self, job_id: str, *, after_chunk_id: int = 0, limit: int = 500):
        """What the Agent CLI printed, in the order it printed it."""

        return self._output.read(
            job_id, after_chunk_id=after_chunk_id, limit=limit,
        )

    def _open_scope(self, job_id: str) -> CancelScope:
        def record(stream: str, text: str) -> None:
            try:
                self._output.append(
                    job_id=job_id, stream=stream, text=text, now=self.clock(),
                )
            except Exception:
                # Console output is diagnostic data. A locked or unavailable
                # store must not change the authoring job it merely observes.
                pass

        scope = CancelScope(on_output=record)
        with self._scope_lock:
            self._scopes[job_id] = scope
        return scope

    def _close_scope(self, job_id: str) -> None:
        with self._scope_lock:
            self._scopes.pop(job_id, None)
            timer = self._deadline_timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()

    def _watch_deadline(self, job_id: str, deadline_at: str) -> None:
        """Enforce a Job deadline without depending on an API reader polling it."""

        if not deadline_at:
            return
        deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
        delay = max(0.0, (deadline - self.clock()).total_seconds())
        timer = Timer(delay, self._expire_due)
        timer.daemon = True
        with self._scope_lock:
            previous = self._deadline_timers.pop(job_id, None)
            self._deadline_timers[job_id] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _record_progress(self, job_id, stage, attempt=None, max_attempts=None):
        try:
            self._output.append(
                job_id=job_id,
                stream="stderr",
                text="\x1eorbit-progress:" + json.dumps({
                    "stage": stage, "attempt": attempt,
                    "max_attempts": max_attempts,
                }, separators=(",", ":")),
                now=self.clock(),
            )
        except Exception:
            # Progress is observational and must never change the authoring job.
            pass

    def _record_validation_diagnostics(
        self, job_id, attempt, max_attempts, diagnostics,
    ):
        lines = [f"[validation {attempt}/{max_attempts}] rejected"]
        for finding in diagnostics[:20]:
            code = str(finding.get("code") or "VALIDATION_ERROR")
            message = str(finding.get("message") or "").strip()
            lines.append(f"{code}: {message}" if message else code)
            rule = str(finding.get("rule") or "").strip()
            if rule:
                lines.append(f"  rule: {rule}")
        try:
            self._output.append(
                job_id=job_id, stream="stderr", text="\n".join(lines) + "\n",
                now=self.clock(),
            )
        except Exception:
            # Validation logging is observational, just like CLI output.
            pass

    @staticmethod
    def _time(value):
        return value.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")

    def _recover(self):
        """Restart queued work; fail indeterminate running work without publishing."""
        stamp = self._time(self.clock())
        with connect_workflow_database(self.path) as db:
            interrupted = list(db.execute(
                "SELECT job_id,actor FROM workflow_authoring_jobs WHERE status='running'"
            ))
            db.execute(
                "UPDATE workflow_authoring_jobs SET status='failed',"
                " cancel_requested=1,error_code='unknown_external_result',"
                " error_message='Authoring process ended before its result was confirmed',"
                " updated_at=? WHERE status='running'",
                (stamp,),
            )
            for row in interrupted:
                persist_audit(
                    db, run_id=None, actor=row["actor"],
                    action="workflow.authoring.unknown_result",
                    target_id=row["job_id"], decision="failed",
                    details={"reason": "process_restart"}, occurred_at=self.clock(),
                )
            queued = [
                (row["job_id"], row["deadline_at"]) for row in db.execute(
                    "SELECT job_id,deadline_at FROM workflow_authoring_jobs"
                    " WHERE status='queued' AND cancel_requested=0"
                )
            ]
            db.commit()
        for job_id, deadline_at in queued:
            self._watch_deadline(job_id, deadline_at or "")
            Thread(target=self._execute, args=(job_id,), daemon=True).start()

    def _dto(self, row):
        status = str(row["status"])
        commands = [] if status not in ACTIVE else [{
            "command": "workflow.authoring.cancel", "label": "Cancel",
            "method": "POST",
            "href": f"/api/v1/workflow-authoring-jobs/{row['job_id']}/cancel",
            "target_aggregate_id": row["job_id"], "expected_version": 0,
            "payload_schema": "workflow-authoring-cancel/1.0",
        }]
        return {
            "job_id": row["job_id"], "type": row["job_type"],
            "workflow_id": row["workflow_id"], "prompt": row["prompt"],
            "mode": row["mode"], "status": status,
            "requested_agent": row["requested_agent"],
            "deadline_at": row["deadline_at"] or None,
            "result": None if row["result_json"] is None else json.loads(row["result_json"]),
            # How many rounds the Agent needed, and what the compiler refused
            # on the last one: the facts a failed job is otherwise silent about.
            "attempts": None if row["attempts"] is None else int(row["attempts"]),
            "error": None if row["error_code"] is None else {
                "code": row["error_code"], "message": row["error_message"],
                "diagnostics": (
                    [] if row["diagnostics_json"] is None
                    else json.loads(row["diagnostics_json"])
                ),
            },
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "href": f"/api/v1/workflow-authoring-jobs/{row['job_id']}",
            # The client never builds a URL: where this job's console lives is
            # something the server says, like every other address it follows.
            "output_href": (
                f"/api/v1/workflow-authoring-jobs/{row['job_id']}/output"
            ),
            "allowed_commands": commands,
        }

    def get(self, job_id, *, actor):
        self._expire_due()
        with connect_workflow_database(self.path, read_only=True) as db:
            row = db.execute(
                "SELECT * FROM workflow_authoring_jobs WHERE job_id=? AND actor=?",
                (job_id, actor),
            ).fetchone()
        if row is None:
            raise LookupError("authoring job not found")
        return self._dto(row)

    def list(self, *, actor, active_only=False, job_type=None):
        self._expire_due()
        clauses, params = ["actor=?"], [actor]
        if active_only:
            clauses.append("status IN ('queued','running')")
        if job_type:
            if job_type not in {"generate", "modify"}:
                raise ValueError("invalid authoring job type")
            clauses.append("job_type=?")
            params.append(job_type)
        with connect_workflow_database(self.path, read_only=True) as db:
            rows = db.execute(
                f"SELECT * FROM workflow_authoring_jobs WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC,job_id",
                tuple(params),
            ).fetchall()
        return [self._dto(row) for row in rows]

    def active_for_workflow(self, workflow_id, *, actor):
        self._expire_due()
        with connect_workflow_database(self.path, read_only=True) as db:
            row = db.execute(
                "SELECT * FROM workflow_authoring_jobs WHERE workflow_id=? AND actor=?"
                " AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
                (workflow_id, actor),
            ).fetchone()
        return None if row is None else self._dto(row)

    def create(
        self, *, actor, prompt, idempotency_key, workflow_id=None, mode="generate",
        display_language=None, agent=None,
    ):
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")
        job_type = "generate" if workflow_id is None else "modify"
        if job_type == "generate":
            workflow_id = f"workflow:wf_{uuid.uuid4()}"
        allowed = {"generate"} if job_type == "generate" else {"modify", "regenerate"}
        if mode not in allowed:
            raise ValueError("invalid authoring mode")
        job_id = "authoring_job:" + hashlib.sha256(
            f"{actor}|{idempotency_key}".encode()
        ).hexdigest()
        now = self.clock()
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            replay = db.execute(
                "SELECT * FROM workflow_authoring_jobs"
                " WHERE actor=? AND idempotency_key=?",
                (actor, idempotency_key),
            ).fetchone()
            if replay is not None:
                db.commit()
                return self._dto(replay)
            if job_type == "generate":
                active = db.execute(
                    "SELECT * FROM workflow_authoring_jobs WHERE actor=?"
                    " AND job_type='generate' AND status IN ('queued','running') LIMIT 1",
                    (actor,),
                ).fetchone()
                conflict = "workflow_generation_already_active"
            else:
                active = db.execute(
                    "SELECT * FROM workflow_authoring_jobs WHERE workflow_id=?"
                    " AND job_type='modify' AND status IN ('queued','running') LIMIT 1",
                    (workflow_id,),
                ).fetchone()
                conflict = "draft_already_active"
            if active is not None:
                db.rollback()
                raise AuthoringJobConflict(conflict, self._dto(active))
            stamp = self._time(now)
            db.execute(
                "INSERT INTO workflow_authoring_jobs("
                "job_id,job_type,actor,workflow_id,prompt,mode,status,idempotency_key,"
                "display_language,requested_agent,deadline_at,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?)",
                (
                    job_id, job_type, actor, workflow_id, prompt, mode,
                    idempotency_key, display_language, agent,
                    (
                        "" if self.timeout_seconds is None
                        else self._time(now + timedelta(seconds=self.timeout_seconds))
                    ),
                    stamp, stamp,
                ),
            )
            db.commit()
        if self.timeout_seconds is not None:
            self._watch_deadline(
                job_id, self._time(now + timedelta(seconds=self.timeout_seconds))
            )
        Thread(target=self._execute, args=(job_id,), daemon=True).start()
        return self.get(job_id, actor=actor)

    def cancel(self, job_id, *, actor):
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workflow_authoring_jobs WHERE job_id=? AND actor=?",
                (job_id, actor),
            ).fetchone()
            if row is None:
                raise LookupError("authoring job not found")
            active = row["status"] in ACTIVE
            if active:
                db.execute(
                    "UPDATE workflow_authoring_jobs SET status='cancelled',"
                    " cancel_requested=1,updated_at=? WHERE job_id=?",
                    (self._time(self.clock()), job_id),
                )
                persist_audit(
                    db, run_id=None, actor=actor,
                    action="workflow.authoring.cancel",
                    target_id=job_id, decision="cancelled",
                    details={
                        "previous_status": row["status"],
                        # Stopping the CLI leaves what it had already done
                        # unknowable, which the audit has to say out loud.
                        "unknown_result": row["status"] == "running",
                    },
                    occurred_at=self.clock(),
                )
            db.commit()
        if active:
            # Outside the transaction: stopping a child waits for it to exit,
            # and holding a write lock for that long would block every reader.
            with self._scope_lock:
                scope = self._scopes.get(job_id)
            if scope is not None:
                scope.cancel(grace_seconds=self.cancel_grace_seconds)
        return self.get(job_id, actor=actor)

    def _execute(self, job_id):
        with connect_workflow_database(self.path) as db:
            changed = db.execute(
                "UPDATE workflow_authoring_jobs SET status='running',updated_at=?"
                " WHERE job_id=? AND status='queued' AND cancel_requested=0",
                (self._time(self.clock()), job_id),
            ).rowcount
            db.commit()
            row = db.execute(
                "SELECT * FROM workflow_authoring_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if not changed or row is None:
            return
        scope = self._open_scope(job_id)
        try:
            with cancellable(scope):
                previous_ir = None
                if row["job_type"] == "generate":
                    outcome = self.authoring.generate(
                        row["prompt"], language=row["display_language"],
                        agent=row["requested_agent"],
                        workflow_id=row["workflow_id"],
                        on_progress=lambda stage, attempt, maximum: self._record_progress(
                            job_id, stage, attempt, maximum,
                        ),
                        on_diagnostics=lambda attempt, maximum, findings: (
                            self._record_validation_diagnostics(
                                job_id, attempt, maximum, findings,
                            )
                        ),
                    )
                    latest = 0
                else:
                    with connect_workflow_database(
                        self.workflow_path, read_only=True,
                    ) as db:
                        current = db.execute(
                            "SELECT version,source_text,canonical_ir_json"
                            " FROM workflow_versions"
                            " WHERE workflow_id=? ORDER BY version DESC LIMIT 1",
                            (row["workflow_id"],),
                        ).fetchone()
                    if current is None or not current["source_text"]:
                        raise ValueError("workflow source is unavailable")
                    previous_ir = json.loads(current["canonical_ir_json"])
                    instruction = row["prompt"]
                    if row["mode"] == "regenerate":
                        instruction = (
                            "Redesign the whole workflow while preserving its identity. "
                            + instruction
                        )
                    outcome = self.authoring.revise(
                        current["source_text"], instruction,
                        expected_workflow_id=row["workflow_id"],
                        language=row["display_language"],
                        agent=row["requested_agent"],
                        on_progress=lambda stage, attempt, maximum: self._record_progress(
                            job_id, stage, attempt, maximum,
                        ),
                        on_diagnostics=lambda attempt, maximum, findings: (
                            self._record_validation_diagnostics(
                                job_id, attempt, maximum, findings,
                            )
                        ),
                    )
                    latest = int(current["version"])
                with connect_workflow_database(self.path, read_only=True) as db:
                    cancelled = db.execute(
                        "SELECT cancel_requested FROM workflow_authoring_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()["cancel_requested"]
                if cancelled:
                    return
                if (
                    self.timeout_seconds is not None
                    and row["deadline_at"]
                    and self.clock() >= datetime.fromisoformat(
                        str(row["deadline_at"]).replace("Z", "+00:00")
                    )
                ):
                    self._expire_due()
                    return
                self._record_progress(job_id, "publishing")
                record = self.publisher.publish_workflow(
                    outcome.source, source_name="<authoring-job>", source_format="json",
                    expected_latest_version=latest, actor=row["actor"],
                )
                result = {
                    "workflow_id": record.workflow_id,
                    "definition_hash": record.definition_hash.value,
                    "name": record.ir.name, "description": record.ir.description,
                    "node_count": len(record.ir.nodes),
                }
                if previous_ir is not None:
                    result["change_summary"] = self._change_summary(
                        previous_ir, record.ir, getattr(outcome, "change_summary", ()),
                    )
                self._settle(
                    job_id, "done", result=result,
                    attempts=getattr(outcome, "attempts", None),
                )
        except AuthoringUnknownResultError as exc:
            # Started and then silenced. Nothing is published and nothing is
            # retried: the Agent may already have done — and been charged for
            # — the work, so the only honest record is "we do not know".
            self._settle_quietly(
                job_id, "failed", error_code="unknown_external_result",
                error_message=str(exc)[:1000],
            )
        except AuthoringFailedError as exc:
            # Every attempt produced something the compiler refused. Keep the
            # findings: which rule the Agent broke is the only evidence there
            # is for whether the prompt needs to say something differently.
            self._settle_quietly(
                job_id, "failed", error_code=type(exc).__name__,
                error_message=str(exc)[:1000],
                attempts=exc.attempts,
                diagnostics=list(exc.diagnostics),
            )
        except Exception as exc:
            self._settle_quietly(
                job_id, "failed", error_code=type(exc).__name__,
                error_message=str(exc)[:1000],
            )
        finally:
            self._close_scope(job_id)

    def _settle_quietly(self, job_id, status, **fields):
        try:
            self._settle(job_id, status, **fields)
        except Exception:
            # The owning process may be shutting down after a response. Job
            # state is durable whenever its database still exists.
            return

    @staticmethod
    def _label_of(node) -> str:
        """What a reader calls this step, falling back to its id.

        Accepts either an IR node object or the stored mapping, because the
        summary compares the definition that was published against the one
        that came out of the database.
        """

        label = node.get("label") if isinstance(node, Mapping) else getattr(node, "label", None)
        if isinstance(label, str) and label.strip():
            return label.strip()
        node_id = node.get("id") if isinstance(node, Mapping) else getattr(node, "id", node)
        return str(node_id)

    @staticmethod
    def _shape_of(node) -> tuple:
        """The parts of a node a reader would call "changed".

        Its name, the Agent behind it, and the ports it exposes. Ordering and
        internal identity are deliberately excluded: moving a node in the file
        is not a change anybody asked about.
        """

        if isinstance(node, Mapping):
            handler = node.get("handler") or {}
            return (
                node.get("label"),
                handler.get("name"), handler.get("version"),
                tuple(sorted(port["id"] for port in node.get("inputs") or ())),
                tuple(sorted(port["id"] for port in node.get("outputs") or ())),
            )
        handler = getattr(node, "handler", None)
        return (
            getattr(node, "label", None),
            None if handler is None else handler.name,
            None if handler is None else handler.version,
            tuple(sorted(port.id for port in node.inputs)),
            tuple(sorted(port.id for port in node.outputs)),
        )

    @classmethod
    def _change_summary(cls, previous_ir, ir, agent_summary):
        """What changed, said the way the person who asked would say it.

        The Agent's own summary is preferred — it knows which edit it made and
        can name it — but only after the server has checked that every entry
        names a node that really exists on the side it claims. An Agent that
        describes a step it did not produce would otherwise put words in the
        Runtime's mouth. Anything unverified is dropped, and if nothing
        survives the structural diff supplies a plain, honest list.
        """

        before = {str(item["id"]): item for item in previous_ir.get("nodes", ())}
        after = {node.id: node for node in ir.nodes}
        verified = [
            entry for entry in agent_summary or ()
            if entry["node_id"] in (before if entry["kind"] == "removed" else after)
        ]
        if verified:
            return {"source": "agent", "entries": verified}

        entries = [
            {"kind": "added", "node_id": node_id, "label": cls._label_of(after[node_id])}
            for node_id in after.keys() - before.keys()
        ] + [
            {"kind": "removed", "node_id": node_id, "label": cls._label_of(before[node_id])}
            for node_id in before.keys() - after.keys()
        ] + [
            # A step that kept its id but changed its name, its Agent or its
            # ports. Without this, replacing a node's Agent reads as "nothing
            # changed", which is the one answer that is certainly wrong.
            {"kind": "changed", "node_id": node_id, "label": cls._label_of(after[node_id])}
            for node_id in sorted(after.keys() & before.keys())
            if cls._shape_of(before[node_id]) != cls._shape_of(after[node_id])
        ]
        entries.sort(key=lambda entry: (entry["kind"], entry["node_id"]))
        before_edges = {str(item["id"]) for item in previous_ir.get("edges", ())}
        after_edges = {item.id for item in ir.edges}
        return {
            "source": "diff",
            "entries": entries,
            # Connection changes have no step to name, so they stay a count
            # rather than becoming a sentence about an edge id.
            "edges_added": len(after_edges - before_edges),
            "edges_removed": len(before_edges - after_edges),
        }

    def _expire_due(self):
        # Existing rows own their deadline. `timeout_seconds` decides whether
        # new rows get one; removing that configuration after a restart must
        # not revoke deadlines already persisted for queued or running Jobs.
        stamp = self._time(self.clock())
        with connect_workflow_database(self.path) as db:
            expired = list(db.execute(
                "SELECT job_id,actor FROM workflow_authoring_jobs"
                " WHERE status IN ('queued','running')"
                " AND deadline_at IS NOT NULL AND deadline_at<>'' AND deadline_at<=?",
                (stamp,),
            ))
            db.execute(
                "UPDATE workflow_authoring_jobs SET status='failed',"
                " cancel_requested=1,error_code='authoring_timeout',"
                " error_message='Workflow authoring exceeded its deadline',"
                " updated_at=? WHERE status IN ('queued','running')"
                " AND deadline_at IS NOT NULL AND deadline_at<>'' AND deadline_at<=?",
                (stamp, stamp),
            )
            for row in expired:
                persist_audit(
                    db, run_id=None, actor=row["actor"],
                    action="workflow.authoring.timeout",
                    target_id=row["job_id"], decision="failed",
                    details={}, occurred_at=self.clock(),
                )
            db.commit()
        # Mark durable state first, then stop any child owned by this process.
        # CancelScope remembers cancellation, so this is also safe in the
        # narrow window before the generator attaches its process handle.
        for row in expired:
            with self._scope_lock:
                scope = self._scopes.get(row["job_id"])
            if scope is not None:
                scope.cancel(grace_seconds=self.cancel_grace_seconds)

    def _settle(
        self, job_id, status, *, result=None, error_code=None, error_message=None,
        attempts=None, diagnostics=None,
    ):
        with connect_workflow_database(self.path) as db:
            row = db.execute(
                "SELECT actor FROM workflow_authoring_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            changed = db.execute(
                "UPDATE workflow_authoring_jobs SET status=?,result_json=?,"
                " error_code=?,error_message=?,attempts=?,diagnostics_json=?,"
                " updated_at=?"
                " WHERE job_id=? AND status='running' AND cancel_requested=0",
                (
                    status, None if result is None else json.dumps(result, ensure_ascii=False),
                    error_code, error_message,
                    None if attempts is None else int(attempts),
                    None if not diagnostics else json.dumps(
                        list(diagnostics), ensure_ascii=False,
                    ),
                    self._time(self.clock()), job_id,
                ),
            ).rowcount
            if changed and row is not None:
                persist_audit(
                    db, run_id=None, actor=row["actor"],
                    action="workflow.authoring.complete",
                    target_id=job_id, decision=status,
                    details={
                        "error_code": error_code,
                        "unknown_result": error_code in UNKNOWN_RESULT_CODES,
                    },
                    occurred_at=self.clock(),
                )
            db.commit()
