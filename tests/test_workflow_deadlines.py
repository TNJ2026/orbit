"""The four moments of an attempt, for every budget the schema admits.

These are pure functions over a duration, so the interesting cases are the
extremes: the shortest budget that is still legal, the longest one, and the
boundary where a wind-down phase starts being worth announcing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from orbit.workflow.application.durable_runtime_service import (
    _attempt_budget_seconds,
)
from orbit.workflow.domain.deadlines import (
    MIN_AGENT_DURATION_SECONDS, SOFT_DEADLINE_MIN_DURATION_SECONDS,
    UnsafeDeadlineConfiguration, attempt_deadlines, cleanup_reserve_seconds,
    validate_lease_budget,
)


START = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
# The handler budgets actually shipped, plus the schema's own bounds.
SHIPPED_DURATIONS = (60, 300, 600, 900, 1800, 86_400)


def offsets(duration: float, *, kill_grace_seconds: float = 10.0):
    result = attempt_deadlines(
        START, duration, kill_grace_seconds=kill_grace_seconds,
    )
    return result, {
        "node": (result.node_deadline - START).total_seconds(),
        "process": (result.process_deadline - START).total_seconds(),
        "settlement": (result.settlement_deadline - START).total_seconds(),
        "soft": (
            None if result.soft_deadline is None
            else (result.soft_deadline - START).total_seconds()
        ),
    }


class DeadlineOrderingTests(unittest.TestCase):
    def test_every_shipped_budget_yields_ordered_deadlines(self) -> None:
        """Fixed subtraction broke here; a 300s budget minus five minutes is `start`."""

        for duration in SHIPPED_DURATIONS:
            with self.subTest(duration=duration):
                _, at = offsets(duration)
                self.assertGreater(at["process"], 0)
                self.assertLess(at["process"], at["settlement"])
                self.assertLessEqual(at["settlement"], at["node"])
                if at["soft"] is not None:
                    self.assertGreater(at["soft"], 0)
                    self.assertLess(at["soft"], at["process"])

    def test_settlement_is_reserved_out_of_the_budget_not_added_after(self) -> None:
        """A node budgeted 300s must be fully settled within those 300s."""

        for duration in SHIPPED_DURATIONS:
            with self.subTest(duration=duration):
                _, at = offsets(duration)
                self.assertLessEqual(at["settlement"], duration)

    def test_the_reserve_covers_the_kill_grace_and_the_settlement_margin(self) -> None:
        _, at = offsets(1800, kill_grace_seconds=10.0)
        self.assertAlmostEqual(
            cleanup_reserve_seconds(10.0), at["settlement"] - at["process"],
        )

    def test_a_longer_kill_grace_moves_the_process_deadline_earlier(self) -> None:
        """Whatever stopping costs comes out of the working time, not the end."""

        _, patient = offsets(120, kill_grace_seconds=30.0)
        _, brisk = offsets(120, kill_grace_seconds=2.0)
        self.assertLess(patient["process"], brisk["process"])
        self.assertLessEqual(patient["settlement"], 120)
        self.assertLessEqual(brisk["settlement"], 120)


class SoftDeadlineTests(unittest.TestCase):
    def test_a_short_budget_gets_no_soft_deadline(self) -> None:
        """No wind-down phase beats a wind-down phase nobody was told about."""

        result, _ = offsets(SOFT_DEADLINE_MIN_DURATION_SECONDS - 1)
        self.assertIsNone(result.soft_deadline)

    def test_the_threshold_budget_gets_one(self) -> None:
        result, _ = offsets(SOFT_DEADLINE_MIN_DURATION_SECONDS)
        self.assertIsNotNone(result.soft_deadline)

    def test_the_wind_down_window_is_bounded_for_very_long_budgets(self) -> None:
        """The proportion carries short budgets; the constant caps long ones."""

        _, day = offsets(86_400)
        self.assertLessEqual(day["process"] - day["soft"], 300)


class RejectedConfigurationTests(unittest.TestCase):
    def test_a_budget_smaller_than_its_own_cleanup_is_refused(self) -> None:
        with self.assertRaises(UnsafeDeadlineConfiguration) as caught:
            attempt_deadlines(START, 20, kill_grace_seconds=10.0)
        self.assertIn("settle", str(caught.exception))

    def test_a_budget_equal_to_the_reserve_is_refused(self) -> None:
        """Equality leaves zero working time, which is not a budget."""

        with self.assertRaises(UnsafeDeadlineConfiguration):
            attempt_deadlines(
                START, cleanup_reserve_seconds(10.0), kill_grace_seconds=10.0,
            )

    def test_the_minimum_configurable_budget_is_actually_usable(self) -> None:
        """The floor authors are held to must produce a valid schedule."""

        _, at = offsets(MIN_AGENT_DURATION_SECONDS)
        self.assertGreater(at["process"], 0)
        self.assertLessEqual(at["settlement"], MIN_AGENT_DURATION_SECONDS)


class NodeBudgetTests(unittest.TestCase):
    """`timeout_seconds` on the node, which nothing used to read."""

    def profile(self, max_duration_seconds: int):
        return SimpleNamespace(
            resource_profile=SimpleNamespace(
                max_duration_seconds=max_duration_seconds,
            )
        )

    def test_a_node_may_ask_for_less_than_its_handler_allows(self) -> None:
        budget = _attempt_budget_seconds(self.profile(1800), {"timeout_seconds": 600})
        self.assertEqual(600.0, budget)

    def test_a_node_may_not_grant_itself_more(self) -> None:
        """The profile is what the Runtime admitted the handler on."""

        budget = _attempt_budget_seconds(self.profile(300), {"timeout_seconds": 9000})
        self.assertEqual(300.0, budget)

    def test_an_unset_timeout_falls_back_to_the_manifest(self) -> None:
        self.assertEqual(1800.0, _attempt_budget_seconds(self.profile(1800), {}))

    def test_a_nonsense_value_falls_back_rather_than_scheduling_nothing(self) -> None:
        """Schema keeps these out; scheduling must not depend on that holding."""

        for value in (0, -5, "600", True, None):
            with self.subTest(value=value):
                self.assertEqual(
                    900.0,
                    _attempt_budget_seconds(self.profile(900), {"timeout_seconds": value}),
                )


class LeaseBudgetTests(unittest.TestCase):
    """Settlement has to finish inside the lease it is settling."""

    def kwargs(self, **overrides):
        return {
            "lease_ttl_seconds": 120.0, "renew_interval_seconds": 10.0,
            "max_consecutive_renewal_failures": 3, "kill_grace_seconds": 10.0,
            **overrides,
        }

    def test_the_shipped_values_hold(self) -> None:
        validate_lease_budget(**self.kwargs())

    def test_a_lease_too_short_for_detection_and_settlement_is_refused(self) -> None:
        with self.assertRaises(UnsafeDeadlineConfiguration) as caught:
            validate_lease_budget(**self.kwargs(lease_ttl_seconds=30.0))
        self.assertIn("race", str(caught.exception))

    def test_slower_renewal_can_break_an_otherwise_fine_lease(self) -> None:
        """Detection time is the interval times the tolerated failure count."""

        with self.assertRaises(UnsafeDeadlineConfiguration):
            validate_lease_budget(**self.kwargs(renew_interval_seconds=40.0))

    def test_a_longer_kill_grace_can_break_it_too(self) -> None:
        with self.assertRaises(UnsafeDeadlineConfiguration):
            validate_lease_budget(**self.kwargs(kill_grace_seconds=70.0))


class WorkerRefusalTests(unittest.TestCase):
    """The inequality is checked where a worker is built, not just in theory."""

    def worker(self, **overrides):
        from orbit.workflow.worker.runtime import WorkerRuntime

        return WorkerRuntime(
            object(), object(), clock=lambda: START,
            **{"lease_ttl": timedelta(seconds=120), **overrides},
        )

    def test_the_shipped_defaults_build_a_worker(self) -> None:
        self.assertIsNotNone(self.worker())

    def test_a_lease_too_short_to_settle_in_refuses_to_start(self) -> None:
        """Capping this silently would leave the reaper racing the worker."""

        with self.assertRaises(UnsafeDeadlineConfiguration):
            self.worker(lease_ttl=timedelta(seconds=30))

    def test_a_kill_grace_that_outgrows_the_lease_refuses_too(self) -> None:
        with self.assertRaises(UnsafeDeadlineConfiguration):
            self.worker(kill_grace_seconds=80.0)


if __name__ == "__main__":
    unittest.main()
