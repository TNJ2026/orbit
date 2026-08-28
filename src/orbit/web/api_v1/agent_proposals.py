"""`/api/v1/agent-proposals` — investigate a CLI, propose it, register nothing.

The allowlist in `agent_discovery` grows by code review and nothing else. This
endpoint does the part of that review a person should not have to do by hand —
is the program here, what does it call itself — and hands back a patch they
read. Everything it produces is inert text.

The division of labour is the whole point. An Agent is asked *what to look at*,
because turning "something that can refactor Rust" into a list of program names
is exactly what a model is for. Orbit does the looking itself, with the same
probe discovery uses, because what a CLI is and what a model says it is are not
the same fact — and this file must not be where they get confused.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .common import WRITE_SCOPE, error

MAX_PROMPT_CHARS = 2000
MAX_CANDIDATES = 8
_NAME_IN_TEXT = re.compile(r'"([^"]{1,64})"')

_ASK = """You are helping decide which command-line coding agents to look for on
this machine. Answer with a JSON array of at most {limit} lowercase program
names — the executable you would type in a shell, nothing else. No paths, no
arguments, no prose, no explanation.

Need:
{prompt}
"""


def build_routes(ctx) -> list[Route]:
    async def propose_agents(request: Request) -> JSONResponse:
        from ...workflow.catalogs.agent_proposal import propose, render_patch

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
                candidates = _names_in(writer(_ASK.format(
                    limit=MAX_CANDIDATES, prompt=prompt,
                )))

            proposals = propose(candidates)
            return {
                "asked_agent": asked,
                "candidates": list(candidates),
                "proposals": [
                    {
                        "executable": item.probe.executable,
                        "verdict": item.verdict,
                        "detail": item.detail,
                        "version": item.probe.version,
                        "on_path": item.probe.on_path,
                        "already_trusted": item.probe.already_trusted,
                    }
                    for item in proposals
                ],
                # Text, and only ever text. Applying it is a person's job and a
                # reviewer's, which is the property the allowlist rests on.
                "patch": render_patch(proposals),
            }

        try:
            return await ctx.mutate(
                request, WRITE_SCOPE, "agent.proposal.probe", command,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never a 500 page
            return error("proposal_failed", str(exc), 502)

    return [
        Route("/api/v1/agent-proposals", propose_agents, methods=["POST"]),
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
