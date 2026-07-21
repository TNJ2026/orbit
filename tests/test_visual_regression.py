"""Visual regression harness (delivery plan P0 / §9.2).

Screenshots are compared against PNG baselines in ``tests/visual_baselines/``.
Baselines are platform-bound: font rasterisation differs across OSes by far
more than the diff budget, so each baseline records the platform it was made
on and other platforms skip loudly instead of failing or silently passing.

Updating a baseline is an explicit act::

    VISUAL_UPDATE=1 .venv/bin/python -m unittest tests.test_visual_regression

which rewrites the PNG and its metadata for review in the PR. A plain run
never writes anything; on mismatch it saves ``*.actual.png`` and
``*.diff.png`` beside the baseline for inspection (both are gitignored).

P0 scope is the harness plus one prototype baseline. P1 extends coverage to
the App Shell, three viewports and both themes; P2 adds the discovery views;
P9 expands this to every key page and state (plan §9.2).
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import platform
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone

try:  # pragma: no cover - the skip below reports the absence
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

from orbit.web.app import create_app
from orbit.web.local_identity import local_authorizer, loopback_authenticator
from orbit.workflow.application.run_service import RunApplicationService
from orbit.workflow.api.routes import RateLimiter
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.persistence.database import connect_workflow_database
from tests.test_web_composition import (
    SCHEMAS, publish_linear_workflow, transform_registration,
)
from tests.test_workflow_drafts import dsl as editable_dsl

BASELINES = Path(__file__).parent / "visual_baselines"
PROTOTYPE = Path(__file__).parent.parent / "prototypes" / "runtime-ui.html"
UPDATE = os.environ.get("VISUAL_UPDATE") == "1"
MAX_DIFF_PIXEL_RATIO = 0.001

# Determinism: freeze everything the page could vary on (plan §9.2).
VIEWPORTS = {
    "360x800": {"width": 360, "height": 800},
    "768x900": {"width": 768, "height": 900},
    "1280x800": {"width": 1280, "height": 800},
}
FREEZE_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  caret-color: transparent !important;
}
"""


def seed_visual_artifact(db, service, backend) -> str:
    run_id = RunApplicationService(db, service).start_run(
        workflow_id="workflow:linear", inputs={"value": 7}, actor="local",
        idempotency_key="visual-artifact",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ).run_id
    receipt = backend.write(b"stable visual artifact", max_size_bytes=1024)
    artifact_id = f"artifact:{receipt.checksum.value.removeprefix('sha256:')}"
    now = "2026-01-01T00:00:00+00:00"
    with connect_workflow_database(db) as connection:
        event_id = connection.execute(
            "SELECT event_id FROM run_events WHERE run_id=? ORDER BY global_position LIMIT 1",
            (run_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact_id, run_id, "workflow:linear", "attempt", "attempt:visual",
                "node_run:visual", "report", "schema:text", "text/plain",
                receipt.checksum.value, receipt.size_bytes, receipt.blob_key,
                "run", run_id, "committed", now, now, event_id,
            ),
        )
        connection.execute(
            "INSERT INTO artifact_acl VALUES (?,'local','read','local',?)",
            (artifact_id, now),
        )
        connection.commit()
    return run_id


def platform_tag() -> str:
    return f"{platform.system()}-{platform.machine()}"


