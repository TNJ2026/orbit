import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, addEdge,
  applyEdgeChanges, applyNodeChanges,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import * as api from "./api.mjs";
import {
  NODE_KINDS, connectionProblem, freshId, toDocument, toGraph, toPositions,
} from "./dsl-graph.mjs";
import { handlersForKind, removePort } from "./document.mjs";
import { clearLayout, readLayout, writeLayout } from "./layout-store.mjs";
import { PatchError, applyPatch } from "./patch.mjs";
import Inspector from "./Inspector.jsx";
import WorkflowPanel from "./WorkflowPanel.jsx";
import WorkflowEdge from "./WorkflowEdge.jsx";
import WorkflowNode from "./WorkflowNode.jsx";

const nodeTypes = { workflow: WorkflowNode };
const edgeTypes = { workflow: WorkflowEdge };
const FIT_VIEW = { padding: 0.2, maxZoom: 1 };

/** A panel's patch as an edge body: `undefined` means the field goes. */
function strip(patch) {
  const out = {};
  for (const [key, value] of Object.entries(patch)) {
    if (value !== undefined) out[key] = value;
  }
  return out;
}

export default function App() {
  const [contract, setContract] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [handlers, setHandlers] = useState([]);
  const [workflowId, setWorkflowId] = useState("");
  const [base, setBase] = useState(null);
  // Every change to the definition, in the order it was made. The document is
  // `base` with these applied — never a thing edited in place — so a change is
  // a value that can be shown, counted, undone or sent, and so an author's
  // edit and an Agent's are the same kind of thing.
  const [operations, setOperations] = useState([]);
  const [version, setVersion] = useState(0);
  // Where each node is drawn. Not state the definition has any claim on: a
  // coordinate in `definition_hash` would make nudging a node publish a new
  // version, so this lives beside the document and never inside it.
  const [positions, setPositions] = useState({});
  const [notice, setNotice] = useState(null);
  const [diagnostics, setDiagnostics] = useState([]);
  const [selection, setSelection] = useState(null);
  // React Flow's own handle, kept so a graph can be re-fitted after it is
  // loaded. `fitView` alone fits once, on mount — when there is no workflow
  // open, no inspector beside the canvas, and so a different width.
  const [flow, setFlow] = useState(null);
  const [busy, setBusy] = useState(false);
  // This tab's expected_version is behind the store: somebody else published
  // while it was open, so every publish from here would be refused.
  const [stale, setStale] = useState(false);

  useEffect(() => {
    Promise.all([api.authoringSchema(), api.listWorkflows(), api.handlerCatalog()])
      .then(([schema, list, registry]) => {
        setContract(schema);
        setCatalog(list.workflows ?? []);
        setHandlers(registry.handlers ?? []);
      })
      .catch((error) => setNotice({ level: "error", text: error.message }));
  }, []);

  const kinds = contract?.node_kinds ?? NODE_KINDS;

  const open = useCallback(async (id) => {
    setNotice(null);
    setDiagnostics([]);
    try {
      const detail = await api.readWorkflow(id);
      // The *source* is what the editor edits, not `definition`. `definition`
      // is the compiled IR the read-only views draw; it has already had
      // handler versions resolved and fingerprints stamped, so editing it and
      // sending it back would be claiming a binding the author never chose.
      // Early versions published without a source are viewable, never
      // editable — the catalog says so with source_available.
      if (!detail.source) {
        setNotice({
          level: "error",
          text: "this workflow was published without an authored source, so it cannot be edited",
        });
        return;
      }
      if (detail.source_format && detail.source_format !== "json") {
        setNotice({
          level: "error",
          text: `this workflow's source is ${detail.source_format}; the canvas reads JSON`,
        });
        return;
      }
      setBase(JSON.parse(detail.source));
      // Opening starts a fresh log: what was edited last time was published
      // or discarded, and either way it is not pending now.
      setOperations([]);
      // The author's own arrangement, where there is one. Nodes without a
      // stored position fall back to the computed layout, so a workflow that
      // grew since it was last arranged still draws sensibly.
      setPositions(readLayout(globalThis.localStorage, id));
      setVersion(detail.latest_version ?? detail.version ?? 0);
      setWorkflowId(id);
      setStale(false);
      setSelection(null);
    } catch (error) {
      setNotice({ level: "error", text: error.message });
    }
  }, []);

  // Fitted once a workflow has been drawn, not when the canvas mounted: at
  // mount there is nothing open, so no inspector beside the canvas and a
  // different width to fit into. Keyed on the workflow rather than on the
  // nodes, so editing one does not keep yanking the view back.
  useEffect(() => {
    if (!flow || !workflowId || !nodes.length) return undefined;
    const frame = requestAnimationFrame(() => flow.fitView(FIT_VIEW));
    return () => cancelAnimationFrame(frame);
  }, [flow, workflowId]);

  const document = useMemo(() => {
    if (!base) return null;
    try {
      return applyPatch(base, operations);
    } catch {
      // Unreachable through the UI — an operation that could not be applied
      // was refused before it entered the log — but a document that will not
      // rebuild must not take the page down with it.
      return base;
    }
  }, [base, operations]);

  const graph = useMemo(
    () => (document ? toGraph(document, positions) : { nodes: [], edges: [] }),
    [document, positions],
  );

  const nodes = useMemo(() => graph.nodes.map((node) => ({
    ...node,
    selected: selection?.kind === "node" && selection.item.id === node.id,
  })), [graph.nodes, selection]);

  const edges = useMemo(() => graph.edges.map((edge) => ({
    ...edge,
    selected: selection?.kind === "edge" && selection.item.id === edge.id,
  })), [graph.edges, selection]);

  // Persisted as the canvas draws them, so an arrangement survives a reload.
  // Never sent anywhere: a coordinate is how a drawing is read, not what it
  // means, and one inside `definition_hash` would make nudging a node publish
  // a new version.
  useEffect(() => {
    if (!workflowId || !nodes.length) return;
    writeLayout(
      globalThis.localStorage, workflowId, toPositions(nodes),
      nodes.map((node) => node.id),
    );
  }, [workflowId, nodes]);

  // Re-read from the derived arrays: the item captured when the author
  // clicked is a snapshot, and the panel has to show what their own edits
  // just produced.
  const live = useMemo(() => {
    if (!selection) return null;
    const source = selection.kind === "edge" ? edges : nodes;
    const item = source.find((entry) => entry.id === selection.item.id);
    return item ? { kind: selection.kind, item } : null;
  }, [selection, nodes, edges]);

  /** Record one or more operations, or say why they could not be applied.
   *
   * Applied here rather than only when the document is derived, so an
   * operation that cannot land is refused at the moment it is made and never
   * enters the log. That is what keeps the log replayable.
   */
  const commit = useCallback((...ops) => {
    const wanted = ops.filter(Boolean);
    if (!wanted.length || !base) return false;
    try {
      applyPatch(applyPatch(base, operations), wanted);
    } catch (error) {
      setNotice({
        level: "error",
        text: error instanceof PatchError ? error.reason : String(error),
      });
      return false;
    }
    setOperations((current) => [...current, ...wanted]);
    return true;
  }, [base, operations]);

  // React Flow reports both kinds of change through one channel, and they
  // belong in different places: a move is layout and a removal is an edit.
  const onNodesChange = useCallback((changes) => {
    const removed = changes.filter((change) => change.type === "remove");
    if (removed.length) {
      commit(...removed.map((change) => ({ op: "remove_node", node_id: change.id })));
    }
    const moved = changes.filter(
      (change) => change.type === "position" && change.position,
    );
    if (moved.length) {
      setPositions((current) => {
        const next = { ...current };
        for (const change of moved) {
          next[change.id] = {
            x: Math.round(change.position.x), y: Math.round(change.position.y),
          };
        }
        return next;
      });
    }
  }, [commit]);

  const onEdgesChange = useCallback((changes) => {
    const removed = changes.filter((change) => change.type === "remove");
    if (removed.length) {
      commit(...removed.map((change) => ({ op: "remove_edge", edge_id: change.id })));
    }
  }, [commit]);

  // Refused while the author is still dragging, so an impossible edge cannot
  // be dropped in the first place. The message on release explains why.
  const isValidConnection = useCallback(
    (connection) => connectionProblem(nodes, connection) === null,
    [nodes],
  );

  const onConnect = useCallback(
    (connection) => {
      const problem = connectionProblem(nodes, connection);
      if (problem) {
        setNotice({ level: "error", text: problem });
        return;
      }
      setNotice(null);
      commit({
        op: "add_edge",
        edge: {
          id: freshId("edge", edges.map((edge) => edge.id)),
          from: { node: connection.source, port: connection.sourceHandle },
          to: { node: connection.target, port: connection.targetHandle },
        },
      });
    },
    [nodes, edges, commit],
  );

  const renameNode = useCallback(
    (id, label) => commit({ op: "set_node_label", node_id: id, label: label || null }),
    [commit],
  );

  /** The operation an inspector field's change means.
   *
   * The panels speak in DSL fields because that is what they show; this is
   * the one place that turns a field into the operation for it, so that
   * everything downstream sees only operations.
   */
  const patchSelected = useCallback((patch) => {
    if (!selection) return;
    const id = selection.item.id;
    if (selection.kind === "edge") {
      const edge = edges.find((item) => item.id === id);
      const body = { ...(edge?.data?.dsl ?? {}), id, ...strip(patch) };
      // `undefined` from a panel means the field is gone, and an operation
      // says that by not carrying it.
      for (const [key, value] of Object.entries(patch)) {
        if (value === undefined) delete body[key];
      }
      commit({ op: "set_edge", edge: body });
      return;
    }
    const ops = [];
    for (const [key, value] of Object.entries(patch)) {
      if (key === "label") ops.push({ op: "set_node_label", node_id: id, label: value ?? null });
      else if (key === "config") ops.push({ op: "set_node_config", node_id: id, config: value ?? {} });
      else if (key === "handler") ops.push({ op: "set_node_handler", node_id: id, handler: value ?? null });
      else if (key === "policies") ops.push({ op: "set_node_policies", node_id: id, policies: value ?? [] });
    }
    commit(...ops);
  }, [selection, edges, commit]);

  /** Replace one side's ports on the selected node. */
  const setPorts = useCallback((side, ports) => {
    if (selection?.kind !== "node") return;
    commit({ op: "set_node_ports", node_id: selection.item.id, side, ports });
  }, [selection, commit]);

  /** Drop a port, and with it every edge that named it.
   *
   * Two operations rather than one: removing a port is not the same act as
   * removing an edge, and the log should say both happened.
   */
  const dropPort = useCallback((side, portId) => {
    if (selection?.kind !== "node") return;
    const nodeId = selection.item.id;
    const node = nodes.find((item) => item.id === nodeId);
    const remaining = (node?.data?.[side] ?? []).filter((port) => port.id !== portId);
    const bound = edges.filter((edge) => (
      side === "outputs"
        ? edge.source === nodeId && edge.sourceHandle === portId
        : edge.target === nodeId && edge.targetHandle === portId
    ));
    const applied = commit(
      ...bound.map((edge) => ({ op: "remove_edge", edge_id: edge.id })),
      { op: "set_node_ports", node_id: nodeId, side, ports: remaining },
    );
    if (applied && bound.length) {
      setNotice({
        level: "info",
        text: `removed ${bound.length} edge(s) bound to ${portId}`,
      });
    }
  }, [selection, nodes, edges, commit]);

  /** Change a node's kind, dropping a handler the new kind cannot use.
   *
   * A manifest declares which kinds it serves, so a binding that survived the
   * switch would be one the compiler refuses — and the author would have to
   * read a diagnostic to learn what the switch did.
   */
  const setKind = useCallback((kind) => {
    if (selection?.kind !== "node") return;
    const id = selection.item.id;
    const handler = nodes.find((node) => node.id === id)?.data?.dsl?.handler;
    const serves = handler && handlersForKind(handlers, kind).some(
      (item) => item.name === handler.name && item.version === handler.version,
    );
    commit(
      { op: "set_node_kind", node_id: id, kind },
      // A binding the new kind cannot serve is one the compiler refuses, and
      // dropping it silently would leave the author to read a diagnostic.
      handler && !serves ? { op: "set_node_handler", node_id: id } : null,
    );
  }, [selection, nodes, handlers, commit]);

  /** Remove whatever is selected, edges bound to a node included.
   *
   * Backspace and Delete do this too, but a keyboard shortcut is not a thing
   * to have to know — and on a canvas there is nothing to right-click either.
   */
  const removeSelected = useCallback(() => {
    if (!selection) return;
    const id = selection.item.id;
    const removed = commit(
      selection.kind === "edge"
        ? { op: "remove_edge", edge_id: id }
        // `remove_node` takes the edges that named it, and its place in
        // entry, terminals and result. One act, one operation.
        : { op: "remove_node", node_id: id },
    );
    if (removed) setSelection(null);
  }, [selection, commit]);

  /** Forget this arrangement and go back to the computed one.
   *
   * Only positions are cleared, and the definition is not consulted: the two
   * have nothing to say to each other, which is the point of keeping them
   * apart.
   */
  const resetLayout = useCallback(() => {
    if (!base || !workflowId) return;
    clearLayout(globalThis.localStorage, workflowId);
    setPositions({});
    setNotice({ level: "info", text: "layout reset" });
  }, [base, workflowId]);

  const addNode = useCallback((kind) => {
    const id = freshId(kind, nodes.map((node) => node.id));
    if (commit({ op: "add_node", node: { id, kind } })) {
      // Where it is drawn is not part of the definition, so it is placed
      // rather than recorded.
      setPositions((current) => ({
        ...current,
        [id]: { x: 60, y: 60 + nodes.length * 30 },
      }));
    }
  }, [nodes, commit]);

  // The canvas is a projection of the document, not a second copy of it:
  // positions are layered on, selection is our own, and the rename callback
  // rides in `data` because xyflow hands a custom node only that.
  const drawn = useMemo(
    () => nodes.map((node) => ({
      ...node, data: { ...node.data, onLabelChange: renameNode },
    })),
    [nodes, renameNode],
  );

  // The compiler answers with diagnostics; showing them is the entire value of
  // asking it, so they are rendered rather than collapsed into "invalid".
  const reportFailure = useCallback((error) => {
    const findings = api.diagnosticsOf(error);
    setDiagnostics(findings ?? []);
    if (api.isConflict(error)) {
      // Nothing the author can fix by editing: someone else published while
      // this tab was open. Saying so, and offering the reload, is the only
      // useful answer — retrying would refuse again forever.
      setStale(true);
      setNotice({ level: "error", text: `${error.message} — reopen to continue` });
      return;
    }
    setNotice({
      level: "error",
      text: findings?.length ? `${findings.length} problem(s)` : error.message,
    });
  }, []);

  const check = useCallback(async () => {
    if (!document) return;
    setBusy(true);
    setNotice({ level: "info", text: "validating…" });
    try {
      const result = await api.validate(document, version);
      setDiagnostics([]);
      setNotice({
        level: "ok",
        text: `compiles · ${result.node_count} nodes · ${result.definition_hash.slice(0, 19)}`,
      });
    } catch (error) {
      reportFailure(error);
    } finally {
      setBusy(false);
    }
  }, [document, version, reportFailure]);

  const publish = useCallback(async () => {
    if (!document) return;
    setBusy(true);
    setNotice({ level: "info", text: "publishing…" });
    try {
      const result = await api.publish(workflowId, document, version);
      setDiagnostics([]);
      // The published document is the new baseline. Without this the next
      // edit would be rebuilt onto the previous one, and the next publish
      // would carry a version that is no longer the latest.
      setBase(document);
      setOperations([]);
      setVersion(result.version);
      setStale(false);
      setNotice({
        level: "ok",
        text: result.version === version
          ? `unchanged · already published as v${result.version}`
          : `published v${result.version} · ${result.definition_hash.slice(0, 19)}`,
      });
      api.listWorkflows()
        .then((list) => setCatalog(list.workflows ?? []))
        .catch(() => {});
    } catch (error) {
      reportFailure(error);
    } finally {
      setBusy(false);
    }
  }, [document, workflowId, version, reportFailure]);

  return (
    <div className="editor">
      <header className="bar">
        <strong>Workflow editor</strong>
        <select
          value={workflowId}
          onChange={(event) => open(event.target.value)}
          aria-label="Workflow"
        >
          <option value="">Open a workflow…</option>
          {catalog.map((item) => (
            <option key={item.workflow_id} value={item.workflow_id}>
              {item.name ?? item.workflow_id}
            </option>
          ))}
        </select>
        <span className="spacer" />
        {kinds.map((kind) => (
          <button key={kind} onClick={() => addNode(kind)} disabled={!base || busy}>
            + {kind}
          </button>
        ))}
        <button onClick={removeSelected} disabled={!selection}>
          Delete
        </button>
        <button onClick={resetLayout} disabled={!base} title="Forget the saved arrangement">
          Reset layout
        </button>
        <button
          onClick={() => setSelection(null)}
          disabled={!base}
          // Clicking empty canvas also gets here, but "deselect to reach the
          // workflow's own settings" is not a thing to have to discover —
          // and `result` lives there and is required to publish at all.
          className={base && !selection ? "active" : ""}
        >
          Workflow
        </button>
        {base ? <span className="version">v{version}</span> : null}
        <button onClick={check} disabled={!document || busy}>
          Validate
        </button>
        <button
          className="primary"
          onClick={publish}
          // Refused while stale: this tab's expected_version is behind, so
          // every publish from here would be rejected. Reopening is the fix,
          // and leaving the button live would only invite the same refusal.
          disabled={!document || busy || stale}
          title={stale ? "reopen the workflow to publish" : "Publish a new version"}
        >
          Publish
        </button>
      </header>

      {notice ? <p className={`notice ${notice.level}`}>{notice.text}</p> : null}

      <div className="workspace">
        <div className="canvas">
        <ReactFlow
          nodes={drawn}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          isValidConnection={isValidConnection}
          onNodeClick={(_event, node) => setSelection({ kind: "node", item: node })}
          onEdgeClick={(_event, edge) => setSelection({ kind: "edge", item: edge })}
          onInit={setFlow}
          onPaneClick={() => setSelection(null)}
          // React Flow themes its own chrome — the minimap, the controls, the
          // background and the edge strokes — from this. Left unset it stays
          // light, which is how a white minimap ended up sitting on a dark
          // canvas: the defaults were being used, just not the one that makes
          // them follow the viewer.
          colorMode="system"
          // Both keys, not React Flow's Backspace alone: Delete is what a
          // Windows or Linux keyboard offers for this, and a key that does
          // nothing reads as a canvas that will not let go of anything.
          deleteKeyCode={["Backspace", "Delete"]}
          // `maxZoom: 1` because fitting must never magnify. It is also what
          // React Flow does when it fits before the nodes have been measured:
          // zero-sized bounds send it to the 2x default, which is how a
          // two-node graph ended up rendered twice its size and clipped.
          fitViewOptions={FIT_VIEW}
          fitView
        >
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
        </div>
        {base && live ? (
          <Inspector
            selection={live}
            document={document}
            onChange={patchSelected}
            kinds={kinds}
            handlers={handlers}
            onPorts={setPorts}
            onRemovePort={dropPort}
            onKind={setKind}
          />
        ) : null}
        {base && !live ? (
          // Nothing selected means the workflow itself: entry, terminals,
          // result and policies belong to the document, not to any one node.
          <WorkflowPanel document={document} onChange={commit} />
        ) : null}
      </div>

      {diagnostics.length ? (
        <ul className="diagnostics">
          {diagnostics.map((item, index) => (
            <li key={index}>
              <code>{item.code}</code> {item.message}
              {item.path ? <span className="path"> {item.path}</span> : null}
              {item.hint ? <em> — {item.hint}</em> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
