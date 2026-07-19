"""M4 Gate: the UI carries no runtime knowledge and no monolingual text.

These are static assertions over the shipped assets. They are cheap, they run
without a browser, and they catch the two regressions the plan calls out by
name: a UI that re-implements the state machine, and a UI that quietly becomes
single-language.
"""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import re
import unittest


UI_ROOT = Path(str(resources.files("orbit").joinpath("static/workflow-ui")))
ASSETS = UI_ROOT / "assets"
LOCALES = ("zh-CN", "en-US")


def catalog(locale: str) -> dict[str, str]:
    return json.loads((ASSETS / f"i18n.{locale}.json").read_text(encoding="utf-8"))


def source_files() -> list[Path]:
    return [UI_ROOT / "index.html", *sorted(ASSETS.rglob("*.js"))]


def stylesheet_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(ASSETS.rglob("*.css"))
    )


class CatalogTests(unittest.TestCase):
    def test_catalogs_have_identical_keys(self) -> None:
        zh, en = catalog("zh-CN"), catalog("en-US")
        self.assertEqual(
            set(), set(zh) ^ set(en),
            f"catalog parity broken: {sorted(set(zh) ^ set(en))}",
        )

    def test_no_translation_is_empty(self) -> None:
        for locale in LOCALES:
            for key, value in catalog(locale).items():
                with self.subTest(locale=locale, key=key):
                    self.assertTrue(value.strip(), f"{locale}:{key} is empty")

    def test_placeholders_match_across_locales(self) -> None:
        """A placeholder dropped in one locale silently loses data at runtime."""

        zh, en = catalog("zh-CN"), catalog("en-US")
        pattern = re.compile(r"\{(\w+)\}")
        for key in zh:
            with self.subTest(key=key):
                self.assertEqual(
                    set(pattern.findall(zh[key])), set(pattern.findall(en[key]))
                )

    def test_the_chinese_catalog_is_actually_translated(self) -> None:
        zh, en = catalog("zh-CN"), catalog("en-US")
        shared = [key for key in zh if zh[key] == en[key]]
        # Brand names and a handful of identifiers legitimately match.
        self.assertLessEqual(
            len(shared), 3, f"untranslated zh-CN entries: {sorted(shared)}"
        )

    def test_replaced_ops_and_shell_terms_are_not_kept_as_dead_keys(self) -> None:
        keys = set(catalog("en-US"))
        self.assertTrue({
            "action.newRun", "action.retry", "newRun.workflow.hint",
            "ops.agents", "ops.agents.empty", "ops.handlers", "ops.health",
            "ops.health.notReady", "ops.health.ready",
        }.isdisjoint(keys))


class SourceTests(unittest.TestCase):
    def test_every_key_used_in_source_exists(self) -> None:
        used = set()
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            used |= set(re.findall(r"""i18n\.t\(\s*["']([\w.]+)["']""", text))
            used |= set(re.findall(r'data-i18n(?:-label)?="([\w.]+)"', text))
        known = set(catalog("en-US"))
        # Keys built from a variable (`${titleKey}.empty`) are checked by the
        # dynamic-prefix test below rather than here.
        self.assertEqual(set(), used - known, f"missing catalog keys: {sorted(used - known)}")

    def test_dynamically_built_keys_resolve(self) -> None:
        known = set(catalog("en-US"))
        for key in (
            "run.timeline.empty", "run.errors.empty", "runs.title", "inbox.title",
            "ops.title", "run.title", "human.decision.approve", "human.decision.reject",
            "state.loading", "state.empty", "state.error", "state.stale",
            "state.pending", "state.retry",
        ):
            with self.subTest(key=key):
                self.assertIn(key, known)

    def test_no_hardcoded_user_visible_chinese(self) -> None:
        """The prototype's hardcoded zh aria-labels must not come back."""

        han = re.compile(r"[一-鿿]")
        for path in source_files():
            with self.subTest(path=path.name):
                self.assertIsNone(han.search(path.read_text(encoding="utf-8")))

    def test_the_ui_has_no_runtime_state_machine(self) -> None:
        """No status-to-next-status table, and no invented mutation endpoints."""

        joined = "\n".join(
            path.read_text(encoding="utf-8") for path in ASSETS.rglob("*.js")
        )
        for forbidden in ("succeeded ->", "TRANSITIONS", "nextStatus", "advanceRun"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, joined)

    def test_mutations_only_travel_through_allowed_commands(self) -> None:
        """Every mutation path in the client comes from the server."""

        api_js = (ASSETS / "api.js").read_text(encoding="utf-8")
        literals = set(re.findall(r'request\(\s*"(POST|PUT|PATCH|DELETE)",\s*"([^"]+)"', api_js))
        self.assertEqual(set(), literals)

        app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("/api/v1/human-tasks", app_js)
        self.assertNotIn("/cancel", app_js)
        self.assertIn("allowed.href", api_js)

    def test_new_run_distinguishes_catalog_failure_from_invalid_workflow(self) -> None:
        app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn('announce(i18n.t("newRun.catalog.unavailable")', app_js)
        self.assertIn('fail("newRun.workflow.invalid")', app_js)
        self.assertIn('fail("newRun.workflow.unavailable")', app_js)

    def test_no_mock_data_survives(self) -> None:
        joined = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        for forbidden in ("mock", "Mock", "fixture", "Lorem", "Good morning"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, joined)


