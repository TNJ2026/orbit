"""Proposing and applying a constrained trusted Agent CLI change.

The property these guard: a suggestion may come from anywhere — a person, a
model reading their prompt — and still cannot become an executable. What it
can become is a patch somebody reads.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from orbit.workflow.catalogs.agent_discovery import (
    TRUSTED_AGENT_CLIS, AgentCliSpec, AgentInvocation, probe_executable,
)
from orbit.workflow.catalogs.agent_proposal import apply_patch, propose, render_patch
from orbit.web.api_v1.agent_proposals import _explicit_cli_names, _mentioned_names


ROOT = Path(__file__).resolve().parent.parent


def which_for(installed):
    return lambda name: f"/usr/local/bin/{name}" if name in installed else None


def version_runner(text="tool 1.2.3", code=0):
    return lambda *a, **k: SimpleNamespace(returncode=code, stdout=text, stderr="")


class ProbeTests(unittest.TestCase):
    def test_a_name_that_is_not_a_bare_program_never_runs_anything(self) -> None:
        """The injection cases, refused before `which` is consulted."""

        def explode(_name):
            self.fail("resolution must not be attempted for a refused name")

        for hostile in ("rm -rf /", "../evil", "/usr/bin/curl", "a;b", "UPPER"):
            with self.subTest(hostile=hostile):
                probe = probe_executable(hostile, which=explode)
                self.assertIsNotNone(probe.refused)
                self.assertFalse(probe.on_path)

    def test_a_probe_reports_the_version_it_actually_read(self) -> None:
        probe = probe_executable(
            "aider", specs=(), which=which_for({"aider"}),
            runner=version_runner("aider 0.86.1"),
        )
        self.assertTrue(probe.on_path)
        self.assertEqual("0.86.1", probe.version)

    def test_a_probe_never_hands_back_the_resolved_path(self) -> None:
        """The one fact the bare-program-name rule exists to withhold."""

        probe = probe_executable(
            "aider", specs=(), which=which_for({"aider"}), runner=version_runner(),
        )
        self.assertNotIn("/usr/local/bin", str(probe))

    def test_a_program_the_allowlist_already_covers_says_so(self) -> None:
        probe = probe_executable("claude", which=which_for({"claude"}),
                                 runner=version_runner())
        self.assertEqual("claude", probe.already_trusted)


class ProposalTests(unittest.TestCase):
    def test_cli_name_is_extracted_from_chinese_prompt_without_model_authority(self) -> None:
        self.assertEqual(["kimi"], _explicit_cli_names("请给 Orbit 添加 Kimi CLI"))

    def test_only_names_adjacent_to_cli_are_extracted(self) -> None:
        self.assertEqual(
            ["kimi"],
            _explicit_cli_names("用 claude 帮我添加 kimi CLI，不要添加 pi"),
        )

    def test_only_cli_names_explicitly_present_in_the_prompt_survive(self) -> None:
        self.assertEqual(
            ["kimi"],
            _mentioned_names("请给 Orbit 添加 Kimi CLI", ["kimi", "pi", "claude"]),
        )

    def test_hyphenated_name_uses_whole_name_boundaries(self) -> None:
        self.assertEqual(
            ["hermes-manager"],
            _mentioned_names(
                "添加 hermes-manager CLI", ["hermes", "hermes-manager"],
            ),
        )

    def test_an_installed_pinned_candidate_is_proposable(self) -> None:
        proposal, = propose(["aider"], specs=(), which=which_for({"aider"}),
                            runner=version_runner("aider 0.86.1"))
        self.assertTrue(proposal.proposable)

    def test_an_installed_candidate_with_no_readable_version_is_not(self) -> None:
        """Detection and registration are different facts, and this is the seam.

        Discovery lists a CLI whose version it could not establish. Registering
        one would put a version in a manifest fingerprint that nothing ever
        confirmed.
        """

        proposal, = propose(["aider"], specs=(), which=which_for({"aider"}),
                            runner=version_runner("no version here"))
        self.assertEqual("unpinned", proposal.verdict)

    def test_nothing_on_path_is_reported_rather_than_guessed_at(self) -> None:
        proposal, = propose(["aider"], specs=(), which=which_for(set()))
        self.assertEqual("not_installed", proposal.verdict)

    def test_a_program_already_trusted_is_not_proposed_twice(self) -> None:
        proposal, = propose(["claude"], which=which_for({"claude"}),
                            runner=version_runner())
        self.assertEqual("already_trusted", proposal.verdict)

    def test_the_same_guess_twice_is_one_candidate(self) -> None:
        self.assertEqual(
            1, len(propose(["aider", "aider"], specs=(), which=which_for({"aider"}),
                           runner=version_runner())),
        )


class PatchTests(unittest.TestCase):
    def sample(self):
        return propose(["aider"], specs=(), which=which_for({"aider"}),
                       runner=version_runner("aider 0.86.1"))

    def test_nothing_proposable_renders_no_patch(self) -> None:
        refused = propose(["rm -rf /"], specs=())
        self.assertEqual("", render_patch(refused, root=ROOT))

    def test_the_patch_applies_and_leaves_the_suite_passing(self) -> None:
        """Applied against a real checkout, not merely well-formed text.

        A generated patch that applies but leaves the pinned assertions failing
        would teach whoever hit it that those assertions are chores rather than
        the review gate the allowlist depends on.
        """

        patch = render_patch(self.sample(), root=ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "checkout"
            subprocess.run(
                ["git", "clone", "--quiet", "--no-hardlinks", "--depth", "1",
                 str(ROOT), str(work)],
                check=True, capture_output=True,
            )
            for relative in (
                "src/orbit/workflow/catalogs/agent_discovery.py",
                "tests/test_agent_discovery.py",
            ):
                (work / relative).write_text(
                    (ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8",
                )
            applied = subprocess.run(
                ["git", "apply", "--verbose", "-"], cwd=work,
                input=patch, text=True, capture_output=True,
            )
            self.assertEqual(0, applied.returncode, applied.stderr)

            checked = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_agent_discovery"],
                cwd=work, capture_output=True, text=True,
                # Only the patched tree on the path. With the real one ahead
                # of it the clone imported this repository's allowlist and the
                # patched assertions failed against an allowlist the patch had
                # never touched.
                env={"PATH": "/usr/bin:/bin", "HOME": str(work),
                     "PYTHONPATH": str(work / "src")},
            )
            self.assertIn("OK", checked.stderr, checked.stderr[-2000:])

    def test_the_proposed_spec_is_listed_and_not_runnable(self) -> None:
        """A careless merge must not produce an executable Agent.

        `invocation=None` is the existing seam for "detected, no reviewed way
        to call it". Proposing into it means the reviewer's remaining job — the
        one nothing can do for them — is also the thing standing between this
        and execution.
        """

        patch = render_patch(self.sample(), root=ROOT)
        added = [
            line for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        self.assertIn('+    AgentCliSpec("aider", "aider"),', added)
        # Context lines carry other specs' invocations; only what this patch
        # *adds* is the claim under test.
        self.assertEqual([], [line for line in added if "AgentInvocation" in line])
        self.assertFalse(AgentCliSpec("aider", "aider").runtime_compatible)

    def test_apply_patch_accepts_only_the_two_reviewed_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            apply_patch("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")

    def test_a_moved_anchor_refuses_rather_than_patching_the_wrong_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp)
            (fake / "src/orbit/workflow/catalogs").mkdir(parents=True)
            (fake / "tests").mkdir()
            (fake / "src/orbit/workflow/catalogs/agent_discovery.py").write_text(
                "# the allowlist moved\n", encoding="utf-8",
            )
            (fake / "tests/test_agent_discovery.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "anchor"):
                render_patch(self.sample(), root=fake)


if __name__ == "__main__":
    unittest.main()


class EndpointTests(unittest.TestCase):
    """`/api/v1/agent-proposals` over HTTP, including who may reach it."""

    def build(self, *, authoring=True):
        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
        from orbit.web.app import create_app
        from test_web_composition import SCHEMAS

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        scopes = [READ_SCOPE, WRITE_SCOPE] if authoring else [READ_SCOPE]
        return create_app(
            Path(temp.name) / "runtime.db",
            schemas=SCHEMAS,
            poll_seconds=0.05,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: scopes),
            # Discovery is what wires an authoring service, and without one the
            # command is not offered at all — which is its own assertion below.
            discover_agents=authoring,
            langgraph_state_directory=Path(temp.name) / "langgraph",
        )

    def test_the_command_is_advertised_on_the_catalog_it_belongs_to(self) -> None:
        from test_web_composition import AsgiHarness

        with AsgiHarness(self.build()) as client:
            data = client.get("/api/v1/handler-catalog", actor="writer").json()["data"]
            self.assertIn(
                "agent.proposal.probe",
                [item["command"] for item in data["allowed_commands"]],
            )

    def test_a_reader_is_offered_nothing(self) -> None:
        """The page asks the server what it may do, and this is the answer."""

        from test_web_composition import AsgiHarness

        with AsgiHarness(self.build(authoring=False)) as client:
            data = client.get("/api/v1/handler-catalog", actor="reader").json()["data"]
            self.assertEqual([], data["allowed_commands"])

    def test_named_candidates_come_back_judged(self) -> None:
        from test_web_composition import AsgiHarness

        with AsgiHarness(self.build()) as client:
            response = client.post(
                "/api/v1/agent-proposals", actor="writer", key="probe-1",
                body={"names": ["claude", "definitelynotinstalledxyz"]},
            )
            self.assertEqual(200, response.status_code, response.text)
            verdicts = {
                item["executable"]: item["verdict"]
                for item in response.json()["data"]["proposals"]
            }
            self.assertEqual("already_trusted", verdicts["claude"])
            self.assertEqual("not_installed", verdicts["definitelynotinstalledxyz"])

    def test_a_hostile_name_is_refused_over_http_too(self) -> None:
        """The boundary is in the probe, so the transport cannot widen it."""

        from test_web_composition import AsgiHarness

        with AsgiHarness(self.build()) as client:
            response = client.post(
                "/api/v1/agent-proposals", actor="writer", key="probe-2",
                body={"names": ["/usr/bin/curl", "rm -rf /"]},
            )
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertEqual(
                {"refused"}, {item["verdict"] for item in data["proposals"]},
            )
            self.assertEqual("", data["patch"])

    def test_a_request_naming_neither_is_refused(self) -> None:
        from test_web_composition import AsgiHarness

        with AsgiHarness(self.build()) as client:
            response = client.post(
                "/api/v1/agent-proposals", actor="writer", key="probe-3", body={},
            )
            self.assertNotEqual(200, response.status_code)
