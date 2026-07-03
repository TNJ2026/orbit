"""CLI entry point: dev-loop serve [--host HOST] [--port PORT] [--db PATH]"""

from __future__ import annotations

import argparse

from .store import project_db_path
from .server import create_server


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
        db_path = args.db or str(project_db_path())
        mcp = create_server(host=args.host, port=args.port, db_path=db_path)
        print(f"dev-loop MCP server listening on http://{args.host}:{args.port}/mcp (db: {db_path})")
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
