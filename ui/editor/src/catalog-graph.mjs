/**
 * The catalog's picture of a published Workflow, as xyflow nodes and edges.
 *
 * The former editor read a workflow's authored *source*, which not every published
 * version has — early ones were published without one, and the catalog says so
 * with `source_available`. Those can still be looked at, so the read-only
 * viewer reads the compiled `graph` instead: every published version has one,
 * and it is the same projection the catalog list is drawn from.
 *
 * Positions come from the server. `orbit.workflow.api.graph_layout` exists so
 * that every picture of one definition places a node in the same spot, and a
 * viewer that laid the graph out for itself would be a second opinion about
 * where things go. Only the pixel spacing is decided here, and it is the
 * spacing used by the workflow canvas.
 *
 * Free of React and of xyflow, like `dsl-graph.mjs`, so it can be tested
 * without a DOM.
 */

import { DEPTH_WIDTH, LANE_HEIGHT } from "./dsl-graph.mjs";

/** A published graph as something xyflow can draw, with nothing to edit.
 *
 * Every interaction that would change the definition is refused per element
 * rather than only on the canvas: a viewer embedded in a page that reads a
 * workflow must not be able to produce one that differs from what was
 * published, whatever it is asked to do.
 */
export function viewerGraph(graph, editable = [], statuses = null) {
  const editableIds = new Set(editable ?? []);
  const at_status = new Map(Object.entries(statuses ?? {}));
  const at = new Map(
    (graph?.layout?.positions ?? []).map((item) => [item.node_id, item]),
  );
  const nodes = (graph?.nodes ?? []).map((node) => {
    const spot = at.get(node.node_id) ?? { depth: 0, lane: 0 };
    return {
      id: node.node_id,
      type: "workflow",
      position: { x: spot.depth * DEPTH_WIDTH, y: spot.lane * LANE_HEIGHT },
      draggable: false,
      connectable: false,
      selectable: false,
      deletable: false,
      data: {
        kind: node.kind,
        label: node.label ?? node.node_id,
        handler: node.handler_name
          ? { name: node.handler_name, version: node.handler_version }
          : null,
        // The compiled projection carries no ports, so there are no handles to
        // draw and an edge joins node to node. Drawing invented ports would be
        // claiming a shape the catalog never reported.
        inputs: [],
        outputs: [],
        readOnly: true,
        // The embedding page said this node has an editor behind it. The
        // viewer does not decide that and does not open it — it reports the
        // click and the page that knows opens its own dialog.
        editable: editableIds.has(node.node_id),
        // Where a run got to, when the page is drawing one. Absent for a
        // definition nobody has run: a picture of a Workflow is not a picture
        // of a run, and colouring every node "not started" would say the
        // opposite of nothing.
        status: at_status.get(node.node_id) ?? null,
      },
    };
  });
  const edges = (graph?.edges ?? []).map((edge) => ({
    id: edge.edge_id,
    type: "workflow",
    source: edge.from,
    target: edge.to,
    animated: Boolean(edge.back_edge),
    selectable: false,
    deletable: false,
    // `dsl` is what `edgeLabel` reads. The projection's route is the authored
    // one; it carries no condition or mapping, so the label says the route and
    // does not imply the rest is absent.
    data: {
      route: edge.route ?? "success",
      backEdge: Boolean(edge.back_edge),
      dsl: { route: edge.route ?? "success" },
      readOnly: true,
    },
  }));
  return { nodes, edges };
}

/** What the embedding page and the viewer say to each other.
 *
 * The graph is handed in rather than fetched. A draft has no workflow id to
 * fetch by — it is not published — and the page doing the embedding has just
 * read the graph anyway, so a second request would only be a second answer
 * that could differ from the one being described.
 */
export const VIEWER_READY = "orbit-viewer-ready";
export const VIEWER_GRAPH = "orbit-viewer-graph";
export const VIEWER_NODE_CLICK = "orbit-viewer-node-click";

/** The graph in a message, or `null` if this was not one.
 *
 * The origin is checked against this page's own: the frame is served by the
 * Runtime and embedded by the Runtime, so a message from anywhere else is not
 * a drawing instruction.
 */
export function readGraphMessage(event, origin) {
  if (!event || event.origin !== origin) return null;
  const data = event.data;
  if (!data || data.type !== VIEWER_GRAPH) return null;
  return {
    graph: data.graph ?? null,
    editable: data.editable ?? [],
    // `{node_id: status}` when the page is drawing a run, absent otherwise.
    statuses: data.statuses ?? null,
  };
}
