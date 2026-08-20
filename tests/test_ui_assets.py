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


EDITOR_SOURCE = Path(__file__).resolve().parents[1] / "ui" / "editor" / "src"


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
        shared = {key for key in zh if zh[key] == en[key]}
        # Brand names, identifiers, and terms deliberately kept in English.
        intentional = {
            "app.title",
            "artifacts.idLabel",
            "artifacts.title",
            "nav.artifacts",
            "run.console.stderr",
            "run.console.stdout",
            "run.data.kind.artifact",
            "shell.breadcrumb.root",
            "wait.none",
        }
        self.assertEqual(
            set(), shared - intentional,
            f"untranslated zh-CN entries: {sorted(shared - intentional)}",
        )

    def test_replaced_ops_and_shell_terms_are_not_kept_as_dead_keys(self) -> None:
        keys = set(catalog("en-US"))
        self.assertTrue({
            "action.newRun", "newRun.workflow.hint",
            "ops.agents", "ops.agents.empty", "ops.handlers", "ops.health",
            "ops.health.notReady", "ops.health.ready",
            "nav.runs", "runs.title", "runs.empty", "runs.orderHint",
        }.isdisjoint(keys))


def translation_calls(text: str):
    """Every literal key inside an `i18n.t(...)` call, wherever it sits.

    Not just the first argument. A key chosen by a conditional —
    `i18n.t(count === 1 ? "history.artifacts.one" : "history.artifacts.many")`
    — is as much a key as a literal one, and matching only the leading
    literal made three of them invisible: two rendered their own key on the
    history list for any goal that produced an Artifact, and the catalog
    check passed the whole time.
    """

    for call in re.finditer(r"i18n\.t\(", text):
        depth, index = 1, call.end()
        while index < len(text) and depth:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        arguments = text[call.end():index - 1]
        # A dotted lowercase literal is a key; `{ count: x }` and message
        # strings are not.
        yield from re.findall(r"""["']([a-z][\w]*(?:\.[\w]+)+)["']""", arguments)


