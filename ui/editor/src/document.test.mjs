import assert from "node:assert/strict";
import { test } from "node:test";

import {
  POLICY_KINDS, POLICY_TEMPLATES, addPolicy, addPort, freshPolicyId,
  policyReferences, removePolicy, removePort, resultOptions, setInputs,
  setMetadata, setResult, toggleMembership, updatePolicy,
} from "./document.mjs";

const DOC = {
  dsl_version: "1.3",
  metadata: { id: "flow", name: "Flow", description: "does a thing" },
  nodes: [
    {
      id: "draft",
      kind: "action",
      outputs: [{ id: "request", schema_id: "s://r/1.0" }],
      policies: ["retry"],
    },
    { id: "done", kind: "terminal", inputs: [{ id: "request", schema_id: "s://r/1.0" }] },
  ],
  edges: [{
    id: "e1", policy: "retry",
    from: { node: "draft", port: "request" }, to: { node: "done", port: "request" },
  }],
  entry: ["draft"],
  terminals: ["done"],
  result: { node: "draft", port: "request" },
  policies: [{ id: "retry", kind: "retry", config: { max_attempts: 2 } }],
};

const clone = () => JSON.parse(JSON.stringify(DOC));

test("nothing here mutates the document it was given", () => {
  const original = clone();
  setMetadata(original, { name: "Renamed" });
  toggleMembership(original, "entry", "done");
  addPolicy(original, "loop");
  removePolicy(original, "retry");
  setResult(original, null);
  assert.deepEqual(original, DOC);
});

test("metadata is patched, and a blank field is removed rather than emptied", () => {
  assert.equal(setMetadata(clone(), { name: "Renamed" }).metadata.name, "Renamed");
  assert.equal(
    setMetadata(clone(), { description: "  " }).metadata.description,
    undefined,
  );
});

test("the workflow id is never dropped by a blank patch", () => {
  // Losing metadata.id would publish the edit onto a different aggregate.
  assert.equal(setMetadata(clone(), { id: "" }).metadata.id, "");
  assert.equal(setMetadata(clone(), { name: "x" }).metadata.id, "flow");
});

test("membership toggles both ways", () => {
  const added = toggleMembership(clone(), "entry", "done");
  assert.deepEqual(added.entry, ["draft", "done"]);
  assert.deepEqual(toggleMembership(added, "entry", "done").entry, ["draft"]);
});

test("an emptied list is removed rather than left as []", () => {
  const empty = toggleMembership(clone(), "entry", "draft");
  assert.equal("entry" in empty, false);
});

test("result options are every declared output port", () => {
  assert.deepEqual(resultOptions(DOC), [{ node: "draft", port: "request" }]);
});

test("the result can be set and cleared", () => {
  assert.deepEqual(
    setResult(clone(), { node: "draft", port: "request" }).result,
    { node: "draft", port: "request" },
  );
  assert.equal("result" in setResult(clone(), null), false);
});

test("a policy id is readable and does not collide", () => {
  assert.equal(freshPolicyId(DOC, "loop"), "loop");
  assert.equal(freshPolicyId(DOC, "retry"), "retry_2");
});

test("a new policy carries the config its kind needs", () => {
  for (const kind of POLICY_KINDS) {
    const added = addPolicy(clone(), kind);
    const policy = added.policies.at(-1);
    assert.equal(policy.kind, kind);
    assert.deepEqual(policy.config, POLICY_TEMPLATES[kind], kind);
  }
});

test("an unknown policy kind is refused", () => {
  assert.throws(() => addPolicy(clone(), "invented"), /unknown policy kind/);
});

test("a policy's config can be replaced", () => {
  const updated = updatePolicy(clone(), "retry", { config: { max_attempts: 9 } });
  assert.deepEqual(updated.policies[0].config, { max_attempts: 9 });
});

test("removing a policy removes every reference to it", () => {
  // A dangling reference is a document the compiler rejects, so removal has
  // to reach the node and the edge that named it.
  const removed = removePolicy(clone(), "retry");
  assert.equal("policies" in removed, false);
  assert.equal("policies" in removed.nodes[0], false);
  assert.equal("policy" in removed.edges[0], false);
});

test("removing a policy leaves other references on a node alone", () => {
  const document = clone();
  document.policies.push({ id: "loop", kind: "loop", config: {} });
  document.nodes[0].policies = ["retry", "loop"];
  const removed = removePolicy(document, "retry");
  assert.deepEqual(removed.nodes[0].policies, ["loop"]);
});

test("references are reported before a removal is confirmed", () => {
  assert.deepEqual(policyReferences(DOC, "retry"), {
    nodes: ["draft"], edges: ["e1"],
  });
  assert.deepEqual(policyReferences(DOC, "absent"), { nodes: [], edges: [] });
});

test("inputs are set, and an emptied list is removed", () => {
  const withInputs = setInputs(clone(), [{ id: "topic", schema_id: "s://o/1.0" }]);
  assert.deepEqual(withInputs.inputs, [{ id: "topic", schema_id: "s://o/1.0" }]);
  assert.equal("inputs" in setInputs(withInputs, []), false);
});

test("a port needs both an id and a schema", () => {
  assert.match(addPort([], { id: "", schema_id: "s" }).problem, /needs an id/);
  assert.match(addPort([], { id: "a", schema_id: " " }).problem, /needs a schema_id/);
});

test("a duplicate port id is refused", () => {
  assert.match(
    addPort([{ id: "value", schema_id: "s" }], { id: "value", schema_id: "s" }).problem,
    /already has a port named value/,
  );
});

test("a port is added trimmed", () => {
  assert.deepEqual(
    addPort([], { id: "  value ", schema_id: " s://x/1.0 " }).value,
    [{ id: "value", schema_id: "s://x/1.0" }],
  );
});

test("removing an output port removes the edges leaving it", () => {
  const graph = {
    nodes: [
      { id: "a", data: { outputs: [{ id: "v", schema_id: "s" }], inputs: [] } },
      { id: "b", data: { inputs: [{ id: "v", schema_id: "s" }], outputs: [] } },
    ],
    edges: [
      { id: "keep", source: "a", sourceHandle: "other", target: "b", targetHandle: "v" },
      { id: "drop", source: "a", sourceHandle: "v", target: "b", targetHandle: "v" },
    ],
  };
  const result = removePort(graph, "a", "outputs", "v");
  assert.deepEqual(result.nodes[0].data.outputs, []);
  assert.deepEqual(result.edges.map((edge) => edge.id), ["keep"]);
  assert.deepEqual(result.removedEdges, ["drop"]);
});

test("removing an input port removes the edges arriving at it", () => {
  const graph = {
    nodes: [{ id: "b", data: { inputs: [{ id: "v", schema_id: "s" }], outputs: [] } }],
    edges: [{ id: "in", source: "a", sourceHandle: "v", target: "b", targetHandle: "v" }],
  };
  const result = removePort(graph, "b", "inputs", "v");
  assert.deepEqual(result.edges, []);
  assert.deepEqual(result.removedEdges, ["in"]);
});

test("removing a port leaves other nodes untouched", () => {
  const graph = {
    nodes: [
      { id: "a", data: { outputs: [{ id: "v", schema_id: "s" }], inputs: [] } },
      { id: "c", data: { outputs: [{ id: "v", schema_id: "s" }], inputs: [] } },
    ],
    edges: [],
  };
  const result = removePort(graph, "a", "outputs", "v");
  assert.deepEqual(result.nodes[1].data.outputs, [{ id: "v", schema_id: "s" }]);
});
