import { BaseEdge, EdgeLabelRenderer, getBezierPath } from "@xyflow/react";

import { edgeLabel } from "./dsl-graph.mjs";
import { conditionText } from "./expressions.mjs";

/**
 * An edge, and the two things about it that decide where a run goes.
 *
 * A route and a condition were invisible: every edge was the same grey curve,
 * so reading a graph meant clicking each one in turn to find out which of them
 * a run would actually take. They are drawn on the edge now.
 *
 * `BaseEdge` keeps React Flow's own path and interaction — this adds a label
 * over it rather than replacing anything. `EdgeLabelRenderer` puts that label
 * in a layer above the edges, which is why it can be ordinary HTML with a
 * pointer cursor instead of foreignObject inside the SVG.
 */
export default function WorkflowEdge({
  id, sourceX, sourceY, targetX, targetY,
  sourcePosition, targetPosition, markerEnd, style, data, selected,
}) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
  });
  const label = edgeLabel(data?.dsl, conditionText);

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {label ? (
        <EdgeLabelRenderer>
          <div
            className={`edge-label${selected ? " selected" : ""}`}
            style={{
              // Follows the curve rather than sitting at a corner of it.
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
          >
            {label.route ? (
              <span className={`route route-${label.route}`}>{label.route}</span>
            ) : null}
            {label.condition ? (
              <code title={label.condition}>{label.condition}</code>
            ) : null}
            {label.mapped ? <span className="mapped" title="mapped">⇄</span> : null}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
