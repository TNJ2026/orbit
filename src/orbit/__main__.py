"""CLI entry point: orbit serve | run | workflow | db."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3

import uvicorn

from . import __version__
from .platform.cutover import (
    ACKNOWLEDGE_FLAG, CutoverRequired, ensure_cutover_acknowledged, read_marker,
)
from .platform.projects import (
    project_db_path,
    project_state_dir,
    resolve_project_root,
    upsert_project,
)


def _runtime_db_path(explicit: str | None, *, acknowledged: bool = False) -> str:
    """Resolve the runtime database, gating on the cutover acknowledgement.

    Every command that touches the default database goes through here, so
    neither the path rule nor the gate can drift between `serve`, `workflow
    publish`, `run start` and `db check`. Putting the gate anywhere else is how
    `orbit workflow publish` came to write a fresh `runtime.db` for a project
    whose legacy data had never been acknowledged.

    An explicit `--db` is not gated: the gate protects the *default* path,
    where abandoning pre-migration data would otherwise be silent. Naming a
    database on the command line is already an explicit choice of which one.
    """

    if explicit:
        return explicit
    try:
        ensure_cutover_acknowledged(acknowledged=acknowledged)
    except CutoverRequired as exc:
        print(str(exc), flush=True)
        raise SystemExit(exc.exit_code) from None
    return str(project_db_path(resolve_project_root()))


def _artifact_root_path(explicit: str | None, db_path: str | Path) -> Path:
    """Resolve the local CAS beside the selected Runtime database by default."""

    if explicit:
        return Path(explicit).expanduser().absolute()
    return Path(db_path).expanduser().absolute().parent / "artifacts"


def _add_db_check_arguments(command) -> None:
    command.add_argument("--db", default=None, help="SQLite database path")
    command.add_argument("--run-id", default=None, help="Optional run:<id> scope")
    command.add_argument(
        "--json", action="store_true", help="Emit stable machine-readable JSON"
    )
    command.add_argument(
        "--drop-invalid-snapshots",
        action="store_true",
        help=(
            "Explicitly delete corrupt snapshots; event and projection data "
            "remain read-only"
        ),
    )


def _db_check_command(args) -> None:
    """Audit the runtime database. Read-only unless --drop-invalid-snapshots."""

    from .workflow.domain.ids import EntityId
    from .workflow.persistence.integrity import check_database

    db_path = _runtime_db_path(args.db)
    if not Path(db_path).exists():
        # A mistyped --db is the common case here. A stack trace tells the user
        # sqlite3 could not open a file; this tells them which one.
        raise SystemExit(f"orbit db check: no database at {db_path}")

    run_id = EntityId.parse(args.run_id) if args.run_id else None
    try:
        report = check_database(db_path, run_id=run_id)
    except sqlite3.DatabaseError as exc:
        raise SystemExit(f"orbit db check: cannot read {db_path}: {exc}") from None
    payload = report.to_dict()

    if args.drop_invalid_snapshots:
        snapshot_ids = tuple(
            item.entity_id
            for item in report.issues
            if item.code == "SNAPSHOT_CORRUPT" and item.entity_id is not None
        )
        if snapshot_ids:
            from .workflow.persistence import SQLiteUnitOfWork

            with SQLiteUnitOfWork(db_path) as uow:
                for snapshot_id in snapshot_ids:
                    uow.snapshots.delete(EntityId.parse(snapshot_id))
                uow.commit()
            report = check_database(db_path, run_id=run_id)
            payload = report.to_dict()
            payload["dropped_invalid_snapshots"] = list(snapshot_ids)

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif report.ok:
        print(
            f"ok: {report.checked_events} events, {report.checked_snapshots} snapshots"
        )
    else:
        print("\n".join(f"{item.code}: {item.message}" for item in report.issues))

    if not report.ok:
        raise SystemExit(4)


def _workflow_command(args) -> None:
    from .workflow.application import WorkflowDefinitionService, load_catalogs
    from .workflow.domain.serialization import canonical_json
    from .workflow.dsl import DiagnosticError, canonical_ir_json
    from .workflow.persistence import PublishConflictError, SQLiteWorkflowVersionStore

    machine_output = getattr(args, "json", False)
    try:
        catalogs = load_catalogs(args.catalog)
        source_path = Path(args.file)
        source = source_path.read_text(encoding="utf-8-sig")
        source_format = "json" if source_path.suffix.lower() == ".json" else "yaml"
        store = None
        if args.workflow_action == "publish":
            store = SQLiteWorkflowVersionStore(_runtime_db_path(args.db))
        service = WorkflowDefinitionService(catalogs, store)
        if args.workflow_action == "validate":
            compiled = service.validate_workflow(
                source, source_name=str(source_path), source_format=source_format
            )
            result = {
                "valid": True,
                "definition_hash": compiled.definition_hash.value,
                "workflow_id": compiled.ir.workflow_id,
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True) if machine_output else f"valid {result['workflow_id']} {result['definition_hash']}")
            return
        if args.workflow_action == "compile":
            compiled = service.compile_workflow(
                source, source_name=str(source_path), source_format=source_format
            )
            output = canonical_ir_json(compiled) + "\n"
            if args.output == "-":
                print(output, end="")
            else:
                Path(args.output).write_text(output, encoding="utf-8")
            return
        record = service.publish_workflow(
            source,
            source_name=str(source_path),
            source_format=source_format,
            expected_latest_version=args.expected_version,
            actor=args.actor,
        )
        result = {
            "workflow_id": record.workflow_id,
            "version": record.version.value,
            "definition_hash": record.definition_hash.value,
        }
        print(canonical_json(result) if machine_output else f"published {record.workflow_id}@{record.version.value} {record.definition_hash.value}")
    except DiagnosticError as exc:
        payload = [item.to_dict() for item in exc.diagnostics]
        if machine_output:
            print(json.dumps({"valid": False, "diagnostics": payload}, ensure_ascii=False, sort_keys=True))
        else:
            for item in exc.diagnostics:
                location = ""
                if item.source_range is not None:
                    location = f"{item.source_range.source}:{item.source_range.start_line}:{item.source_range.start_column}: "
                print(f"{location}{item.code} {item.json_path}: {item.message}")
        raise SystemExit(2) from None
    except PublishConflictError as exc:
        print(json.dumps({"code": "WORKFLOW_PUBLISH_CONFLICT", "expected": exc.expected, "actual": exc.actual}) if machine_output else str(exc))
        raise SystemExit(3) from None


def _run_command(args) -> None:
    """`orbit run start|inspect`.

    The CLI writes through the same RunApplicationService the HTTP API uses, so
    a start from the terminal and a start from the UI produce identical events.
    It talks to SQLite directly rather than to a running server: the kernel is
    the concurrency boundary, and adding an HTTP hop would only mean the CLI
    stops working whenever the server is down.
    """

    import uuid

    from .workflow.application.durable_runtime_service import (
        DurableRuntimeApplicationService,
    )
    from .workflow.application.run_service import (
        RunApplicationService, RunStartError,
    )

    db_path = _runtime_db_path(args.db)
    service = RunApplicationService(
        db_path, DurableRuntimeApplicationService(db_path),
        enforce_single_goal=True,
    )

    try:
        if args.run_action == "inspect":
            print(json.dumps(service.inspect(args.run_id), ensure_ascii=False, indent=2, sort_keys=True))
            return

        started = service.start_run(
            workflow_id=args.workflow_id,
            version=args.workflow_version,
            inputs=json.loads(args.input) if args.input else {},
            goal=args.goal or "",
            budget_microunits=args.budget_microunits,
            actor=args.actor,
            # A generated key still makes the start idempotent under retry
            # inside one invocation; passing --idempotency-key is what makes a
            # re-run of the whole command idempotent.
            idempotency_key=args.idempotency_key or uuid.uuid4().hex,
        )
    except (RunStartError, ValueError) as exc:
        raise SystemExit(f"orbit run: {exc}")

    if args.json:
        print(json.dumps(started.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        state = "replayed" if started.replayed else "started"
        print(f"{state} {started.run_id} ({started.workflow_id}@{started.workflow_version})")


def _serve(args) -> None:
    """Start the new Runtime composition root."""

    from .web.app import create_app
    from .web.builtin_handlers import BUILTIN_SCHEMAS, builtin_handlers
    from .web.local_identity import local_authorizer, loopback_authenticator
    from .web.schema_guard import MixedSchemaError, assert_runtime_schema
    from .workflow.artifacts import LocalCASBackend

    project_root = resolve_project_root()

    # `serve` is the one command that can *grant* the acknowledgement; the gate
    # itself lives in _runtime_db_path so every other command is covered too.
    db_path = _runtime_db_path(
        args.db, acknowledged=args.acknowledge_discard_legacy_data
    )
    if args.acknowledge_discard_legacy_data:
        marker = read_marker()
        if marker is not None:
            print(
                f"cutover acknowledged at {marker.acknowledged_at}; "
                "legacy files are left untouched",
                flush=True,
            )

    # Preserve the cutover fail-closed boundary: a refused legacy database must
    # not create even an empty Artifact directory as a startup side effect.
    try:
        assert_runtime_schema(db_path)
    except MixedSchemaError as exc:
        raise SystemExit(f"error: {exc}") from None

    artifact_root = _artifact_root_path(args.artifact_root, db_path)
    try:
        artifact_backend = LocalCASBackend(artifact_root)
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"orbit serve: cannot initialize Artifact store at "
            f"{artifact_root}: {exc}"
        ) from None

    handlers = list(builtin_handlers())
    if args.dev_tools:
        # Opt-in on purpose: this is the only switch that lets a workflow run a
        # child process against the checkout, so it is never the default.
        from .web.builtin_handlers import dev_tool_handlers
        from .workflow.handlers.dev_tools import VerifyProfile

        dev_handlers, tool_names = dev_tool_handlers(
            project_root,
            project_state_dir(project_root),
            verify_profiles=(
                VerifyProfile(
                    "unit", ("python", "-m", "unittest", "discover", "-s", "tests"),
                    "the project's unittest suite",
                ),
            ),
            environment={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
            },
        )
        handlers.extend(dev_handlers)
        print(f"dev tools: {', '.join(tool_names) or 'none granted'}", flush=True)

    try:
        app = create_app(
            db_path,
            handlers=handlers,
            schemas=BUILTIN_SCHEMAS,
            artifact_backend=artifact_backend,
            worker_count=args.runner_concurrency,
            discover_agents=not args.no_agent_discovery,
            serve_ui=True,
            authenticator=loopback_authenticator,
            authorizer=local_authorizer(),
        )
    except MixedSchemaError as exc:
        raise SystemExit(f"error: {exc}") from None

    upsert_project(
        project_root=project_root, db_path=db_path,
        host=args.host, port=args.port,
    )
    print(
        f"orbit Runtime listening on http://{args.host}:{args.port}/ui/ "
        f"(health: /health/ready, workers: {args.runner_concurrency}) "
        f"(db: {db_path}, artifacts: {artifact_backend.root})",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(prog="orbit", description="Local multi-agent workflow orchestrator")
    parser.add_argument(
        "--version", action="version", version=f"orbit {__version__}",
        help="Show the orbit version and exit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_cmd = sub.add_parser(
        "serve", help="Start the Runtime: API, UI, workers and timers"
    )
    serve_cmd.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve_cmd.add_argument("--port", type=int, default=8848, help="Port (default: 8848)")
    serve_cmd.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: per-project database under ~/.orbit/projects/)",
    )
    serve_cmd.add_argument(
        "--artifact-root",
        default=None,
        help=(
            "Local content-addressed Artifact directory "
            "(default: artifacts/ beside the Runtime database)"
        ),
    )
    serve_cmd.add_argument(
        "--runner-concurrency",
        type=int,
        default=5,
        help="How many jobs the in-process worker runs in parallel (default: 5).",
    )
    serve_cmd.add_argument(
        "--no-agent-discovery",
        action="store_true",
        help="Skip probing for installed Agent CLIs at startup",
    )
    serve_cmd.add_argument(
        "--dev-tools",
        action="store_true",
        help=(
            "Register the trusted git and verify tools. Workflows may then run "
            "reviewed commands inside a git worktree; they still cannot supply "
            "a command of their own."
        ),
    )
    serve_cmd.add_argument(
        ACKNOWLEDGE_FLAG,
        action="store_true",
        help=(
            "Acknowledge, once, that pre-migration data from the legacy engine "
            "is abandoned. orbit never opens, imports or deletes those files."
        ),
    )

    workflow_cmd = sub.add_parser(
        "workflow",
        help="Validate, compile, or publish a Workflow DSL 1.0 definition",
    )
    workflow_sub = workflow_cmd.add_subparsers(
        dest="workflow_action", required=True
    )
    for action in ("validate", "compile", "publish"):
        command = workflow_sub.add_parser(action)
        command.add_argument("file", help="Workflow DSL .yaml, .yml, or .json file")
        command.add_argument(
            "--catalog",
            required=True,
            help="Compile-time Handler, Schema, and Extension catalog JSON",
        )
        if action in {"validate", "publish"}:
            command.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
        if action == "compile":
            command.add_argument("--output", default="-", help="Canonical IR output path (default: stdout)")
        if action == "publish":
            command.add_argument("--db", default=None, help="SQLite database path")
            command.add_argument("--expected-version", type=int, required=True)
            command.add_argument("--actor", default="local-cli")
    run_cmd = sub.add_parser("run", help="Start and inspect workflow runs")
    run_sub = run_cmd.add_subparsers(dest="run_action", required=True)
    run_start = run_sub.add_parser("start", help="Start a run of a published workflow")
    run_start.add_argument("workflow_id")
    run_start.add_argument("--workflow-version", type=int, default=None, help="Default: latest published")
    run_start.add_argument("--input", default=None, help="Run input as a JSON object")
    run_start.add_argument("--goal", default=None)
    run_start.add_argument("--budget-microunits", type=int, default=None)
    run_start.add_argument("--actor", default="local-cli")
    run_start.add_argument(
        "--idempotency-key", default=None,
        help="Reuse to make repeated invocations resolve to the same run",
    )
    run_inspect = run_sub.add_parser("inspect", help="Why is this run where it is")
    run_inspect.add_argument("run_id")
    for command in (run_start, run_inspect):
        command.add_argument("--db", default=None, help="SQLite database path")
        command.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")

    db_cmd = sub.add_parser("db", help="Inspect the project runtime database")
    db_sub = db_cmd.add_subparsers(dest="db_action", required=True)
    db_check = db_sub.add_parser(
        "check",
        help="Audit runtime event, projection, receipt, and snapshot integrity",
    )
    _add_db_check_arguments(db_check)

    args = parser.parse_args()

    if args.command == "workflow":
        _workflow_command(args)
        return

    if args.command == "db":
        _db_check_command(args)
        return

    if args.command == "run":
        _run_command(args)
        return

    if args.command == "serve":
        _serve(args)
        return


if __name__ == "__main__":
    main()
