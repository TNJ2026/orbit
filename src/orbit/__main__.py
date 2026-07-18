"""CLI entry point: orbit serve|start|runner|config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from . import __version__
from .platform.projects import (
    project_db_path,
    resolve_project_root,
    upsert_project,
    warn_about_legacy_database,
)
from .store import Store, project_state_dir
from .server import create_server, runner_loop


def _runtime_db_path(explicit: str | None) -> str:
    """Resolve the runtime database, warning once about abandoned legacy data.

    Every command that touches the default database goes through here, so the
    warning and the path rule can never drift apart between `serve`, `workflow
    publish` and `db check`.
    """

    if explicit:
        return explicit
    warn_about_legacy_database()
    return str(project_db_path(resolve_project_root()))


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
    run_id = EntityId.parse(args.run_id) if args.run_id else None
    report = check_database(db_path, run_id=run_id)
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
        if args.workflow_action == "db-check":
            # Deprecated nesting kept only so in-flight scripts do not break
            # mid-migration; `orbit db check` is the product surface and this
            # alias is removed in M6.
            _db_check_command(args)
            return
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


def init_project(project_root: Path) -> dict[str, list[str]]:
    """Bootstrap a project for orbit in one shot: default workflow config and
    gitignore. Idempotent — existing files are left untouched."""
    from .server import (
        default_workflow_edges,
        default_workflow_steps,
        write_workflow_config,
    )

    created: list[str] = []
    skipped: list[str] = []

    def _mark(path: Path, was_created: bool) -> None:
        (created if was_created else skipped).append(str(path.relative_to(project_root)))

    # Per-project state dir: .orbit for fresh projects, or a legacy .dev_loop
    # if that is what this project already uses.
    state_dir = project_state_dir(project_root)

    # 1. Default workflow.
    workflow_path = state_dir / "workflow.json"
    if workflow_path.exists():
        _mark(workflow_path, False)
    else:
        write_workflow_config(
            default_workflow_steps(), str(project_root), default_workflow_edges()
        )
        _mark(workflow_path, True)

    # 2. Keep runtime task logs and per-task worktree checkouts out of git.
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    wanted = [f"{state_dir.name}/tasks/", f"{state_dir.name}/worktrees/"]
    missing = [line for line in wanted if line not in existing]
    if not missing:
        _mark(gitignore, False)
    else:
        joiner = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(
            existing + joiner + "".join(f"{line}\n" for line in missing),
            encoding="utf-8",
        )
        _mark(gitignore, True)

    return {"created": created, "skipped": skipped}


def _serve_hint(host: str, port: int) -> str:
    """A serve command that actually works in the caller's shell: plain
    orbit when it is on PATH, otherwise route through the checkout's env."""
    import shutil

    if shutil.which("orbit"):
        return f"orbit serve --host {host} --port {port}"
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").exists():
        return f"uv run --project {repo} orbit serve --host {host} --port {port}"
    return f"python -m orbit serve --host {host} --port {port}"


def append_missing_gitignore(project_root: Path, entries: list[str]) -> list[str]:
    """Append any of `entries` not already in the repo's .gitignore, returning
    the ones actually added. An entry counts as present with or without its
    trailing slash. Already-tracked files are unaffected by gitignore, so a
    project that committed a path keeps it tracked regardless."""
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [
        e for e in entries if e not in present and e.rstrip("/") not in present
    ]
    if not missing:
        return []
    joiner = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(
        existing + joiner + "".join(f"{e}\n" for e in missing), encoding="utf-8"
    )
    return missing


def ensure_state_dir_gitignored(project_root: Path) -> bool:
    """Add the per-project state dir (e.g. `.orbit/`) to the repo's .gitignore
    so runtime task logs and worktrees never show up in `git status`. Returns
    True if the file was modified."""
    state_name = project_state_dir(project_root).name
    return bool(append_missing_gitignore(project_root, [f"{state_name}/"]))


