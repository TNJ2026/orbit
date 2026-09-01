"""`/api/v1/agent-proposals` — investigate and optionally add an Agent CLI.

The allowlist in `agent_discovery` grows by code review and nothing else. This
endpoint does the part of that review a person should not have to do by hand —
is the program here, what does it call itself — and builds a constrained patch.
The UI may ask Orbit to apply that patch; it can touch only the allowlist and
its pinned contract tests, and the new registration takes effect on restart.

The division of labour is the whole point. An Agent is asked *what to look at*,
because turning "something that can refactor Rust" into a list of program names
is exactly what a model is for. Orbit does the looking itself, with the same
probe discovery uses, because what a CLI is and what a model says it is are not
the same fact — and this file must not be where they get confused.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from threading import Lock, Thread
from typing import Any, Mapping
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .common import READ_SCOPE, WRITE_SCOPE, error

MAX_PROMPT_CHARS = 2000
MAX_CANDIDATES = 8
_NAME_IN_TEXT = re.compile(r'"([^"]{1,64})"')

_ASK = """Handle the user's request to add an Agent CLI to Orbit. Report your
work clearly in plain text: identify only the CLI explicitly requested, explain
the executable name you resolved from the request, and state what Orbit should
check before registering it. Do not recommend, mention, or add alternatives.
Keep the report focused, but do not reduce it to a JSON array. This report is
shown verbatim to the user as the Agent's stdout.

