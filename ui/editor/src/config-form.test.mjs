import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applyField, describeFields, emptied, fieldValue, isClosed, unknownKeys,
} from "./config-form.mjs";

// The schema every discovered Agent Handler actually carries.
const AGENT = {
  type: "object",
  properties: {
    prompt: { type: "string" },
    timeout_seconds: {
      type: "integer", minimum: 1, maximum: 1800,
      description: "How long this step may run.",
    },
  },
  additionalProperties: false,
};

const field = (schema, name) =>
  describeFields(schema).find((item) => item.name === name);

test("the Agent schema describes the two fields it declares", () => {
  const fields = describeFields(AGENT);
  assert.deepEqual(fields.map((item) => item.name), ["prompt", "timeout_seconds"]);
});

test("a long string gets room, a bounded one gets a line", () => {
  assert.equal(field(AGENT, "prompt").control, "textarea");
  assert.equal(
    field({ properties: { name: { type: "string", maxLength: 40 } } }, "name").control,
    "text",
  );
});

test("an integer carries its bounds and the prose beside it", () => {
  const timeout = field(AGENT, "timeout_seconds");
  assert.equal(timeout.control, "number");
  assert.equal(timeout.integer, true);
  assert.equal(timeout.minimum, 1);
  assert.equal(timeout.maximum, 1800);
  assert.match(timeout.description, /How long this step may run/);
});

test("a boolean is a checkbox and an enum is a choice", () => {
  const schema = {
    properties: {
      streaming: { type: "boolean" },
      mode: { type: "string", enum: ["fast", "thorough"] },
    },
  };
  assert.equal(field(schema, "streaming").control, "checkbox");
  assert.deepEqual(field(schema, "mode").choices, ["fast", "thorough"]);
});

test("required is carried through", () => {
  const schema = { properties: { a: { type: "string" } }, required: ["a"] };
  assert.equal(field(schema, "a").required, true);
  assert.equal(field(AGENT, "prompt").required, false);
});

test("a schema with no properties drives no form", () => {
  // `transform` is exactly this: it takes whatever it likes, and a form with
  // no fields would tell the author the opposite.
  assert.equal(describeFields({ type: "object" }), null);
  assert.equal(describeFields({}), null);
  assert.equal(describeFields(undefined), null);
  assert.equal(describeFields({ type: "object", properties: {} }), null);
});

test("one property that cannot be drawn refuses the whole form", () => {
  // An author would otherwise edit around a field they cannot see.
  const schema = {
    properties: {
      prompt: { type: "string" },
      retries: { type: "array", items: { type: "string" } },
    },
  };
  assert.equal(describeFields(schema), null);
});

test("a closed schema is one a form can hold entirely", () => {
  assert.equal(isClosed(AGENT), true);
  assert.equal(isClosed({ type: "object" }), false);
});

test("keys the schema does not describe are reported, never dropped", () => {
  assert.deepEqual(
    unknownKeys({ prompt: "hi", model: "opus", seed: 1 }, AGENT),
    ["model", "seed"],
  );
  assert.deepEqual(unknownKeys({ prompt: "hi" }, AGENT), []);
  assert.deepEqual(unknownKeys(undefined, AGENT), []);
});

test("a field shows what is set, and the schema's default when it is not", () => {
  const schema = { properties: { mode: { type: "string", default: "fast" } } };
  const mode = field(schema, "mode");
  assert.equal(fieldValue({ mode: "thorough" }, mode), "thorough");
  assert.equal(fieldValue({}, mode), "fast");
  assert.equal(fieldValue(undefined, mode), "fast");
});

test("a string is stored as typed", () => {
  const result = applyField({}, field(AGENT, "prompt"), "summarise it");
  assert.deepEqual(result.value, { prompt: "summarise it" });
});

test("emptying a field removes the key rather than blanking it", () => {
  // Absent and blank are different to a Handler, and only the absent one
  // takes the schema's default.
  const result = applyField({ prompt: "x" }, field(AGENT, "prompt"), "");
  assert.deepEqual(result.value, {});
});

test("a number is stored as a number, not as its text", () => {
  const result = applyField({}, field(AGENT, "timeout_seconds"), "600");
  assert.deepEqual(result.value, { timeout_seconds: 600 });
});

test("the schema's own bounds are enforced before the Handler sees them", () => {
  const timeout = field(AGENT, "timeout_seconds");
  assert.match(applyField({}, timeout, "0").problem, /at least 1/);
  assert.match(applyField({}, timeout, "5000").problem, /at most 1800/);
  assert.match(applyField({}, timeout, "1.5").problem, /whole number/);
  assert.match(applyField({}, timeout, "abc").problem, /must be a number/);
});

test("a maxLength is enforced too", () => {
  const short = field({ properties: { name: { type: "string", maxLength: 4 } } }, "name");
  assert.match(applyField({}, short, "toolong").problem, /at most 4 characters/);
  assert.deepEqual(applyField({}, short, "ok").value, { name: "ok" });
});

test("a checkbox writes true or removes the key", () => {
  const flag = field({ properties: { on: { type: "boolean" } } }, "on");
  assert.deepEqual(applyField({}, flag, true).value, { on: true });
  assert.deepEqual(applyField({ on: true }, flag, false).value, {});
});

test("editing one field never disturbs another, described or not", () => {
  const config = { prompt: "keep me", model: "opus" };
  const result = applyField(config, field(AGENT, "timeout_seconds"), "120");
  assert.deepEqual(result.value, { prompt: "keep me", model: "opus", timeout_seconds: 120 });
  // And the original is untouched.
  assert.deepEqual(config, { prompt: "keep me", model: "opus" });
});

test("a config emptied of every key is absent, not an empty object", () => {
  assert.equal(emptied({}), undefined);
  assert.equal(emptied(undefined), undefined);
  assert.deepEqual(emptied({ a: 1 }), { a: 1 });
});
