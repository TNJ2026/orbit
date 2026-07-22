"""Persistent workflow editing drafts (docs/ui/workflow-editor-implementation-plan.md).

Editing never touches a published WorkflowVersion: an actor edits a durable
Draft and publishing INSERTs a new immutable version through the same
``WorkflowDefinitionService`` the CLI uses. The Draft is an aggregate with an
optimistic ``revision``; every candidate, decision, publish and discard is a CAS.

Concurrency and failure semantics worth naming:

* **One active draft per (workflow, actor)** — enforced by a partial unique
  index. Creating with the same base resumes; a different base surfaces the
  existing draft in a typed conflict, never silently rebasing it.
* **Validation is synchronous** and happens inside one transaction; there is
  no observable "validating" state.
* **Publish crash window** — the version INSERT and the draft's completion
  are different transactions. If the process dies between them, the retry of
  the same publish is safe (content idempotency returns the same version) and
  ``_reconcile`` finishes the bookkeeping on the next read: a draft whose
  validated definition hash already exists as a published version is marked
  published rather than offered for a second publish.
* **No-op publishes succeed**: publishing a draft identical to its base
  returns the base version (`published_version == base_version`) and mints
  nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
from typing import Any, Mapping

from ..domain.ids import EntityId, new_id
from ..domain.serialization import definition_hash
from ..dsl import DiagnosticError
from ..persistence.database import connect_workflow_database
from ..persistence.control import audit
from ..persistence.workflow_versions import PublishConflictError
from .workflows import WorkflowDefinitionService

MAX_SOURCE_BYTES = 256 * 1024


class DraftNotFoundError(LookupError):
    pass


class DraftForbiddenError(PermissionError):
    pass


class DraftVersionConflictError(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"draft revision conflict: expected {expected}, actual {actual}")
        self.expected = expected
        self.actual = actual


class DraftAlreadyActiveError(RuntimeError):
    """A different-base create collided with the actor's active draft."""

    def __init__(self, draft: Mapping[str, Any]) -> None:
        super().__init__("an active draft already exists for this workflow")
        self.draft = dict(draft)


class DraftNotValidatedError(RuntimeError):
    pass


class DraftSourceTooLargeError(ValueError):
    def __init__(self, size: int) -> None:
        super().__init__(
            f"draft source is {size} bytes; the limit is {MAX_SOURCE_BYTES}"
        )
        self.size = size


class WorkflowVersionConflictError(RuntimeError):
    def __init__(self, base_version: int, latest_version: int) -> None:
        super().__init__(
            f"workflow moved on: draft base is v{base_version}, latest is v{latest_version}"
        )
        self.base_version = base_version
        self.latest_version = latest_version


class SourceUnavailableError(RuntimeError):
    """The base version predates stored source; it is viewable, not editable."""


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    workflow_id: str
    base_version: int
    actor: str
    source_format: str
    source_text: str
    source_hash: str
    validation_status: str
    validated_source_hash: str | None
    validated_definition_hash: str | None
    diagnostics: tuple[Mapping[str, Any], ...]
    revision: int
    status: str
    created_at: str
    updated_at: str
    published_version: int | None


