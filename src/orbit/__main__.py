"""CLI entry point: orbit serve|start|runner|config."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from . import __version__
from .project_index import project_id, server_url
from .project_lock import ProjectProcessLock
from .store import Store, project_db_path, project_state_dir, resolve_project_root
from .server import create_server, runner_loop


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

# Database location used by orbit before databases became per-project.
LEGACY_DB_PATH = Path.home() / ".dev_loop" / "messages.db"


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
    db_path = args.db or str(project_db_path(project_root))
    if args.db is None and LEGACY_DB_PATH.exists():
        print(
            f"note: legacy shared database exists at {LEGACY_DB_PATH} and is NOT "
            f"used anymore — agents and messages stored there will not appear.\n"
            f"      To keep using it: orbit serve --db {LEGACY_DB_PATH}\n"
            f"      To migrate it to this project: cp {LEGACY_DB_PATH} {db_path}",
            flush=True,
        )
    project = {
        "id": project_id(project_root),
        "name": project_root.name or str(project_root),
        "project_root": str(project_root),
        "db_path": db_path,
        "server_url": server_url(args.host, args.port),
        "host": args.host,
        "port": args.port,
        "last_seen": "",
    }
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
    with ProjectProcessLock(project_root):
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

    args = parser.parse_args()

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
