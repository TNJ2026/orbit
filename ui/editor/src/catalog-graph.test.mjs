import assert from "node:assert/strict";
import test from "node:test";

import {
  VIEWER_GRAPH, readGraphMessage, viewerGraph, viewerRequest,
} from "./catalog-graph.mjs";
import { DEPTH_WIDTH, LANE_HEIGHT } from "./dsl-graph.mjs";

const graph = {
  nodes: [
    {
      node_id: "collect", kind: "action", label: "Collect",
      handler_name: "agent.hermes", handler_version: "0.20.0",
    },
    { node_id: "done", kind: "terminal", label: null },
  ],
  edges: [
    {
      edge_id: "flow", from: "collect", to: "done",
      route: "success", priority: 0, back_edge: false,
    },
    {
      edge_id: "again", from: "done", to: "collect",
      route: "error", priority: 1, back_edge: true,
    },
  ],
  entry: ["collect"],
  terminals: ["done"],
  layout: {
    mode: "outline",
    positions: [
      { node_id: "collect", depth: 0, lane: 0 },
      { node_id: "done", depth: 1, lane: 0 },
    ],
  },
};

test("a node is placed where the server said, at the editor's spacing", () => {
  const { nodes } = viewerGraph(graph);
  assert.deepEqual(
    nodes.map((node) => [node.id, node.position]),
    [
      ["collect", { x: 0, y: 0 }],
      ["done", { x: DEPTH_WIDTH, y: 0 }],
    ],
  );
});

test("a lane is a row", () => {
  const { nodes } = viewerGraph({
    ...graph,
    layout: {
      positions: [
        { node_id: "collect", depth: 0, lane: 0 },
        { node_id: "done", depth: 0, lane: 1 },
      ],
    },
  });
  assert.deepEqual(nodes[1].position, { x: 0, y: LANE_HEIGHT });
});

test("nothing on the canvas can be moved, joined, selected or deleted", () => {
  const { nodes, edges } = viewerGraph(graph);
  for (const node of nodes) {
    assert.equal(node.draggable, false);
    assert.equal(node.connectable, false);
    assert.equal(node.selectable, false);
    assert.equal(node.deletable, false);
  }
  for (const edge of edges) {
    assert.equal(edge.selectable, false);
    assert.equal(edge.deletable, false);
  }
});

test("a node without a label is named by its id", () => {
  const { nodes } = viewerGraph(graph);
  assert.equal(nodes[0].data.label, "Collect");
  assert.equal(nodes[1].data.label, "done");
});

test("a handler is carried whole, or not at all", () => {
  const { nodes } = viewerGraph(graph);
  assert.deepEqual(nodes[0].data.handler, {
    name: "agent.hermes", version: "0.20.0",
  });
  assert.equal(nodes[1].data.handler, null);
});

test("an edge joins node to node, because the projection has no ports", () => {
  const { nodes, edges } = viewerGraph(graph);
  assert.equal(edges[0].source, "collect");
  assert.equal(edges[0].target, "done");
  assert.equal(edges[0].sourceHandle, undefined);
  for (const node of nodes) {
    assert.deepEqual(node.data.inputs, []);
    assert.deepEqual(node.data.outputs, []);
  }
});

test("a back edge is drawn as one", () => {
  const { edges } = viewerGraph(graph);
  assert.equal(edges[0].animated, false);
  assert.equal(edges[1].animated, true);
  assert.equal(edges[1].data.backEdge, true);
});

test("only the nodes the page named are marked editable", () => {
  const { nodes } = viewerGraph(graph, ["collect"]);
  assert.equal(nodes[0].data.editable, true);
  assert.equal(nodes[1].data.editable, false);
  assert.equal(viewerGraph(graph).nodes[0].data.editable, false);
});

test("a node the layout forgot is still drawn", () => {
  const { nodes } = viewerGraph({ ...graph, layout: { positions: [] } });
  assert.equal(nodes.length, 2);
  assert.deepEqual(nodes[0].position, { x: 0, y: 0 });
});

test("an absent graph is an empty canvas, not a crash", () => {
  assert.deepEqual(viewerGraph(undefined), { nodes: [], edges: [] });
  assert.deepEqual(viewerGraph({}), { nodes: [], edges: [] });
});

test("the request is read from the query string", () => {
  assert.deepEqual(viewerRequest("?readonly=1"), { readOnly: true });
  assert.deepEqual(viewerRequest("?readonly=0"), { readOnly: false });
  assert.deepEqual(viewerRequest(""), { readOnly: false });
});

test("a graph message from this origin is accepted", () => {
  const message = {
    origin: "http://127.0.0.1:8848",
    data: { type: VIEWER_GRAPH, graph, editable: ["collect"] },
  };
  assert.deepEqual(
    readGraphMessage(message, "http://127.0.0.1:8848"),
    { graph, editable: ["collect"], statuses: null },
  );
});

test("a message from anywhere else is not a drawing instruction", () => {
  const message = {
    origin: "https://elsewhere.example",
    data: { type: VIEWER_GRAPH, graph },
  };
  assert.equal(readGraphMessage(message, "http://127.0.0.1:8848"), null);
});

test("a message that is not a graph is ignored", () => {
  const origin = "http://127.0.0.1:8848";
  assert.equal(readGraphMessage({ origin, data: null }, origin), null);
  assert.equal(readGraphMessage({ origin, data: { type: "other" } }, origin), null);
  assert.equal(readGraphMessage(null, origin), null);
});

test("a graph message may clear the drawing", () => {
  const origin = "http://127.0.0.1:8848";
  assert.deepEqual(
    readGraphMessage({ origin, data: { type: VIEWER_GRAPH } }, origin),
    { graph: null, editable: [], statuses: null },
  );
});

test("a run's statuses ride along with the graph", () => {
  const origin = "http://127.0.0.1:8848";
  const message = {
    origin,
    data: {
      type: VIEWER_GRAPH, graph,
      statuses: { collect: "succeeded", done: "not_reached" },
    },
  };
  assert.deepEqual(
    readGraphMessage(message, origin).statuses,
    { collect: "succeeded", done: "not_reached" },
  );
});

test("a node carries the status of the run being drawn", () => {
  const { nodes } = viewerGraph(graph, [], { collect: "running" });
  const drawn = new Map(nodes.map((node) => [node.id, node.data.status]));
  assert.equal(drawn.get("collect"), "running");
  // A node the run has nothing to say about is not given a state.
  assert.equal(drawn.get("done"), null);
});

test("a definition nobody has run is drawn without any status", () => {
  const { nodes } = viewerGraph(graph, []);
  assert.ok(
    nodes.every((node) => node.data.status === null),
    "a picture of a Workflow is not a picture of a run",
  );
});
