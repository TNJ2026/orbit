import { useEffect, useState } from "react";
import { Background, Controls, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  VIEWER_NODE_CLICK, VIEWER_READY, readGraphMessage, viewerGraph,
} from "./catalog-graph.mjs";
import WorkflowEdge from "./WorkflowEdge.jsx";
import WorkflowNode from "./WorkflowNode.jsx";

const nodeTypes = { workflow: WorkflowNode };
const edgeTypes = { workflow: WorkflowEdge };
const FIT_VIEW = { padding: 0.2, maxZoom: 1 };

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

  useEffect(() => {
    const origin = globalThis.location?.origin;
    const onMessage = (event) => {
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

  if (!drawing?.graph) return <p className="viewer-state">…</p>;

  const { nodes, edges } = viewerGraph(
    drawing.graph, drawing.editable, drawing.statuses,
  );
  return (
    <div className="viewer">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode="system"
        fitView
        fitViewOptions={FIT_VIEW}
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
