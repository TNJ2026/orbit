import { useEffect, useState } from "react";

import { addPort } from "./document.mjs";
import {
  availableReferences, conditionText, conditionValue, mappingText,
  mappingValue, objectText, objectValue,
} from "./expressions.mjs";

const ROUTES = ["success", "error", "timeout", "cancel"];

/** A field that only reports upward once it parses.
 *
 * Typing `{"a":` is a state every JSON field passes through, so a keystroke
 * cannot be allowed to write into the document — the graph would be rebuilt
 * from a half-typed value and the canvas would fight the author. The text is
 * local until it is valid; `problem` says why it is not being saved.
 */
function useDraft(initial, parse, commit) {
  const [text, setText] = useState(initial);
  const [problem, setProblem] = useState(null);
  useEffect(() => {
    setText(initial);
    setProblem(null);
  }, [initial]);
  const change = (next) => {
    setText(next);
    const result = parse(next);
    setProblem(result.problem ?? null);
    if (!result.problem && result.changed) commit(result.value);
  };
  return [text, change, problem];
}

function ConditionField({ edge, document, onChange }) {
  const original = edge.data?.dsl?.condition;
  const initial = conditionText(original);
  const editable = initial !== null;
  const [text, change, problem] = useDraft(
    initial ?? "",
    (next) => conditionValue(next, original),
    (value) => onChange({ condition: value }),
  );
  const references = availableReferences(document, edge.data?.dsl);
  if (!editable) {
    return (
      <label>
        <span>Condition</span>
        <textarea readOnly rows={3} value={JSON.stringify(original, null, 2)} />
        <em className="hint">
          This condition has no text form — it holds a value the expression
          syntax cannot write, so it is shown as it is stored.
        </em>
      </label>
    );
  }
  return (
    <label>
      <span>Condition</span>
      <textarea
        rows={2}
        value={text}
        placeholder="always"
        spellCheck={false}
        onChange={(event) => change(event.target.value)}
      />
      {problem ? <em className="problem">{problem}</em> : null}
      <em className="hint">
        Python expression syntax; empty means always. In scope:{" "}
        {references.map((reference) => (
          <code key={reference}>{reference}</code>
        ))}
      </em>
    </label>
  );
}

function MappingField({ edge, onChange }) {
  const original = edge.data?.dsl?.mapping;
  const initial = mappingText(original);
  const [schemaId, setSchemaId] = useState(initial.schemaId);
  const [value, setValue] = useState(initial.value);
  const [problem, setProblem] = useState(null);
  useEffect(() => {
    setSchemaId(initial.schemaId);
    setValue(initial.value);
    setProblem(null);
  }, [initial.schemaId, initial.value]);

  const apply = (next) => {
    setSchemaId(next.schemaId);
    setValue(next.value);
    const result = mappingValue(next, original);
    setProblem(result.problem ?? null);
    if (!result.problem && result.changed) onChange({ mapping: result.value });
  };

  return (
    <label>
      <span>Mapping</span>
      <input
        value={schemaId}
        placeholder="schema_id — empty carries the port across unchanged"
        spellCheck={false}
        onChange={(event) => apply({ schemaId: event.target.value, value })}
      />
      <textarea
        rows={4}
        value={value}
        placeholder='{"field": "$source.port"}'
        spellCheck={false}
        onChange={(event) => apply({ schemaId, value: event.target.value })}
      />
      {problem ? <em className="problem">{problem}</em> : null}
      <em className="hint">
        JSON. A string starting with <code>$</code> is a reference; anything
        else is a literal.
      </em>
    </label>
  );
}

function EdgeInspector({ edge, document, policies, onChange }) {
  const dsl = edge.data?.dsl ?? {};
  return (
    <>
      <h2>
        Edge <code>{edge.id}</code>
      </h2>
      <p className="path">
        {edge.source}.{edge.sourceHandle} → {edge.target}.{edge.targetHandle}
      </p>
      <label>
        <span>Route</span>
        <select
          value={dsl.route ?? "success"}
          onChange={(event) => onChange({ route: event.target.value })}
        >
          {ROUTES.map((route) => (
            <option key={route} value={route}>{route}</option>
          ))}
        </select>
      </label>
      <label>
        <span>Priority</span>
        <input
          type="number"
          min={0}
          value={dsl.priority ?? 0}
          onChange={(event) =>
            onChange({ priority: Math.max(0, Number(event.target.value) || 0) })
          }
        />
      </label>
      <label className="inline">
        <input
          type="checkbox"
          checked={Boolean(dsl.back_edge)}
          onChange={(event) => onChange({ back_edge: event.target.checked || undefined })}
        />
        <span>Back edge</span>
      </label>
      <label>
        <span>Policy</span>
        <select
          value={dsl.policy ?? ""}
          onChange={(event) => onChange({ policy: event.target.value || undefined })}
        >
          <option value="">none</option>
          {policies.map((policy) => (
            <option key={policy.id} value={policy.id}>
              {policy.id} · {policy.kind}
            </option>
          ))}
        </select>
        {dsl.back_edge && !dsl.policy ? (
          // The compiler refuses to route a back edge without one; saying so
          // here saves a publish round trip to find out.
          <em className="problem">a back edge needs a loop or rework policy</em>
        ) : null}
      </label>
      <ConditionField edge={edge} document={document} onChange={onChange} />
      <MappingField edge={edge} onChange={onChange} />
    </>
  );
}

