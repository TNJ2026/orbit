import { useEffect, useState } from "react";
import { Background, Controls, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  VIEWER_NODE_CLICK, VIEWER_READY, readGraphMessage,
  readThemeMessage, viewerGraph,
} from "./catalog-graph.mjs";
import WorkflowEdge from "./WorkflowEdge.jsx";
import WorkflowNode from "./WorkflowNode.jsx";

const nodeTypes = { workflow: WorkflowNode };
const edgeTypes = { workflow: WorkflowEdge };
// Keep node labels readable on every embedded surface. Fit View obeys the
// same floor as wheel/pinch/control zoom, so no path can shrink below 50%.
const MIN_ZOOM = 0.5;
const FIT_VIEW = { padding: 0.2, minZoom: MIN_ZOOM, maxZoom: 1 };

/**
 * One Workflow graph, drawn and nothing else.
 *
 * This is what the Runtime's own pages embed where they used to draw their own
 * SVG. Two renderers of one definition is a shape that drifts — the workflow
 * detail page and the editor were separately deciding what a decision node
 * looks like and where a back edge goes — so the drawing lives in one place
 * and both surfaces show it.
 *
 * The graph arrives by message rather than being fetched. It draws published
 * workflows and unpublished drafts alike, and a draft has no id to fetch by;
 * the page embedding this has the graph in hand either way.
 */
export default function Viewer() {
  const [drawing, setDrawing] = useState(null);
  // Null until the embedding page says. Held rather than only written to the
  // document because xyflow themes its own furniture — the zoom controls
  // above all — from `colorMode`, and left on "system" they stayed white in
  // a dark canvas while everything this bundle styles had already followed.
  const [theme, setTheme] = useState(null);

  useEffect(() => {
    const origin = globalThis.location?.origin;
    const onMessage = (event) => {
      const chosen = readThemeMessage(event, origin);
      if (chosen) setTheme(chosen);
      const message = readGraphMessage(event, origin);
      if (message) setDrawing(message);
    };
    globalThis.addEventListener("message", onMessage);
    // Announced, not waited for: the parent cannot know when this bundle has
    // parsed, and posting before then would be a message nobody is listening
    // for yet.
    globalThis.parent?.postMessage({ type: VIEWER_READY }, origin);
    return () => globalThis.removeEventListener("message", onMessage);
  }, []);

  // The palette is CSS custom properties on `:root`, so it is set on the
  // document rather than passed down: nothing this component renders needs
  // to know which one is in force.
  useEffect(() => {
    if (theme) globalThis.document.documentElement.dataset.theme = theme;
  }, [theme]);

  if (!drawing?.graph) return <p className="viewer-state">…</p>;

  const { nodes, edges } = viewerGraph(
    drawing.graph, drawing.editable, drawing.statuses, drawing.bindings,
  );
  return (
    <div className="viewer">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode={theme ?? "system"}
        fitView
        fitViewOptions={FIT_VIEW}
        minZoom={MIN_ZOOM}
        // Panning and zooming are how a graph is read, so they stay. Every
        // other interaction is off at the canvas as well as per element: a
        // viewer that could be talked into showing something other than what
        // it was given would not be a viewer.
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        edgesFocusable={false}
        nodesFocusable={false}
        deleteKeyCode={null}
          // The React Flow badge. Removing it is permitted by its MIT
          // licence; the library asks for a subscription in exchange rather
          // than requiring the mark, and this Runtime credits xyflow in its
          // dependency manifest instead.
          proOptions={{ hideAttribution: true }}
          // The React Flow badge. Removing it is permitted by its MIT
          // licence; the library asks for a subscription in exchange rather
          // than requiring the mark, and this Runtime credits xyflow in its
          // dependency manifest instead.
        // Reported, never acted on. Which nodes have an editor behind them and
        // what opening one means belong to the page that embedded this.
        onNodeClick={(_event, node) => {
          if (!node.data?.editable) return;
          globalThis.parent?.postMessage(
            { type: VIEWER_NODE_CLICK, nodeId: node.id },
            globalThis.location?.origin,
          );
        }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
