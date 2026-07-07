"""CLI entry point: dev-loop serve|runner|init."""

from __future__ import annotations

import argparse
import json
from importlib import resources
from pathlib import Path

from .project_index import upsert_project
from .store import Store, project_db_path, resolve_project_root
from .server import create_server, runner_loop

_CLAUDE_MD_SECTION = """
## 多 agent 角色

本项目用 devloop 做多 agent 协作（MCP server 名 `devloop`）。如果启动时被指定了角色\
（如「按 agents/hub.md 工作」），读取 `agents/<role>.md` 并遵循；\
通信协议见 `agents/_protocol.md`。未指定角色时忽略本节。
"""


def init_project(
    project_root: Path, host: str = "127.0.0.1", port: int = 8848
) -> dict[str, list[str]]:
    """Bootstrap a project for dev-loop in one shot: role prompts, default
    workflow/team config, MCP registration, gitignore, CLAUDE.md section.
    Idempotent — existing files are left untouched."""
    from .server import (
        default_workflow_edges,
        default_workflow_steps,
        detect_agent_tools,
        write_team_config,
        write_workflow_config,
    )

    created: list[str] = []
    skipped: list[str] = []

    def _mark(path: Path, was_created: bool) -> None:
        (created if was_created else skipped).append(str(path.relative_to(project_root)))

    # 1. Role prompts from the packaged templates.
    agents_dir = project_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    templates = resources.files("dev_loop") / "role_templates"
    for entry in sorted(templates.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".md"):
            continue
        dest = agents_dir / entry.name
        if dest.exists():
            _mark(dest, False)
            continue
        dest.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
        _mark(dest, True)

    # 2. Default workflow (after roles exist, so role validation passes).
    workflow_path = project_root / ".dev_loop" / "workflow.json"
    if workflow_path.exists():
        _mark(workflow_path, False)
    else:
        write_workflow_config(
            default_workflow_steps(), str(project_root), default_workflow_edges()
        )
        _mark(workflow_path, True)

    # 3. Default team: spread the core roles over the installed agent CLIs
    # (repeating when fewer than three are installed).
    team_path = project_root / ".dev_loop" / "team.json"
    if team_path.exists():
        _mark(team_path, False)
    else:
        installed = [
            tool["agent_name"] for tool in detect_agent_tools() if tool.get("installed")
        ]
        names = installed or ["claude-code"]
        members = [
            {"agent_name": names[index % len(names)], "role_id": role_id}
            for index, role_id in enumerate(("hub", "implementer", "reviewer"))
        ]
        write_team_config(members, str(project_root))
        _mark(team_path, True)

    # 4. Register the MCP server for Claude Code and friends.
    mcp_path = project_root / ".mcp.json"
    mcp_config: dict = {}
    if mcp_path.exists():
        try:
            mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mcp_config = {}
        if not isinstance(mcp_config, dict):
            mcp_config = {}
    servers = mcp_config.setdefault("mcpServers", {})
    if "devloop" in servers:
        _mark(mcp_path, False)
    else:
        servers["devloop"] = {
            "type": "http",
            "url": f"http://{host}:{port}/mcp",
        }
        mcp_path.write_text(
            json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _mark(mcp_path, True)

    # 5. Keep runtime task logs out of git.
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".dev_loop/tasks/" in existing:
        _mark(gitignore, False)
    else:
        joiner = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(existing + joiner + ".dev_loop/tasks/\n", encoding="utf-8")
        _mark(gitignore, True)

    # 6. CLAUDE.md pointer so role-assigned sessions know where to look.
    claude_md = project_root / "CLAUDE.md"
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    if "多 agent 角色" in existing:
        _mark(claude_md, False)
    else:
        claude_md.write_text(existing + _CLAUDE_MD_SECTION, encoding="utf-8")
        _mark(claude_md, True)

    return {"created": created, "skipped": skipped}

# Database location used by dev-loop before databases became per-project.
LEGACY_DB_PATH = Path.home() / ".dev_loop" / "messages.db"


def _serve_hint(host: str, port: int) -> str:
    """A serve command that actually works in the caller's shell: plain
    dev-loop when it is on PATH, otherwise route through the checkout's env."""
    import shutil

    if shutil.which("dev-loop"):
        return f"dev-loop serve --host {host} --port {port}"
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").exists():
        return f"uv run --project {repo} dev-loop serve --host {host} --port {port}"
    return f"python -m dev_loop serve --host {host} --port {port}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="dev-loop", description="Local MCP mailbox for LLM agents")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve",
        help="Start the UI/API + Scheduler server (Streamable HTTP)",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8848, help="Port (default: 8848)")
    serve.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: per-project database under ~/.dev_loop/projects/)",
    )

    runner = sub.add_parser(
        "runner",
        help="Start a Runner server that claims queued workflow run jobs",
    )
    runner.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: per-project database under ~/.dev_loop/projects/)",
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
        "--roles",
        default="",
        help="Only run jobs for these workflow roles (comma-separated, e.g. "
        "implementer,reviewer). Default: all roles.",
    )
    runner.add_argument(
        "--project",
        default=None,
        help="Project root to serve (default: resolved from the current directory).",
    )
    runner.add_argument(
        "--max-concurrency",
        type=int,
        default=1,
        help="Run up to this many jobs in parallel (default: 1).",
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

    init = sub.add_parser(
        "init",
        help="Bootstrap the current project: role prompts, default workflow/team, "
        ".mcp.json, gitignore, CLAUDE.md section",
    )
    init.add_argument("--host", default="127.0.0.1", help="Server host for .mcp.json (default: 127.0.0.1)")
    init.add_argument("--port", type=int, default=8848, help="Server port for .mcp.json (default: 8848)")

    args = parser.parse_args()

    if args.command == "init":
        project_root = resolve_project_root()
        summary = init_project(project_root, host=args.host, port=args.port)
        for path in summary["created"]:
            print(f"created  {path}")
        for path in summary["skipped"]:
            print(f"kept     {path}")
        print(
            f"\nproject ready: {project_root}\n"
            f"next: {_serve_hint(args.host, args.port)}\n"
            f"then open http://{args.host}:{args.port}/ui to review the team & workflow",
            flush=True,
        )
        return

    if args.command == "serve":
        project_root = resolve_project_root()
        db_path = args.db or str(project_db_path(project_root))
        if args.db is None and LEGACY_DB_PATH.exists():
            print(
                f"note: legacy shared database exists at {LEGACY_DB_PATH} and is NOT "
                f"used anymore — agents and messages stored there will not appear.\n"
                f"      To keep using it: dev-loop serve --db {LEGACY_DB_PATH}\n"
                f"      To migrate it to this project: cp {LEGACY_DB_PATH} {db_path}",
                flush=True,
            )
        project = upsert_project(
            project_root=project_root,
            db_path=db_path,
            host=args.host,
            port=args.port,
        )
        mcp = create_server(
            host=args.host,
            port=args.port,
            db_path=db_path,
            project=project,
        )
        print(
            f"dev-loop UI/Scheduler listening on http://{args.host}:{args.port}/mcp (db: {db_path})",
            flush=True,
        )
        mcp.run(transport="streamable-http")
        return

    if args.command == "runner":
        project_root = resolve_project_root(args.project)
        db_path = args.db or str(project_db_path(project_root))
        roles = [r.strip() for r in (args.roles or "").split(",") if r.strip()]
        scope = []
        if args.agent:
            scope.append(f"agents={','.join(args.agent)}")
        if roles:
            scope.append(f"roles={','.join(roles)}")
        if args.max_concurrency > 1:
            scope.append(f"concurrency={args.max_concurrency}")
        suffix = f" [{'; '.join(scope)}]" if scope else ""
        print(
            f"dev-loop Runner {args.name} watching {project_root} (db: {db_path}){suffix}",
            flush=True,
        )
        runner_loop(
            Store(db_path),
            str(project_root),
            runner_name=args.name,
            agents=args.agent or None,
            poll_seconds=args.poll_seconds,
            once=args.once,
            roles=roles or None,
            max_concurrency=args.max_concurrency,
        )
        return


if __name__ == "__main__":
    main()
