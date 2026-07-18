from __future__ import annotations

import ast
from pathlib import Path
import unittest


class WorkflowDependencyBoundaryTests(unittest.TestCase):
    def test_domain_and_dsl_do_not_import_runtime_or_infrastructure(self) -> None:
        root = Path(__file__).parents[1] / "src" / "orbit" / "workflow"
        forbidden = {"sqlite3", "starlette", "uvicorn", "orbit.server", "orbit.store"}
        violations = []
        for directory in (root / "domain", root / "dsl"):
            for path in directory.glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [item.name for item in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    for name in names:
                        if any(name == item or name.startswith(item + ".") for item in forbidden):
                            violations.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual([], violations)

    def test_runtime_core_does_not_import_legacy_engine_or_infrastructure(self) -> None:
        root = Path(__file__).parents[1] / "src" / "orbit" / "workflow" / "runtime"
        forbidden = {
            "sqlite3", "starlette", "uvicorn", "orbit.server", "orbit.store",
            "subprocess", "requests", "socket",
        }
        core = {
            "kernel.py", "events.py", "reducers.py", "plan_instantiator.py",
            "snapshot_coordinator.py", "recovery.py",
            "durable_kernel.py", "durable_recovery.py", "work_scheduler.py",
        }
        violations = []
        for path in (root / name for name in core):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if any(name == item or name.startswith(item + ".") for item in forbidden):
                        violations.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual([], violations)

    def test_handlers_and_worker_do_not_import_runtime_repositories(self) -> None:
        root = Path(__file__).parents[1] / "src" / "orbit" / "workflow"
        forbidden = {
            "sqlite3", "orbit.server", "orbit.store",
            "orbit.workflow.persistence", "orbit.workflow.runtime.kernel",
            "orbit.workflow.application", "persistence", "runtime.kernel",
            "application",
        }
        violations = []
        paths = [*(root / "handlers").glob("*.py"), *(root / "worker").glob("*.py")]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import): names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module: names = [node.module]
                for name in names:
                    if any(name == item or name.startswith(item + ".") for item in forbidden):
                        violations.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