def _serve(args) -> None:
    """Start the UI/API + Scheduler server (shared by `serve` and `up`)."""
    project_root = resolve_project_root()
    db_path = _runtime_db_path(args.db)
    project = upsert_project(
        project_root=project_root,
        db_path=db_path,
        host=args.host,
        port=args.port,
    )
    app = create_server(
        host=args.host,
        port=args.port,
        db_path=db_path,
        project=project,
        run_worker=not args.no_runner,
        worker_concurrency=args.runner_concurrency,
    )
    worker = "no in-process runner (start `orbit runner` separately)" if args.no_runner \
        else f"with in-process runner (concurrency={args.runner_concurrency})"
    print(
        f"orbit UI/Scheduler listening on http://{args.host}:{args.port}/ui "
        f"({worker}) (db: {db_path})",
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

    # Shared flags for the two ways to bring the server up (serve / up).
    serve_common = argparse.ArgumentParser(add_help=False)
    serve_common.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve_common.add_argument("--port", type=int, default=8848, help="Port (default: 8848)")
    serve_common.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: per-project database under ~/.orbit/projects/)",
    )
    serve_common.add_argument(
        "--no-runner",
        action="store_true",
        help="Do not run an in-process worker; start standalone `orbit "
        "runner` process(es) instead (decoupled / multi-host / restart-safe).",
    )
    serve_common.add_argument(
        "--runner-concurrency",
        type=int,
        default=5,
        help="How many jobs the in-process worker runs in parallel (default: 5).",
    )

    sub.add_parser(
        "serve",
        parents=[serve_common],
        help="Start the UI/API + Scheduler server",
    )

    sub.add_parser(
        "start",
        aliases=["up"],  # back-compat: `orbit up` still works
        parents=[serve_common],
        help="Zero-setup start: gitignore the state dir, then serve with the "
        "packaged workflow defaults — no config copied into the repo. "
        "Run `orbit config` instead to customize and commit them.",
    )

    runner = sub.add_parser(
        "runner",
        help="Start a Runner server that claims queued workflow run jobs",
    )
    runner.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: per-project database under ~/.orbit/projects/)",
    )
    runner.add_argument(
        "--name",
        default="runner-local",
        help="Runner instance name for job leases (default: runner-local)",
    )
    runner.add_argument(
        "--agent",
        action="append",
        default=[],
        help="Only run jobs assigned to this agent; repeatable. Default: all agents.",
    )
    runner.add_argument(
        "--steps",
        default="",
        help="Only run jobs for these workflow step ids (comma-separated, e.g. "
        "implement,review). Default: all steps.",
    )
    runner.add_argument(
        "--project",
        default=None,
        help="Project root to serve (default: resolved from the current directory).",
    )
    runner.add_argument(
        "--max-concurrency",
        type=int,
        default=5,
        help="Run up to this many jobs in parallel (default: 5).",
    )
    runner.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Polling interval when no jobs are available (default: 2.0)",
    )
    runner.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one job and exit when no job is available.",
    )

    config_cmd = sub.add_parser(
        "config",
        aliases=["init"],  # back-compat: `orbit init` still works
        help="Generate editable, committable workflow config. Optional — "
        "start/serve work without it; use this to customize the workflow.",
    )
    config_cmd.add_argument("--host", default="127.0.0.1", help="Host for the printed serve hint (default: 127.0.0.1)")
    config_cmd.add_argument("--port", type=int, default=8848, help="Port for the printed serve hint (default: 8848)")

    workflow_cmd = sub.add_parser(
        "workflow",
        help="Validate, compile, or publish a Workflow DSL 1.0 definition",
    )
    # metavar hides the deprecated db-check alias from help without removing it.
    workflow_sub = workflow_cmd.add_subparsers(
        dest="workflow_action", required=True, metavar="{validate,compile,publish}"
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
    # Deprecated alias; `orbit db check` is the product surface. Registered
    # without help text so it stays runnable but unadvertised until M6 removes
    # it outright.
    _add_db_check_arguments(workflow_sub.add_parser("db-check"))

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

    if args.command in ("config", "init"):
        project_root = resolve_project_root()
        summary = init_project(project_root)
        for path in summary["created"]:
            print(f"created  {path}")
        for path in summary["skipped"]:
            print(f"kept     {path}")
        print(
            f"\nproject ready: {project_root}\n"
            f"next: {_serve_hint(args.host, args.port)}\n"
            f"then open http://{args.host}:{args.port}/ui to configure Agents and workflow",
            flush=True,
        )
        return

    if args.command in ("start", "up"):
        project_root = resolve_project_root()
        state_name = project_state_dir(project_root).name
        # Keep the per-project state dir out of git; `start` copies nothing you
        # need to commit.
        added = append_missing_gitignore(project_root, [f"{state_name}/"])
        if added:
            print(f"gitignore: added {', '.join(added)}", flush=True)
        else:
            print(f"gitignore: {state_name}/ already ignored", flush=True)
        print(
            "orbit start: serving with packaged workflow defaults — no files "
            "copied into the repo. Run `orbit config` to customize and commit them.",
            flush=True,
        )
        _serve(args)
        return

    if args.command == "serve":
        _serve(args)
        return

    if args.command == "runner":
        project_root = resolve_project_root(args.project)
        db_path = args.db or str(project_db_path(project_root))
        steps = [s.strip() for s in (args.steps or "").split(",") if s.strip()]
        scope = []
        if args.agent:
            scope.append(f"agents={','.join(args.agent)}")
        if steps:
            scope.append(f"steps={','.join(steps)}")
        if args.max_concurrency > 1:
            scope.append(f"concurrency={args.max_concurrency}")
        suffix = f" [{'; '.join(scope)}]" if scope else ""
        print(
            f"orbit Runner {args.name} watching {project_root} (db: {db_path}){suffix}",
            flush=True,
        )
        runner_loop(
            Store(db_path),
            str(project_root),
            runner_name=args.name,
            agents=args.agent or None,
            poll_seconds=args.poll_seconds,
            once=args.once,
            steps=steps or None,
            max_concurrency=args.max_concurrency,
        )
        return


if __name__ == "__main__":
    main()
