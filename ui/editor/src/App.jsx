import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, addEdge,
  applyEdgeChanges, applyNodeChanges,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import * as api from "./api.mjs";
import {
  NODE_KINDS, connectionProblem, freshId, toDocument, toGraph,
} from "./dsl-graph.mjs";
import WorkflowNode from "./WorkflowNode.jsx";

const nodeTypes = { workflow: WorkflowNode };

export default function App() {
  const [contract, setContract] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [workflowId, setWorkflowId] = useState("");
  const [base, setBase] = useState(null);
  const [version, setVersion] = useState(0);
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [notice, setNotice] = useState(null);
  const [diagnostics, setDiagnostics] = useState([]);

  useEffect(() => {
    Promise.all([api.authoringSchema(), api.listWorkflows()])
      .then(([schema, list]) => {
        setContract(schema);
        setCatalog(list.workflows ?? []);
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
      const graph = toGraph(document);
      setBase(document);
      setVersion(detail.latest_version ?? detail.version ?? 0);
      setWorkflowId(id);
      setNodes(graph.nodes);
      setEdges(graph.edges);
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

  const document = useMemo(
    () => (base ? toDocument(base, { nodes, edges }) : null),
    [base, nodes, edges],
  );

  const check = useCallback(async () => {
    if (!document) return;
    setNotice({ level: "info", text: "validating…" });
    try {
      const result = await api.validate(document, version);
      setDiagnostics([]);
      setNotice({
        level: "ok",
        text: `compiles · ${result.node_count} nodes · ${result.definition_hash.slice(0, 12)}`,
      });
    } catch (error) {
      // The compiler answers with diagnostics; showing them is the entire
      // value of asking it, so they are rendered rather than collapsed into
      // "invalid".
      let findings = [];
      try {
        findings = JSON.parse(error.message)?.diagnostics ?? [];
      } catch {
        findings = [];
      }
      setDiagnostics(findings);
      setNotice({
        level: "error",
        text: findings.length ? `${findings.length} problem(s)` : error.message,
      });
    }
  }, [document, version]);

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
          <button key={kind} onClick={() => addNode(kind)} disabled={!base}>
            + {kind}
          </button>
        ))}
        <button className="primary" onClick={check} disabled={!document}>
          Validate
        </button>
      </header>

      {notice ? <p className={`notice ${notice.level}`}>{notice.text}</p> : null}

      <div className="canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          isValidConnection={isValidConnection}
          fitView
        >
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
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
