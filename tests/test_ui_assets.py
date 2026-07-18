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
    return [UI_ROOT / "index.html", *sorted(ASSETS.glob("*.js"))]


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
            path.read_text(encoding="utf-8") for path in ASSETS.glob("*.js")
        )
        for forbidden in ("succeeded ->", "TRANSITIONS", "nextStatus", "advanceRun"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, joined)

    def test_mutations_only_travel_through_allowed_commands(self) -> None:
        """Every POST path in the client comes from the server, except start_run.

        `POST /api/v1/runs` is the one command with no prior aggregate to hang
        an allowed_commands entry on, so it is the single permitted literal.
        """

        api_js = (ASSETS / "api.js").read_text(encoding="utf-8")
        literals = set(re.findall(r'request\(\s*"(POST|PUT|PATCH|DELETE)",\s*"([^"]+)"', api_js))
        self.assertEqual({("POST", "/api/v1/runs")}, literals)

        app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("/api/v1/human-tasks", app_js)
        self.assertNotIn("/cancel", app_js)
        self.assertIn("allowed.href", api_js)

    def test_no_mock_data_survives(self) -> None:
        joined = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        for forbidden in ("mock", "Mock", "fixture", "Lorem", "Good morning"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, joined)


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
        css = (ASSETS / "app.css").read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)

    def test_the_layout_responds_to_small_screens(self) -> None:
        css = (ASSETS / "app.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width", css)


if __name__ == "__main__":
    unittest.main()
