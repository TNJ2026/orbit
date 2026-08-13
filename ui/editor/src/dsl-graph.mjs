/**
 * The canvas and the DSL document, converted into each other.
 *
 * This module is deliberately free of React and of xyflow: it is the part that
 * can corrupt somebody's workflow, so it has to be testable without a DOM.
 *
 * The property everything else rests on is that a round trip changes nothing.
 * An author who opens a workflow, drags one node and saves must get back a
 * document whose `definition_hash` is what it was — positions are not part of
 * the definition, and neither is anything the canvas does not yet understand.
 * That is why each node and edge keeps the object it was built from and is
 * rebuilt by overlaying onto it, rather than by listing the fields we know:
 * a field this editor has never heard of survives being edited beside.
 */

/** Node kinds the LangGraph runtime compiles. Served by the Runtime; this is
 * the fallback for a canvas that has not fetched the contract yet. */
export const NODE_KINDS = ["action", "decision", "human", "join", "terminal"];

/** Exported so the read-only viewer places a node the same distance apart.
 * It is fed the server's depth and lane rather than computing its own, and a
 * different spacing there would make the same workflow read as two shapes. */
export const LANE_HEIGHT = 140;
export const DEPTH_WIDTH = 320;

/** Longest-path depth per node, and a lane within each depth.
 *
 * Deterministic and cycle-safe: a back edge is ignored for depth (following it
 * would not terminate), so a loop draws as the forward flow it decorates. This
 * mirrors what `orbit.workflow.api.graph_layout` computes server-side, so a
 * definition looks the same before and after it has been published.
 */
export function layout(document) {
  const nodes = document.nodes ?? [];
  const edges = (document.edges ?? []).filter((edge) => !edge.back_edge);
  const incoming = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    incoming.get(edge.to?.node)?.push(edge.from?.node);
  }
  const depth = new Map();
  const resolve = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const parents = incoming.get(id) ?? [];
    const value = parents.length
      ? Math.max(...parents.map((parent) => resolve(parent, seen) + 1))
      : 0;
    depth.set(id, value);
    return value;
  };
  const lanes = new Map();
  const positions = new Map();
  for (const node of nodes) {
    const d = resolve(node.id);
    const lane = lanes.get(d) ?? 0;
    lanes.set(d, lane + 1);
    positions.set(node.id, { x: d * DEPTH_WIDTH, y: lane * LANE_HEIGHT });
  }
  return positions;
}

/** A DSL document as xyflow nodes and edges.
 *
 * `positions` overrides the computed layout for nodes the author has moved.
 */
export function toGraph(document, positions = {}) {
  const computed = layout(document);
  const nodes = (document.nodes ?? []).map((node) => ({
    id: node.id,
    type: "workflow",
    position: positions[node.id] ?? computed.get(node.id) ?? { x: 0, y: 0 },
    data: {
      kind: node.kind,
      label: node.label ?? node.id,
      handler: node.handler ?? null,
      inputs: node.inputs ?? [],
      outputs: node.outputs ?? [],
      // The document this node came from, kept whole. Rebuilding overlays onto
      // it so a field the canvas cannot edit is not a field it can destroy.
      dsl: node,
    },
  }));
  const edges = (document.edges ?? []).map((edge) => ({
    id: edge.id,
    type: "workflow",
    source: edge.from?.node,
    target: edge.to?.node,
    sourceHandle: edge.from?.port,
    targetHandle: edge.to?.port,
    animated: Boolean(edge.back_edge),
    data: {
      route: edge.route ?? "success",
      backEdge: Boolean(edge.back_edge),
      dsl: edge,
    },
  }));
  return { nodes, edges };
}

/** The positions the canvas is holding, keyed by node id. */
export function toPositions(nodes) {
  return Object.fromEntries(
    nodes.map((node) => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }]),
  );
}

function rebuildNode(node) {
  const base = node.data?.dsl ?? {};
  const rebuilt = { ...base, id: node.id, kind: node.data.kind };
  // A label is optional in the DSL and blank is not allowed, so an emptied one
  // is removed rather than written as "".
  const label = (node.data.label ?? "").trim();
  if (label && label !== node.id) rebuilt.label = label;
  else delete rebuilt.label;
  if (node.data.handler) rebuilt.handler = node.data.handler;
  else delete rebuilt.handler;
  if (node.data.inputs?.length) rebuilt.inputs = node.data.inputs;
  else delete rebuilt.inputs;
  if (node.data.outputs?.length) rebuilt.outputs = node.data.outputs;
  else delete rebuilt.outputs;
  return rebuilt;
}

