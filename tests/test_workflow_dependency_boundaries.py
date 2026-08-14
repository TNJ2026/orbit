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

    def test_the_engine_adapter_owns_its_own_storage(self) -> None:
        """The LangGraph adapter must not reach into the project database.

        It keeps its runs, checkpoints and Artifacts in its own files. An
        import of the project persistence package from here would put two
        owners on one schema, which is exactly the coupling the previous
        engine's removal was able to be clean because it never had.
        """

        root = Path(__file__).parents[1] / "src" / "orbit" / "workflow"
        allowed = {"orbit.workflow.persistence.workflow_versions"}
        violations = []
        for path in (root / "langgraph_runtime").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    resolved = name.replace("..", "orbit.workflow.").replace(
                        "orbit.workflow..", "orbit.workflow."
                    )
                    if "persistence" not in resolved:
                        continue
                    if any(resolved.endswith(item.rsplit(".", 1)[-1]) for item in allowed):
                        continue
                    violations.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual([], violations)

    def test_handlers_do_not_import_runtime_repositories(self) -> None:
        root = Path(__file__).parents[1] / "src" / "orbit" / "workflow"
        forbidden = {
            "sqlite3", "orbit.server", "orbit.store",
            "orbit.workflow.persistence", "orbit.workflow.application",
            "persistence", "application",
        }
        violations = []
        paths = list((root / "handlers").glob("*.py"))
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
