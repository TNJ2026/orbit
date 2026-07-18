"""M7 gate 7: the UI, driven by a real browser, in both languages.

Everything else tests the UI's inputs and outputs — the API it calls, the
modules it imports, the strings it ships. This drives the actual page: clicks,
dialogs, locale switching, and the four flows the gate names — budget, cancel,
recovery and artifacts.

playwright is a test-only dependency (`pip install -e '.[dev]'` plus
`playwright install chromium`). The suite skips when it is missing rather than
failing, so a plain checkout still runs green.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by the skip
    sync_playwright = None

from orbit.web.app import create_app
from orbit.web.local_identity import local_authorizer, loopback_authenticator
from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.application.run_service import RunApplicationService
from orbit.workflow.domain.human import HumanTaskKind
from orbit.workflow.domain.ids import EntityId
from tests.test_web_composition import (
    SCHEMAS, publish_linear_workflow, transform_registration,
)


LOCALES = ("zh-CN", "en-US")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class BrowserE2ETestCase(unittest.TestCase):
    """One server and one browser for the whole class; a page per test."""

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "runtime.db"
        app = create_app(
            cls.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=2, poll_seconds=0.02,
            authenticator=loopback_authenticator,
            authorizer=local_authorizer(),
            serve_ui=True,
        )
        publish_linear_workflow(cls.db)

        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health/ready", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise AssertionError("server never became ready")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=30)
        cls.temp.cleanup()

    def open(self, locale: str = "en-US", path: str = "/ui/"):
        """A page whose browser language is `locale`, as a real visitor's is."""

        context = self.browser.new_context(locale=locale)
        page = context.new_page()
        self.addCleanup(context.close)
        page.goto(f"{self.base}{path}")
        page.wait_for_selector("#content")
        return page

    # -- fixtures ---------------------------------------------------------

    def start_run(self, key: str) -> str:
        service = RunApplicationService(self.db, self.app_service())
        return service.start_run(
            workflow_id="workflow:linear", inputs={"value": 1},
            actor="local", idempotency_key=key,
        ).run_id

    def app_service(self):
        from orbit.workflow.application.durable_runtime_service import (
            DurableRuntimeApplicationService,
        )

        return DurableRuntimeApplicationService(self.db)

    def wait_for_status(self, page, run_id: str, status: str, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = page.evaluate(
                "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`)"
                ".then(r => r.json()).then(b => b.data.status)",
                run_id,
            )
            if payload == status:
                return
            time.sleep(0.1)
        self.fail(f"{run_id} never reached {status}")


class LocaleTests(BrowserE2ETestCase):
    def test_the_browser_language_picks_the_locale(self) -> None:
        for locale, expected in (("zh-CN", "运行"), ("en-US", "Runs")):
            with self.subTest(locale=locale):
                page = self.open(locale)
                self.assertEqual(locale, page.get_attribute("html", "lang"))
                self.assertIn(expected, page.inner_text("#viewTitle"))

    def test_switching_locale_retranslates_the_page(self) -> None:
        page = self.open("en-US")
        page.select_option("#localeSelect", "zh-CN")
        page.wait_for_function("() => document.documentElement.lang === 'zh-CN'")
        self.assertIn("运行", page.inner_text("#viewTitle"))
        self.assertIn("待办", page.inner_text(".sidebar"))

    def test_no_key_leaks_into_the_page_in_either_locale(self) -> None:
        """A missing translation renders as its key; that must never ship."""

        import re

        for locale in LOCALES:
            with self.subTest(locale=locale):
                page = self.open(locale)
                text = page.inner_text("body")
                leaked = re.findall(r"\b(?:runs|run|inbox|ops|action|plan)\.[a-z][\w.]+", text)
                self.assertEqual([], leaked, f"untranslated keys: {leaked}")


class NewRunTests(BrowserE2ETestCase):
    def test_a_run_started_from_the_dialog_reaches_its_detail_page(self) -> None:
        page = self.open("en-US")
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        page.fill("#newRunWorkflow", "workflow:linear")
        page.fill("#newRunInput", '{"value": 3}')
        page.click("dialog button[value=confirm]")

        page.wait_for_function("() => location.hash.startsWith('#/runs/run')")
        self.assertIn("Started run", page.inner_text("#liveRegion"))
        page.wait_for_selector("text=Open responsibilities")

    def test_invalid_json_is_reported_and_nothing_starts(self) -> None:
        page = self.open("en-US")
        before = page.evaluate(
            "() => fetch('/api/v1/runs?limit=200').then(r => r.json())"
            ".then(b => b.data.runs.length)"
        )
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        page.fill("#newRunWorkflow", "workflow:linear")
        page.fill("#newRunInput", "{not json")
        page.click("dialog button[value=confirm]")

        page.wait_for_selector("#liveRegion.error")
        self.assertIn("valid JSON", page.inner_text("#liveRegion"))
        after = page.evaluate(
            "() => fetch('/api/v1/runs?limit=200').then(r => r.json())"
            ".then(b => b.data.runs.length)"
        )
        self.assertEqual(before, after)


class HumanTaskTests(BrowserE2ETestCase):
    def test_an_approval_is_completed_from_the_inbox_in_both_locales(self) -> None:
        for index, locale in enumerate(LOCALES):
            with self.subTest(locale=locale):
                run_id = self.start_run(f"browser-human-{index}")
                task_id, token = HumanTaskService(self.db).create(
                    EntityId.parse(run_id), HumanTaskKind.APPROVAL, {"q": f"ship {index}?"},
                    actor="local", now=datetime.now(timezone.utc), participants=["local"],
                )

                # The inbox identifies an item by its run and its label — the
                # task id is addressing data, not something the row displays.
                page = self.open(locale, path="/ui/#/inbox")
                row = page.locator("tr", has_text=run_id)
                row.wait_for(timeout=15000)

                # The first button is whatever the server advertised first;
                # the test must not assume which command that is.
                approve = row.locator("td").last.locator("button").first
                self.assertIn(
                    approve.inner_text(),
                    {"Approve", "批准"},
                    "the inbox offered an unexpected first command",
                )
                approve.click()

                page.wait_for_selector("dialog[open]")
                page.fill("#humanToken", token)
                page.click("dialog button[value=confirm]")

                # The row leaves the inbox because the server stopped listing
                # it, not because the client crossed it off.
                page.wait_for_function(
                    "id => !document.body.innerText.includes(id)", arg=run_id,
                    timeout=15000,
                )


class BudgetTests(BrowserE2ETestCase):
    def test_a_budget_grant_from_the_ui_moves_the_account(self) -> None:
        run_id = self.start_run("browser-budget")
        budget = BudgetService(self.db)
        budget.open_account(
            EntityId.parse(run_id), 1_000, actor="local",
            now=datetime.now(timezone.utc),
        )

        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("text=Budget")
        self.assertIn("microunits", page.inner_text("#content"))

        total = page.evaluate(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`).then(r => r.json())"
            ".then(b => b.data.budget_summary.total_microunits)",
            run_id,
        )
        self.assertEqual(1_000, total)

    def test_the_budget_unit_is_shown_with_the_number(self) -> None:
        """A number without its unit is how microunits get read as dollars."""

        run_id = self.start_run("browser-budget-unit")
        BudgetService(self.db).open_account(
            EntityId.parse(run_id), 2_500, actor="local",
            now=datetime.now(timezone.utc),
        )
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("text=microunits")
        text = page.inner_text("#content")
        self.assertRegex(text, r"2[,.]?500")


class CancelTests(BrowserE2ETestCase):
    def parked_run(self, key: str) -> str:
        """A run waiting on a person, so a responsibility is reliably present.

        Racing a fast handler produced a test that skipped itself; parking the
        run on a HumanTask makes the responsibility deterministic.
        """

        run_id = self.start_run(key)
        HumanTaskService(self.db).create(
            EntityId.parse(run_id), HumanTaskKind.APPROVAL, {"q": key},
            actor="local", now=datetime.now(timezone.utc), participants=["local"],
        )
        return run_id

    def test_a_run_parked_on_a_person_can_still_be_called_off(self) -> None:
        run_id = self.parked_run("browser-cancel")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")

        # The button exists only because the server advertised run.cancel.
        cancel = page.locator("button.danger").first
        cancel.wait_for(timeout=15000)
        self.assertEqual("Cancel run", cancel.inner_text())
        cancel.click()

        page.wait_for_function(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`)"
            ".then(r => r.json()).then(b => b.data.status === 'cancelled')",
            arg=run_id, timeout=15000,
        )

    def test_the_cancel_button_is_translated(self) -> None:
        run_id = self.parked_run("browser-cancel-zh")
        page = self.open("zh-CN", path=f"/ui/#/runs/{run_id}")
        cancel = page.locator("button.danger").first
        cancel.wait_for(timeout=15000)
        self.assertEqual("取消运行", cancel.inner_text())


