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
export default function WorkflowNode({ id, data, selected }) {
  const inputs = data.inputs ?? [];
  const outputs = data.outputs ?? [];
  const rows = Math.max(inputs.length, outputs.length, 1);
  return (
    <div className={`node node-${data.kind}${selected ? " selected" : ""}`}>
      <header>
        <span className="kind">{data.kind}</span>
        {/* The label is the one part of a node this canvas can already change,
            so it is edited in place rather than behind a panel. `nodrag` stops
            xyflow from treating a click in the field as the start of a drag,
            which would otherwise make the text impossible to select. */}
        <input
          className="title nodrag"
          value={data.label ?? ""}
          placeholder={id}
          aria-label={`Label for ${id}`}
          onChange={(event) => data.onLabelChange?.(id, event.target.value)}
        />
      </header>
      {data.handler ? (
        <p className="handler">
          {data.handler.name}
          <span className="version"> {data.handler.version}</span>
        </p>
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