Need:
{prompt}
"""


def build_routes(ctx) -> list[Route]:
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = Lock()

    def finish(candidates, asked, agent_output, apply):
        from ...workflow.catalogs.agent_proposal import apply_patch, propose, render_patch

        proposals = propose(candidates)
        patch = render_patch(proposals)
        applied = False
        if apply and patch:
            apply_patch(patch, root=Path.cwd())
            applied = True
        return {
            "asked_agent": asked, "agent_output": agent_output,
            "candidates": list(candidates),
            "proposals": [{
                "executable": item.probe.executable,
                "verdict": item.verdict, "detail": item.detail,
                "version": item.probe.version, "on_path": item.probe.on_path,
                "already_trusted": item.probe.already_trusted,
            } for item in proposals],
            "patch": patch, "applied": applied, "restart_required": applied,
        }

    def run_job(job_id, writer, prompt, candidates, apply):
        from ...workflow.authoring.generator import CancelScope, cancellable

        def output(stream, text):
            if stream != "stdout":
                return
            with jobs_lock:
                job = jobs.get(job_id)
                if job is not None:
                    job["agent_output"] += text

        try:
            with jobs_lock:
                jobs[job_id]["status"] = "running"
            with cancellable(CancelScope(on_output=output, job_id=job_id)):
                complete = writer(_ASK.format(limit=MAX_CANDIDATES, prompt=prompt))
            result = finish(candidates, jobs[job_id]["asked_agent"], complete, apply)
            with jobs_lock:
                jobs[job_id].update(status="done", result=result, agent_output=complete)
        except Exception as exc:  # noqa: BLE001 - exposed through job status
            with jobs_lock:
                jobs[job_id].update(status="failed", error=str(exc))

    async def propose_agents(request: Request) -> JSONResponse:
        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            names = body.get("names")
            prompt = body.get("prompt")
            if names is None and not isinstance(prompt, str):
                raise ValueError("names or prompt is required")
            if names is not None:
                if not isinstance(names, list) or not all(
                    isinstance(item, str) for item in names
                ):
                    raise ValueError("names must be a list of strings")
                candidates = names[:MAX_CANDIDATES]
                asked = None
                return finish(candidates, asked, None, body.get("apply") is True)
            else:
                prompt = prompt.strip()
                if not prompt:
                    raise ValueError("prompt must not be empty")
                if len(prompt) > MAX_PROMPT_CHARS:
                    raise ValueError(
                        f"prompt exceeds {MAX_PROMPT_CHARS} characters"
                    )
                if ctx.authoring_service is None:
                    raise ValueError("no Agent is available to suggest candidates")
                asked = body.get("agent")
                if asked is not None and not isinstance(asked, str):
                    raise ValueError("agent must be a string")
                writer = ctx.authoring_service.text_writer(asked)
                # The Agent still interprets the request, but it does not own
                # scope. The executable beside the word CLI in the person's
                # prompt is the authoritative name; model output may neither
                # add to it nor rename it.
                candidates = _explicit_cli_names(prompt)
                job_id = f"agent_proposal:{uuid.uuid4()}"
                job = {
                    "job_id": job_id, "actor": actor, "status": "queued",
                    "asked_agent": asked, "agent_output": "", "result": None,
                    "error": None,
                    "status_href": f"/api/v1/agent-proposals/{job_id}",
                }
                with jobs_lock:
                    jobs[job_id] = job
                Thread(
                    target=run_job,
                    args=(job_id, writer, prompt, candidates, body.get("apply") is True),
                    daemon=True,
                ).start()
                return {key: value for key, value in job.items() if key != "actor"}

        try:
            return await ctx.mutate(
                request, WRITE_SCOPE, "agent.proposal.probe", command,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never a 500 page
            return error("proposal_failed", str(exc), 502)

    async def proposal_job(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        with jobs_lock:
            job = jobs.get(request.path_params["job_id"])
            if job is None or job["actor"] != actor:
                return error("proposal_not_found", "agent proposal job not found", 404)
            payload = {key: value for key, value in job.items() if key != "actor"}
        return JSONResponse({"schema_version": "1.0", "projection_version": None, "data": payload})

    return [
        Route("/api/v1/agent-proposals", propose_agents, methods=["POST"]),
        Route("/api/v1/agent-proposals/{job_id}", proposal_job, methods=["GET"]),
    ]


def _names_in(text: str) -> list[str]:
    """Program names out of a model's answer, however it chose to wrap them.

    A JSON array is what was asked for and what is tried first. Falling back to
    quoted fragments costs nothing to be wrong about: every name goes through
    the same bare-program-name refusal afterwards, so the worst a bad parse can
    do is propose a candidate that is then rejected on sight.
    """

    fenced = text.strip()
    if "[" in fenced and "]" in fenced:
        chunk = fenced[fenced.index("["): fenced.rindex("]") + 1]
        try:
            value = json.loads(chunk)
        except ValueError:
            value = None
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)][:MAX_CANDIDATES]
    return _NAME_IN_TEXT.findall(fenced)[:MAX_CANDIDATES]


def _mentioned_names(prompt: str, candidates: list[str]) -> list[str]:
    """Keep only executable names that occur literally in the user's prompt.

    The selected Agent is an extractor, not an authority to expand scope. This
    second server-side check makes an extra recommendation harmless even when
    the model ignores that instruction.
    """

    lowered = prompt.casefold()
    kept: list[str] = []
    for candidate in candidates:
        name = candidate.strip().casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            continue
        pattern = rf"(?<![a-z0-9_-]){re.escape(name)}(?![a-z0-9_-])"
        if re.search(pattern, lowered) and name not in kept:
            kept.append(name)
    return kept[:MAX_CANDIDATES]


def _explicit_cli_names(prompt: str) -> list[str]:
    """Names written immediately before or after ``CLI`` in the prompt."""

    lowered = prompt.casefold()
    name = r"[a-z][a-z0-9_-]{0,31}"
    patterns = (
        rf"(?<![a-z0-9_-])({name})\s+(?:agent\s+)?cli(?![a-z0-9_-])",
        rf"(?<![a-z0-9_-])cli\s+({name})(?![a-z0-9_-])",
    )
    found: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            candidate = match.group(1)
            if candidate not in found:
                found.append(candidate)
    return found[:MAX_CANDIDATES]
