import assert from "node:assert/strict";
import { test } from "node:test";

import {
  clearLayout, layoutKey, pruneLayout, readLayout, writeLayout,
} from "./layout-store.mjs";

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
    data,
  };
}

const hostile = (method) => ({
  getItem: () => {
    if (method === "getItem") throw new Error("blocked");
    return null;
  },
  setItem: () => {
    if (method === "setItem") throw new Error("quota");
  },
  removeItem: () => {
    if (method === "removeItem") throw new Error("blocked");
  },
});

test("a layout is keyed by workflow, not shared between them", () => {
  assert.notEqual(layoutKey("workflow:a"), layoutKey("workflow:b"));
  assert.match(layoutKey("workflow:a"), /workflow:a$/);
});

test("an arrangement round trips", () => {
  const storage = memoryStorage();
  const positions = { work: { x: 10, y: 20 }, done: { x: 30, y: 40 } };
  assert.equal(writeLayout(storage, "w", positions, ["work", "done"]), true);
  assert.deepEqual(readLayout(storage, "w"), positions);
});

test("nothing stored reads as no arrangement, not as an error", () => {
  assert.deepEqual(readLayout(memoryStorage(), "w"), {});
});

test("positions for nodes that are gone are not kept", () => {
  // A workflow outlives the nodes drawn in it; without this the store grows a
  // coordinate for everything anyone ever deleted.
  const storage = memoryStorage();
  writeLayout(storage, "w", { a: { x: 1, y: 1 }, b: { x: 2, y: 2 } }, ["a"]);
  assert.deepEqual(readLayout(storage, "w"), { a: { x: 1, y: 1 } });
});

test("pruning is available on its own and keeps only what is present", () => {
  assert.deepEqual(
    pruneLayout({ a: { x: 1, y: 1 }, b: { x: 2, y: 2 } }, ["b"]),
    { b: { x: 2, y: 2 } },
  );
  assert.deepEqual(pruneLayout(undefined, ["a"]), {});
});

test("an arrangement that prunes to nothing is removed rather than left empty", () => {
  const storage = memoryStorage();
  writeLayout(storage, "w", { a: { x: 1, y: 1 } }, ["a"]);
  writeLayout(storage, "w", { a: { x: 1, y: 1 } }, []);
  assert.equal(storage.data.has(layoutKey("w")), false);
});

test("a corrupt layout reads as none instead of failing the open", () => {
  // Refusing to open a workflow because its saved positions were unreadable
  // trades a small loss for a total one.
  for (const raw of ["not json", "[1,2]", '"a string"', "null", '{"a":{"x":"no"}}']) {
    assert.deepEqual(readLayout(memoryStorage({ [layoutKey("w")]: raw }), "w"), {}, raw);
  }
});

test("a partially valid layout is rejected whole rather than half applied", () => {
  const raw = JSON.stringify({ a: { x: 1, y: 1 }, b: { x: 1 } });
  assert.deepEqual(readLayout(memoryStorage({ [layoutKey("w")]: raw }), "w"), {});
});

test("storage that throws on read is treated as empty", () => {
  assert.deepEqual(readLayout(hostile("getItem"), "w"), {});
});

test("storage that throws on write gives up quietly and says so", () => {
  assert.equal(writeLayout(hostile("setItem"), "w", { a: { x: 1, y: 1 } }, ["a"]), false);
});

test("storage that throws on remove does not escape either", () => {
  assert.equal(clearLayout(hostile("removeItem"), "w"), false);
  assert.equal(writeLayout(hostile("removeItem"), "w", {}, []), false);
});

test("no storage at all is survivable", () => {
  // Some privacy modes do not merely throw, they leave nothing to call.
  assert.deepEqual(readLayout(null, "w"), {});
  assert.equal(writeLayout(null, "w", {}, []), true);
  assert.equal(clearLayout(undefined, "w"), true);
});

test("clearing removes just this workflow's arrangement", () => {
  const storage = memoryStorage();
  writeLayout(storage, "a", { n: { x: 1, y: 1 } }, ["n"]);
  writeLayout(storage, "b", { n: { x: 2, y: 2 } }, ["n"]);
  clearLayout(storage, "a");
  assert.deepEqual(readLayout(storage, "a"), {});
  assert.deepEqual(readLayout(storage, "b"), { n: { x: 2, y: 2 } });
});
