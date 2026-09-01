import { createRoot } from "react-dom/client";
import {
  Background,
  Controls,
  PanOnScrollMode,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { viewerGraph } from "./catalog-graph.mjs";
import WorkflowEdge from "./WorkflowEdge.jsx";
import WorkflowNode from "./WorkflowNode.jsx";
import "./app.css";
import "./mcp-workflow-graph.css";

const roots = new WeakMap();
const nodeTypes = { workflow: WorkflowNode };
const edgeTypes = { workflow: WorkflowEdge };
const MIN_ZOOM = 0.5;
const fitViewOptions = { padding: 0.2, minZoom: MIN_ZOOM, maxZoom: 1 };

function ReadOnlyGraph({ graph, theme }) {
  const { nodes, edges } = viewerGraph(graph);
  return (
    <div className="mcp-xyflow-viewer" data-theme={theme}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode={theme ?? "system"}
        fitView
        fitViewOptions={fitViewOptions}
        minZoom={MIN_ZOOM}
        maxZoom={2}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        edgesFocusable={false}
        nodesFocusable={false}
        deleteKeyCode={null}
        panOnDrag
        panOnScroll
        panOnScrollMode={PanOnScrollMode.Horizontal}
        zoomOnPinch
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function mount(element, graph, theme = null) {
  if (!element) throw new Error("workflow graph mount element is required");
  let root = roots.get(element);
  if (!root) {
    root = createRoot(element);
    roots.set(element, root);
  }
  root.render(<ReadOnlyGraph graph={graph ?? {}} theme={theme} />);
}

export function unmount(element) {
  const root = roots.get(element);
  if (!root) return;
  root.unmount();
  roots.delete(element);
}
