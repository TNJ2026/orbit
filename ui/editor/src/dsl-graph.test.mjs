import assert from "node:assert/strict";
import { test } from "node:test";

import {
  connectionProblem, edgeLabel, freshId, layout, toDocument, toGraph, toPositions,
} from "./dsl-graph.mjs";
import { conditionText } from "./expressions.mjs";

const DOCUMENT = {
  dsl_version: "1.3",
  metadata: { id: "summarize_flow", name: "Summarize flow", labels: { team: "ops" } },
  inputs: [{ id: "topic", schema_id: "schema://object/1.0" }],
  nodes: [
    {
      id: "draft",
      kind: "action",
      label: "Draft the summary",
      handler: { name: "collect", version: "^1.0" },
      inputs: [{ id: "topic", schema_id: "schema://object/1.0" }],
      outputs: [{ id: "request", schema_id: "example://request/1.0" }],
      config: { model: "opus" },
      policies: ["retry_twice"],
    },
    {
      id: "review",
      kind: "human",
      inputs: [{ id: "request", schema_id: "example://request/1.0" }],
      outputs: [{ id: "request", schema_id: "example://request/1.0" }],
    },
    {
      id: "done",
      kind: "terminal",
      inputs: [{ id: "request", schema_id: "example://request/1.0" }],
    },
  ],
  edges: [
    {
      id: "draft_review",
      from: { node: "draft", port: "request" },
      to: { node: "review", port: "request" },
      route: "success",
      priority: 0,
    },
    {
      id: "review_done",
      from: { node: "review", port: "request" },
      to: { node: "done", port: "request" },
      condition: { op: "literal", value: true },
    },
  ],
  entry: ["draft"],
  terminals: ["done"],
  result: { node: "draft", port: "request" },
  policies: [{ id: "retry_twice", kind: "retry", config: { max_attempts: 2 } }],
};

const clone = (value) => JSON.parse(JSON.stringify(value));

test("a round trip through the canvas changes nothing", () => {
  const graph = toGraph(DOCUMENT);
  assert.deepEqual(toDocument(clone(DOCUMENT), graph), DOCUMENT);
});

test("moving a node changes no part of the definition", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes[0].position = { x: 999, y: -40 };
  assert.deepEqual(toDocument(clone(DOCUMENT), graph), DOCUMENT);
});

test("fields the canvas cannot edit survive being edited beside", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes[0].data.label = "Renamed";
  const rebuilt = toDocument(clone(DOCUMENT), graph).nodes[0];
  assert.equal(rebuilt.label, "Renamed");
  assert.deepEqual(rebuilt.config, { model: "opus" });
  assert.deepEqual(rebuilt.policies, ["retry_twice"]);
});

test("edge endpoints are rebuilt under their DSL names", () => {
  const [edge] = toDocument(clone(DOCUMENT), toGraph(DOCUMENT)).edges;
  assert.deepEqual(edge.from, { node: "draft", port: "request" });
  assert.deepEqual(edge.to, { node: "review", port: "request" });
  assert.equal(edge.source, undefined);
  assert.equal(edge.target, undefined);
});

test("an edge keeps its condition and priority through the canvas", () => {
  const rebuilt = toDocument(clone(DOCUMENT), toGraph(DOCUMENT));
  assert.deepEqual(rebuilt.edges[1].condition, { op: "literal", value: true });
  assert.equal(rebuilt.edges[0].priority, 0);
});

test("everything outside nodes and edges is carried through untouched", () => {
  const rebuilt = toDocument(clone(DOCUMENT), toGraph(DOCUMENT));
  for (const key of ["metadata", "inputs", "policies", "result", "dsl_version"]) {
    assert.deepEqual(rebuilt[key], DOCUMENT[key], key);
  }
});

test("a label equal to the node id is not written out", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes[1].data.label = "review";
  assert.equal(toDocument(clone(DOCUMENT), graph).nodes[1].label, undefined);
});

test("a blank label is removed rather than written as an empty string", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes[0].data.label = "   ";
  assert.equal(toDocument(clone(DOCUMENT), graph).nodes[0].label, undefined);
});

test("a deleted node is removed from entry, terminals and result", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes = graph.nodes.filter((node) => node.id !== "draft");
  graph.edges = graph.edges.filter((edge) => edge.source !== "draft");
  const rebuilt = toDocument(clone(DOCUMENT), graph);
  assert.deepEqual(rebuilt.entry, []);
  assert.deepEqual(rebuilt.terminals, ["done"]);
  assert.equal(rebuilt.result, undefined);
});

test("a deleted terminal leaves the rest of terminals alone", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes = graph.nodes.filter((node) => node.id !== "done");
  const rebuilt = toDocument(clone(DOCUMENT), graph);
  assert.deepEqual(rebuilt.terminals, []);
  assert.deepEqual(rebuilt.entry, ["draft"]);
});

test("a new node with no prior document still rebuilds", () => {
  const graph = toGraph(DOCUMENT);
  graph.nodes.push({
    id: "extra",
    position: { x: 0, y: 0 },
    data: { kind: "action", label: "Extra", inputs: [], outputs: [] },
  });
  const rebuilt = toDocument(clone(DOCUMENT), graph).nodes.at(-1);
  assert.deepEqual(rebuilt, { id: "extra", kind: "action", label: "Extra" });
});

