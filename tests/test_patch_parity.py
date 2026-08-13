"""The two patch appliers, held to the same corpus.

The editor applies operations in the browser and the authoring service applies
them in Python. Two implementations of one semantics is the drift this
codebase has been bitten by once already — the editor's expression renderer
looked right and produced text the compiler refused — and the failure mode is
the same shape here: an operation that means one thing when an author clicks
and another when a model emits it, agreeing on every case anyone happened to
write a test for.

So neither side is tested only against its own expectations. `patch_corpus.json`
is run through both, and the documents that come out have to be equal. A case
added to one applier belongs in the corpus, or it is not covered at all.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from orbit.workflow.dsl.patch import GraphPatch, PatchError, apply_patch


ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).parent / "patch_corpus.json"
APPLIER = ROOT / "ui" / "editor" / "src" / "patch.mjs"
NODE = shutil.which("node")


def _corpus() -> dict:
    return json.loads(CORPUS.read_text())


@unittest.skipUnless(NODE, "node is not installed")
@unittest.skipUnless(APPLIER.is_file(), "editor sources are not in this checkout")
class PatchParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = _corpus()
        script = (
            f"import {{ applyPatch, PatchError }} from {json.dumps(str(APPLIER))};\n"
            "const corpus = JSON.parse(process.argv[1]);\n"
            "const out = { applied: [], failed: [] };\n"
            "for (const item of corpus.cases) {\n"
            "  out.applied.push(applyPatch(corpus.document, item.operations));\n"
            "}\n"
            "for (const item of corpus.failures) {\n"
            "  try {\n"
            "    applyPatch(corpus.document, item.operations);\n"
            "    out.failed.push({ raised: false });\n"
            "  } catch (error) {\n"
            "    out.failed.push({\n"
            "      raised: error instanceof PatchError,\n"
            "      index: error.index,\n"
            "      reason: error.reason,\n"
            "    });\n"
            "  }\n"
            "}\n"
            "console.log(JSON.stringify(out));\n"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, json.dumps(cls.corpus)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(f"the editor applier failed:\n{result.stderr}")
        cls.browser = json.loads(result.stdout)

    def test_every_case_produces_the_same_document_on_both_sides(self) -> None:
        document = self.corpus["document"]
        self.assertEqual(len(self.corpus["cases"]), len(self.browser["applied"]))
        for case, from_browser in zip(self.corpus["cases"], self.browser["applied"]):
            with self.subTest(case=case["name"]):
                patch = GraphPatch.model_validate(
                    {"base_version": 1, "operations": case["operations"]}
                )
                self.assertEqual(apply_patch(document, patch), from_browser)

    def test_every_refusal_is_a_refusal_on_both_sides(self) -> None:
        document = self.corpus["document"]
        self.assertEqual(len(self.corpus["failures"]), len(self.browser["failed"]))
        for case, from_browser in zip(self.corpus["failures"], self.browser["failed"]):
            with self.subTest(case=case["name"]):
                patch = GraphPatch.model_validate(
                    {"base_version": 1, "operations": case["operations"]}
                )
                with self.assertRaises(PatchError) as caught:
                    apply_patch(document, patch)
                self.assertTrue(from_browser["raised"], "the editor accepted it")
                # The same operation, so a model or an author is pointed at
                # the same one whichever side refused.
                self.assertEqual(case["index"], caught.exception.index)
                self.assertEqual(case["index"], from_browser["index"])

    def test_neither_applier_mutates_the_document_it_was_given(self) -> None:
        untouched = _corpus()["document"]
        self.assertEqual(untouched, self.corpus["document"])
        patch = GraphPatch.model_validate({
            "base_version": 1,
            "operations": [{"op": "remove_node", "node_id": "work"}],
        })
        apply_patch(self.corpus["document"], patch)
        self.assertEqual(untouched, self.corpus["document"])

    def test_the_corpus_covers_every_operation_the_contract_admits(self) -> None:
        """A new operation is uncovered until the corpus knows about it."""

        declared = set()
        for annotation in GraphPatch.model_fields["operations"].annotation.__args__:
            for option in getattr(annotation, "__args__", ()):
                literal = getattr(option, "model_fields", {}).get("op")
                if literal is not None:
                    declared.update(literal.annotation.__args__)
        exercised = {
            operation["op"]
            for group in ("cases", "failures")
            for case in self.corpus[group]
            for operation in case["operations"]
        }
        self.assertEqual(set(), declared - exercised, "operations with no case")


if __name__ == "__main__":
    unittest.main()
