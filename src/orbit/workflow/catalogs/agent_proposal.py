"""A reviewable proposal to trust one more Agent CLI. Never a registration.

The allowlist in `agent_discovery` grows one way: somebody edits that file and
somebody reviews the edit. This module exists so the tedious half of that —
working out whether a program is even here, and what it calls itself — can be
done for them, without the answer ever becoming an executable on its own.

So the output is a patch. A patch is inert: it changes nothing until a person
reads it and applies it, which is exactly the review the allowlist's comment
asks for. And the spec it proposes carries no `invocation`, so even a careless
merge yields a CLI that is *listed and not runnable* — `runtime_compatible` is
False until a human adds an invocation they probed themselves. The one thing
that cannot be automated is the one thing left for the reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path
from typing import Callable, Sequence
import shutil
import subprocess

from .agent_discovery import (
    TRUSTED_AGENT_CLIS, AgentCliSpec, CandidateProbe, probe_executable,
)


DISCOVERY_FILE = "src/orbit/workflow/catalogs/agent_discovery.py"
DISCOVERY_TESTS = "tests/test_agent_discovery.py"

# Where each edit goes. Anchored on text rather than line numbers so a patch is
# either correct or refused outright: a moved anchor raises here instead of
# producing a diff that applies to the wrong place.
_ALLOWLIST_END = "    AgentCliSpec(\"opencode\", \"opencode\", invocation=AgentInvocation(args=(\"run\",))),\n)"
_REVIEWED_SET_END = "                (\"opencode\", \"opencode\"),\n            },"
_PERMISSIONS_END = "                \"gemini\": (),\n            },"


@dataclass(frozen=True)
class AgentProposal:
    """One candidate, with the verdict a reviewer needs and why."""

    probe: CandidateProbe
    verdict: str
    detail: str

    @property
    def proposable(self) -> bool:
        return self.verdict == "proposable"


def propose(
    names: Sequence[str],
    *,
    specs: Sequence[AgentCliSpec] = TRUSTED_AGENT_CLIS,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
) -> tuple[AgentProposal, ...]:
    """Judge each proposed program name, in the order it was suggested.

    Duplicates collapse: a suggestion list is somebody's guesses, and guessing
    the same thing twice is not two candidates.
    """

    seen: set[str] = set()
    out: list[AgentProposal] = []
    for name in names:
        probe = probe_executable(name, specs=specs, which=which, runner=runner)
        if probe.executable in seen:
            continue
        seen.add(probe.executable)
        out.append(AgentProposal(probe, *_judge(probe)))
    return tuple(out)


def _judge(probe: CandidateProbe) -> tuple[str, str]:
    if probe.refused is not None:
        return "refused", probe.refused
    if probe.already_trusted is not None:
        return (
            "already_trusted",
            f"the allowlist already covers this program as {probe.already_trusted!r}",
        )
    if not probe.on_path:
        return "not_installed", "nothing by this name resolves on PATH"
    if probe.version is None:
        # Detection and registration are separate facts, and this is where they
        # part: `agent_discovery` will list a CLI whose version it could not
        # establish, but registering one would put a version in a manifest
        # fingerprint that nothing ever confirmed.
        return (
            "unpinned",
            "it is installed, but its version flag produced no version — "
            "an unpinned build cannot be registered",
        )
    return "proposable", f"installed and reporting version {probe.version}"


def render_patch(
    proposals: Sequence[AgentProposal], *, root: Path | str = Path("."),
) -> str:
    """A unified diff adding every proposable candidate, or "" when none are.

    Both pinned tests are edited alongside the allowlist, because leaving them
    to fail would teach whoever hits it that those assertions are chores to
    update rather than the review gate they are.
    """

    additions = [item for item in proposals if item.proposable]
    if not additions:
        return ""

    root = Path(root)
    specs = "".join(
        f"    # Proposed from a system probe: installed here, reporting version\n"
        f"    # {item.probe.version}. No invocation yet — nobody has watched this CLI\n"
        f"    # accept a prompt or refuse a permission prompt, so it is listed and\n"
        f"    # not runnable until someone does and records what they saw.\n"
        f'    AgentCliSpec("{item.probe.executable}", "{item.probe.executable}"),\n'
        for item in additions
    )
    reviewed = "".join(
        f'                ("{item.probe.executable}", "{item.probe.executable}"),\n'
        for item in additions
    )
    permissions = "".join(
        f"                # Not probed: no invocation is proposed either, so\n"
        f"                # there is no prompt to waive yet.\n"
        f'                "{item.probe.executable}": (),\n'
        for item in additions
    )

    return "".join([
        # After the last entry, not before it: the anchor's first line carries
        # opencode's own comment, and inserting ahead of it would leave that
        # sentence sitting above somebody else's spec.
        _edit(root, DISCOVERY_FILE, _ALLOWLIST_END,
              _ALLOWLIST_END.replace("\n)", "\n" + specs + ")")),
        _edit(root, DISCOVERY_TESTS, _REVIEWED_SET_END,
              _REVIEWED_SET_END.replace("            },", reviewed + "            },")),
        _edit(root, DISCOVERY_TESTS, _PERMISSIONS_END,
              _PERMISSIONS_END.replace("            },", permissions + "            },")),
    ])


def _edit(root: Path, relative: str, anchor: str, replacement: str) -> str:
    path = root / relative
    before = path.read_text(encoding="utf-8")
    if before.count(anchor) != 1:
        raise ValueError(
            f"{relative}: expected exactly one anchor, found {before.count(anchor)}"
        )
    after = before.replace(anchor, replacement)
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{relative}", tofile=f"b/{relative}", n=3,
    ))