class SourceTests(unittest.TestCase):
    def test_every_key_used_in_source_exists(self) -> None:
        used = set()
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            used |= set(translation_calls(text))
            used |= set(re.findall(r'data-i18n(?:-label)?="([\w.]+)"', text))
        known = set(catalog("en-US"))
        # Keys built from a variable (`${titleKey}.empty`) are checked by the
        # dynamic-prefix test below rather than here.
        self.assertEqual(set(), used - known, f"missing catalog keys: {sorted(used - known)}")

    def test_dynamically_built_keys_resolve(self) -> None:
        known = set(catalog("en-US"))
        for key in (
            "human.decision.approve", "human.decision.reject",
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

        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        self.assertNotIn("/api/v1/human-tasks", app_js)
        self.assertNotIn("/cancel", app_js)
        self.assertIn("allowed.href", api_js)

class AccessibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = (UI_ROOT / "index.html").read_text(encoding="utf-8")

    def test_the_page_has_a_skip_link_and_a_live_region(self) -> None:
        self.assertIn('class="skip-link"', self.index)
        self.assertIn('aria-live="polite"', self.index)

    def test_icon_only_controls_carry_labels(self) -> None:
        self.assertIn('data-i18n-label="action.more"', self.index)
        self.assertIn('data-i18n-label="theme.light"', self.index)
        self.assertIn('data-i18n-label="theme.dark"', self.index)
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

    def test_goal_composer_has_designed_structure_and_shortcut(self) -> None:
        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        css = stylesheet_source()
        for marker in (
            "simplified-goal-page", "simplifiedGoalTitle",
            "simplified.start.description", "simplified.start.shortcut",
        ):
            self.assertIn(marker, app_js)
        self.assertIn("event.currentTarget.form?.requestSubmit()", app_js)
        self.assertRegex(css, r"\.simplified-goal-field textarea\s*\{[^}]*min-height:\s*200px")
        self.assertIn(".simplified-workflow-picker { grid-template-columns: 1fr; }", css)

    def test_workflow_generation_progress_offers_server_authorized_cancel(self) -> None:
        generation_js = (
            ASSETS / "workflow" / "generation-progress.js"
        ).read_text(encoding="utf-8")
        self.assertIn('class: "button workflow-generation-cancel"', generation_js)
        self.assertIn(
            '(item) => item.command === "workflow.authoring.cancel"', generation_js
        )
        self.assertIn(
            '`workflow.authoring.cancel:${job.job_id}`', generation_js
        )

    def messages(self, text: str) -> set[str]:
        return set(re.findall(r'"(orbit-viewer-[a-z-]+)"', text))

    def test_both_ends_name_the_same_messages(self) -> None:
        page = (ASSETS / "workflow" / "definition-views.js").read_text(
            encoding="utf-8"
        )
        canvas = (EDITOR_SOURCE / "catalog-graph.mjs").read_text(encoding="utf-8")
        self.assertEqual(
            {"orbit-viewer-ready", "orbit-viewer-graph", "orbit-viewer-node-click"},
            self.messages(page),
        )
        self.assertEqual(self.messages(page), self.messages(canvas))

    def test_the_canvas_has_no_editor_mode(self) -> None:
        """The embedded bundle always renders the read-only viewer."""

        page = (ASSETS / "workflow" / "definition-views.js").read_text(
            encoding="utf-8"
        )
        main = (EDITOR_SOURCE / "main.jsx").read_text(encoding="utf-8")
        self.assertNotIn("?readonly=1", page)
        self.assertIn("<Viewer />", main)
        self.assertNotIn("<App />", main)


class HandlerConsoleRenderingTests(unittest.TestCase):
    def test_the_console_follows_only_while_the_run_is_alive(self) -> None:
        """Polling a finished run's console for ever is a busy loop for nothing."""

        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        self.assertIn("else if (live) timer = setTimeout(poll, 2000);", app_js)
        self.assertIn("activeViewCleanup = () => {", app_js)

    def test_the_console_loads_only_when_it_is_opened(self) -> None:
        """An Agent's output is the largest thing on the page.

        Fetching it on every visit to a run detail spends the request on
        something most visits never look at.
        """

        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        self.assertIn('details.addEventListener("toggle"', app_js)
        self.assertIn("if (stopped || loading || !details.open) return;", app_js)

    def test_the_console_reads_the_engine_route(self) -> None:
        api_js = (ASSETS / "api.js").read_text(encoding="utf-8")
        self.assertIn(
            "/api/v1/langgraph-runs/${encodeURIComponent(runId)}/output", api_js,
        )


class SelfContainedAssetTests(unittest.TestCase):
    def test_the_ui_loads_nothing_from_a_third_party(self) -> None:
        """A Runtime that binds to loopback should not phone anywhere.

        It used to fetch its typefaces from a font CDN on every page load,
        which made an offline machine fall back anyway, gave a proxied one a
        pause first, and told a third party when this tool was opened.
        """

        page = (ASSETS.parent / "index.html").read_text(encoding="utf-8")
        css = stylesheet_source()
        for source in (page, css):
            for host in ("//fonts.googleapis.com", "//fonts.gstatic.com", "http://", "https://"):
                self.assertNotIn(host, source)

    def test_type_is_asked_of_the_platform(self) -> None:
        css = stylesheet_source()
        self.assertIn("--font: ui-sans-serif, system-ui", css)
        self.assertIn("--mono: ui-monospace", css)
        # The display token survives the face it used to name, so a heading
        # still says it is one and can be given a face again in one place.
        self.assertIn("--font-display: var(--font)", css)


class StepListRenderingTests(unittest.TestCase):
    def test_the_steps_still_to_come_are_drawn_too(self) -> None:
        """A list that grew as the run progressed would hide how much is left.

        The rows come from the definition, so the shape of the run is legible
        before it has done anything.
        """

        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        self.assertIn("api.runSteps(runId)", app_js)
        self.assertIn("not_reached", app_js)

    def test_a_repeated_step_is_one_row_that_says_so(self) -> None:
        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        self.assertIn('i18n.t("simplified.steps.repeated"', app_js)

    def test_every_branch_state_the_server_can_send_has_a_word(self) -> None:
        """The status is a template hole, so a missing word is a raw key.

        The list is the engine's own: `edges()` documents six answers, and a
        seventh added there without a string here would render as
        `simplified.branches.status.whatever` on the page.
        """

        english = json.loads(
            (ASSETS / "i18n.en-US.json").read_text(encoding="utf-8")
        )
        chinese = json.loads(
            (ASSETS / "i18n.zh-CN.json").read_text(encoding="utf-8")
        )
        from orbit.workflow.langgraph_runtime.service import EDGE_STATUSES

        self.assertGreaterEqual(len(EDGE_STATUSES), 6)
        for status in EDGE_STATUSES:
            with self.subTest(status=status):
                self.assertIn(f"simplified.branches.status.{status}", english)
                self.assertIn(f"simplified.branches.status.{status}", chinese)

    def test_only_forks_are_reported_so_the_footnote_stays_one(self) -> None:
        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        self.assertIn("api.runEdges(runId)", app_js)
        self.assertIn("items.length > 1", app_js)

    def test_every_step_state_has_a_word_and_a_colour(self) -> None:
        """Meaning never rides on colour alone, the run pills' own rule."""

        app_js = "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        css = stylesheet_source()
        english = json.loads(
            (ASSETS / "i18n.en-US.json").read_text(encoding="utf-8")
        )
        chinese = json.loads(
            (ASSETS / "i18n.zh-CN.json").read_text(encoding="utf-8")
        )
        for status in (
            "succeeded", "failed", "unknown", "cancelled", "running", "waiting",
            "answered", "not_reached",
        ):
            with self.subTest(status=status):
                self.assertIn(f"simplified.steps.status.{status}", english)
                self.assertIn(f"simplified.steps.status.{status}", chinese)
                self.assertIn(f"{status}:", app_js.split("STEP_MARKS")[1][:200])
        self.assertIn(".step-row.succeeded .step-mark", css)
        self.assertIn(".step-row.failed .step-mark", css)
        self.assertIn(".step-row.unknown .step-mark", css)

        node = (EDITOR_SOURCE / "WorkflowNode.jsx").read_text(encoding="utf-8")
        editor_css = (EDITOR_SOURCE / "app.css").read_text(encoding="utf-8")
        self.assertIn('unknown: "outcome unknown"', node)
        self.assertIn(".node-run-unknown", editor_css)