test("layout puts a node after the node that feeds it", () => {
  const positions = layout(DOCUMENT);
  assert.ok(positions.get("draft").x < positions.get("review").x);
  assert.ok(positions.get("review").x < positions.get("done").x);
});

test("layout terminates on a back edge instead of following it forever", () => {
  const looping = {
    nodes: [
      { id: "start", kind: "action", outputs: [{ id: "v", schema_id: "s" }] },
      { id: "again", kind: "action", outputs: [{ id: "v", schema_id: "s" }] },
    ],
    edges: [
      { id: "f", from: { node: "start", port: "v" }, to: { node: "again", port: "v" } },
      {
        id: "b", back_edge: true, policy: "loop",
        from: { node: "again", port: "v" }, to: { node: "start", port: "v" },
      },
    ],
  };
  const positions = layout(looping);
  assert.ok(positions.get("start").x < positions.get("again").x);
});

test("an author's position wins over the computed layout", () => {
  const graph = toGraph(DOCUMENT, { draft: { x: 12, y: 34 } });
  assert.deepEqual(graph.nodes[0].position, { x: 12, y: 34 });
});

test("positions are captured as whole pixels", () => {
  const positions = toPositions([
    { id: "draft", position: { x: 10.4, y: -3.6 } },
  ]);
  assert.deepEqual(positions, { draft: { x: 10, y: -4 } });
});

test("a back edge is drawn as one", () => {
  const graph = toGraph({
    nodes: [{ id: "a", kind: "action" }, { id: "b", kind: "action" }],
    edges: [{
      id: "b_a", back_edge: true,
      from: { node: "b", port: "v" }, to: { node: "a", port: "v" },
    }],
  });
  assert.equal(graph.edges[0].animated, true);
  assert.equal(graph.edges[0].data.backEdge, true);
});

test("a connection between matching ports is allowed", () => {
  const { nodes } = toGraph(DOCUMENT);
  assert.equal(
    connectionProblem(nodes, {
      source: "draft", sourceHandle: "request",
      target: "review", targetHandle: "request",
    }),
    null,
  );
});

test("a connection to a port the node does not declare is refused", () => {
  const { nodes } = toGraph(DOCUMENT);
  assert.match(
    connectionProblem(nodes, {
      source: "draft", sourceHandle: "request",
      target: "review", targetHandle: "absent",
    }),
    /review has no input named absent/,
  );
});

test("a connection between mismatched schemas is refused", () => {
  const { nodes } = toGraph(DOCUMENT);
  assert.match(
    connectionProblem(nodes, {
      source: "draft", sourceHandle: "request",
      target: "draft", targetHandle: "topic",
    }),
    /cannot fill/,
  );
});

test("a connection from an input port is refused", () => {
  const { nodes } = toGraph(DOCUMENT);
  assert.match(
    connectionProblem(nodes, {
      source: "draft", sourceHandle: "topic",
      target: "review", targetHandle: "request",
    }),
    /draft has no output named topic/,
  );
});

test("a fresh id skips the ones already taken", () => {
  assert.equal(freshId("node", ["node_1", "node_2"]), "node_3");
  assert.equal(freshId("node", []), "node_1");
});

test("an ordinary edge has nothing worth labelling", () => {
  // `success` with no condition is the default everywhere; writing it on every
  // edge would bury the ones that are not.
  assert.equal(edgeLabel({ id: "e", route: "success" }, conditionText), null);
  assert.equal(edgeLabel({ id: "e" }, conditionText), null);
  assert.equal(edgeLabel(null, conditionText), null);
});

test("a non-default route is said", () => {
  assert.deepEqual(
    edgeLabel({ route: "error" }, conditionText),
    { route: "error", condition: null, mapped: false },
  );
});

test("a condition is shown as the author wrote it", () => {
  assert.equal(
    edgeLabel({ condition: "source.value > 5" }, conditionText).condition,
    "source.value > 5",
  );
  assert.equal(edgeLabel({ condition: true }, conditionText).condition, "True");
});

test("a compiled condition is rendered back to text for the label", () => {
  const ast = { op: "gt", left: { op: "ref", path: "source.v" }, right: { op: "literal", value: 5 } };
  assert.equal(edgeLabel({ condition: ast }, conditionText).condition, "source.v > 5");
});

test("a condition with no text form still says one is there", () => {
  // Silence would read as "no condition", which is the opposite of the truth.
  const ast = { op: "literal", value: -7 };
  assert.equal(edgeLabel({ condition: ast }, conditionText).condition, "…");
});

test("a long condition is cut rather than allowed to cover the graph", () => {
  const long = `source.value == ${"x".repeat(80)}`;
  const label = edgeLabel({ condition: long }, conditionText);
  assert.ok(label.condition.length <= 44, label.condition.length);
  assert.ok(label.condition.endsWith("…"));
});

test("a mapping is marked without being spelled out", () => {
  assert.equal(edgeLabel({ mapping: { schema_id: "x", value: {} } }, conditionText).mapped, true);
  assert.equal(edgeLabel({ mapping: {} }, conditionText), null);
});

test("route, condition and mapping can all be said at once", () => {
  assert.deepEqual(
    edgeLabel(
      { route: "timeout", condition: "exists(source.v)", mapping: { schema_id: "x", value: 1 } },
      conditionText,
    ),
    { route: "timeout", condition: "exists(source.v)", mapped: true },
  );
});
