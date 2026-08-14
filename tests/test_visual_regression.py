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

import base64
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


# A fixed 16x16 checkerboard: the catalog draws image Artifacts as themselves,
# so the baseline needs a picture whose bytes never change.
CHECKER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAM0lEQVR42mN0av3CAAPL0uFMhq"
    "iZDFjFmRhIBLTXwPjizReC7kYWH4R+YCHG3aPxQHMNAER/FF7xPoSoAAAAAElFTkSuQmCC"
)


def seed_visual_artifact(engine) -> str:
    """One completed run carrying a stable Artifact of each rendered kind.

    Written straight into the engine's own store rather than executed: the
    baseline needs bytes that never change, and a Handler that produced them
    would put a fresh checksum in the page on every capture.
    """

    started = engine.start(
        "workflow:linear", {"value": 7},
        idempotency_key="visual-artifact", actor="local",
    ).run_id
    # The engine mints a random run id, and the page prints it. A baseline
    # cannot be stable while a uuid is on screen, so the seeded run is given
    # a fixed one — the only thing this fixture needs from it is determinism.
    run_id = "langgraph_run:00000000000000000000000000000001"
    # The engine keeps its own clock, so freezing the app's does not reach the
    # timestamps the history row renders as a time and a duration.
    stamp = "2026-01-01T00:00:00Z"
    with engine._connect() as connection:
        # The receipt row references the run, so it moves with it.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE langgraph_run_receipts SET run_id=? WHERE run_id=?",
            (run_id, started),
        )
        connection.execute(
            "UPDATE langgraph_runs SET run_id=?,created_at=?,updated_at=?"
            " WHERE run_id=?",
            (run_id, stamp, stamp, started),
        )
        connection.commit()
    store = engine.artifacts
    with store._connect() as connection:
        for content, content_type, port in (
            (b"# Stable visual artifact\n\nBody.\n", "text/markdown", "report"),
            (CHECKER_PNG, "image/png", "chart"),
        ):
            receipt = store.backend.write(content, max_size_bytes=64 * 1024)
            artifact_id = (
                "artifact:" + receipt.checksum.value.removeprefix("sha256:")
            )
            connection.execute(
                "INSERT OR REPLACE INTO langgraph_artifacts VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id, run_id, "attempt:visual", "work", port,
                    "schema:text", content_type, receipt.size_bytes,
                    receipt.blob_key, "committed", None, "local",
                ),
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
class VisualCaptureCase(unittest.TestCase):
    """Boots one Runtime and screenshots the Goal UI in both themes."""

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "runtime.db"
        cls.artifact_backend = LocalCASBackend(Path(cls.temp.name) / "artifacts")
        source = json.dumps(editable_dsl("linear", "Visual Editor"))
        cls.app = create_app(
            cls.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            authenticator=loopback_authenticator,
            authorizer=local_authorizer(),
            rate_limiter=RateLimiter(requests=1_000),
            artifact_backend=cls.artifact_backend,
            workflow_generator=lambda _prompt: source,
            serve_ui=True,
            single_goal_mode=True,
            langgraph_state_directory=Path(cls.temp.name) / "langgraph",
        )
        publish_linear_workflow(
            cls.db,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        # Keep discovery baselines stable: the Editor fixture is a draft of
        # the existing linear workflow, not a second published catalog entry.
        cls.visual_draft_id = "workflow_draft:visual"
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
        cls.visual_run_id = seed_visual_artifact(cls.app.state.langgraph_service)
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
            # Nothing off this machine. The shell asks Google for a webfont,
            # and offline that request fails after a delay nobody controls —
            # so whether the fallback metrics had settled before the shot was
            # a coin toss, and the same page differed by a few pixels of
            # vertical drift between runs. Refused instantly and identically
            # instead: a suite that freezes everything the page can vary on
            # cannot leave a third-party fetch in the middle of it.
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if "127.0.0.1" in route.request.url
                    or route.request.url.startswith("data:")
                    else route.abort()
                ),
            )
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
                    "**/api/v1/workflow-drafts/*/revise",
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
                # Nothing is captured before permissions have been resolved,
                # or the shot catches a view rendered without its commands.
                page.wait_for_function(
                    "() => document.documentElement.dataset.shell === 'ready'"
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
                    page.fill("#draftRevisionInstruction", "Change this workflow")
                    page.click("#draftRevise")
                    page.wait_for_function(
                        "() => !document.querySelector('#liveRegion').hidden",
                        timeout=15000,
                    )
            # The fonts the page will actually use, rather than a fixed
            # guess at how long they take to fail.
            page.wait_for_function("() => document.fonts.status === 'loaded'")
            page.wait_for_timeout(100)  # one settle tick after layout
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


class SimplifiedVisualRegressionTests(VisualCaptureCase):
    """The simplified Goal UI, which draws its own shell, workspace and run.

    These are separate baselines rather than extra assertions on the full UI's:
    the two modes share a stylesheet but almost no layout, so a regression in
    one is invisible in the other.
    """

    def test_the_workspace_and_catalog_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for theme in ("dark", "light"):
            name = f"simplified-workspace-{theme}-1280x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/", theme=theme, viewport=viewport,
                    ready_selector=".simplified-workspace-composer",
                )
                self.assert_matches_baseline(name, image, viewport)

    def test_the_run_page_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for theme in ("dark", "light"):
            name = f"simplified-run-{theme}-1280x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/#/runs/{self.visual_run_id}",
                    theme=theme, viewport=viewport,
                    ready_selector=".simplified-run-hero",
                )
                self.assert_matches_baseline(name, image, viewport)

    def test_the_history_page_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for theme in ("dark", "light"):
            name = f"simplified-history-{theme}-1280x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/#/goals", theme=theme, viewport=viewport,
                    ready_selector=".history-goal-row",
                )
                self.assert_matches_baseline(name, image, viewport)

    def test_the_workflows_page_in_both_themes(self) -> None:
        viewport = VIEWPORTS["1280x800"]
        for theme in ("dark", "light"):
            name = f"simplified-workflows-{theme}-1280x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/#/workflows", theme=theme, viewport=viewport,
                    ready_selector=".simplified-workflow-generator",
                )
                self.assert_matches_baseline(name, image, viewport)

    def test_the_workspace_on_a_phone(self) -> None:
        """The simplified UI is the one most likely to be opened on a phone."""

        viewport = VIEWPORTS["360x800"]
        for theme in ("dark", "light"):
            name = f"simplified-workspace-{theme}-360x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/", theme=theme, viewport=viewport,
                    ready_selector=".simplified-workspace-composer",
                )
                self.assert_matches_baseline(name, image, viewport)

    def test_the_workflows_page_on_a_phone(self) -> None:
        viewport = VIEWPORTS["360x800"]
        for theme in ("dark", "light"):
            name = f"simplified-workflows-{theme}-360x800"
            with self.subTest(name=name):
                image = self.capture(
                    f"{self.base}/ui/#/workflows", theme=theme, viewport=viewport,
                    ready_selector=".simplified-workflow-generator",
                )
                self.assert_matches_baseline(name, image, viewport)


if __name__ == "__main__":
    unittest.main()
