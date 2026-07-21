/* Pure Workflow Editor transforms.
 *
 * The browser never compiles or publishes a workflow. These helpers only turn
 * structured form values into a candidate DSL document and filter the
 * server-projected Handler Catalog by exact port compatibility. The candidate
 * still has to pass the Runtime compiler before it can be published.
 */

export function parseWorkflowSource(source) {
  const document = JSON.parse(source);
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new TypeError("workflow source must be a JSON object");
  }
  if (!document.metadata || !Array.isArray(document.nodes)) {
    throw new TypeError("workflow source has no editable metadata or nodes");
  }
  return document;
}

export function formatWorkflowSource(document) {
  return `${JSON.stringify(document, null, 2)}\n`;
}

function portContract(ports = []) {
  return Object.fromEntries(ports.map((port) => [port.id, port.schema_id]));
}

function sameContract(left, right) {
  const a = Object.entries(left).sort(([x], [y]) => x.localeCompare(y));
  const b = Object.entries(right).sort(([x], [y]) => x.localeCompare(y));
  return JSON.stringify(a) === JSON.stringify(b);
}

export function compatibleHandlers(node, handlers = []) {
  const inputs = portContract(node.inputs);
  const outputs = portContract(node.outputs);
  return handlers.filter((handler) =>
    (handler.node_kinds || []).includes(node.kind)
    && sameContract(handler.inputs || {}, inputs)
    && sameContract(handler.outputs || {}, outputs));
}

export function replaceMetadata(document, values) {
  return {
    ...document,
    metadata: {
      ...document.metadata,
      name: values.name,
      description: values.description,
      labels: values.labels,
    },
  };
}

export function replaceNode(document, index, node) {
  const prior = index === null || index === undefined ? null : document.nodes[index];
  const nodes = replaceAt(document.nodes, index, node);
  const renamed = prior && prior.id !== node.id;
  const rename = (value) => renamed && value === prior.id ? node.id : value;
  let terminals = (document.terminals || []).map(rename);
  if (node.kind === "terminal" && !terminals.includes(node.id)) terminals.push(node.id);
  if (node.kind !== "terminal") terminals = terminals.filter((value) => value !== node.id);
  return {
    ...document,
    nodes,
    entry: (document.entry || []).map(rename),
    terminals,
    edges: (document.edges || []).map((edge) => ({
      ...edge,
      from: { ...edge.from, node: rename(edge.from.node) },
      to: { ...edge.to, node: rename(edge.to.node) },
    })),
  };
}

function replaceAt(values, index, value) {
  const next = [...(values || [])];
  if (index === null || index === undefined) next.push(value);
  else next[index] = value;
  return next;
}

export function removeNode(document, index) {
  const removed = document.nodes[index]?.id;
  return {
    ...document,
    nodes: document.nodes.filter((_, item) => item !== index),
    entry: (document.entry || []).filter((value) => value !== removed),
    terminals: (document.terminals || []).filter((value) => value !== removed),
    edges: (document.edges || []).filter(
      (edge) => edge.from.node !== removed && edge.to.node !== removed,
    ),
  };
}

export function replaceEdge(document, index, edge) {
  return { ...document, edges: replaceAt(document.edges, index, edge) };
}

export function removeEdge(document, index) {
  return { ...document, edges: (document.edges || []).filter((_, item) => item !== index) };
}

export function replacePolicy(document, index, policy) {
  const prior = index === null || index === undefined ? null : (document.policies || [])[index];
  const renamed = prior && prior.id !== policy.id;
  return {
    ...document,
    policies: replaceAt(document.policies, index, policy),
    nodes: renamed ? document.nodes.map((node) => ({
      ...node,
      policies: (node.policies || []).map((value) => value === prior.id ? policy.id : value),
    })) : document.nodes,
    edges: renamed ? (document.edges || []).map((edge) => edge.policy === prior.id
      ? { ...edge, policy: policy.id } : edge) : document.edges,
  };
}

export function removePolicy(document, index) {
  const removed = (document.policies || [])[index]?.id;
  return {
    ...document,
    policies: (document.policies || []).filter((_, item) => item !== index),
    nodes: document.nodes.map((node) => ({
      ...node,
      policies: (node.policies || []).filter((policyId) => policyId !== removed),
    })),
    edges: (document.edges || []).map((edge) => edge.policy === removed
      ? Object.fromEntries(Object.entries(edge).filter(([key]) => key !== "policy"))
      : edge),
  };
}

export function diagnosticTarget(diagnostic) {
  const path = diagnostic.json_path || diagnostic.path || "$";
  const match = /^\$\.(nodes|edges|policies)(?:\[(\d+)\])?/.exec(path);
  const range = diagnostic.source_range?.start || diagnostic.source_range;
  return {
    path,
    pane: match ? match[1] : "source",
    index: match?.[2] === undefined ? null : Number(match[2]),
    line: Number(range?.line || range?.start_line || 1),
    column: Number(range?.column || range?.start_column || 1),
  };
}
