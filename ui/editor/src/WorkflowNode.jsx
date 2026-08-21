import { Handle, Position } from "@xyflow/react";

/**
 * One workflow node, drawn with a handle per declared port.
 *
 * A handle per port rather than one per side is the whole point: an edge in
 * this DSL connects an output *port* to an input *port*, so a canvas that
 * connects node-to-node would be describing something the compiler cannot
 * accept. Naming the handle after the port id is what lets the connection an
 * author draws be rebuilt as `from`/`to` without guessing.
 */
const RUN_STATUS_TEXT = {
  succeeded: "done",
  running: "working",
  waiting: "waiting",
  failed: "failed",
  unknown: "outcome unknown",
  cancelled: "cancelled",
  answered: "answered",
  not_reached: "not started",
};

export default function WorkflowNode({ id, data, selected }) {
  const inputs = data.inputs ?? [];
  const outputs = data.outputs ?? [];
  const rows = Math.max(inputs.length, outputs.length, 1);
  // A version is small print nobody reads off a diagram, and it crowded the
  // one line that says who does the work. The exception is a substitution
  // between two builds of the same Agent: there the version is the only thing
  // the arrow points at, and without it the card would read "claude → claude".
  const versioned = Boolean(
    data.rebound && data.rebound.name === data.handler?.name,
  );
  return (
    <div
      className={`node node-${data.kind}${selected ? " selected" : ""}${
        data.editable ? " node-editable" : ""}${
        data.status ? ` node-run node-run-${data.status}` : ""}`}
      data-node-id={id}
    >
      <header>
        {/* Kind and state share one row, not one each. They are the same
            register of small print about the same node, and a card drawing a
            run was a whole line taller than the same card drawing its
            definition — enough for a two-line label to reach out of its lane
            and into the node below it. */}
        <span className="meta">
          <span className="kind">{data.kind}</span>
          {/* Where a run got to, when one is being drawn. Spelled as well as
              coloured: a picture whose only difference is a hue is unreadable
              to anyone who cannot see the hue, and this canvas already refuses
              to let meaning ride on colour for routes. */}
          {data.status ? (
            <span className={`run-status run-status-${data.status}`}>
              {RUN_STATUS_TEXT[data.status] ?? data.status}
            </span>
          ) : null}
        </span>
        {/* The label is the one part of a node this canvas can already change,
            so it is edited in place rather than behind a panel. `nodrag` stops
            xyflow from treating a click in the field as the start of a drag,
            which would otherwise make the text impossible to select.

            A viewer shows the same node as text: a disabled field still reads
            as somewhere an author could type, and there is nothing here to
            type into. */}
        {data.readOnly ? (
          // Two lines and then an ellipsis, with the whole of it on hover. A
          // label is meant to be a short title, but nothing enforces that, and
          // an unbounded one grows the card past the fixed lane it is placed
          // in — which the reader sees as two nodes drawn on top of each other.
          <span className="title" title={data.label ?? id}>{data.label ?? id}</span>
        ) : (
          <input
            className="title nodrag"
            value={data.label ?? ""}
            placeholder={id}
            aria-label={`Label for ${id}`}
            onChange={(event) => data.onLabelChange?.(id, event.target.value)}
          />
        )}
      </header>
      {/* What the definition names, and — when the Runtime substitutes one
          — what will really run it. Both, never one instead of the other: a
          drawing that showed only the substitute would disagree with the
          definition it is a picture of, and one that showed only the
          published name would be wrong about the run. No wording, because
          this bundle carries no translations; the arrow is the sentence, and
          the page around it says the rest. */}
      {data.handler ? (
        <p className="handler">
          <span className={data.rebound ? "superseded" : undefined}>
            {data.handler.name}
            {versioned ? (
              <span className="version"> {data.handler.version}</span>
            ) : null}
          </span>
          {data.rebound ? (
            <>
              <span className="rebound-arrow" aria-hidden="true"> → </span>
              <span className="rebound">
                {data.rebound.name}
                {versioned ? (
                  <span className="version"> {data.rebound.version}</span>
                ) : null}
              </span>
            </>
          ) : null}
        </p>
      ) : null}
      {/* A drawing needs somewhere for an edge to land. The compiled
          projection the viewer reads carries no ports, so there are no
          per-port handles to attach to and an edge would have nothing to
          anchor against — xyflow drops it, and the graph draws as loose
          boxes. One handle a side, not interactive, and hidden: it is an
          anchor, not an offer to connect. */}
      {data.readOnly ? (
        <>
          <Handle type="target" position={Position.Left} isConnectable={false} />
          <Handle type="source" position={Position.Right} isConnectable={false} />
        </>
      ) : null}
      <div className="ports" style={{ minHeight: `${rows * 22}px` }}>
        <ul className="side">
          {inputs.map((port) => (
            <li key={port.id}>
              <Handle type="target" position={Position.Left} id={port.id} />
              <span title={port.schema_id}>{port.id}</span>
            </li>
          ))}
        </ul>
        <ul className="side right">
          {outputs.map((port) => (
            <li key={port.id}>
              <span title={port.schema_id}>{port.id}</span>
              <Handle type="source" position={Position.Right} id={port.id} />
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
