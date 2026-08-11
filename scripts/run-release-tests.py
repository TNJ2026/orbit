#!/usr/bin/env python3
"""Run the deterministic release gate, leaving visual/browser suites separate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


EXCLUDED_MODULES = frozenset({"test_visual_regression", "test_browser_e2e"})
ROOT = Path(__file__).resolve().parents[1]


def iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def main() -> int:
    # The suite contains both `tests.test_x` and legacy `test_x` imports.
    # A script launched from scripts/ has neither location on sys.path by
    # default, so make the same two roots available as unittest discovery.
    for path in (ROOT, ROOT / "tests"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    discovered = unittest.defaultTestLoader.discover("tests")
    selected = unittest.TestSuite(
        test for test in iter_tests(discovered)
        if not EXCLUDED_MODULES.intersection(test.id().split("."))
    )
    result = unittest.TextTestRunner(verbosity=1).run(selected)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
