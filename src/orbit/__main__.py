"""CLI entry point: orbit serve | run | workflow | db."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

import uvicorn

from . import __version__
from .platform.cutover import (
    ACKNOWLEDGE_FLAG, CutoverRequired, ensure_cutover_acknowledged, read_marker,
)
from .platform.projects import (
    project_db_path,
    public_workflow_db_path,
    project_state_dir,
    resolve_project_root,
    upsert_project,
)


def _workflow_db_path(
    explicit: str | None,
    *,
    project_root: Path | str | None = None,
) -> str:
    """Which library holds published definitions, for every command alike.

    One rule now, which is the point: an explicit database is self-contained,
    and everything else is the one host-wide library. There used to be two,
    one per authoring product, and which one a command addressed depended on a
    flag — so a Workflow published from the command line could be invisible in
    the UI served from the same machine.
    """

    if explicit:
        return explicit
    _runtime_db_path(
        None, project_root=project_root,
    )  # Preserve the existing cutover acknowledgement gate.
    return str(public_workflow_db_path())


def _runtime_db_path(
    explicit: str | None,
    *,
    acknowledged: bool = False,
    project_root: Path | str | None = None,
) -> str:
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
        ensure_cutover_acknowledged(
            acknowledged=acknowledged, project_dir=project_root,
        )
    except CutoverRequired as exc:
        print(str(exc), flush=True)
        raise SystemExit(exc.exit_code) from None
    return str(project_db_path(resolve_project_root(project_root)))


def _artifact_root_path(explicit: str | None, db_path: str | Path) -> Path:
    """Resolve the local CAS beside the selected Runtime database by default."""

    if explicit:
        return Path(explicit).expanduser().absolute()
    return Path(db_path).expanduser().absolute().parent / "artifacts"


def _goal_readiness_buckets(path: Path) -> dict[str, list[dict[str, object]]]:
    """Published Workflows grouped by whether a Goal can start them."""

    from .workflow.api.workflow_catalog import WorkflowCatalogReadModelService
    from .workflow.catalogs import InMemorySchemaCatalog
    from .web.builtin_handlers import BUILTIN_SCHEMAS

    buckets: dict[str, list[dict[str, object]]] = {
        "ready": [], "needs_upgrade": [], "needs_migration": [],
    }
    # Goal readiness is a projection of the same entry-port contract the
    # Runtime advertises.  An empty schema catalog makes every object prompt
    # opaque and falsely reports that its conventional goal binding is absent.
    reads = WorkflowCatalogReadModelService(
        path, InMemorySchemaCatalog(BUILTIN_SCHEMAS)
    )
    for entry in reads.list():
        buckets.setdefault(entry["goal_readiness"], []).append({
            "workflow_id": entry["workflow_id"],
            "name": entry["name"],
            "reason": entry["readiness_reason"],
            "source_available": entry["source_available"],
        })
    for group in buckets.values():
        group.sort(key=lambda item: item["workflow_id"])
    return buckets


def _report_goal_readiness(db_path) -> None:
    """Say at startup which Workflows cannot start a Goal.

    The UI withholds `run.start` from anything that cannot run from a single
    Goal, so the operator is told before their users find out. It never blocks
    the boot: a report is information, not a gate.
    """

    try:
        buckets = _goal_readiness_buckets(Path(db_path))
    except Exception as exc:  # pragma: no cover - reporting must not stop serve
        print(f"warning: could not survey workflow goal readiness: {exc}", flush=True)
        return
    upgrade, migrate = buckets["needs_upgrade"], buckets["needs_migration"]
    print(
        f"goal readiness: {len(buckets['ready'])} workflow(s) can start a goal, "
        f"{len(upgrade)} need an upgrade, {len(migrate)} cannot be upgraded",
        flush=True,
    )
    for label, group in (("needs upgrade", upgrade), ("cannot upgrade", migrate)):
        for item in group:
            print(f"  {label}: {item['workflow_id']}  {item['name']}", flush=True)
    if upgrade or migrate:
        print(
            "  run `orbit workflow inventory --json` for the full report",
            flush=True,
        )


def _workflow_inventory(args, machine_output: bool) -> None:
    """Who can start a Goal today, and who cannot.

    The UI hides every Workflow that cannot run from a single Goal, so an
    operator deserves that list before their users find it. The report only
    reads the catalog projection: it publishes nothing, edits nothing, and is
    safe to run against a live database.
    """

    path = Path(_workflow_db_path(args.db))
    if not path.exists():
        raise SystemExit(
            f"no runtime database at {path}; run `orbit serve` once, or pass --db"
        )
    buckets = _goal_readiness_buckets(path)
    report = {
        "database": str(path),
        "counts": {key: len(value) for key, value in buckets.items()},
        "workflows": buckets,
    }
    if machine_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    total = sum(report["counts"].values())
    print(f"{total} published workflow(s) in {path}")
    labels = {
        "ready": "ready — can start a Goal",
        "needs_upgrade": "needs upgrade — the author can fix this with a prompt",
        "needs_migration": "needs migration — no author source; generate a replacement",
    }
    for key in ("ready", "needs_upgrade", "needs_migration"):
        group = buckets.get(key) or []
        print(f"\n{labels[key]}: {len(group)}")
        for item in group:
            suffix = "" if item["reason"] is None else f"  [{item['reason']}]"
            print(f"  {item['workflow_id']}  {item['name']}{suffix}")


def _workflow_command(args) -> None:
    from .workflow.application import WorkflowDefinitionService, load_catalogs
    from .workflow.domain.serialization import canonical_json
    from .workflow.dsl import DiagnosticError, canonical_ir_json
    from .workflow.persistence import PublishConflictError, SQLiteWorkflowVersionStore

    machine_output = getattr(args, "json", False)
    if args.workflow_action == "inventory":
        _workflow_inventory(args, machine_output)
        return
    try:
        catalogs = load_catalogs(args.catalog)
        source_path = Path(args.file)
        source = source_path.read_text(encoding="utf-8-sig")
        source_format = "json" if source_path.suffix.lower() == ".json" else "yaml"
        store = None
        if args.workflow_action == "publish":
            store = SQLiteWorkflowVersionStore(
                _workflow_db_path(args.db)
            )
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


def _run_engine(args):
    """The engine, wired for reading only.

    No Handlers are registered. Reads never invoke one — `list`, `get` and
    `steps` go to the run store and the checkpoint — so binding the Agent CLIs
    a server would bind means discovering them, resolving their workspaces and
    holding their secrets to answer a question about the past.

    The state directory follows the same rule `serve` uses, or the two would
    describe different engines against the same database.
    """

    from .workflow.langgraph_runtime import build_service

    db_path = _runtime_db_path(args.db)
    state = (
        Path(args.langgraph_state_dir).expanduser().absolute()
        if getattr(args, "langgraph_state_dir", None)
        else Path(db_path).parent
    )
    return build_service(
        Path(_workflow_db_path(args.db)),
        [],
        state_directory=state,
    )


def _run_command(args) -> None:
    """`orbit run list|inspect` — read-only.

    There is no `start` here. A run executes inside the process that starts
    it, so a CLI that started one would have to rebuild the whole Handler
    wiring a server has — discovery, workspaces, secrets — and would still
    behave differently from the server that normally runs them. Starting
    belongs to the UI, or to `start_run` over `orbit mcp`.
    """

    engine = _run_engine(args)
    if args.run_action == "list":
        runs = engine.list_runs(limit=args.limit)
        if args.json:
            print(json.dumps(
                [
                    {
                        "run_id": run.run_id, "workflow_id": run.workflow_id,
                        "status": run.status, "goal": run.goal,
                        "created_at": run.created_at,
                        "updated_at": run.updated_at,
                    }
                    for run in runs
                ],
                ensure_ascii=False, indent=2, sort_keys=True,
            ))
            return
        if not runs:
            print("no runs")
            return
        for run in runs:
            print(f"{run.status:<12} {run.run_id}  {run.goal or run.workflow_id}")
        return

    try:
        run = engine.get(args.run_id)
        steps = engine.steps(args.run_id)
    except LookupError as exc:
        raise SystemExit(f"orbit run: {exc}") from None
    if args.json:
        print(json.dumps({
            "run_id": run.run_id, "workflow_id": run.workflow_id,
            "status": run.status, "goal": run.goal, "error": run.error,
            "created_at": run.created_at, "updated_at": run.updated_at,
            "steps": [dict(step) for step in steps],
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"{run.run_id}  {run.status}")
    if run.goal:
        print(f"  goal      {run.goal}")
    print(f"  workflow  {run.workflow_id}@{run.workflow_version}")
    if run.error:
        print(f"  error     {run.error}")
    marks = {
        "succeeded": "✓", "failed": "✕", "running": "●",
        "waiting": "◔", "answered": "✓", "not_reached": "○",
    }
    for step in steps:
        repeated = f"  ×{step['runs']}" if step["runs"] > 1 else ""
        print(f"  {marks.get(step['status'], '○')} {step['label']}"
              f"  [{step['status']}]{repeated}")


def _structured_agents(values) -> dict[str, str] | None:
    """Parse repeated `--structured-agent NAME=MODEL` into a mapping.

    Both halves are required and neither may be blank: a bare name has no
    model to call, and a bare model has no name an author could select.
    """

    if not values:
        return None
    agents: dict[str, str] = {}
    for entry in values:
        name, separator, model = str(entry).partition("=")
        name, model = name.strip(), model.strip()
        if not separator or not name or not model:
            raise SystemExit(
                f"error: --structured-agent expects NAME=MODEL, got {entry!r}"
            )
        if name in agents:
            raise SystemExit(f"error: duplicate structured agent name: {name}")
        agents[name] = model
    return agents


def _serve(args) -> None:
    """Start the new Runtime composition root."""

    from .web.app import create_app
    from .web.builtin_handlers import BUILTIN_SCHEMAS, builtin_handlers
    from .web.local_identity import (
        LOCAL_ACTOR, local_authorizer, loopback_authenticator,
        loopback_scoped_mcp_authenticator,
    )
    from .web.schema_guard import MixedSchemaError, assert_runtime_schema
    from .workflow.artifacts import LocalCASBackend
    from .platform.runtime_ownership import RuntimeOwnership, RuntimeOwnershipError

    project_root = resolve_project_root(getattr(args, "project_root", None))

    # `serve` is the one command that can *grant* the acknowledgement; the gate
    # itself lives in _runtime_db_path so every other command is covered too.
    db_path = _runtime_db_path(
        args.db,
        acknowledged=args.acknowledge_discard_legacy_data,
        project_root=project_root,
    )
    if args.acknowledge_discard_legacy_data:
        marker = read_marker(project_root)
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

    ownership = RuntimeOwnership(db_path)
    try:
        ownership.acquire()
    except RuntimeOwnershipError as exc:
        raise SystemExit(f"orbit serve: {exc}") from None

    artifact_root = _artifact_root_path(args.artifact_root, db_path)
    try:
        artifact_backend = LocalCASBackend(artifact_root)
    except (OSError, ValueError) as exc:
        ownership.release()
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

    workflow_db_path = Path(
        _workflow_db_path(args.db, project_root=project_root)
    )
    langgraph_state_directory = (
        Path(args.langgraph_state_dir).expanduser().absolute()
        if args.langgraph_state_dir else Path(db_path).parent
    )

    structured_agents = _structured_agents(getattr(args, "structured_agent", None))

    def request_shutdown() -> None:
        # Uvicorn owns graceful shutdown and lifespan cleanup. Raising the same
        # signal as Ctrl-C keeps its public `run` entrypoint (and embedders that
        # patch it) intact while still stopping workers through app lifespan.
        os.kill(os.getpid(), signal.SIGINT)

    try:
        harness_actor_prefix = "harness:session:" if args.mcp_tool_profile == "harness" else None
        authenticator = loopback_authenticator if harness_actor_prefix is None else (
            lambda request: loopback_scoped_mcp_authenticator(
                request, trusted_prefix=harness_actor_prefix,
            )
        )
        app = create_app(
            db_path,
            workflow_db_path=workflow_db_path,
            handlers=handlers,
            schemas=BUILTIN_SCHEMAS,
            artifact_backend=artifact_backend,
            discover_agents=not args.no_agent_discovery,
            serve_ui=True,
            authenticator=authenticator,
            authorizer=local_authorizer(trusted_prefix=harness_actor_prefix),
            # One operator, one machine: the rate limit would only ever throttle
            # this person's own browser, which polls several endpoints per tick.
            unlimited_actors=(LOCAL_ACTOR,),
            # The approval token proves the answer came from the person the
            # task was delivered to. On loopback that person is the only actor
            # there is, and they can fetch the token at will — so requiring
            # them to paste it back buys nothing.
            token_exempt_actors=(LOCAL_ACTOR,),
            # Recovery takeovers are answered by this person too.
            operator_actors=(LOCAL_ACTOR,),
            shutdown_request=request_shutdown,
            langgraph_state_directory=langgraph_state_directory,
            agent_workspace_root=args.agent_workspace,
            structured_agents=structured_agents,
            mcp_tool_profile=args.mcp_tool_profile,
            execution_workers=args.execution_workers,
            serve_mcp=False,
        )
    except MixedSchemaError as exc:
        ownership.release()
        raise SystemExit(f"error: {exc}") from None
    except (ValueError, ImportError) as exc:
        ownership.release()
        # A misspelled --structured-agent, a name that collides with a
        # discovered CLI, or the optional dependency not installed. All are
        # things the operator typed, so they get an error at the prompt rather
        # than a Runtime that starts and refuses the first generation.
        raise SystemExit(f"error: {exc}") from None

    # Bound here rather than by the server, because with `--port 0` the kernel
    # chooses and nothing below — the banner, the project record, the ownership
    # record a client discovers this Runtime by — can say where it answers until
    # the choice has been made. Asking the server to bind and then guessing the
    # number would be a guess that is wrong exactly when it matters.
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    try:
        listener = config.bind_socket()
    except SystemExit:
        ownership.release()
        raise
    port = listener.getsockname()[1]

    upsert_project(
        project_root=project_root, db_path=db_path,
        host=args.host, port=port,
    )
    print(
        f"orbit Runtime listening on http://{args.host}:{port}/ui/ "
        f"(health: /health/ready, engine: langgraph) "
        f"(db: {db_path}, artifacts: {artifact_backend.root})",
        flush=True,
    )
    _report_goal_readiness(workflow_db_path)
    # Publish where this Runtime answers, so a client that did not start it can
    # find it. A wildcard bind is not an address anyone can connect to, so the
    # record names loopback — the interface a local client actually uses.
    reachable = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    base_url = f"http://{reachable}:{port}"
    ownership.publish(
        transport="http",
        project_root=str(project_root),
        base_url=base_url,
        mcp_url=f"{base_url}/mcp",
    )
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()
        ownership.release()


def _mcp(args) -> None:
    """Serve MCP over stdin/stdout, with the Runtime running in this process.

    Not a client for a `serve` that is already up: this stands up the same
    composition and starts its workers, so a run an agent starts here actually
    executes. A client that can only speak stdio therefore needs nothing else
    running — but it does mean two of these against one database are two
    Runtimes, exactly as two `serve` processes would be.

    Everything diagnostic goes to stderr. stdout carries the protocol, and one
    stray line on it is a parse error at the other end.
    """

    from .web.app import create_app
    from .web.builtin_handlers import BUILTIN_SCHEMAS, builtin_handlers
    from .web.local_identity import LOCAL_ACTOR, local_authorizer
    from .web.mcp import serve_stdio
    from .web.schema_guard import MixedSchemaError, assert_runtime_schema
    from .workflow.artifacts import LocalCASBackend
    from .platform.runtime_ownership import RuntimeOwnership, RuntimeOwnershipError

    actor_prefix = getattr(args, "actor_prefix", None)
    if actor_prefix is not None and not actor_prefix.strip():
        raise SystemExit("orbit mcp: --actor-prefix cannot be empty")
    project_root = resolve_project_root(getattr(args, "project_root", None))
    db_path = _runtime_db_path(args.db, project_root=project_root)
    try:
        assert_runtime_schema(db_path)
    except MixedSchemaError as exc:
        raise SystemExit(f"error: {exc}") from None

    # Ownership first, the way `orbit serve` takes it: the cleanup below
    # releases the lock, so the lock has to exist by the time anything can
    # fail into it. Taken the other way round, an Artifact store that failed
    # for a reason other than OSError/ValueError raised UnboundLocalError from
    # the handler and buried the fault that actually happened.
    ownership = RuntimeOwnership(db_path)
    try:
        ownership.acquire()
    except RuntimeOwnershipError as exc:
        raise SystemExit(f"orbit mcp: {exc}") from None
    # Discoverable, but deliberately without an endpoint: this Runtime speaks
    # only to the process holding its stdio. Saying so keeps a client from
    # reading "no base_url yet" as "still starting up" and waiting forever.
    ownership.publish(transport="stdio", project_root=str(project_root))

    artifact_root = _artifact_root_path(args.artifact_root, db_path)
    try:
        artifact_backend = LocalCASBackend(artifact_root)
    except (OSError, ValueError) as exc:
        ownership.release()
        raise SystemExit(
            f"orbit mcp: cannot initialize Artifact store at "
            f"{artifact_root}: {exc}"
        ) from None
    except Exception:
        ownership.release()
        raise

    from .web.app import HandlerRegistration
    from .workflow.langgraph_runtime.harness_subagent import (
        DelegationQueue, HARNESS_SUBAGENT_MANIFEST, HarnessSubagentHandler,
    )

    delegation_queue = DelegationQueue(Path(db_path).parent / "langgraph-runs.sqlite3")
    handlers = list(builtin_handlers())
    handlers.append(HandlerRegistration(
        HARNESS_SUBAGENT_MANIFEST, HarnessSubagentHandler(delegation_queue),
        "harness.subagent@1.0.0",
    ))
    try:
        app = create_app(
            db_path,
            workflow_db_path=_workflow_db_path(
                args.db, project_root=project_root,
            ),
            handlers=handlers,
            schemas=BUILTIN_SCHEMAS,
            artifact_backend=artifact_backend,
            discover_agents=not args.no_agent_discovery,
            serve_ui=False,
            # There is no connection to authenticate. The person who started this
            # process is the caller, and on a local runtime that is `local` —
            # the same actor loopback would have resolved to.
            authorizer=local_authorizer(
                args.actor, trusted_prefix=actor_prefix,
            ),
            unlimited_actors=(args.actor,),
            token_exempt_actors=(args.actor,),
            operator_actors=(args.actor,),
            langgraph_state_directory=Path(db_path).parent,
            mcp_tool_profile=args.mcp_tool_profile,
            delegation_queue=delegation_queue,
        )
    except Exception:
        ownership.release()
        raise
    composition = app.state.runtime
    try:
        # Started by hand because no ASGI server will run the lifespan here.
        composition.start()
        print(
            f"orbit MCP on stdio (db: {db_path}, engine: langgraph)",
            file=sys.stderr, flush=True,
        )
        serve_stdio(
            app.state.mcp_dispatch, args.actor,
            actor_prefix=actor_prefix,
        )
    except KeyboardInterrupt:
        pass
    finally:
        # Nested, because stopping is the part that can fail: a composition
        # that raises on the way down would otherwise carry the exception out
        # past the release and leave the database owned. A CLI process exiting
        # has the kernel to fall back on; an embedded caller, a test, or
        # anything that catches this and keeps running does not.
        try:
            stragglers = composition.stop()
        finally:
            ownership.release()
        if stragglers:
            print(f"loops still running at exit: {stragglers}", file=sys.stderr)


def _agent_app(args) -> None:
    """Run the generic local Agent App host without coupling it to Orbit Runtime."""

    from dataclasses import replace

    from .agent_apps.host import AgentAppHost, AgentAppHostError, default_workspace
    from .agent_apps.manifest import EventSpec, McpSpec
    from .agent_apps.mcp_proxy import serve_proxy
    from .hub import WorkspaceRegistry, workspace_urls

    host = AgentAppHost(state_root=args.state_dir)
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace is not None else default_workspace()
    )
    identifier, _ = WorkspaceRegistry().register(workspace)
    try:
        ensured = host.ensure(args.manifest)
    except (AgentAppHostError, ValueError) as exc:
        raise SystemExit(f"orbit agent-app: {exc}") from None
    if args.agent_app_action == "ensure":
        print(workspace_urls(identifier)["ui_url"])
        return
    urls = workspace_urls(identifier)
    selected = replace(
        ensured.manifest,
        ui_url=urls["ui_url"],
        mcp=McpSpec(url=urls["mcp_url"]),
        events=EventSpec(url=urls["events_url"]),
    )
    try:
        # The Hub process is global, but each proxy session still needs an
        # isolated event inbox so one workspace cannot consume another's
        # Runtime events.
        serve_proxy(
            selected,
            state_dir=ensured.state_dir / "workspaces" / identifier,
        )
    except RuntimeError as exc:
        raise SystemExit(f"orbit agent-app mcp-proxy: {exc}") from None


def build_parser() -> argparse.ArgumentParser:
    """The whole command line, as a value.

    Separate from `main` so that what a command resolves — which database,
    which library — can be asserted without running it.
    """

    parser = argparse.ArgumentParser(prog="orbit", description="Local multi-agent workflow orchestrator")
    parser.add_argument(
        "--version", action="version", version=f"orbit {__version__}",
        help="Show the orbit version and exit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_cmd = sub.add_parser(
        "serve", help="Start the Runtime: API, UI and background loops"
    )
    serve_cmd.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    serve_cmd.add_argument(
        "--port", type=int, default=8848,
        help=(
            "Port (default: 8848). Use 0 to let the kernel pick a free one — "
            "the number it chose is published in the Runtime's ownership "
            "record, so `orbit runtimes` and any client that discovers this "
            "Runtime still find it."
        ),
    )
    serve_cmd.add_argument(
        "--project-root", default=None,
        help="Project directory used for Runtime state (default: current directory)",
    )
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
        "--agent-workspace",
        default=None,
        metavar="DIR",
        help=(
            "Directory a run's Agents work in, one subdirectory per run "
            "(default: agent-workspaces/ beside the Runtime database). An "
            "Agent does what it is asked in the directory it is given, so "
            "point this at a checkout only when that is the intent."
        ),
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
        "--structured-agent",
        action="append",
        default=None,
        metavar="NAME=MODEL",
        help=(
            "Add a workflow writer that asks a model API for a typed "
            "definition instead of forking a local Agent CLI, e.g. "
            "--structured-agent gpt=openai:gpt-5.2. Repeatable. Needs the "
            "optional 'pydantic-ai-slim' dependency and that provider's "
            "credentials in the environment."
        ),
    )
    serve_cmd.add_argument(
        "--langgraph-state-dir",
        default=None,
        help=(
            "Directory for LangGraph run and checkpoint databases "
            "(default: beside the Runtime database)."
        ),
    )
    serve_cmd.add_argument(
        "--mcp-tool-profile", choices=("full", "harness"), default="full",
        help="MCP tool surface to advertise (default: full)",
    )
    serve_cmd.add_argument(
        "--execution-workers", type=int, default=1, metavar="N",
        help="Independent Handler worker processes per workspace (default: 1, max: 16)",
    )
    serve_cmd.add_argument(
        ACKNOWLEDGE_FLAG,
        action="store_true",
        help=(
            "Acknowledge, once, that pre-migration data from the legacy engine "
            "is abandoned. orbit never opens, imports or deletes those files."
        ),
    )

    mcp_cmd = sub.add_parser(
        "mcp",
        help="Serve the MCP tools over stdio, Runtime included",
    )
    mcp_cmd.add_argument(
        "--project-root", default=None,
        help="Project directory used for Runtime state (default: current directory)",
    )
    mcp_cmd.add_argument(
        "--db", default=None,
        help="SQLite path (default: per-project database under ~/.orbit/projects/)",
    )
    mcp_cmd.add_argument(
        "--artifact-root", default=None,
        help=(
            "Local content-addressed Artifact directory "
            "(default: artifacts/ beside the Runtime database)"
        ),
    )
    mcp_cmd.add_argument(
        "--no-agent-discovery", action="store_true",
        help="Skip probing for installed Agent CLIs at startup",
    )
    mcp_cmd.add_argument(
        "--mcp-tool-profile", choices=("full", "harness"), default="full",
        help="MCP tool surface to advertise (default: full)",
    )
    mcp_cmd.add_argument(
        "--actor", default="local",
        help="Owner actor for stdio calls (default: local)",
    )
    mcp_cmd.add_argument(
        "--actor-prefix", default=None,
        help="Trust per-call _meta orbit/actor values under this prefix",
    )

    runtimes_cmd = sub.add_parser(
        "runtimes",
        help="Live Runtimes on this machine and where they answer",
    )
    runtimes_cmd.add_argument(
        "--json", action="store_true",
        help="Machine-readable output, for a client discovering a Runtime",
    )
    runtimes_cmd.add_argument(
        "--root", default=None,
        help="Directory to search (default: ~/.orbit)",
    )

    hub_cmd = sub.add_parser("hub", help="Run or configure the multi-workspace Hub")
    hub_sub = hub_cmd.add_subparsers(dest="hub_action", required=True)
    hub_serve = hub_sub.add_parser("serve", help="Serve the stable workspace router")
    hub_serve.add_argument("--host", default="127.0.0.1")
    hub_serve.add_argument("--port", type=int, default=8848)
    hub_register = hub_sub.add_parser("register", help="Register a workspace and print its URLs")
    hub_register.add_argument("workspace")

    agent_app_cmd = sub.add_parser(
        "agent-app", help="Host a manifest-declared local Agent App",
    )
    agent_app_sub = agent_app_cmd.add_subparsers(
        dest="agent_app_action", required=True,
    )
    for action, help_text in (
        ("ensure", "Start the App if needed and print its UI URL"),
        ("mcp-proxy", "Carry its HTTP JSON-RPC MCP endpoint over stdio"),
    ):
        command = agent_app_sub.add_parser(action, help=help_text)
        command.add_argument("manifest", help="Path to agent-app.json")
        command.add_argument(
            "--workspace", default=None,
            help=(
                "Workspace identity and working directory for workspace-scoped Apps "
                "(default: ORBIT_DEFAULT_WORKSPACE or ~/.orbit/workspaces/default)"
            ),
        )
        command.add_argument(
            "--state-dir", default=None,
            help="Host state directory (default: AGENT_APP_STATE_DIR or user state)",
        )

    run_cmd = sub.add_parser(
        "run", help="Look at workflow runs. Read-only."
    )
    run_sub = run_cmd.add_subparsers(dest="run_action", required=True)
    run_list = run_sub.add_parser("list", help="Runs, newest first")
    run_list.add_argument(
        "--limit", type=int, default=20, help="How many to show (default: 20)"
    )
    run_inspect = run_sub.add_parser(
        "inspect", help="One run, and how far through its steps it got"
    )
    run_inspect.add_argument("run_id")
    for command in (run_list, run_inspect):
        command.add_argument("--db", default=None, help="SQLite database path")
        command.add_argument(
            "--langgraph-state-dir", default=None,
            help="Engine state directory (default: beside the database)",
        )
        command.add_argument(
            "--json", action="store_true", help="Emit stable machine-readable JSON"
        )

    workflow_cmd = sub.add_parser(
        "workflow",
        help="Validate, compile, or publish a Workflow DSL 1.0 definition",
    )
    workflow_sub = workflow_cmd.add_subparsers(
        dest="workflow_action", required=True
    )
    inventory = workflow_sub.add_parser(
        "inventory",
        help=(
            "Report which published Workflows can start a Goal, which the "
            "author can upgrade, and which need operator attention. "
            "Read-only."
        ),
    )
    inventory.add_argument("--db", default=None, help="SQLite database path")
    inventory.add_argument(
        "--json", action="store_true", help="Emit stable machine-readable JSON"
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
    return parser


def _runtimes(args) -> None:
    """List the Runtimes a client could connect to right now.

    The discovery entry point for anything that did not start a Runtime
    itself. `--json` is the contract a plugin host reads; the plain output is
    for a person asking "is one up, and on what port".
    """

    from .platform.runtime_ownership import discover_runtimes

    found = discover_runtimes(args.root)
    if args.json:
        print(json.dumps(
            [
                {
                    "db_path": runtime.db_path,
                    "pid": runtime.pid,
                    **{
                        key: value for key, value in runtime.facts.items()
                        if key not in ("db_path", "pid")
                    },
                }
                for runtime in found
            ],
            indent=2, sort_keys=True,
        ))
        return
    if not found:
        print("no Runtime is running")
        return
    for runtime in found:
        where = runtime.base_url or f"({runtime.facts.get('transport', 'starting')})"
        print(f"{where}\tpid {runtime.pid}\t{runtime.db_path}")


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "workflow":
        _workflow_command(args)
        return

    if args.command == "run":
        _run_command(args)
        return

    if args.command == "serve":
        _serve(args)
        return

    if args.command == "mcp":
        _mcp(args)
        return

    if args.command == "agent-app":
        _agent_app(args)
        return

    if args.command == "runtimes":
        _runtimes(args)
        return

    if args.command == "hub":
        from .hub import WorkspaceRegistry, create_hub_app, workspace_urls

        if args.hub_action == "register":
            identifier, _ = WorkspaceRegistry().register(args.workspace)
            print(json.dumps(workspace_urls(identifier), sort_keys=True))
            return
        uvicorn.run(create_hub_app(), host=args.host, port=args.port, log_level="info")
        return


if __name__ == "__main__":
    main()
