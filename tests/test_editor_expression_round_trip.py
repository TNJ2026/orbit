"""The editor's expression renderer, checked against the real compiler.

`ui/editor/src/expressions.mjs` renders a compiled condition AST back to the
source text it came from, so an author who opens an edge authored as an AST
sees an expression they can edit. That renderer is a second implementation of
a grammar this package owns, and the failure it invites is silent: text that
looks right, parses, and means something else — a dropped parenthesis turning
`(a or b) and c` into `a or (b and c)`, or a JavaScript `true` where
`ast.parse` needs `True`.

So the check is a round trip through both sides: render each AST with node,
compile the text with `compile_condition`, and require the AST that comes back
to be the one that went in. Unit tests on either side alone cannot see a
disagreement between them.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from orbit.workflow.dsl.diagnostics import DiagnosticError
from orbit.workflow.dsl.expressions import compile_condition


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "ui" / "editor" / "src" / "expressions.mjs"
NODE = shutil.which("node")


def ref(path: str) -> dict:
    return {"op": "ref", "path": path}


def literal(value) -> dict:
    return {"op": "literal", "value": value}


CASES: tuple[dict, ...] = (
    literal(True),
    literal(False),
    literal(None),
    literal(42),
    literal(0),
    literal(1.5),
    literal("a string"),
    literal('quotes "inside"'),
    ref("source.value"),
    ref("workflow.inputs.topic"),
    {"op": "eq", "left": ref("source.v"), "right": literal(1)},
    {"op": "ne", "left": ref("source.v"), "right": literal("x")},
    {"op": "lt", "left": ref("source.v"), "right": literal(0)},
    {"op": "lte", "left": ref("source.v"), "right": literal(0)},
    {"op": "gt", "left": ref("source.v"), "right": literal(5)},
    {"op": "gte", "left": ref("source.v"), "right": literal(5)},
    {"op": "in", "left": ref("source.v"), "right": {
        "op": "list", "items": [literal("a"), literal("b")],
    }},
    {"op": "not_in", "left": ref("source.v"), "right": {
        "op": "list", "items": [literal(1), literal(2)],
    }},
    {"op": "not", "arg": ref("source.flag")},
    {"op": "and", "args": [ref("a"), ref("b")]},
    {"op": "or", "args": [ref("a"), ref("b")]},
    {"op": "and", "args": [ref("a"), ref("b"), ref("c")]},
    # Precedence: the parenthesised group must survive the round trip. Without
    # the parentheses this reads as `a or (b and c)`, a different condition.
    {"op": "and", "args": [{"op": "or", "args": [ref("a"), ref("b")]}, ref("c")]},
    {"op": "or", "args": [{"op": "and", "args": [ref("a"), ref("b")]}, ref("c")]},
    {"op": "not", "arg": {"op": "and", "args": [ref("a"), ref("b")]}},
    {"op": "not", "arg": {"op": "or", "args": [ref("a"), ref("b")]}},
    {"op": "and", "args": [
        {"op": "not", "arg": ref("a")},
        {"op": "gt", "left": ref("source.v"), "right": literal(3)},
    ]},
    {"op": "call", "name": "exists", "args": [ref("source.v")]},
    {"op": "call", "name": "length", "args": [ref("source.items")]},
    {"op": "gt", "left": {
        "op": "call", "name": "length", "args": [ref("source.items")],
    }, "right": literal(0)},
    {"op": "list", "items": []},
    {"op": "list", "items": [literal(1), literal("a"), literal(True)]},
    {"op": "or", "args": [
        {"op": "and", "args": [
            {"op": "eq", "left": ref("source.kind"), "right": literal("urgent")},
            {"op": "gt", "left": ref("source.score"), "right": literal(0.8)},
        ]},
        {"op": "not", "arg": {"op": "call", "name": "exists", "args": [ref("source.owner")]}},
    ]},
)


@unittest.skipUnless(NODE, "node is not installed")
@unittest.skipUnless(RENDERER.is_file(), "editor sources are not in this checkout")
class ExpressionRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        script = (
            f"import {{ astToText }} from {json.dumps(str(RENDERER))};\n"
            "const cases = JSON.parse(process.argv[1]);\n"
            "console.log(JSON.stringify(cases.map(astToText)));\n"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, json.dumps(list(CASES))],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(f"renderer failed:\n{result.stderr}")
        cls.rendered = json.loads(result.stdout)

    def test_every_rendered_expression_compiles_back_to_its_ast(self) -> None:
        self.assertEqual(len(CASES), len(self.rendered))
        for original, text in zip(CASES, self.rendered):
            with self.subTest(text=text):
                self.assertTrue(text, f"rendered nothing for {original}")
                self.assertEqual(original, compile_condition(text, ()))

    def test_the_grouped_expression_really_needed_its_parentheses(self) -> None:
        """Proves the precedence case above is not passing by accident."""

        grouped = {"op": "and", "args": [
            {"op": "or", "args": [ref("a"), ref("b")]}, ref("c"),
        ]}
        self.assertIn("(a or b) and c", self.rendered)
        self.assertNotEqual(grouped, compile_condition("a or b and c", ()))

    def test_an_expression_with_no_text_form_renders_as_nothing(self) -> None:
        """Some ASTs cannot be written as text at all, and must not pretend to.

        A negative number is the plain case: `ast.parse("-7")` is a
        UnaryOp(USub) and `_compile` admits only Constant, so the compiler
        refuses `-7` outright. Such an AST is reachable through the structured
        form, and the renderer has to say it has no text form rather than
        produce one the compiler will reject — the editor then shows it
        read-only instead of offering a field that cannot be saved.
        """

        unrenderable = [
            literal(-7),
            literal(-0.5),
            {"op": "gt", "left": ref("a"), "right": literal(-1)},
            {"op": "and", "args": [ref("a"), literal(-2)]},
        ]
        script = (
            f"import {{ astToText }} from {json.dumps(str(RENDERER))};\n"
            "const cases = JSON.parse(process.argv[1]);\n"
            "console.log(JSON.stringify(cases.map(astToText)));\n"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, json.dumps(unrenderable)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([None] * len(unrenderable), json.loads(result.stdout))

        # And the reason it must: the obvious rendering does not compile.
        with self.assertRaises(DiagnosticError):
            compile_condition("-7", ())

    def test_python_literals_are_rendered_not_javascript_ones(self) -> None:
        """`true` would parse as a name lookup and mean something else."""

        for javascript in ("true", "false", "null"):
            self.assertNotIn(javascript, self.rendered)
        for python in ("True", "False", "None"):
            self.assertIn(python, self.rendered)


if __name__ == "__main__":
    unittest.main()