def baseline_metadata(name: str) -> dict | None:
    meta = BASELINES / f"{name}.json"
    if not meta.exists():
        return None
    return json.loads(meta.read_text(encoding="utf-8"))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def pixel_difference(expected: bytes, actual: bytes) -> tuple[float, bytes | None]:
    """Return the changed-pixel ratio and a reviewable PNG diff."""
    try:
        import io

        from PIL import Image, ImageChops
    except ImportError:
        return (0.0 if expected == actual else 1.0), None
    left = Image.open(io.BytesIO(expected)).convert("RGBA")
    right = Image.open(io.BytesIO(actual)).convert("RGBA")
    if left.size != right.size:
        return 1.0, None
    diff = ImageChops.difference(left, right)
    red, green, blue, alpha = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = ImageChops.lighter(mask, alpha)
    changed = sum(mask.histogram()[1:])
    output = io.BytesIO()
    diff.save(output, format="PNG")
    return changed / (left.size[0] * left.size[1]), output.getvalue()


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class VisualRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "runtime.db"
        cls.artifact_backend = LocalCASBackend(Path(cls.temp.name) / "artifacts")
        cls.app = create_app(
            cls.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1,
            poll_seconds=0.02,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            authenticator=loopback_authenticator,
            authorizer=local_authorizer(),
            rate_limiter=RateLimiter(requests=1_000),
            artifact_backend=cls.artifact_backend,
            serve_ui=True,
        )
        publish_linear_workflow(cls.db)
        # Keep discovery baselines stable: the Editor fixture is a draft of
        # the existing linear workflow, not a second published catalog entry.
        cls.visual_draft_id = "workflow_draft:visual"
        source = json.dumps(editable_dsl("linear", "Visual Editor"))
        source_hash = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        now = "2026-01-01T00:00:00+00:00"
        with connect_workflow_database(cls.db) as connection:
            connection.execute(
                "INSERT INTO workflow_drafts VALUES (?,?,?,?,?,?,?,'dirty',"
                "NULL,NULL,'[]',1,'active',?,?,NULL)",
                (
                    cls.visual_draft_id, "workflow:linear", 1, "local", "json",
                    source, source_hash, now, now,
                ),
            )
            connection.commit()
        cls.visual_run_id = seed_visual_artifact(
            cls.db, cls.app.state.runtime.service, cls.artifact_backend
        )
        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(
            uvicorn.Config(cls.app, host="127.0.0.1", port=port, log_level="error")
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health/ready", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise AssertionError("visual regression server never became ready")

        # The seeded run is executed by the real worker after lifespan starts.
        # Wait for durable work to settle so Goals/Runs never alternate between
        # running and succeeded depending on screenshot timing.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with connect_workflow_database(cls.db, read_only=True) as connection:
                unsettled = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN "
                    "('ready','leased','running','retry_wait')"
                ).fetchone()[0]
            if not unsettled:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("visual fixture durable work never settled")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=30)
        cls.temp.cleanup()

    def capture(
        self, url: str, *, theme: str, viewport: dict[str, int], wizard: bool = False,
        ready_selector: str | None = None, fail_path: str | None = None,
        editor_conflict: bool = False,
    ) -> bytes:
        context = self.browser.new_context(
            viewport=viewport, locale="en-US", timezone_id="UTC",
            color_scheme="dark" if theme == "dark" else "light",
        )
        try:
            if fail_path:
                context.route(
                    f"**{fail_path}",
                    lambda route: route.fulfill(
                        status=503, content_type="application/json",
                        body=json.dumps({
                            "error": {
                                "code": "temporarily_unavailable",
                                "message": "projection is rebuilding",
                                "details": {},
                            }
                        }),
                    ),
                )
            if editor_conflict:
                context.route(
                    "**/api/v1/workflow-drafts/*/save",
                    lambda route: route.fulfill(
                        status=409, content_type="application/json",
                        body=json.dumps({
                            "error": {
                                "code": "draft_version_conflict",
                                "message": "the draft changed in another tab",
                                "details": {"expected": 1, "actual": 2},
                            }
                        }),
                    ),
                )
            if url.startswith("http"):
                context.add_init_script(
                    f"localStorage.setItem('orbit.theme', {json.dumps(theme)})"
                )
            page = context.new_page()
            page.goto(url)
            page.add_style_tag(content=FREEZE_CSS)
            if url.startswith("http"):
                page.wait_for_function(
                    "() => document.querySelector('#actorChip').textContent === 'local'"
                )
                if ready_selector:
                    page.wait_for_selector(ready_selector)
                else:
                    page.wait_for_selector(".panel")
                if wizard:
                    page.click("#newRun")
                    page.wait_for_selector("dialog[open]")
                    page.check('input[name="workflow"][value="workflow:linear"]')
                    page.click('[data-wizard-next]')
                    page.wait_for_selector("#newRunGoal")
                if editor_conflict:
                    page.click("[data-editor-tab='source']")
                    page.fill("#draftSource", page.input_value("#draftSource") + " ")
                    page.wait_for_selector("#draftReload", timeout=15000)
            page.wait_for_timeout(100)  # one settle tick after fonts/layout
            return page.screenshot(full_page=False)
        finally:
            context.close()

    def assert_matches_baseline(
        self, name: str, image: bytes, viewport: dict[str, int]
    ) -> None:
        BASELINES.mkdir(exist_ok=True)
        png = BASELINES / f"{name}.png"
        meta = baseline_metadata(name)

        if UPDATE:
            png.write_bytes(image)
            (BASELINES / f"{name}.json").write_text(
                json.dumps(
                    {"platform": platform_tag(), "viewport": viewport},
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            return

        if meta is None or not png.exists():
            self.skipTest(
                f"no baseline for {name}; record one with VISUAL_UPDATE=1"
            )
        if meta["platform"] != platform_tag():
            # Loud skip, not silent pass: the reference platform rule of §9.2.
            self.skipTest(
                f"baseline for {name} was recorded on {meta['platform']}; "
                f"this is {platform_tag()} — visual checks only run on the "
                "reference platform"
            )
        ratio, diff = pixel_difference(png.read_bytes(), image)
        if ratio > MAX_DIFF_PIXEL_RATIO:
            (BASELINES / f"{name}.actual.png").write_bytes(image)
            if diff is not None:
                (BASELINES / f"{name}.diff.png").write_bytes(diff)
            self.fail(
                f"{name} differs from baseline: {ratio:.4%} of pixels "
                f"(budget {MAX_DIFF_PIXEL_RATIO:.1%}); wrote {name}.actual.png"
            )

    def test_prototype_dark_1280(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        image = self.capture(PROTOTYPE.as_uri(), theme="dark", viewport=viewport)
        self.assert_matches_baseline("prototype-dark-1280x800", image, viewport)

    def test_app_shell_three_viewports_in_both_themes(self) -> None:
        for viewport_name, viewport in VIEWPORTS.items():
            for theme in ("dark", "light"):
                name = f"shell-{theme}-{viewport_name}"
                with self.subTest(name=name):
                    image = self.capture(
                        f"{self.base}/ui/", theme=theme, viewport=viewport
                    )
                    self.assert_matches_baseline(name, image, viewport)

    def test_p2_discovery_views_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for view in ("goals", "runs"):
            for theme in ("dark", "light"):
                name = f"{view}-{theme}-1280x800"
                with self.subTest(name=name):
                    image = self.capture(
                        f"{self.base}/ui/#/{view}", theme=theme, viewport=viewport
                    )
                    self.assert_matches_baseline(name, image, viewport)

    def test_runs_phone_cards_in_both_themes(self) -> None:
        """The operator list stays usable without horizontal table panning."""

        viewport = VIEWPORTS["360x800"]
        for theme in ("dark", "light"):
            name = f"runs-{theme}-360x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/#/runs", theme=theme, viewport=viewport,
                    ready_selector=".runs-table tbody tr",
                )
                self.assert_matches_baseline(name, image, viewport)

    def test_p3_workflows_and_wizard_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for theme in ("dark", "light"):
            with self.subTest(view=f"workflows-{theme}"):
                image = self.capture(
                    f"{self.base}/ui/#/workflows", theme=theme, viewport=viewport
                )
                self.assert_matches_baseline(
                    f"workflows-{theme}-1280x800", image, viewport
                )
            with self.subTest(view=f"new-goal-{theme}"):
                image = self.capture(
                    f"{self.base}/ui/", theme=theme, viewport=viewport, wizard=True
                )
                self.assert_matches_baseline(
                    f"new-goal-{theme}-1280x800", image, viewport
                )

    def test_p5_editor_three_viewports_in_both_themes(self) -> None:
        self._set_visual_draft("dirty")
        url = f"{self.base}/ui/#/workflows/workflow:linear/edit/{self.visual_draft_id}"
        for viewport_name, viewport in VIEWPORTS.items():
            for theme in ("dark", "light"):
                name = f"editor-{theme}-{viewport_name}"
                with self.subTest(name=name):
                    image = self.capture(
                        url, theme=theme, viewport=viewport,
                        ready_selector="[data-editor-tab='outline']",
                    )
                    self.assert_matches_baseline(name, image, viewport)

    def _set_visual_draft(self, state: str) -> None:
        source = json.dumps(editable_dsl("linear", "Visual Editor"))
        diagnostics = []
        validated_source_hash = None
        validated_definition_hash = None
        if state == "empty":
            document = editable_dsl("linear", "Empty Editor")
            document["nodes"] = []
            document["edges"] = []
            document["entry"] = []
            document["terminals"] = []
            source = json.dumps(document)
        if state == "invalid":
            diagnostics = [{
                "code": "DSL_GRAPH_CYCLE", "message": "A cycle requires a loop policy.",
                "json_path": "$.edges[0]", "severity": "error",
                "source_range": {"start": {"line": 12, "column": 3}},
            }]
        with connect_workflow_database(self.db) as connection:
            source_hash = connection.execute(
                "SELECT source_hash FROM workflow_drafts WHERE draft_id=?",
                (self.visual_draft_id,),
            ).fetchone()[0]
            if state == "valid":
                validated_source_hash = source_hash
                validated_definition_hash = "sha256:" + "a" * 64
            connection.execute(
                "UPDATE workflow_drafts SET source_text=?, validation_status=?, "
                "validated_source_hash=?, validated_definition_hash=?, diagnostics_json=? "
                "WHERE draft_id=?",
                (
                    source, "valid" if state == "valid" else "invalid" if state == "invalid" else "dirty",
                    validated_source_hash, validated_definition_hash,
                    json.dumps(diagnostics), self.visual_draft_id,
                ),
            )
            connection.commit()

    def test_p5_editor_states_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        url = f"{self.base}/ui/#/workflows/workflow:linear/edit/{self.visual_draft_id}"
        for state in ("empty", "invalid", "valid", "conflict"):
            self._set_visual_draft("dirty" if state == "conflict" else state)
            for theme in ("dark", "light"):
                name = f"editor-{state}-{theme}-1280x800"
                with self.subTest(name=name):
                    image = self.capture(
                        url, theme=theme, viewport=viewport,
                        ready_selector="[data-editor-tab='outline']",
                        editor_conflict=state == "conflict",
                    )
                    self.assert_matches_baseline(name, image, viewport)
        self._set_visual_draft("dirty")

    def test_p6_artifacts_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                image = self.capture(
                    f"{self.base}/ui/#/artifacts", theme=theme, viewport=viewport,
                    ready_selector=".artifact-card-main",
                )
                self.assert_matches_baseline(
                    f"artifacts-{theme}-1280x800", image, viewport
                )

    def test_p7_admin_views_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for view in ("agents", "ops", "settings"):
            for theme in ("dark", "light"):
                with self.subTest(view=view, theme=theme):
                    image = self.capture(
                        f"{self.base}/ui/#/{view}", theme=theme, viewport=viewport,
                    )
                    self.assert_matches_baseline(
                        f"{view}-{theme}-1280x800", image, viewport
                    )

    def test_p9_key_pages_and_states_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        cases = (
            ("inbox", f"{self.base}/ui/#/inbox", None, None),
            (
                "run-detail", f"{self.base}/ui/#/runs/{self.visual_run_id}",
                ".run-hero", None,
            ),
            (
                "error", f"{self.base}/ui/#/home", ".data-state.error",
                "/api/v1/dashboard",
            ),
        )
        for name, url, selector, fail_path in cases:
            for theme in ("dark", "light"):
                with self.subTest(name=name, theme=theme):
                    image = self.capture(
                        url, theme=theme, viewport=viewport,
                        ready_selector=selector, fail_path=fail_path,
                    )
                    self.assert_matches_baseline(
                        f"{name}-{theme}-1280x800", image, viewport
                    )


if __name__ == "__main__":
    unittest.main()
