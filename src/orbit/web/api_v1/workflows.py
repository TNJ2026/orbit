"""Workflow catalog, authoring-job, and publish routes for `/api/v1`.

Catalog reads project the workflow read model and advertise commands;
writes (generate, modify, delete, validate, publish, action update, rebind)
all cross the single command boundary, `ctx.mutate`.
"""

from __future__ import annotations

import json

import yaml
from typing import Any, Mapping
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import envelope, page_size
from ...workflow.application.authoring_job_service import AuthoringJobConflict
from ...workflow.domain.ids import EntityId
from ...workflow.dsl import LANGGRAPH_NODE_KINDS, authoring_json_schema
from ...workflow.persistence.database import connect_workflow_database

from .common import (
    READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    _display_language, _generation_agent, _required_version,
    _retarget_handlers, error,
)


def build_routes(ctx, *, authoring: bool = True) -> list[Route]:
    """Catalog routes, and the prompt-driven authoring ones when asked.

    `authoring=False` returns only what reading, compiling and publishing a
    definition needs — what the graph editor uses. Single-agent mode composes
    it that way: a goal there is written by the connected Agent through a
    template, so `/generate` and the jobs that track it would be a second
    authoring path that product was built without.
    """

    def _generation_prompt(body: Mapping[str, Any]) -> str:
        prompt = str(body.get("prompt", body.get("instruction", ""))).strip()
        profile = str(body.get("authoring_profile", "multi_agent"))
        if profile != "multi_agent":
            raise ValueError("this Runtime uses the multi_agent authoring profile")
        return prompt

    async def workflow_catalog(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        may_start = ctx.guard.allows(actor, WRITE_SCOPE)
        registry = getattr(ctx.durable_service, "execution_registry", None)
        has_action_agent = bool(
            registry is not None and registry.sealed
            and any(
                "agent.invoke" in entry.manifest.capabilities
                for entry in registry.entries()
            )
        )
        workflows = ctx.workflow_reads.list()
        for item in workflows:
            bindings = handler_bindings(ctx.workflow_reads.detail(item["workflow_id"]))
            incompatible = [
                binding for binding in bindings if binding["status"] != "current"
            ]
            item["handler_compatibility"] = {
                "compatible": not incompatible,
                "bindings": bindings,
            }
            item["langgraph_compatibility"] = (
                {"compatible": False, "reason": "service_not_configured"}
                if ctx.langgraph_service is None
                else ctx.langgraph_service.compatibility(
                    item["workflow_id"], item["latest_version"]
                )
            )
            if incompatible:
                item["goal_readiness"] = (
                    "needs_upgrade" if item.get("source_available")
                    else "needs_migration"
                )
                item["readiness_reason"] = "handler_binding_unavailable"
            item["active_job"] = (
                None if ctx.authoring_jobs is None
                else ctx.authoring_jobs.active_for_workflow(item["workflow_id"], actor=actor)
            )
            item["allowed_commands"] = ([{
                    "command": "langgraph_run.start",
                    "label": "Start run",
                    "method": "POST",
                    "href": "/api/v1/langgraph-runs",
                    "target_aggregate_id": item["workflow_id"],
                    "expected_version": 0,
                    "payload_schema": "langgraph-run-start/1.0",
                }] if may_start and item["langgraph_compatibility"]["compatible"] else [{
                    "command": "run.start",
                    "label": "Start run",
                    "method": "POST",
                    "href": "/api/v1/runs",
                    "target_aggregate_id": item["workflow_id"],
                    "expected_version": 0,
                    "payload_schema": "run-start/1.0",
                }] if may_start and ctx.legacy_execution
                and item["goal_readiness"] == "ready" else [])
            if may_start and ctx.workflow_publisher is not None:
                item["allowed_commands"].append({
                    "command": "workflow.delete",
                    "label": "Delete workflow",
                    "method": "DELETE",
                    "href": (
                        f"/api/v1/workflows/"
                        f"{quote(item['workflow_id'], safe=':')}"
                    ),
                    "target_aggregate_id": item["workflow_id"],
                    "expected_version": item["latest_version"],
                    "payload_schema": "workflow-delete/1.0",
                    "confirmation": "explicit",
                })
            item["editing_available"] = bool(
                may_start and item.get("source_available")
                and (ctx.authoring_jobs is not None or has_action_agent)
            )
        # Generation is a catalog-level act — there is no aggregate yet — so
        # its command is advertised beside the list, not on an entry.
        catalog_commands = ([{
            "command": "workflow.generate",
            "label": "Generate workflow",
            "method": "POST",
            "href": "/api/v1/workflows/generate",
            "target_aggregate_id": "workflow_catalog",
            "expected_version": 0,
            "payload_schema": "workflow-generate/1.0",
        }] if ctx.authoring_service is not None and may_start else [])
        return JSONResponse(envelope({
            "workflows": workflows,
            "allowed_commands": catalog_commands,
        }))

    def _publish_command(workflow_id: str, expected_latest_version: int) -> dict[str, Any]:
        return {
            "command": "workflow.publish",
            "label": "Publish workflow",
            "method": "POST",
            "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}/versions",
            "target_aggregate_id": workflow_id,
            "expected_version": expected_latest_version,
            "payload_schema": "workflow-publish/1.0",
        }

    def _validate_command(workflow_id: str, expected_latest_version: int) -> dict[str, Any]:
        return {
            "command": "workflow.validate",
            "label": "Validate workflow draft",
            "method": "POST",
            "href": "/api/v1/workflows/validate",
            "target_aggregate_id": workflow_id,
            "expected_version": expected_latest_version,
            "payload_schema": "workflow-validate/1.0",
        }

    def _draft_commands(workflow_id: str, latest: int) -> list[dict[str, Any]]:
        return [
            _publish_command(workflow_id, latest),
            _validate_command(workflow_id, latest),
        ]

    async def workflow_generate(request: Request) -> JSONResponse:
        """Queue natural-language generation and publish its valid result."""
        if ctx.authoring_service is None:
            return error(
                "generation_unavailable",
                "no generation-capable agent CLI was discovered", 503,
            )

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            if ctx.authoring_jobs is None:
                raise ValueError("workflow generation jobs are unavailable")
            agent = _generation_agent(body)
            if agent is not None:
                agent = ctx.authoring_service.ensure_agent(agent)
            try:
                return ctx.authoring_jobs.create(
                    actor=actor,
                    prompt=_generation_prompt(body),
                    idempotency_key=key,
                    display_language=_display_language(body),
                    agent=agent,
                )
            except AuthoringJobConflict as exc:
                raise ValueError(json.dumps({
                    "code": exc.code, "active_job": exc.job,
                }, ensure_ascii=False)) from None

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.generate", command)

    async def workflow_modify(request: Request) -> JSONResponse:
        if ctx.authoring_jobs is None:
            return error("generation_unavailable", "workflow modification is unavailable", 503)
        workflow_id = str(EntityId.parse(request.path_params["workflow_id"]))

        def command(body: Mapping[str, Any], actor: str, key: str):
            agent = _generation_agent(body)
            if agent is not None:
                agent = ctx.authoring_service.ensure_agent(agent)
            try:
                return ctx.authoring_jobs.create(
                    actor=actor, workflow_id=workflow_id,
                    prompt=str(body.get("prompt", "")),
                    mode=str(body.get("mode", "modify")),
                    display_language=_display_language(body),
                    agent=agent,
                    idempotency_key=key,
                )
            except AuthoringJobConflict as exc:
                raise ValueError(json.dumps({
                    "code": exc.code, "active_job": exc.job,
                }, ensure_ascii=False)) from None

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.modify", command)

    async def workflow_delete(request: Request) -> JSONResponse:
        if ctx.workflow_publisher is None:
            return error("delete_unavailable", "workflow deletion is unavailable", 503)
        workflow_id = str(EntityId.parse(request.path_params["workflow_id"]))

        def command(body: Mapping[str, Any], actor: str, key: str):
            from ...workflow.persistence import PublishConflictError

            with connect_workflow_database(ctx.path, read_only=True) as connection:
                active = connection.execute(
                    "SELECT 1 FROM workflow_authoring_jobs WHERE workflow_id=?"
                    " AND status IN ('queued','running') LIMIT 1",
                    (workflow_id,),
                ).fetchone()
            if active is not None:
                raise ValueError("workflow authoring is still active")
            try:
                ctx.workflow_publisher.delete_workflow(
                    workflow_id,
                    expected_latest_version=_required_version(body),
                )
            except PublishConflictError as exc:
                raise ValueError(str(exc)) from None
            return {"workflow_id": workflow_id, "deleted": True}

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.delete", command)

    async def workflow_authoring_schema(request: Request) -> JSONResponse:
        """The contract an editor must draw within, served by the server.

        A canvas that hard-codes its own copy of the node kinds, the port
        fields or the edge shape is a second definition of the authoring
        boundary, and the two drift the first time one moves. Serving it means
        the editor's palette and the compiler's answer come from one place.

        Read scope: this is the shape of a definition, not any definition.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        return JSONResponse(envelope({
            "dsl_version": "1.3",
            "node_kinds": list(LANGGRAPH_NODE_KINDS),
            "schema": authoring_json_schema(),
        }))

    async def authoring_job_list(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        if ctx.authoring_jobs is None:
            return JSONResponse(envelope({"jobs": []}))
        unknown = set(request.query_params) - {"mine", "active", "type"}
        if unknown:
            return error("invalid_request", "unknown authoring job query parameter")
        jobs = ctx.authoring_jobs.list(
            actor=actor,
            active_only=request.query_params.get("active") == "true",
            job_type=request.query_params.get("type") or None,
        )
        return JSONResponse(envelope({"jobs": jobs}))

    async def authoring_job_read(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            job = ctx.authoring_jobs.get(request.path_params["job_id"], actor=actor)
        except (AttributeError, LookupError):
            return error("not_found", "authoring job not found", 404)
        return JSONResponse(envelope(job))

    async def authoring_job_output(request: Request) -> JSONResponse:
        """What the Agent CLI printed while it wrote this job's DSL.

        A tail, like a run's console: the client asks "what is new since chunk
        N". Sensitive scope, because a console holds whatever the Agent echoed,
        and the job lookup is what proves this actor owns it.
        """

        actor = ctx.authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        allowed_params = {"after", "limit"}
        unknown = set(request.query_params) - allowed_params
        if unknown:
            return error(
                "invalid_request", f"unknown output parameter: {sorted(unknown)[0]}"
            )
        try:
            after = int(request.query_params.get("after") or 0)
            limit = page_size(request.query_params.get("limit"))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        job_id = request.path_params["job_id"]
        try:
            ctx.authoring_jobs.get(job_id, actor=actor)
        except (AttributeError, LookupError):
            return error("not_found", "authoring job not found", 404)
        chunks, next_after = ctx.authoring_jobs.output(
            job_id, after_chunk_id=after, limit=limit,
        )
        return JSONResponse(envelope({
            "chunks": chunks,
            "next_after": next_after,
            "has_more": next_after is not None,
        }))

    async def authoring_job_cancel(request: Request) -> JSONResponse:
        if ctx.authoring_jobs is None:
            return error("generation_unavailable", "authoring jobs are unavailable", 503)

        def command(body: Mapping[str, Any], actor: str, key: str):
            return ctx.authoring_jobs.cancel(request.path_params["job_id"], actor=actor)

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.authoring.cancel", command)

    async def workflow_validate(request: Request) -> JSONResponse:
        """Compile an edited draft without publishing or changing state."""

        if ctx.workflow_publisher is None:
            return error(
                "validation_unavailable", "workflow validation is not wired", 503,
            )

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            from ...workflow.dsl import DiagnosticError

            source = body.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("source is required")
            expected = _required_version(body)
            try:
                compiled = ctx.workflow_publisher.validate_workflow(
                    source, source_name="<api-draft>", source_format="json",
                )
            except DiagnosticError as exc:
                raise ValueError(json.dumps({
                    "message": "workflow source failed validation",
                    "diagnostics": [item.to_dict() for item in exc.diagnostics],
                }, ensure_ascii=False))
            workflow_id = compiled.ir.workflow_id
            latest = next((
                item["latest_version"] for item in ctx.workflow_reads.list()
                if item["workflow_id"] == workflow_id
            ), 0)
            if expected != latest:
                raise ValueError(
                    f"draft version conflict: expected {expected}, actual {latest}"
                )
            return {
                "source": source,
                "workflow_id": workflow_id,
                "definition_hash": compiled.definition_hash.value,
                "node_count": len(compiled.ir.nodes),
                "latest_version": latest,
                "allowed_commands": _draft_commands(workflow_id, latest),
            }

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.validate", command)

    async def workflow_publish(request: Request) -> JSONResponse:
        if ctx.workflow_publisher is None:
            return error(
                "publish_unavailable", "workflow publishing is not wired", 503,
            )
        workflow_id = request.path_params["workflow_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            from ...workflow.dsl import DiagnosticError
            from ...workflow.persistence import PublishConflictError

            source = body.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("source is required")
            expected = _required_version(body)
            # Compile-and-check before any write: a body that fails validation
            # or compiles to a different workflow than the route names must
            # leave nothing behind.
            try:
                compiled = ctx.workflow_publisher.compile_workflow(
                    source, source_name="<api>", source_format="json",
                )
            except DiagnosticError as exc:
                raise ValueError(json.dumps({
                    "message": "workflow source failed validation",
                    "diagnostics": [item.to_dict() for item in exc.diagnostics],
                }, ensure_ascii=False))
            if compiled.ir.workflow_id != workflow_id:
                raise ValueError(
                    f"source declares {compiled.ir.workflow_id}, route names {workflow_id}"
                )
            try:
                record = ctx.workflow_publisher.publish_workflow(
                    source, source_name="<api>", source_format="json",
                    expected_latest_version=expected, actor=actor,
                )
            except PublishConflictError as exc:
                raise ValueError(
                    f"publish conflict: expected {exc.expected}, actual {exc.actual}"
                )
            return {
                "workflow_id": record.workflow_id,
                "version": record.version.value,
                "definition_hash": record.definition_hash.value,
            }

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.publish", command)

    def handler_bindings(item: Mapping[str, Any]) -> list[dict[str, Any]]:
        """How each node's pinned Handler compares with what is registered.

        A published plan names the exact Handler build it was compiled against,
        and an Agent's build *is* its CLI version — so upgrading a CLI retires
        every binding to the old one. That is the guarantee working, not a
        fault, but the operator has to be able to see it: without this the run
        simply refuses to start and nothing says which Agent moved.
        """

        registry = getattr(ctx.durable_service, "execution_registry", None)
        available: dict[str, Any] = {}
        if registry is not None and registry.sealed:
            for entry in registry.entries():
                available[entry.manifest.name] = entry.manifest
        bindings = []
        for node in item.get("definition", {}).get("nodes", ()):
            handler = node.get("handler")
            if not handler:
                continue
            name, pinned = handler["name"], handler["version"]
            current = available.get(name)
            current_version = None if current is None else current.version
            agent_binding = (
                current is not None and "agent.invoke" in current.capabilities
            )
            bindings.append({
                "node_id": node["id"],
                "handler_name": name,
                "pinned_version": pinned,
                "available_version": current_version,
                "status": (
                    "current" if current_version == pinned or agent_binding
                    else "missing" if current is None else "version_changed"
                ),
            })
        return bindings

    def action_editors(item: Mapping[str, Any], actor: str) -> dict[str, Any]:
        """Editable Action fields and compatible Agent choices, server-owned.

        A Handler switch is offered only when its port contract exactly matches
        the node already wired into the graph.  The browser never infers this
        from names or rewrites ports and edges to make a choice fit.
        """

        if (
            ctx.workflow_publisher is None
            or not item.get("source_available")
            or not ctx.guard.allows(actor, WRITE_SCOPE)
        ):
            return {}
        registry = getattr(ctx.durable_service, "execution_registry", None)
        if registry is None or not registry.sealed:
            return {}
        agent_manifests = [
            entry.manifest for entry in registry.entries()
            if "agent.invoke" in entry.manifest.capabilities
        ]
        editors: dict[str, Any] = {}
        for node in item.get("definition", {}).get("nodes", ()):
            if node.get("kind") != "action" or not node.get("handler"):
                continue
            inputs = {port["id"]: port["schema_id"] for port in node.get("inputs", ())}
            outputs = {port["id"]: port["schema_id"] for port in node.get("outputs", ())}
            choices = [
                {"name": manifest.name, "version": manifest.version}
                for manifest in agent_manifests
                if dict(manifest.inputs) == inputs and dict(manifest.outputs) == outputs
            ]
            if not choices:
                continue
            node_id = str(node["id"])
            editors[node_id] = {
                "handlers": choices,
                "allowed_command": {
                    "command": "workflow.action.update",
                    "label": "Update action",
                    "method": "POST",
                    "href": (
                        f"/api/v1/workflows/{quote(item['workflow_id'], safe=':')}"
                        f"/actions/{quote(node_id, safe='')}"
                    ),
                    "target_aggregate_id": item["workflow_id"],
                    "expected_version": item["latest_version"],
                    "payload_schema": "workflow-action-update/1.0",
                },
            }
        return editors

    async def workflow_detail(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            workflow_id = str(EntityId.parse(request.path_params["workflow_id"]))
            if not workflow_id.startswith("workflow:"):
                raise ValueError("workflow id is required")
            # No version selector: the catalog serves the current definition
            # and nothing else, so `?version=` is an unknown parameter now.
            if set(request.query_params):
                raise ValueError("unknown workflow query parameter")
            item = ctx.workflow_reads.detail(workflow_id)
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        item["active_job"] = (
            None if ctx.authoring_jobs is None
            else ctx.authoring_jobs.active_for_workflow(workflow_id, actor=actor)
        )
        item["langgraph_compatibility"] = (
            {"compatible": False, "reason": "service_not_configured"}
            if ctx.langgraph_service is None
            else ctx.langgraph_service.compatibility(
                workflow_id, item["latest_version"]
            )
        )
        item["allowed_commands"] = ([{
            "command": "langgraph_run.start",
            "label": "Start run",
            "method": "POST",
            "href": "/api/v1/langgraph-runs",
            "target_aggregate_id": workflow_id,
            "expected_version": 0,
            "payload_schema": "langgraph-run-start/1.0",
        }] if ctx.guard.allows(actor, WRITE_SCOPE)
        and item["langgraph_compatibility"]["compatible"] else [{
            "command": "run.start",
            "label": "Start run",
            "method": "POST",
            "href": "/api/v1/runs",
            "target_aggregate_id": item["workflow_id"],
            "expected_version": 0,
            "payload_schema": "run-start/1.0",
        }] if ctx.guard.allows(actor, WRITE_SCOPE) and ctx.legacy_execution
        and item["goal_readiness"] == "ready" else [])
        if ctx.guard.allows(actor, WRITE_SCOPE) and ctx.workflow_publisher is not None:
            item["allowed_commands"].append({
                "command": "workflow.delete",
                "label": "Delete workflow",
                "method": "DELETE",
                "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}",
                "target_aggregate_id": workflow_id,
                "expected_version": item["latest_version"],
                "payload_schema": "workflow-delete/1.0",
                "confirmation": "explicit",
            })
        if (
            ctx.draft_service is not None
            and getattr(ctx.draft_service, "reviser", None) is not None
            and item.get("source_available")
            and ctx.guard.allows(actor, WRITE_SCOPE)
        ):
            item["allowed_commands"].append({
                "command": "workflow.draft.create",
                "label": "Edit workflow",
                "method": "POST",
                "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}/drafts",
                "target_aggregate_id": workflow_id,
                "expected_version": item["latest_version"],
                "payload_schema": "workflow-draft-create/1.0",
            })
        if (
            ctx.authoring_jobs is not None and item.get("source_available")
            and ctx.guard.allows(actor, WRITE_SCOPE)
        ):
            item["allowed_commands"].append({
                "command": "workflow.modify",
                "label": "Modify workflow",
                "method": "POST",
                "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}/modify",
                "target_aggregate_id": workflow_id,
                "expected_version": item["latest_version"],
                "payload_schema": "workflow-modify/1.0",
            })
        item["handler_bindings"] = handler_bindings(item)
        stale = [
            binding for binding in item["handler_bindings"]
            if binding["status"] != "current"
        ]
        item["handler_drift"] = stale
        item["action_editors"] = action_editors(item, actor)
        # Rebinding recompiles the same source against what is installed now.
        # Offered only when every stale binding has somewhere to land: a
        # Handler that is gone entirely is not a version to move to.
        if (
            stale
            and all(binding["status"] == "version_changed" for binding in stale)
            and item.get("source_available")
            and ctx.workflow_publisher is not None
            and ctx.guard.allows(actor, WRITE_SCOPE)
        ):
            item["allowed_commands"].append({
                "command": "workflow.rebind",
                "label": "Rebind to installed handlers",
                "method": "POST",
                "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}/rebind",
                "target_aggregate_id": workflow_id,
                "expected_version": item["latest_version"],
                "payload_schema": "workflow-rebind/1.0",
                "confirmation": "explicit",
            })
        return JSONResponse(envelope(item))

    async def workflow_action_update(request: Request) -> JSONResponse:
        if ctx.workflow_publisher is None:
            return error("publish_unavailable", "workflow publishing is not wired", 503)
        workflow_id = str(EntityId.parse(request.path_params["workflow_id"]))
        node_id = request.path_params["node_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            from ...workflow.persistence import PublishConflictError

            unexpected = set(body) - {"expected_version", "label", "handler", "prompt"}
            if unexpected:
                raise ValueError(f"unknown action update field: {sorted(unexpected)[0]}")
            expected = _required_version(body)
            label = body.get("label")
            prompt = body.get("prompt")
            handler = body.get("handler")
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > 80:
                raise ValueError("label must be 1-80 characters")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > 4000:
                raise ValueError("prompt must be 1-4000 characters")
            if not isinstance(handler, Mapping) or set(handler) != {"name", "version"}:
                raise ValueError("handler must contain exactly name and version")
            handler_name = handler.get("name")
            handler_version = handler.get("version")
            if not isinstance(handler_name, str) or not isinstance(handler_version, str):
                raise ValueError("handler name and version must be strings")

            item = ctx.workflow_reads.detail(workflow_id)
            if int(item["latest_version"]) != expected:
                raise ValueError(
                    f"publish conflict: expected {expected}, actual {item['latest_version']}"
                )
            editor = action_editors(item, actor).get(node_id)
            if editor is None:
                raise ValueError("node is not an editable action")
            choice = {"name": handler_name.strip(), "version": handler_version.strip()}
            if choice not in editor["handlers"]:
                raise ValueError("handler is not compatible with this action's ports")

            source = item.get("source")
            source_format = item.get("source_format")
            if not isinstance(source, str) or source_format not in {"json", "yaml"}:
                raise ValueError("workflow source is unavailable")
            document = json.loads(source) if source_format == "json" else yaml.safe_load(source)
            nodes = document.get("nodes") if isinstance(document, Mapping) else None
            node = next(
                (entry for entry in nodes or () if isinstance(entry, Mapping)
                 and str(entry.get("id")) == node_id),
                None,
            )
            if node is None or node.get("kind") != "action":
                raise ValueError("node is not an editable action")
            node["label"] = label.strip()
            node["handler"] = choice
            config = node.get("config")
            if config is None:
                config = {}
                node["config"] = config
            if not isinstance(config, dict):
                raise ValueError("action config must be an object")
            config["prompt"] = prompt.strip()
            rewritten = (
                json.dumps(document, ensure_ascii=False, indent=2)
                if source_format == "json"
                else yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
            )
            try:
                record = ctx.workflow_publisher.publish_workflow(
                    rewritten, source_name="<action-update>", source_format=source_format,
                    expected_latest_version=expected, actor=actor,
                )
            except PublishConflictError as exc:
                raise ValueError(
                    f"publish conflict: expected {exc.expected}, actual {exc.actual}"
                ) from None
            return {
                "workflow_id": record.workflow_id,
                "version": record.version.value,
                "definition_hash": record.definition_hash.value,
                "node_id": node_id,
                "changed_fields": ["label", "handler", "prompt"],
            }

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.action.update", command)

    async def workflow_rebind(request: Request) -> JSONResponse:
        """Republish this workflow, moving each node to the installed Handler.

        A plain recompile does not help: the source pins the build the workflow
        was authored against, and an exact pin that is gone cannot resolve —
        while a range that once matched excludes the newer build (`^0.18` stops
        before `0.19`). So the rebind rewrites each node's handler version to
        the one registered now, then republishes. The old version keeps its
        bindings and its runs; immutability holds, this is simply a new one.
        """

        workflow_id = str(EntityId.parse(request.path_params["workflow_id"]))

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            expected = _required_version(body)
            detail = ctx.workflow_reads.detail(workflow_id)
            source = detail.get("source")
            if not source:
                raise ValueError(
                    "this version was published without its source, so it cannot "
                    "be rebound; publish the workflow again from its source"
                )
            source_format = detail.get("source_format") or "yaml"
            available = {
                binding["handler_name"]: binding["available_version"]
                for binding in handler_bindings(detail)
                if binding["available_version"] is not None
            }
            document = (
                json.loads(source) if source_format == "json"
                else yaml.safe_load(source)
            )
            moved = _retarget_handlers(document, available)
            if not moved:
                raise ValueError(
                    "nothing to rebind: no node names a handler that is installed "
                    "at a different version"
                )
            rewritten = (
                json.dumps(document) if source_format == "json"
                else yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
            )
            try:
                record = ctx.workflow_publisher.publish_workflow(
                    rewritten, source_name="<rebind>", source_format=source_format,
                    expected_latest_version=expected, actor=actor,
                )
            except PublishConflictError as exc:
                raise ValueError(
                    f"publish conflict: expected {exc.expected}, actual {exc.actual}"
                )
            return {
                "workflow_id": record.workflow_id,
                "version": record.version.value,
                "definition_hash": record.definition_hash.value,
                "rebound": moved,
            }

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.rebind", command)

    # Reading a definition, compiling it and publishing a version. The graph
    # editor needs exactly these, and they say nothing about which authoring UI
    # is on, so both modes get them.
    catalog = [
        Route("/api/v1/workflows", workflow_catalog, methods=["GET"]),
        Route("/api/v1/workflows/validate", workflow_validate, methods=["POST"]),
        Route(
            "/api/v1/workflows/authoring-schema",
            workflow_authoring_schema, methods=["GET"],
        ),
        Route(
            "/api/v1/workflows/{workflow_id}", workflow_detail, methods=["GET"]
        ),
        Route(
            "/api/v1/workflows/{workflow_id}/versions", workflow_publish,
            methods=["POST"],
        ),
    ]
    if not authoring:
        return catalog

    # Prompt-driven authoring, which single-agent mode deliberately does not
    # have: there, a goal is written by the connected Agent through a template,
    # not by asking this endpoint for DSL. Mounting it would give that product
    # a second authoring path it was built without.
    return [
        *catalog[:2],
        # /generate before /{workflow_id}: Starlette matches in order, and the
        # literal segment must not be captured as a workflow id.
        Route("/api/v1/workflows/generate", workflow_generate, methods=["POST"]),
        *catalog[2:3],
        Route(
            "/api/v1/workflow-authoring-jobs", authoring_job_list, methods=["GET"],
        ),
        Route(
            "/api/v1/workflow-authoring-jobs/{job_id}",
            authoring_job_read, methods=["GET"],
        ),
        Route(
            "/api/v1/workflow-authoring-jobs/{job_id}/output",
            authoring_job_output, methods=["GET"],
        ),
        Route(
            "/api/v1/workflow-authoring-jobs/{job_id}/cancel",
            authoring_job_cancel, methods=["POST"],
        ),
        *catalog[3:4],
        Route(
            "/api/v1/workflows/{workflow_id}", workflow_delete, methods=["DELETE"]
        ),
        Route(
            "/api/v1/workflows/{workflow_id}/modify",
            workflow_modify, methods=["POST"],
        ),
        Route(
            "/api/v1/workflows/{workflow_id}/actions/{node_id}",
            workflow_action_update, methods=["POST"],
        ),
        *catalog[4:],
        Route(
            "/api/v1/workflows/{workflow_id}/rebind", workflow_rebind,
            methods=["POST"],
        ),
    ]
