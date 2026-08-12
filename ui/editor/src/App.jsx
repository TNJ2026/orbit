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
import Inspector from "./Inspector.jsx";
import WorkflowPanel from "./WorkflowPanel.jsx";
import WorkflowNode from "./WorkflowNode.jsx";

const nodeTypes = { workflow: WorkflowNode };

export default function App() {
  const [contract, setContract] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [handlers, setHandlers] = useState([]);
  const [workflowId, setWorkflowId] = useState("");
  const [base, setBase] = useState(null);
  const [version, setVersion] = useState(0);
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [notice, setNotice] = useState(null);
  const [diagnostics, setDiagnostics] = useState([]);
  const [selection, setSelection] = useState(null);
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
      const document = JSON.parse(detail.source);
      // The author's own arrangement, where there is one. Nodes without a
      // stored position fall back to the computed layout, so a workflow that
      // grew since it was last arranged still draws sensibly.
      const graph = toGraph(document, readLayout(globalThis.localStorage, id));
      setBase(document);
      setVersion(detail.latest_version ?? detail.version ?? 0);
      setWorkflowId(id);
      setNodes(graph.nodes);
      setEdges(graph.edges);
      setStale(false);
      setSelection(null);
    } catch (error) {
      setNotice({ level: "error", text: error.message });
    }
  }, []);

  const onNodesChange = useCallback(
    (changes) => setNodes((current) => applyNodeChanges(changes, current)),
    [],
  );
  const onEdgesChange = useCallback(
    (changes) => setEdges((current) => applyEdgeChanges(changes, current)),
    [],
  );

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
      setEdges((current) =>
        addEdge(
          {
            ...connection,
            id: freshId("edge", current.map((edge) => edge.id)),
            data: { route: "success", dsl: {} },
          },
          current,
        ),
      );
    },
    [nodes],
  );

  const renameNode = useCallback((id, label) => {
    setNodes((current) =>
      current.map((node) =>
        node.id === id ? { ...node, data: { ...node.data, label } } : node,
      ),
    );
  }, []);

  /** Merge a patch into the stored DSL object of whatever is selected.
   *
   * `undefined` in the patch deletes the field rather than writing a null,
   * because the DSL schema rejects a null where it expects an absent one.
   */
  const patchSelected = useCallback((patch) => {
    if (!selection) return;
    const merge = (item) => {
      const dsl = { ...(item.data?.dsl ?? {}) };
      for (const [key, value] of Object.entries(patch)) {
        if (value === undefined) delete dsl[key];
        else dsl[key] = value;
      }
      // The card draws from `data.handler`, so a binding change has to reach
      // it as well as the stored document it is rebuilt from.
      const data = { ...item.data, dsl };
      if ("handler" in patch) data.handler = patch.handler ?? null;
      return { ...item, data };
    };
    const apply = (current) =>
      current.map((item) => (item.id === selection.item.id ? merge(item) : item));
    if (selection.kind === "edge") setEdges(apply);
    else setNodes(apply);
  }, [selection]);

  /** Replace one side's ports on the selected node. */
  const setPorts = useCallback((side, ports) => {
    if (selection?.kind !== "node") return;
    setNodes((current) =>
      current.map((node) =>
        node.id === selection.item.id
          ? { ...node, data: { ...node.data, [side]: ports } }
          : node,
      ),
    );
  }, [selection]);

  /** Drop a port, and with it every edge that named it.
   *
   * Computed once from the current graph rather than inside the state
   * updaters: nesting them would run this side effect during a render, and
   * the edges have to be decided from the same snapshot as the nodes.
   */
  const dropPort = useCallback((side, portId) => {
    if (selection?.kind !== "node") return;
    const result = removePort({ nodes, edges }, selection.item.id, side, portId);
    setNodes(result.nodes);
    setEdges(result.edges);
    setNotice(
      result.removedEdges.length
        ? {
          level: "info",
          text: `removed ${result.removedEdges.length} edge(s) bound to ${portId}`,
        }
        : null,
    );
  }, [selection, nodes, edges]);

  /** Change a node's kind, dropping a handler the new kind cannot use.
   *
   * A manifest declares which kinds it serves, so a binding that survived the
   * switch would be one the compiler refuses — and the author would have to
   * read a diagnostic to learn what the switch did.
   */
  const setKind = useCallback((kind) => {
    if (selection?.kind !== "node") return;
    setNodes((current) =>
      current.map((node) => {
        if (node.id !== selection.item.id) return node;
        const dsl = { ...(node.data.dsl ?? {}), kind };
        const bound = dsl.handler
          && handlersForKind(handlers, kind).some(
            (item) => item.name === dsl.handler.name && item.version === dsl.handler.version,
          );
        if (!bound) delete dsl.handler;
        return { ...node, data: { ...node.data, kind, handler: dsl.handler ?? null, dsl } };
      }),
    );
  }, [selection, handlers]);

  /** Remove whatever is selected, edges bound to a node included.
   *
   * Backspace and Delete do this too, but a keyboard shortcut is not a thing
   * to have to know — and on a canvas there is nothing to right-click either.
   */
  const removeSelected = useCallback(() => {
    if (!selection) return;
    const id = selection.item.id;
    if (selection.kind === "edge") {
      setEdges((current) => current.filter((edge) => edge.id !== id));
    } else {
      setNodes((current) => current.filter((node) => node.id !== id));
      setEdges((current) =>
        current.filter((edge) => edge.source !== id && edge.target !== id),
      );
    }
    setSelection(null);
  }, [selection]);

  /** Forget this arrangement and go back to the computed one. */
  const resetLayout = useCallback(() => {
    if (!base || !workflowId) return;
    clearLayout(globalThis.localStorage, workflowId);
    setNodes(toGraph(base, {}).nodes.map((node) => {
      const current = nodes.find((item) => item.id === node.id);
      // Keep everything the canvas has edited; only the position goes back.
      return current ? { ...current, position: node.position } : node;
    }));
    setNotice({ level: "info", text: "layout reset" });
  }, [base, workflowId, nodes]);

  const addNode = useCallback(
    (kind) => {
      setNodes((current) => {
        const id = freshId(kind, current.map((node) => node.id));
        return [
          ...current,
          {
            id,
            type: "workflow",
            position: { x: 60, y: 60 + current.length * 30 },
            data: { kind, label: id, inputs: [], outputs: [], dsl: {} },
          },
        ];
      });
    },
    [],
  );

  // xyflow hands a custom node only its `data`, so the rename callback rides
  // there. Attached at render rather than stored on the node, which keeps it
  // out of what `toDocument` rebuilds from.
  const drawn = useMemo(
    () => nodes.map((node) => ({
      ...node, data: { ...node.data, onLabelChange: renameNode },
    })),
    [nodes, renameNode],
  );

  useEffect(() => {
    if (!workflowId || !nodes.length) return;
    writeLayout(
      globalThis.localStorage,
      workflowId,
      toPositions(nodes),
      nodes.map((node) => node.id),
    );
  }, [workflowId, nodes]);

  const document = useMemo(
    () => (base ? toDocument(base, { nodes, edges }) : null),
    [base, nodes, edges],
  );

  // Re-read from the live arrays: the item captured when the author clicked is
  // a snapshot, and the panel has to show what their own edits just produced.
  const live = useMemo(() => {
    if (!selection) return null;
    const source = selection.kind === "edge" ? edges : nodes;
    const item = source.find((entry) => entry.id === selection.item.id);
    return item ? { kind: selection.kind, item } : null;
  }, [selection, nodes, edges]);

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
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          isValidConnection={isValidConnection}
          onNodeClick={(_event, node) => setSelection({ kind: "node", item: node })}
          onEdgeClick={(_event, edge) => setSelection({ kind: "edge", item: edge })}
          onPaneClick={() => setSelection(null)}
          // Both keys, not React Flow's Backspace alone: Delete is what a
          // Windows or Linux keyboard offers for this, and a key that does
          // nothing reads as a canvas that will not let go of anything.
          deleteKeyCode={["Backspace", "Delete"]}
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
          <WorkflowPanel document={document} onChange={setBase} />
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
