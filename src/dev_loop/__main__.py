"""CLI entry point: dev-loop serve [--host HOST] [--port PORT] [--db PATH]"""

from __future__ import annotations

import argparse
from pathlib import Path

from .project_index import upsert_project
from .store import project_db_path, resolve_project_root
from .server import create_server

# Database location used by dev-loop before databases became per-project.
LEGACY_DB_PATH = Path.home() / ".dev_loop" / "messages.db"


def main() -> None:
    parser = argparse.ArgumentParser(prog="dev-loop", description="Local MCP mailbox for LLM agents")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the MCP server (Streamable HTTP)")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8848, help="Port (default: 8848)")
    serve.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: per-project database under ~/.dev_loop/projects/)",
    )

    args = parser.parse_args()

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
            f"dev-loop MCP server listening on http://{args.host}:{args.port}/mcp (db: {db_path})",
            flush=True,
        )
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