class PlanSeparationTests(unittest.TestCase):
    """M4.6: definition, overlay and diff must not be merged in the client."""

    def setUp(self) -> None:
        self.app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.api_js = (ASSETS / "api.js").read_text(encoding="utf-8")

    def test_each_view_has_its_own_renderer(self) -> None:
        for function in ("planDefinitionView", "planOverlayView", "planDiffView"):
            with self.subTest(function=function):
                self.assertIn(f"function {function}", self.app_js)

    def test_each_view_has_its_own_fetch(self) -> None:
        for method in ("planDefinition(", "planOverlay(", "planDiff("):
            with self.subTest(method=method):
                self.assertIn(method, self.api_js)

    def test_the_overlay_always_states_the_version_it_describes(self) -> None:
        """Otherwise a reader cannot tell they are looking at stale state."""

        self.assertIn("plan.overlay.for", self.app_js)
        self.assertIn("overlay.plan_version", self.app_js)

    def test_the_definition_view_renders_no_status(self) -> None:
        start = self.app_js.index("async function planDefinitionView")
        end = self.app_js.index("async function planOverlayView")
        definition_view = self.app_js[start:end]
        for forbidden in ("status", "pill(", "attempts", "generation"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, definition_view)

    def test_the_overlay_view_renders_no_handler_identity(self) -> None:
        start = self.app_js.index("async function planOverlayView")
        end = self.app_js.index("async function planDiffView")
        overlay_view = self.app_js[start:end]
        for forbidden in ("handler_name", "handler_version", "edges", "kind"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, overlay_view)


class ErrorRenderingTests(unittest.TestCase):
    """An error entry is an event; its fields live under `payload.error`.

    Reading them from the top level rendered an empty Errors panel for a run
    that had failed with a perfectly precise message — the worst possible
    failure mode for the one panel an operator opens when something breaks.
    """

    def setUp(self) -> None:
        self.app_js = (ASSETS / "app.js").read_text(encoding="utf-8")

    def test_the_error_renderer_reads_the_event_payload(self) -> None:
        self.assertIn("item.payload && item.payload.error", self.app_js)

    def test_the_error_renderer_surfaces_more_than_a_message(self) -> None:
        start = self.app_js.index("function errorItem")
        section = self.app_js[start:start + 900]
        for field in ("error.message", "error.category", "error.source"):
            with self.subTest(field=field):
                self.assertIn(field, section)


class AccessibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = (UI_ROOT / "index.html").read_text(encoding="utf-8")

    def test_the_page_has_a_skip_link_and_a_live_region(self) -> None:
        self.assertIn('class="skip-link"', self.index)
        self.assertIn('aria-live="polite"', self.index)

    def test_icon_only_controls_carry_labels(self) -> None:
        self.assertIn('data-i18n-label="theme.toggle"', self.index)
        self.assertIn('data-i18n-label="locale.switch"', self.index)

    def test_focus_is_visible(self) -> None:
        css = stylesheet_source()
        self.assertIn(":focus-visible", css)

    def test_text_tokens_meet_wcag_aa_contrast(self) -> None:
        """Body and muted text remain readable on both page and panel surfaces."""

        tokens = (ASSETS / "styles/tokens.css").read_text(encoding="utf-8")

        def block(selector: str) -> str:
            match = re.search(rf"{re.escape(selector)}\s*\{{(.*?)\}}", tokens, re.S)
            self.assertIsNotNone(match, selector)
            return match.group(1)

        def variables(source: str) -> dict[str, str]:
            return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", source))

        def luminance(hex_color: str) -> float:
            channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                channel / 12.92 if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(left: str, right: str) -> float:
            bright, dark = sorted((luminance(left), luminance(right)), reverse=True)
            return (bright + 0.05) / (dark + 0.05)

        dark = variables(block(":root"))
        light = {**dark, **variables(block('html[data-theme="light"]'))}
        for theme, palette in (("dark", dark), ("light", light)):
            for foreground in ("text", "muted"):
                for background in ("bg", "panel"):
                    with self.subTest(theme=theme, foreground=foreground, background=background):
                        self.assertGreaterEqual(
                            contrast(palette[foreground], palette[background]), 4.5
                        )

    def test_the_layout_responds_to_small_screens(self) -> None:
        css = stylesheet_source()
        self.assertIn("@media (max-width", css)

    def test_mobile_navigation_is_a_real_drawer(self) -> None:
        css = stylesheet_source()
        self.assertIn('body[data-nav-open="true"] .sidebar', css)
        self.assertIn('id="navToggle"', self.index)
        self.assertIn('aria-controls="sidebar"', self.index)


class CapacityRenderingTests(unittest.TestCase):
    def test_large_inline_values_are_bounded_before_entering_the_dom(self) -> None:
        app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn("rawValue.length <= 500", app_js)
        self.assertIn("rawValue.slice(0, 500)", app_js)
        self.assertIn("i18n.number(item.size_bytes)", app_js)


if __name__ == "__main__":
    unittest.main()