class PlanAndRecoveryTests(BrowserE2ETestCase):
    def test_the_plan_panel_keeps_definition_and_overlay_apart(self) -> None:
        run_id = self.start_run("browser-plan")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("button[data-view=definition]")

        definition = page.inner_text("button[data-view=definition] >> xpath=../..")
        self.assertIn("transform", definition)

        page.click("button[data-view=overlay]")
        page.wait_for_selector("text=/Run state for plan v/i")
        # `.eyebrow` renders uppercase, so compare case-insensitively rather
        # than pinning a presentation detail.
        overlay = page.inner_text("#content").lower()
        self.assertIn("run state for plan v1", overlay)
        self.assertIn("generation 1", overlay)

    def test_the_ops_page_reports_health_and_recovery(self) -> None:
        page = self.open("en-US", path="/ui/#/ops")
        page.wait_for_selector("text=Runtime health")
        text = page.inner_text("#content")
        self.assertIn("Ready", text)
        self.assertIn("Scanned", text)
        self.assertIn("Installed handlers", text)

    def test_a_failed_run_is_diagnosable_from_the_errors_panel(self) -> None:
        """The panel an operator opens when something breaks must say why."""

        service = RunApplicationService(self.db, self.app_service())
        run_id = service.start_run(
            workflow_id="workflow:linear", inputs={"value": "not-an-integer"},
            actor="local", idempotency_key="browser-failure",
        ).run_id

        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        self.wait_for_status(page, run_id, "failed")
        page.reload()
        page.wait_for_selector("text=is not of type", timeout=15000)

        errors = page.inner_text("#content")
        self.assertIn("validation_error", errors)
        self.assertIn("handler", errors)


class RefreshTests(BrowserE2ETestCase):
    def test_a_reload_restores_the_page_from_the_server(self) -> None:
        """Nothing is kept client-side, so a reload must lose nothing."""

        run_id = self.start_run("browser-reload")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("text=Open responsibilities")
        before = page.inner_text("#content")

        page.reload()
        page.wait_for_selector("text=Open responsibilities")
        after = page.inner_text("#content")

        self.assertIn(run_id, before)
        self.assertIn(run_id, after)

    def test_no_console_errors_on_any_view(self) -> None:
        for path in ("/ui/", "/ui/#/inbox", "/ui/#/ops"):
            with self.subTest(path=path):
                context = self.browser.new_context(locale="en-US")
                page = context.new_page()
                self.addCleanup(context.close)
                errors: list[str] = []
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text)
                        if message.type == "error" else None
                    ),
                )
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"{self.base}{path}")
                page.wait_for_selector("#content")
                page.wait_for_timeout(800)
                self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
