"""Persistent workflow editing drafts (docs/ui/workflow-editor-implementation-plan.md).

Editing never touches a published WorkflowVersion: an actor edits a durable
Draft and publishing INSERTs a new immutable version through the same
``WorkflowDefinitionService`` the CLI uses. The Draft is an aggregate with an
optimistic ``revision``; every save, validate, publish and discard is a CAS.

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
from datetime import datetime
import json
from pathlib import Path
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


class WorkflowDraftApplicationService:
    def __init__(self, path: Path | str, definitions: WorkflowDefinitionService) -> None:
        self.path = Path(path)
        self.definitions = definitions

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
            audit(
                db, run_id=None, actor=actor, action="workflow.draft.discard",
                target_id=str(draft_id), decision="allowed",
                details={}, occurred_at=now,
            )
            db.commit()
            return self._read(db, draft_id)

    # -- internals ---------------------------------------------------------

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
        publish again would rely on content idempotency to save us. Completing
        it here makes the recovery visible instead of accidental (plan §9).
        """
        if record.status != "active" or record.validated_definition_hash is None:
            return None
        if record.validated_source_hash != record.source_hash:
            return None
        with connect_workflow_database(self.path) as db:
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
