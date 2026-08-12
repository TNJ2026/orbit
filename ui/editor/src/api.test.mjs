import assert from "node:assert/strict";
import { test } from "node:test";

import {
  diagnosticsOf, isConflict, publishKey, sourceDiscriminator,
} from "./api.mjs";

test("the discriminator is stable for identical source", () => {
  assert.equal(sourceDiscriminator('{"a":1}'), sourceDiscriminator('{"a":1}'));
});

test("the discriminator changes when the source does", () => {
  assert.notEqual(sourceDiscriminator('{"a":1}'), sourceDiscriminator('{"a":2}'));
});

test("the discriminator is eight hex characters", () => {
  for (const text of ["", "{}", "a".repeat(5000)]) {
    assert.match(sourceDiscriminator(text), /^[0-9a-f]{8}$/, text.slice(0, 8));
  }
});

test("a retry of one attempt reuses its key, so the receipt replays", () => {
  const source = '{"metadata":{"id":"w"}}';
  assert.equal(
    publishKey("workflow:w", 3, source),
    publishKey("workflow:w", 3, source),
  );
});

test("fixing a rejected document publishes under a new key", () => {
  // Reusing the key here would answer the author with idempotency_conflict
  // instead of publishing the fix they just made.
  assert.notEqual(
    publishKey("workflow:w", 3, '{"broken":true}'),
    publishKey("workflow:w", 3, '{"fixed":true}'),
  );
});

test("publishing from a different version is a different attempt", () => {
  const source = '{"metadata":{"id":"w"}}';
  assert.notEqual(
    publishKey("workflow:w", 3, source),
    publishKey("workflow:w", 4, source),
  );
});

test("two workflows never share a publish key", () => {
  const source = '{"metadata":{"id":"w"}}';
  assert.notEqual(
    publishKey("workflow:a", 1, source),
    publishKey("workflow:b", 1, source),
  );
});

test("compiler findings are recovered from a rejection", () => {
  const error = new Error(JSON.stringify({
    message: "workflow source failed validation",
    diagnostics: [{ code: "DSL_RESULT_REQUIRED", message: "needs a result" }],
  }));
  assert.deepEqual(diagnosticsOf(error), [
    { code: "DSL_RESULT_REQUIRED", message: "needs a result" },
  ]);
});

test("a rejection that carries no findings yields none rather than throwing", () => {
  assert.equal(diagnosticsOf(new Error("publish conflict: expected 1, actual 2")), null);
  assert.equal(diagnosticsOf(new Error("")), null);
  assert.equal(diagnosticsOf({}), null);
});

test("a conflict is told apart from a document that did not compile", () => {
  assert.equal(isConflict(new Error("publish conflict: expected 1, actual 2")), true);
  assert.equal(isConflict(new Error("workflow source failed validation")), false);
  assert.equal(isConflict(undefined), false);
});
