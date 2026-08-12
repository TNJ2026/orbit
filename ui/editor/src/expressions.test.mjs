import assert from "node:assert/strict";
import { test } from "node:test";

import {
  astToText, availableReferences, conditionEditable, conditionText, conditionValue,
  mappingText, mappingValue, objectText, objectValue,
} from "./expressions.mjs";

test("a reference renders as its path", () => {
  assert.equal(astToText({ op: "ref", path: "source.value" }), "source.value");
});

test("literals render in Python syntax, because ast.parse reads them", () => {
  assert.equal(astToText({ op: "literal", value: true }), "True");
  assert.equal(astToText({ op: "literal", value: false }), "False");
  assert.equal(astToText({ op: "literal", value: null }), "None");
  assert.equal(astToText({ op: "literal", value: 5 }), "5");
  assert.equal(astToText({ op: "literal", value: "ok" }), '"ok"');
});

test("comparisons render with their operator", () => {
  const cases = [
    ["eq", "=="], ["ne", "!="], ["lt", "<"], ["lte", "<="],
    ["gt", ">"], ["gte", ">="], ["in", "in"], ["not_in", "not in"],
  ];
  for (const [op, symbol] of cases) {
    assert.equal(
      astToText({
        op,
        left: { op: "ref", path: "source.v" },
        right: { op: "literal", value: 1 },
      }),
      `source.v ${symbol} 1`,
      op,
    );
  }
});

test("and binds tighter than or, and only the loose side is parenthesised", () => {
  const node = {
    op: "or",
    args: [
      { op: "and", args: [{ op: "ref", path: "a" }, { op: "ref", path: "b" }] },
      { op: "ref", path: "c" },
    ],
  };
  assert.equal(astToText(node), "a and b or c");
});

test("an or inside an and keeps its parentheses", () => {
  const node = {
    op: "and",
    args: [
      { op: "or", args: [{ op: "ref", path: "a" }, { op: "ref", path: "b" }] },
      { op: "ref", path: "c" },
    ],
  };
  assert.equal(astToText(node), "(a or b) and c");
});

test("not parenthesises anything looser than itself", () => {
  assert.equal(
    astToText({
      op: "not",
      arg: { op: "and", args: [{ op: "ref", path: "a" }, { op: "ref", path: "b" }] },
    }),
    "not (a and b)",
  );
  assert.equal(astToText({ op: "not", arg: { op: "ref", path: "a" } }), "not a");
});

test("calls and lists render as written", () => {
  assert.equal(
    astToText({ op: "call", name: "exists", args: [{ op: "ref", path: "source.v" }] }),
    "exists(source.v)",
  );
  assert.equal(
    astToText({
      op: "list",
      items: [{ op: "literal", value: 1 }, { op: "literal", value: "a" }],
    }),
    '[1, "a"]',
  );
});

test("an absent condition edits as empty, not as True", () => {
  // The compiler substitutes "always" for an absent condition; showing True
  // would ask an author to delete something they never wrote.
  assert.equal(conditionText(undefined), "");
  assert.equal(conditionText(null), "");
});

test("each authored form of a condition renders back to itself", () => {
  assert.equal(conditionText(true), "True");
  assert.equal(conditionText(false), "False");
  assert.equal(conditionText("source.value > 5"), "source.value > 5");
  assert.equal(
    conditionText({ op: "gt", left: { op: "ref", path: "source.v" }, right: { op: "literal", value: 5 } }),
    "source.v > 5",
  );
});

test("text that has not moved leaves the original form untouched", () => {
  // Opening an edge and closing it must not rewrite how it was written.
  for (const original of [true, false, "source.v > 1", { op: "literal", value: true }, undefined]) {
    const result = conditionValue(conditionText(original), original);
    assert.equal(result.changed, false, JSON.stringify(original));
    assert.deepEqual(result.value, original);
  }
});

test("clearing a condition removes it", () => {
  assert.deepEqual(conditionValue("   ", "source.v > 1"), { value: undefined, changed: true });
});

test("True and False are stored as booleans, not strings", () => {
  assert.deepEqual(conditionValue("True", "x"), { value: true, changed: true });
  assert.deepEqual(conditionValue("False", "x"), { value: false, changed: true });
});

test("anything else is stored as the string the compiler will parse", () => {
  assert.deepEqual(
    conditionValue("source.value > 5", undefined),
    { value: "source.value > 5", changed: true },
  );
});

