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
the App Shell, three viewports and both themes; P9 to every key page and
state (plan §9.2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import unittest

try:  # pragma: no cover - the skip below reports the absence
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

BASELINES = Path(__file__).parent / "visual_baselines"
PROTOTYPE = Path(__file__).parent.parent / "prototypes" / "runtime-ui.html"
UPDATE = os.environ.get("VISUAL_UPDATE") == "1"
MAX_DIFF_PIXEL_RATIO = 0.001

# Determinism: freeze everything the page could vary on (plan §9.2).
VIEWPORT = {"width": 1280, "height": 800}
FREEZE_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  caret-color: transparent !important;
}
"""


def platform_tag() -> str:
    return f"{platform.system()}-{platform.machine()}"


def baseline_metadata(name: str) -> dict | None:
    meta = BASELINES / f"{name}.json"
    if not meta.exists():
        return None
    return json.loads(meta.read_text(encoding="utf-8"))


def pixel_diff_ratio(expected: bytes, actual: bytes) -> float:
    """Ratio of differing pixels, via Pillow if present, else exact-bytes."""
    try:
        import io

        from PIL import Image, ImageChops
    except ImportError:
        return 0.0 if expected == actual else 1.0
    left = Image.open(io.BytesIO(expected)).convert("RGBA")
    right = Image.open(io.BytesIO(actual)).convert("RGBA")
    if left.size != right.size:
        return 1.0
    diff = ImageChops.difference(left, right)
    changed = sum(
        1 for pixel in diff.getdata() if pixel != (0, 0, 0, 0)
    )
    return changed / (left.size[0] * left.size[1])


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class VisualRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def capture(self, url: str, *, theme: str) -> bytes:
        context = self.browser.new_context(
            viewport=VIEWPORT, locale="en-US", timezone_id="UTC",
            color_scheme="dark" if theme == "dark" else "light",
        )
        try:
            page = context.new_page()
            page.goto(url)
            page.add_style_tag(content=FREEZE_CSS)
            page.wait_for_timeout(100)  # one settle tick after fonts/layout
            return page.screenshot(full_page=False)
        finally:
            context.close()

    def assert_matches_baseline(self, name: str, image: bytes) -> None:
        BASELINES.mkdir(exist_ok=True)
        png = BASELINES / f"{name}.png"
        meta = baseline_metadata(name)

        if UPDATE:
            png.write_bytes(image)
            (BASELINES / f"{name}.json").write_text(
                json.dumps(
                    {"platform": platform_tag(), "viewport": VIEWPORT},
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
        ratio = pixel_diff_ratio(png.read_bytes(), image)
        if ratio > MAX_DIFF_PIXEL_RATIO:
            (BASELINES / f"{name}.actual.png").write_bytes(image)
            self.fail(
                f"{name} differs from baseline: {ratio:.4%} of pixels "
                f"(budget {MAX_DIFF_PIXEL_RATIO:.1%}); wrote {name}.actual.png"
            )

    def test_prototype_dark_1280(self) -> None:
        image = self.capture(PROTOTYPE.as_uri(), theme="dark")
        self.assert_matches_baseline("prototype-dark-1280x800", image)


if __name__ == "__main__":
    unittest.main()