function PortList({ node, side, onAdd, onRemove }) {
  const [draft, setDraft] = useState({ id: "", schema_id: "" });
  const [problem, setProblem] = useState(null);
  const ports = node.data?.[side] ?? [];
  return (
    <fieldset>
      <legend>{side}</legend>
      <ul className="ports-list">
        {ports.map((port) => (
          <li key={port.id}>
            <code>{port.id}</code>
            <span className="schema" title={port.schema_id}>{port.schema_id}</span>
            <button
              type="button"
              className="link"
              // The edges bound to it go too: an edge naming a port that is
              // gone cannot compile, so leaving them would turn one deletion
              // into a diagnostic to chase.
              title="remove, along with any edge bound to it"
              onClick={() => onRemove(side, port.id)}
            >
              remove
            </button>
          </li>
        ))}
      </ul>
      <div className="port-add">
        <input
          value={draft.id}
          placeholder="id"
          spellCheck={false}
          onChange={(event) => setDraft({ ...draft, id: event.target.value })}
        />
        <input
          value={draft.schema_id}
          placeholder="schema_id"
          spellCheck={false}
          onChange={(event) => setDraft({ ...draft, schema_id: event.target.value })}
        />
        <button
          type="button"
          onClick={() => {
            const result = addPort(ports, draft);
            if (result.problem) {
              setProblem(result.problem);
              return;
            }
            setProblem(null);
            setDraft({ id: "", schema_id: "" });
            onAdd(side, result.value);
          }}
        >
          add
        </button>
      </div>
      {problem ? <em className="problem">{problem}</em> : null}
    </fieldset>
  );
}

function NodeInspector({ node, policies, onChange, onPorts, onRemovePort }) {
  const dsl = node.data?.dsl ?? {};
  const initial = objectText(dsl.config);
  const [config, changeConfig, problem] = useDraft(
    initial,
    (next) => objectValue(next, dsl.config),
    (value) => onChange({ config: value }),
  );
  const selected = new Set(dsl.policies ?? []);
  return (
    <>
      <h2>
        Node <code>{node.id}</code>
      </h2>
      <p className="path">{node.data.kind}</p>
      <label>
        <span>Config</span>
        <textarea
          rows={5}
          value={config}
          placeholder="{}"
          spellCheck={false}
          onChange={(event) => changeConfig(event.target.value)}
        />
        {problem ? <em className="problem">{problem}</em> : null}
        <em className="hint">
          JSON object. Its shape belongs to the Handler, which is what checks it.
        </em>
      </label>
      <PortList node={node} side="inputs" onAdd={onPorts} onRemove={onRemovePort} />
      <PortList node={node} side="outputs" onAdd={onPorts} onRemove={onRemovePort} />
      {policies.length ? (
        <fieldset>
          <legend>Policies</legend>
          {policies.map((policy) => (
            <label className="inline" key={policy.id}>
              <input
                type="checkbox"
                checked={selected.has(policy.id)}
                onChange={(event) => {
                  const next = new Set(selected);
                  if (event.target.checked) next.add(policy.id);
                  else next.delete(policy.id);
                  onChange({ policies: next.size ? [...next].sort() : undefined });
                }}
              />
              <span>
                {policy.id} <em>{policy.kind}</em>
              </span>
            </label>
          ))}
        </fieldset>
      ) : null}
    </>
  );
}

/** The panel for whatever one thing is selected.
 *
 * Everything it writes goes onto the node's or edge's stored DSL object, which
 * is what `toDocument` rebuilds from — so a field this panel does not show is
 * still carried through untouched.
 */
export default function Inspector({
  selection, document, onChange, onPorts, onRemovePort,
}) {
  const policies = document?.policies ?? [];
  if (!selection) {
    return (
      <aside className="inspector empty">
        <p>Select a node or an edge to edit what the canvas cannot draw.</p>
      </aside>
    );
  }
  return (
    <aside className="inspector">
      {selection.kind === "edge" ? (
        <EdgeInspector
          edge={selection.item}
          document={document}
          policies={policies}
          onChange={onChange}
        />
      ) : (
        <NodeInspector
          node={selection.item}
          policies={policies}
          onChange={onChange}
          onPorts={onPorts}
          onRemovePort={onRemovePort}
        />
      )}
    </aside>
  );
}