test("a condition past the compiler's length limit is refused here", () => {
  assert.match(conditionValue("a".repeat(4097), undefined).problem, /4096/);
});

test("the references offered are the source port and the workflow inputs", () => {
  const document = { inputs: [{ id: "topic" }, { id: "tone" }] };
  assert.deepEqual(
    availableReferences(document, { from: { node: "a", port: "result" } }),
    ["source.result", "workflow.inputs.topic", "workflow.inputs.tone"],
  );
});

test("no edge means nothing to offer", () => {
  assert.deepEqual(availableReferences({ inputs: [{ id: "a" }] }, null), []);
});

test("an absent mapping edits as empty rather than a fabricated object", () => {
  assert.deepEqual(mappingText(undefined), { schemaId: "", value: "" });
  assert.deepEqual(mappingText({}), { schemaId: "", value: "" });
});

test("a mapping round trips through its editable text", () => {
  const mapping = { schema_id: "x://y/1.0", value: { field: "$source.v" } };
  const result = mappingValue(mappingText(mapping), mapping);
  assert.equal(result.changed, false);
  assert.deepEqual(result.value, mapping);
});

test("a mapping needs both halves", () => {
  assert.match(mappingValue({ schemaId: "x://y/1.0", value: "" }, undefined).problem, /value/);
  assert.match(mappingValue({ schemaId: "", value: "{}" }, undefined).problem, /schema_id/);
});

test("clearing both halves removes the mapping", () => {
  const original = { schema_id: "x://y/1.0", value: {} };
  assert.deepEqual(
    mappingValue({ schemaId: "", value: "" }, original),
    { value: undefined, changed: true },
  );
});

test("a mapping value that is not JSON says so", () => {
  assert.match(
    mappingValue({ schemaId: "x://y/1.0", value: "{not json" }, undefined).problem,
    /not JSON/,
  );
});

test("an edited mapping is stored with its parsed value", () => {
  assert.deepEqual(
    mappingValue({ schemaId: "x://y/1.0", value: '{"a": "$source.v"}' }, undefined),
    { value: { schema_id: "x://y/1.0", value: { a: "$source.v" } }, changed: true },
  );
});

test("an object field round trips and rejects non-objects", () => {
  assert.equal(objectText(undefined), "");
  assert.equal(objectText({}), "");
  const config = { model: "opus" };
  assert.equal(objectValue(objectText(config), config).changed, false);
  assert.match(objectValue("[1,2]", undefined).problem, /JSON object/);
  assert.match(objectValue("{oops", undefined).problem, /not JSON/);
  assert.deepEqual(objectValue("", config), { value: undefined, changed: true });
});

test("a negative literal has no text form and says so", () => {
  // `ast.parse("-7")` is a UnaryOp(USub), which the compiler refuses. Showing
  // "-7" would hand the author a field whose contents cannot be saved.
  assert.equal(astToText({ op: "literal", value: -7 }), null);
  assert.equal(astToText({ op: "literal", value: -0.5 }), null);
});

test("a non-finite literal has no text form either", () => {
  assert.equal(astToText({ op: "literal", value: Infinity }), null);
  assert.equal(astToText({ op: "literal", value: NaN }), null);
});

test("one un-renderable leaf makes the whole expression un-renderable", () => {
  const node = {
    op: "and",
    args: [
      { op: "ref", path: "a" },
      { op: "gt", left: { op: "ref", path: "b" }, right: { op: "literal", value: -1 } },
    ],
  };
  assert.equal(astToText(node), null);
  assert.equal(conditionText(node), null);
  assert.equal(conditionEditable(node), false);
});

test("an un-renderable condition refuses to be saved over", () => {
  const node = { op: "literal", value: -7 };
  assert.match(conditionValue("anything", node).problem, /cannot be edited here/);
});

test("an unknown operation is un-renderable rather than silently empty", () => {
  assert.equal(astToText({ op: "regex_match", left: {}, right: {} }), null);
});

test("conditions that do have a text form stay editable", () => {
  for (const node of [
    { op: "literal", value: 0 },
    { op: "literal", value: 7 },
    { op: "literal", value: 1.5 },
    { op: "ref", path: "source.v" },
  ]) {
    assert.equal(conditionEditable(node), true, JSON.stringify(node));
  }
});