def _draft_diagnostic(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project old and new stored diagnostics onto WorkflowDraft 2.0."""
    item = dict(value)
    item["json_path"] = item.pop("path", item.get("json_path", "$"))
    return item


def _record(row) -> DraftRecord:
    return DraftRecord(
        draft_id=row["draft_id"], workflow_id=row["workflow_id"],
        base_version=row["base_version"], actor=row["actor"],
        source_format=row["source_format"], source_text=row["source_text"],
        source_hash=row["source_hash"],
        validation_status=row["validation_status"],
        validated_source_hash=row["validated_source_hash"],
        validated_definition_hash=row["validated_definition_hash"],
        diagnostics=tuple(
            _draft_diagnostic(item)
            for item in json.loads(row["diagnostics_json"])
        ),
        revision=row["revision"], status=row["status"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        published_version=row["published_version"],
    )


def _source_hash(source: str) -> str:
    return definition_hash({"draft_source": source}).value


def _token_hash(value: str) -> str:
    """Only the hash of a lease token is stored, as for planner attempts."""
    return hashlib.sha256(value.encode()).hexdigest()


class RevisionUnavailableError(ValueError):
    """No agent reviser is wired, so prompt-driven editing cannot run."""


class DraftRevisionStateError(ValueError):
    """The requested candidate decision is not valid for the current draft."""


class RevisionNotFoundError(LookupError):
    """No such revision job for this draft and actor."""


class RevisionLeaseError(RuntimeError):
    """A worker tried to settle a job it no longer owns."""


@dataclass(frozen=True)
class DraftRevisionRecord:
    revision_id: str
    draft_id: str
    base_draft_revision: int
    instruction_text: str
    instruction_hash: str
    previous_source_text: str
    previous_source_hash: str
    previous_validation_status: str
    previous_validated_source_hash: str | None
    previous_definition_hash: str | None
    # Unset while the job is queued, running or failed: the Agent has not
    # produced a compiler-accepted candidate yet.
    proposed_source_text: str | None
    proposed_source_hash: str | None
    proposed_definition_hash: str | None
    attempts: int | None
    status: str
    created_at: str
    decided_at: str | None
    decided_by: str | None
    cancel_requested: bool = False
    fencing_token: int = 0
    agent_command: str | None = None
    model_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    requested_agent: str | None = None

    @property
    def in_flight(self) -> bool:
        return self.status in {"queued", "running"}


def _revision_record(row) -> DraftRevisionRecord:
    return DraftRevisionRecord(
        revision_id=row["revision_id"], draft_id=row["draft_id"],
        base_draft_revision=row["base_draft_revision"],
        instruction_text=row["instruction_text"],
        instruction_hash=row["instruction_hash"],
        previous_source_text=row["previous_source_text"],
        previous_source_hash=row["previous_source_hash"],
        previous_validation_status=row["previous_validation_status"],
        previous_validated_source_hash=row["previous_validated_source_hash"],
        previous_definition_hash=row["previous_definition_hash"],
        proposed_source_text=row["proposed_source_text"],
        proposed_source_hash=row["proposed_source_hash"],
        proposed_definition_hash=row["proposed_definition_hash"],
        attempts=row["attempts"], status=row["status"],
        created_at=row["created_at"], decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        cancel_requested=bool(row["cancel_requested"]),
        fencing_token=int(row["fencing_token"]),
        agent_command=row["agent_command"], model_id=row["model_id"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        duration_ms=row["duration_ms"], error_code=row["error_code"],
        error_message=row["error_message"],
        requested_agent=row["requested_agent"],
    )


class WorkflowDraftApplicationService:
    def __init__(
        self, path: Path | str, definitions: WorkflowDefinitionService,
        *, reviser=None,
    ) -> None:
        self.path = Path(path)
        self.definitions = definitions
        # Callable[[current_source, instruction], GenerationOutcome]. None when
        # no generation-capable agent CLI was discovered; the editor then has
        # no way to change a workflow (agent-only editing).
        self.reviser = reviser

    # -- reads -------------------------------------------------------------

    def get(self, draft_id: EntityId, *, actor: str, now: datetime) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            row = db.execute(
                "SELECT * FROM workflow_drafts WHERE draft_id=?", (str(draft_id),)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError(f"draft not found: {draft_id}")
            if row["actor"] != actor:
                # Indistinguishable from absent: draft ids must not leak
                # other actors' editing activity.
                raise DraftNotFoundError(f"draft not found: {draft_id}")
            record = _record(row)
        reconciled = self._reconcile(record, now)
        return reconciled if reconciled is not None else record

    def revision_context(
        self, draft_id: EntityId, *, actor: str, limit: int = 10,
    ) -> tuple[DraftRevisionRecord | None, tuple[DraftRevisionRecord, ...], bool]:
        """Return the current revision, recent decisions and undo availability.

        "Current" spans the whole job: queued and running come back too, so a
        reloaded page can show work still in flight rather than an empty panel
        that looks like nothing was ever asked for.
        """

        with connect_workflow_database(self.path) as db:
            row = db.execute(
                "SELECT actor, source_hash FROM workflow_drafts WHERE draft_id=?",
                (str(draft_id),),
            ).fetchone()
            if row is None or row["actor"] != actor:
                raise DraftNotFoundError(f"draft not found: {draft_id}")
            pending_row = db.execute(
                "SELECT * FROM workflow_draft_revisions"
                " WHERE draft_id=? AND status IN ('queued','running','pending')",
                (str(draft_id),),
            ).fetchone()
            history_rows = db.execute(
                "SELECT * FROM workflow_draft_revisions WHERE draft_id=?"
                " ORDER BY base_draft_revision DESC, revision_id DESC LIMIT ?",
                (str(draft_id), max(1, min(int(limit), 50))),
            ).fetchall()
            undoable = db.execute(
                "SELECT 1 FROM workflow_draft_revisions"
                " WHERE draft_id=? AND status='accepted'"
                " AND proposed_source_hash=? LIMIT 1",
                (str(draft_id), row["source_hash"]),
            ).fetchone() is not None
        return (
            None if pending_row is None else _revision_record(pending_row),
            tuple(_revision_record(item) for item in history_rows),
            undoable,
        )

    # -- lifecycle ---------------------------------------------------------

    def create_or_resume(
        self, workflow_id: str, *, base_version: int | None, actor: str,
        now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT * FROM workflow_drafts WHERE workflow_id=? AND actor=?"
                " AND status='active'",
                (workflow_id, actor),
            ).fetchone()
            if base_version is None:
                latest = db.execute(
                    "SELECT MAX(version) FROM workflow_versions WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()[0]
                if latest is None:
                    raise DraftNotFoundError(f"workflow not found: {workflow_id}")
                base_version = int(latest)
            if active is not None:
                record = _record(active)
                if record.base_version == int(base_version):
                    db.commit()
                    reconciled = self._reconcile(record, now)
                    return reconciled if reconciled is not None else record
                raise DraftAlreadyActiveError({
                    "draft_id": record.draft_id,
                    "base_version": record.base_version,
                    "updated_at": record.updated_at,
                })
            version = db.execute(
                "SELECT source_format, source_text FROM workflow_versions"
                " WHERE workflow_id=? AND version=?",
                (workflow_id, int(base_version)),
            ).fetchone()
            if version is None:
                raise DraftNotFoundError(
                    f"workflow version not found: {workflow_id} v{base_version}"
                )
            if version["source_text"] is None:
                raise SourceUnavailableError(
                    f"{workflow_id} v{base_version} has no stored source"
                )
            draft_id = str(new_id("workflow_draft"))
            source = version["source_text"]
            db.execute(
                """INSERT INTO workflow_drafts(
                     draft_id, workflow_id, base_version, actor, source_format,
                     source_text, source_hash, validation_status,
                     validated_source_hash, validated_definition_hash,
                     diagnostics_json, revision, status, created_at, updated_at,
                     published_version
                   ) VALUES (?,?,?,?,?,?,?,'dirty',NULL,NULL,'[]',1,'active',?,?,NULL)""",
                (
                    draft_id, workflow_id, int(base_version), actor,
                    version["source_format"] or "json", source,
                    _source_hash(source), now.isoformat(), now.isoformat(),
                ),
            )
            audit(
                db, run_id=None, actor=actor, action="workflow.draft.create",
                target_id=draft_id, decision="allowed",
                details={"workflow_id": workflow_id, "base_version": int(base_version)},
                occurred_at=now,
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM workflow_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            return _record(row)

    def save(
        self, draft_id: EntityId, source: str, *, expected_revision: int,
        actor: str, now: datetime,
    ) -> DraftRecord:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise DraftSourceTooLargeError(len(source.encode("utf-8")))
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._owned_active(db, draft_id, actor, expected_revision)
            new_hash = _source_hash(source)
            # An unchanged-from-validated source keeps its verdict; anything
            # else is dirty until the compiler says otherwise.
            status = (
                "valid" if record.validated_source_hash == new_hash
                else "dirty"
            )
            db.execute(
                """UPDATE workflow_drafts SET source_text=?, source_hash=?,
                     validation_status=?, revision=revision+1, updated_at=?
                   WHERE draft_id=? AND revision=?""",
                (
                    source, new_hash, status, now.isoformat(),
                    str(draft_id), expected_revision,
                ),
            )
            audit(
                db, run_id=None, actor=actor, action="workflow.draft.save",
                target_id=str(draft_id), decision="allowed",
                details={"revision": expected_revision + 1, "source_hash": new_hash},
                occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def validate(
        self, draft_id: EntityId, *, expected_revision: int, actor: str,
        now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._owned_active(db, draft_id, actor, expected_revision)
            try:
                compiled = self.definitions.validate_workflow(
                    record.source_text, source_name="<draft>",
                    source_format=record.source_format,
                )
                verdict, diagnostics = "valid", []
                validated_definition = compiled.definition_hash.value
            except DiagnosticError as exc:
                verdict = "invalid"
                diagnostics = [
                    _draft_diagnostic(item.to_dict()) for item in exc.diagnostics
                ]
                validated_definition = None
            db.execute(
                """UPDATE workflow_drafts SET validation_status=?,
                     validated_source_hash=?, validated_definition_hash=?,
                     diagnostics_json=?, revision=revision+1, updated_at=?
                   WHERE draft_id=? AND revision=?""",
                (
                    verdict,
                    record.source_hash if verdict == "valid" else None,
                    validated_definition,
                    json.dumps(diagnostics, ensure_ascii=False),
                    now.isoformat(), str(draft_id), expected_revision,
                ),
            )
            audit(
                db, run_id=None, actor=actor, action="workflow.draft.validate",
                target_id=str(draft_id), decision=verdict,
                details={"diagnostic_count": len(diagnostics)},
                occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def publish(
        self, draft_id: EntityId, *, expected_revision: int, actor: str,
        now: datetime,
    ) -> tuple[DraftRecord, Mapping[str, Any]]:
        with connect_workflow_database(self.path) as db:
            record = self._owned_active(db, draft_id, actor, expected_revision)
            if self._pending(db, draft_id) is not None:
                raise DraftRevisionStateError(
                    "the pending Agent revision must be accepted or rejected"
                )
        if (
            record.validation_status != "valid"
            or record.validated_source_hash != record.source_hash
        ):
            raise DraftNotValidatedError(
                "the draft's current source has not passed validation"
            )
        try:
            version = self.definitions.publish_workflow(
                record.source_text, source_name="<draft>",
                source_format=record.source_format,
                expected_latest_version=record.base_version,
                actor=actor,
            )
        except PublishConflictError as exc:
            with connect_workflow_database(self.path) as db:
                audit(
                    db, run_id=None, actor=actor,
                    action="workflow.draft.publish", target_id=str(draft_id),
                    decision="conflict",
                    details={"base_version": exc.expected, "latest_version": exc.actual},
                    occurred_at=now,
                )
                db.commit()
            raise WorkflowVersionConflictError(record.base_version, exc.actual)
        finished = self._finish_publish(
            draft_id, expected_revision, version.version.value, actor, now
        )
        return finished, {
            "workflow_id": version.workflow_id,
            "version": version.version.value,
            "definition_hash": version.definition_hash.value,
        }

    def discard(
        self, draft_id: EntityId, *, expected_revision: int, actor: str,
        now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned_active(db, draft_id, actor, expected_revision)
            db.execute(
                """UPDATE workflow_drafts SET status='discarded',
                     revision=revision+1, updated_at=?
                   WHERE draft_id=? AND revision=?""",
                (now.isoformat(), str(draft_id), expected_revision),
            )
            db.execute(
                "UPDATE workflow_draft_revisions SET status='rejected',"
                " decided_at=?, decided_by=? WHERE draft_id=? AND status='pending'",
                (now.isoformat(), actor, str(draft_id)),
            )
            audit(
                db, run_id=None, actor=actor, action="workflow.draft.discard",
                target_id=str(draft_id), decision="allowed",
                details={}, occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def revise(
        self, draft_id: EntityId, instruction: str, *, expected_revision: int,
        actor: str, now: datetime, agent: str | None = None,
    ) -> DraftRecord:
        """Enqueue an Agent revision and return immediately.

        The CLI call can take minutes, so it does not run inside the request.
        The job is recorded as ``queued`` here; a dispatcher leases it, runs
        the Agent and settles it into a ``pending`` candidate or a ``failed``
        job. A reload therefore always finds the truth in the database rather
        than in a request that may have been abandoned.
        """
        if self.reviser is None:
            raise RevisionUnavailableError("no agent reviser is configured")
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("instruction is required")
        instruction_hash = definition_hash({"instruction": instruction}).value
        revision_id = str(new_id("workflow_revision"))
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._owned_active(db, draft_id, actor, expected_revision)
            active = self._active_revision(db, draft_id)
            if active is not None:
                raise DraftRevisionStateError(
                    "an Agent revision is already queued, running or awaiting"
                    " a decision for this draft"
                )
            db.execute(
                """INSERT INTO workflow_draft_revisions(
                     revision_id,draft_id,base_draft_revision,instruction_text,
                     instruction_hash,previous_source_text,previous_source_hash,
                     previous_validation_status,previous_validated_source_hash,
                     previous_definition_hash,status,created_at,requested_agent
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    revision_id, str(draft_id), expected_revision, instruction,
                    instruction_hash, record.source_text, record.source_hash,
                    record.validation_status, record.validated_source_hash,
                    record.validated_definition_hash, now.isoformat(),
                    # Recorded now, honoured minutes later: the dispatcher must
                    # run the Agent that was chosen, not today's default.
                    (agent or None),
                ),
            )
            db.execute(
                "UPDATE workflow_drafts SET revision=revision+1, updated_at=?"
                " WHERE draft_id=? AND revision=?",
                (now.isoformat(), str(draft_id), expected_revision),
            )
            audit(
                db, run_id=None, actor=actor,
                action="workflow.draft.revise", target_id=str(draft_id),
                decision="queued",
                details={
                    "revision_id": revision_id,
                    "instruction_hash": instruction_hash,
                    "previous_source_hash": record.source_hash,
                },
                occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    # -- revision jobs -----------------------------------------------------

    def claim_revision(
        self, worker_id: str, now: datetime, *, lease_ttl: timedelta,
    ) -> tuple[DraftRevisionRecord, str] | None:
        """Lease the oldest queued job. Returns the job and its lease token."""

        if not worker_id.strip() or lease_ttl <= timedelta(0):
            raise ValueError("invalid revision lease")
        raw = secrets.token_urlsafe(32)
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workflow_draft_revisions WHERE status='queued'"
                " ORDER BY created_at, revision_id LIMIT 1"
            ).fetchone()
            if row is None:
                db.commit()
                return None
            if row["cancel_requested"]:
                # Cancelled before anyone picked it up: settle it without
                # spending a model call.
                self._settle_cancelled(db, row["revision_id"], now)
                db.commit()
                return None
            fence = int(row["fencing_token"]) + 1
            db.execute(
                """UPDATE workflow_draft_revisions
                     SET status='running', lease_owner=?, lease_token_hash=?,
                         lease_expires_at=?, fencing_token=?, started_at=?
                   WHERE revision_id=? AND status='queued'""",
                (
                    worker_id, _token_hash(raw),
                    (now + lease_ttl).isoformat(), fence, now.isoformat(),
                    row["revision_id"],
                ),
            )
            db.commit()
            claimed = db.execute(
                "SELECT * FROM workflow_draft_revisions WHERE revision_id=?",
                (row["revision_id"],),
            ).fetchone()
            return _revision_record(claimed), raw

    def execute_revision(
        self, job: DraftRevisionRecord, lease_token: str, *,
        clock=None, agent_command: str | None = None,
        model_id: str | None = None,
    ) -> DraftRevisionRecord:
        """Run the Agent for a leased job and settle it.

        The call happens outside any transaction. Whatever it produces — a
        candidate, a rejection from the compiler, or an unavailable CLI — is
        written back under the lease fence, with how long it took.
        """
        tick = clock or (lambda: datetime.now(timezone.utc))
        started = tick()
        try:
            outcome = self.reviser(
                job.previous_source_text, job.instruction_text,
                expected_workflow_id=self._workflow_id_for(job.draft_id),
                agent=job.requested_agent,
            )
        except Exception as exc:  # settled as a failure, never a lost job
            code = type(exc).__name__
            return self._settle_failure(
                job, lease_token, code=code, message=str(exc)[:500],
                started_at=started, finished_at=tick(),
                agent_command=agent_command, model_id=model_id,
            )
        if len(outcome.source.encode("utf-8")) > MAX_SOURCE_BYTES:
            return self._settle_failure(
                job, lease_token, code="DraftSourceTooLargeError",
                message=f"revised source exceeds {MAX_SOURCE_BYTES} bytes",
                started_at=started, finished_at=tick(),
                agent_command=agent_command, model_id=model_id,
            )
        return self._settle_candidate(
            job, lease_token, outcome, started_at=started, finished_at=tick(),
            agent_command=agent_command, model_id=model_id,
        )

    def cancel_revision(
        self, draft_id: EntityId, revision_id: EntityId, *, actor: str,
        now: datetime,
    ) -> DraftRecord:
        """Ask an in-flight job to stop; settle it now when it never started."""

        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned_draft(db, draft_id, actor)
            row = db.execute(
                "SELECT * FROM workflow_draft_revisions"
                " WHERE revision_id=? AND draft_id=?",
                (str(revision_id), str(draft_id)),
            ).fetchone()
            if row is None:
                raise RevisionNotFoundError(f"revision not found: {revision_id}")
            if row["status"] not in {"queued", "running"}:
                raise DraftRevisionStateError(
                    f"revision is {row['status']} and cannot be cancelled"
                )
            if row["status"] == "queued":
                self._settle_cancelled(db, row["revision_id"], now)
            else:
                # Running: flag it. The worker notices and settles, so the
                # record never claims a result the Agent did not produce.
                db.execute(
                    "UPDATE workflow_draft_revisions SET cancel_requested=1"
                    " WHERE revision_id=?",
                    (row["revision_id"],),
                )
            audit(
                db, run_id=None, actor=actor,
                action="workflow.draft.revision.cancel",
                target_id=str(revision_id), decision="requested",
                details={"draft_id": str(draft_id), "status": row["status"]},
                occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def expire_revisions(self, now: datetime, *, limit: int = 50) -> tuple[str, ...]:
        """Fail jobs whose worker died holding the lease.

        Generation has no external side effect, so an abandoned job is simply
        failed and left for the operator to retry — quietly re-running it
        would spend another model call nobody asked for.
        """
        expired: list[str] = []
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT revision_id FROM workflow_draft_revisions"
                " WHERE status='running' AND lease_expires_at IS NOT NULL"
                " AND lease_expires_at<=? ORDER BY lease_expires_at LIMIT ?",
                (now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE workflow_draft_revisions
                         SET status='failed', error_code='lease_expired',
                             error_message='the worker running this revision stopped',
                             finished_at=?, lease_owner=NULL,
                             lease_token_hash=NULL, lease_expires_at=NULL
                       WHERE revision_id=? AND status='running'""",
                    (now.isoformat(), row["revision_id"]),
                )
                expired.append(row["revision_id"])
            db.commit()
        return tuple(expired)

    def accept_revision(
        self, draft_id: EntityId, *, expected_revision: int, actor: str,
        now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned_active(db, draft_id, actor, expected_revision)
            candidate = self._pending(db, draft_id)
            if candidate is None:
                raise DraftRevisionStateError("there is no pending Agent revision")
            db.execute(
                """UPDATE workflow_drafts SET source_text=?,source_hash=?,
                     source_format='json',validation_status='valid',
                     validated_source_hash=?,validated_definition_hash=?,
                     diagnostics_json='[]',revision=revision+1,updated_at=?
                   WHERE draft_id=? AND revision=?""",
                (
                    candidate.proposed_source_text, candidate.proposed_source_hash,
                    candidate.proposed_source_hash,
                    candidate.proposed_definition_hash, now.isoformat(),
                    str(draft_id), expected_revision,
                ),
            )
            db.execute(
                "UPDATE workflow_draft_revisions SET status='accepted',"
                " decided_at=?,decided_by=? WHERE revision_id=?",
                (now.isoformat(), actor, candidate.revision_id),
            )
            audit(
                db, run_id=None, actor=actor,
                action="workflow.draft.revision.accept",
                target_id=str(draft_id), decision="allowed",
                details={"revision_id": candidate.revision_id}, occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def reject_revision(
        self, draft_id: EntityId, *, expected_revision: int, actor: str,
        now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned_active(db, draft_id, actor, expected_revision)
            candidate = self._pending(db, draft_id)
            if candidate is None:
                raise DraftRevisionStateError("there is no pending Agent revision")
            db.execute(
                "UPDATE workflow_draft_revisions SET status='rejected',"
                " decided_at=?,decided_by=? WHERE revision_id=?",
                (now.isoformat(), actor, candidate.revision_id),
            )
            db.execute(
                "UPDATE workflow_drafts SET revision=revision+1,updated_at=?"
                " WHERE draft_id=? AND revision=?",
                (now.isoformat(), str(draft_id), expected_revision),
            )
            audit(
                db, run_id=None, actor=actor,
                action="workflow.draft.revision.reject",
                target_id=str(draft_id), decision="allowed",
                details={"revision_id": candidate.revision_id}, occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def undo_revision(
        self, draft_id: EntityId, *, expected_revision: int, actor: str,
        now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._owned_active(db, draft_id, actor, expected_revision)
            if self._pending(db, draft_id) is not None:
                raise DraftRevisionStateError(
                    "reject the pending Agent revision before undoing"
                )
            row = db.execute(
                "SELECT * FROM workflow_draft_revisions"
                " WHERE draft_id=? AND status='accepted'"
                " AND proposed_source_hash=?"
                " ORDER BY base_draft_revision DESC,revision_id DESC LIMIT 1",
                (str(draft_id), record.source_hash),
            ).fetchone()
            if row is None:
                raise DraftRevisionStateError("there is no accepted revision to undo")
            accepted = _revision_record(row)
            db.execute(
                """UPDATE workflow_drafts SET source_text=?,source_hash=?,
                     validation_status=?,validated_source_hash=?,
                     validated_definition_hash=?,diagnostics_json='[]',
                     revision=revision+1,updated_at=?
                   WHERE draft_id=? AND revision=?""",
                (
                    accepted.previous_source_text, accepted.previous_source_hash,
                    accepted.previous_validation_status,
                    accepted.previous_validated_source_hash,
                    accepted.previous_definition_hash, now.isoformat(),
                    str(draft_id), expected_revision,
                ),
            )
            db.execute(
                "UPDATE workflow_draft_revisions SET status='undone',"
                " decided_at=?,decided_by=? WHERE revision_id=?",
                (now.isoformat(), actor, accepted.revision_id),
            )
            audit(
                db, run_id=None, actor=actor,
                action="workflow.draft.revision.undo",
                target_id=str(draft_id), decision="allowed",
                details={"revision_id": accepted.revision_id}, occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _pending(db, draft_id: EntityId) -> DraftRevisionRecord | None:
        row = db.execute(
            "SELECT * FROM workflow_draft_revisions"
            " WHERE draft_id=? AND status='pending'",
            (str(draft_id),),
        ).fetchone()
        return None if row is None else _revision_record(row)

    @staticmethod
    def _active_revision(db, draft_id: EntityId) -> DraftRevisionRecord | None:
        """Queued, running or awaiting a decision — the single-flight guard."""
        row = db.execute(
            "SELECT * FROM workflow_draft_revisions WHERE draft_id=?"
            " AND status IN ('queued','running','pending')",
            (str(draft_id),),
        ).fetchone()
        return None if row is None else _revision_record(row)

    def _owned_draft(self, db, draft_id: EntityId, actor: str) -> DraftRecord:
        row = db.execute(
            "SELECT * FROM workflow_drafts WHERE draft_id=?", (str(draft_id),)
        ).fetchone()
        if row is None or row["actor"] != actor:
            raise DraftNotFoundError(f"draft not found: {draft_id}")
        return _record(row)

    def _workflow_id_for(self, draft_id: str) -> str:
        with connect_workflow_database(self.path, read_only=True) as db:
            row = db.execute(
                "SELECT workflow_id FROM workflow_drafts WHERE draft_id=?",
                (str(draft_id),),
            ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"draft not found: {draft_id}")
        return row["workflow_id"]

    @staticmethod
    def _settle_cancelled(db, revision_id: str, now: datetime) -> None:
        db.execute(
            """UPDATE workflow_draft_revisions
                 SET status='cancelled', cancel_requested=1, finished_at=?,
                     lease_owner=NULL, lease_token_hash=NULL,
                     lease_expires_at=NULL
               WHERE revision_id=? AND status IN ('queued','running')""",
            (now.isoformat(), revision_id),
        )

    def _fenced_update(
        self, db, job: DraftRevisionRecord, lease_token: str, sql: str,
        params: tuple,
    ) -> None:
        """Apply a settle only if this worker still owns the lease."""
        row = db.execute(
            "SELECT status, lease_token_hash, fencing_token"
            " FROM workflow_draft_revisions WHERE revision_id=?",
            (job.revision_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "running"
            or row["lease_token_hash"] != _token_hash(lease_token)
            or int(row["fencing_token"]) != job.fencing_token
        ):
            raise RevisionLeaseError(
                f"the lease on {job.revision_id} is no longer held"
            )
        db.execute(sql, params)

    def _settle_candidate(
        self, job, lease_token, outcome, *, started_at, finished_at,
        agent_command, model_id,
    ) -> DraftRevisionRecord:
        proposed_hash = _source_hash(outcome.source)
        duration = int((finished_at - started_at).total_seconds() * 1000)
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            cancelled = db.execute(
                "SELECT cancel_requested FROM workflow_draft_revisions"
                " WHERE revision_id=?", (job.revision_id,),
            ).fetchone()
            if cancelled is not None and cancelled["cancel_requested"]:
                # The operator asked to stop while the Agent was working; the
                # answer is discarded rather than presented as a candidate.
                self._settle_cancelled(db, job.revision_id, finished_at)
                db.commit()
                return self._revision(db, job.revision_id)
            self._fenced_update(
                db, job, lease_token,
                """UPDATE workflow_draft_revisions
                     SET status='pending', proposed_source_text=?,
                         proposed_source_hash=?, proposed_definition_hash=?,
                         attempts=?, agent_command=?, model_id=?,
                         finished_at=?, duration_ms=?, lease_owner=NULL,
                         lease_token_hash=NULL, lease_expires_at=NULL
                   WHERE revision_id=?""",
                (
                    outcome.source, proposed_hash, outcome.definition_hash,
                    outcome.attempts, agent_command, model_id,
                    finished_at.isoformat(), duration, job.revision_id,
                ),
            )
            audit(
                db, run_id=None, actor="system:revision",
                action="workflow.draft.revise", target_id=job.draft_id,
                decision="candidate",
                details={
                    "revision_id": job.revision_id,
                    "instruction_hash": job.instruction_hash,
                    "previous_source_hash": job.previous_source_hash,
                    "proposed_source_hash": proposed_hash,
                    "definition_hash": outcome.definition_hash,
                    "attempts": outcome.attempts,
                    "agent_command": agent_command, "model_id": model_id,
                    "duration_ms": duration,
                },
                occurred_at=finished_at,
            )
            db.commit()
            return self._revision(db, job.revision_id)

    def _settle_failure(
        self, job, lease_token, *, code, message, started_at, finished_at,
        agent_command, model_id,
    ) -> DraftRevisionRecord:
        duration = int((finished_at - started_at).total_seconds() * 1000)
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._fenced_update(
                db, job, lease_token,
                """UPDATE workflow_draft_revisions
                     SET status='failed', error_code=?, error_message=?,
                         agent_command=?, model_id=?, finished_at=?,
                         duration_ms=?, lease_owner=NULL,
                         lease_token_hash=NULL, lease_expires_at=NULL
                   WHERE revision_id=?""",
                (
                    code, message, agent_command, model_id,
                    finished_at.isoformat(), duration, job.revision_id,
                ),
            )
            audit(
                db, run_id=None, actor="system:revision",
                action="workflow.draft.revise", target_id=job.draft_id,
                decision="failed",
                details={
                    "revision_id": job.revision_id, "error_code": code,
                    "agent_command": agent_command, "model_id": model_id,
                    "duration_ms": duration,
                },
                occurred_at=finished_at,
            )
            db.commit()
            return self._revision(db, job.revision_id)

    @staticmethod
    def _revision(db, revision_id: str) -> DraftRevisionRecord:
        return _revision_record(db.execute(
            "SELECT * FROM workflow_draft_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone())

    def _read(self, db, draft_id: EntityId) -> DraftRecord:
        row = db.execute(
            "SELECT * FROM workflow_drafts WHERE draft_id=?", (str(draft_id),)
        ).fetchone()
        return _record(row)

    def _owned_active(
        self, db, draft_id: EntityId, actor: str, expected_revision: int
    ) -> DraftRecord:
        row = db.execute(
            "SELECT * FROM workflow_drafts WHERE draft_id=?", (str(draft_id),)
        ).fetchone()
        if row is None or row["actor"] != actor:
            raise DraftNotFoundError(f"draft not found: {draft_id}")
        record = _record(row)
        if record.status != "active":
            raise DraftNotFoundError(f"draft is {record.status}: {draft_id}")
        if record.revision != expected_revision:
            raise DraftVersionConflictError(expected_revision, record.revision)
        return record

    def _finish_publish(
        self, draft_id: EntityId, expected_revision: int,
        published_version: int, actor: str, now: datetime,
    ) -> DraftRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """UPDATE workflow_drafts SET status='published',
                     published_version=?, revision=revision+1, updated_at=?
                   WHERE draft_id=? AND revision=? AND status='active'""",
                (published_version, now.isoformat(), str(draft_id), expected_revision),
            )
            if updated.rowcount != 1:
                # A concurrent tab bumped the revision between our check and
                # the version INSERT. The version exists; _reconcile completes
                # the bookkeeping on the next read. Reporting the conflict is
                # more honest than pretending this request finished the job.
                db.rollback()
                current = self._read(db, draft_id)
                raise DraftVersionConflictError(expected_revision, current.revision)
            audit(
                db, run_id=None, actor=actor, action="workflow.draft.publish",
                target_id=str(draft_id), decision="allowed",
                details={"published_version": published_version},
                occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    def _reconcile(self, record: DraftRecord, now: datetime) -> DraftRecord | None:
        """Finish a publish whose draft bookkeeping was lost to a crash.

        A validated draft whose definition hash already exists as a published
        version newer than or equal to its base was published; offering it for
        again would rely on content idempotency to save us. Completing it here
        makes the recovery visible instead of accidental (plan §9).
        """
        if record.status != "active" or record.validated_definition_hash is None:
            return None
        if record.validated_source_hash != record.source_hash:
            return None
        with connect_workflow_database(self.path) as db:
            if self._pending(db, EntityId.parse(record.draft_id)) is not None:
                return None
            row = db.execute(
                "SELECT version FROM workflow_versions"
                " WHERE workflow_id=? AND definition_hash=? AND version>=?",
                (
                    record.workflow_id, record.validated_definition_hash,
                    record.base_version,
                ),
            ).fetchone()
            if row is None:
                return None
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE workflow_drafts SET status='published',
                     published_version=?, revision=revision+1, updated_at=?
                   WHERE draft_id=? AND revision=? AND status='active'""",
                (
                    int(row["version"]), now.isoformat(),
                    record.draft_id, record.revision,
                ),
            )
            audit(
                db, run_id=None, actor=record.actor,
                action="workflow.draft.publish", target_id=record.draft_id,
                decision="reconciled",
                details={"published_version": int(row["version"])},
                occurred_at=now,
            )
            db.commit()
            fresh = db.execute(
                "SELECT * FROM workflow_drafts WHERE draft_id=?",
                (record.draft_id,),
            ).fetchone()
            return _record(fresh)