function rebuildEdge(edge) {
  const base = edge.data?.dsl ?? {};
  return {
    ...base,
    id: edge.id,
    // `from`/`to`, not `source`/`target`: those are xyflow's names, and a
    // document carrying them is rejected before it reaches the compiler.
    from: { node: edge.source, port: edge.sourceHandle },
    to: { node: edge.target, port: edge.targetHandle },
  };
}

/** The DSL document a canvas describes.
 *
 * `base` is the document that was loaded; everything on it that the canvas
 * does not model — metadata, inputs, policies, result, entry, terminals — is
 * carried through untouched.
 */
export function toDocument(base, { nodes, edges }) {
  const present = new Set(nodes.map((node) => node.id));
  const document = {
    ...base,
    nodes: nodes.map(rebuildNode),
    edges: edges.map(rebuildEdge),
  };
  // Entry and terminals name nodes. A node deleted on the canvas must not
  // linger in either list, or the document names something that is gone.
  if (Array.isArray(document.entry)) {
    document.entry = document.entry.filter((id) => present.has(id));
  }
  if (Array.isArray(document.terminals)) {
    document.terminals = document.terminals.filter((id) => present.has(id));
  }
  if (document.result && !present.has(document.result.node)) {
    delete document.result;
  }
  return document;
}

/** Whether a connection the author is drawing is allowed.
 *
 * Only what can be decided from the two ports: the port must exist on the
 * right side of each node, and the schemas must match. Everything else — that
 * a Handler is registered, that a loop is bounded, that the result resolves —
 * belongs to the server, and guessing at it here would be a second compiler.
 */
export function connectionProblem(nodes, connection) {
  const find = (id) => nodes.find((node) => node.id === id);
  const source = find(connection.source);
  const target = find(connection.target);
  if (!source || !target) return "unknown node";
  const from = (source.data.outputs ?? []).find((port) => port.id === connection.sourceHandle);
  const to = (target.data.inputs ?? []).find((port) => port.id === connection.targetHandle);
  if (!from) return `${source.id} has no output named ${connection.sourceHandle}`;
  if (!to) return `${target.id} has no input named ${connection.targetHandle}`;
  if (from.schema_id !== to.schema_id) {
    return `${from.schema_id} cannot fill ${to.schema_id}`;
  }
  return null;
}

/** An id that is stable, readable, and not already taken.
 *
 * Not a uuid: an author reads these in diagnostics and in the definition tab.
 */
export function freshId(prefix, taken) {
  const used = new Set(taken);
  for (let index = 1; ; index += 1) {
    const candidate = `${prefix}_${index}`;
    if (!used.has(candidate)) return candidate;
  }
}

/** The most a label may say before it stops being readable on a line. */
const LABEL_LIMIT = 44;

/**
 * What an edge is worth saying on the canvas, or nothing.
 *
 * A route and a condition are the two things that decide where a run goes,
 * and until now neither was visible: a conditional edge and an unconditional
 * one were the same grey curve, so reading a graph meant clicking every edge
 * in it. `success` is the default and stays unwritten — labelling every edge
 * with it would bury the ones that are not.
 */
export function edgeLabel(edge, conditionText) {
  if (!edge) return null;
  const route = edge.route && edge.route !== "success" ? edge.route : null;
  const rendered = conditionText ? conditionText(edge.condition) : null;
  // `null` from the renderer means an AST with no text form; it is still worth
  // saying that a condition is there, just not what it says.
  const condition = edge.condition === undefined || edge.condition === null
    ? null
    : rendered === null
      ? "…"
      : rendered.length > LABEL_LIMIT
        ? `${rendered.slice(0, LABEL_LIMIT - 1)}…`
        : rendered;
  const mapped = Boolean(edge.mapping && Object.keys(edge.mapping).length);
  if (!route && !condition && !mapped) return null;
  return { route, condition, mapped };
}
