/**
 * The parts of a definition that are not the drawing.
 *
 * `entry`, `terminals` and `result` name nodes but are not edges, so no amount
 * of canvas gets at them — and `result` is required at DSL 1.3, so a workflow
 * that loses it cannot be published at all. Policies are the same shape of
 * problem from the other side: an edge can *reference* one, so without a way
 * to create one a bounded loop is undrawable.
 *
 * Every function here returns a new document and never mutates its argument,
 * which is what lets the caller hold the previous one and compare.
 */

const POLICY_KINDS = ["retry", "loop", "rework", "join", "completion"];

/** Policy kinds the LangGraph runtime compiles, with the config each needs.
 *
 * The required keys are the ones the compiler reads and would fail without —
 * `max_iterations` for a loop, `max_attempts` for a retry. Offering them
 * pre-filled is the difference between a policy an author can use and a
 * policy that compiles to a diagnostic.
 */
export const POLICY_TEMPLATES = {
  retry: { max_attempts: 2 },
  loop: { max_iterations: 3 },
  rework: { max_generations: 2 },
  join: { mode: "all" },
  completion: { required_terminal_count: 1 },
};

export { POLICY_KINDS };

const withoutEmpty = (document, key) => {
  const next = { ...document };
  if (Array.isArray(next[key]) && next[key].length === 0) delete next[key];
  return next;
};

export function setMetadata(document, patch) {
  const metadata = { ...document.metadata, ...patch };
  for (const [key, value] of Object.entries(patch)) {
    // A blank description or name is absent, not an empty string: the schema
    // gives `description` a default and requires `name` to be non-empty.
    if (typeof value === "string" && !value.trim() && key !== "id") {
      delete metadata[key];
    }
  }
  return { ...document, metadata };
}

/** Add or remove a node from `entry` or `terminals`. */
export function toggleMembership(document, key, nodeId) {
  const current = document[key] ?? [];
  const next = current.includes(nodeId)
    ? current.filter((id) => id !== nodeId)
    : [...current, nodeId];
  return withoutEmpty({ ...document, [key]: next }, key);
}

/** Every (node, output port) a result could name. */
export function resultOptions(document) {
  const options = [];
  for (const node of document?.nodes ?? []) {
    for (const port of node.outputs ?? []) {
      options.push({ node: node.id, port: port.id });
    }
  }
  return options;
}

export function setResult(document, endpoint) {
  if (!endpoint) {
    const next = { ...document };
    delete next.result;
    return next;
  }
  return { ...document, result: { node: endpoint.node, port: endpoint.port } };
}

/** A policy id that is readable and free, derived from the kind. */
export function freshPolicyId(document, kind) {
  const taken = new Set((document.policies ?? []).map((policy) => policy.id));
  if (!taken.has(kind)) return kind;
  for (let index = 2; ; index += 1) {
    const candidate = `${kind}_${index}`;
    if (!taken.has(candidate)) return candidate;
  }
}

export function addPolicy(document, kind) {
  if (!POLICY_KINDS.includes(kind)) throw new Error(`unknown policy kind: ${kind}`);
  const policy = {
    id: freshPolicyId(document, kind),
    kind,
    config: { ...POLICY_TEMPLATES[kind] },
  };
  return { ...document, policies: [...(document.policies ?? []), policy] };
}

export function updatePolicy(document, id, patch) {
  return {
    ...document,
    policies: (document.policies ?? []).map((policy) =>
      policy.id === id ? { ...policy, ...patch } : policy,
    ),
  };
}

/** Remove a policy, and every reference to it.
 *
 * A dangling `policies: ["gone"]` on a node, or `policy: "gone"` on an edge,
 * is a document the compiler rejects — so removal has to reach the references
 * too rather than leave the author to find them.
 */
export function removePolicy(document, id) {
  const next = withoutEmpty(
    {
      ...document,
      policies: (document.policies ?? []).filter((policy) => policy.id !== id),
    },
    "policies",
  );
  next.nodes = (next.nodes ?? []).map((node) => {
    if (!node.policies?.includes(id)) return node;
    const remaining = node.policies.filter((item) => item !== id);
    const copy = { ...node };
    if (remaining.length) copy.policies = remaining;
    else delete copy.policies;
    return copy;
  });
  next.edges = (next.edges ?? []).map((edge) => {
    if (edge.policy !== id) return edge;
    const copy = { ...edge };
    delete copy.policy;
    return copy;
  });
  return next;
}

/** Which nodes and edges still name a policy, for a confirmation. */
export function policyReferences(document, id) {
  return {
    nodes: (document.nodes ?? [])
      .filter((node) => node.policies?.includes(id))
      .map((node) => node.id),
    edges: (document.edges ?? [])
      .filter((edge) => edge.policy === id)
      .map((edge) => edge.id),
  };
}

export function setInputs(document, inputs) {
  return withoutEmpty({ ...document, inputs }, "inputs");
}

/** Add a port to a node's inputs or outputs, or say why it cannot be added. */
export function addPort(ports, port) {
  if (!port.id?.trim()) return { problem: "a port needs an id" };
  if (!port.schema_id?.trim()) return { problem: "a port needs a schema_id" };
  if ((ports ?? []).some((item) => item.id === port.id.trim())) {
    return { problem: `this node already has a port named ${port.id.trim()}` };
  }
  return {
    value: [...(ports ?? []), { id: port.id.trim(), schema_id: port.schema_id.trim() }],
  };
}

/** Remove a port, and every edge bound to it.
 *
 * An edge whose `from.port` or `to.port` no longer exists is a document that
 * cannot compile, so the edges go with the port. Leaving them would turn one
 * deliberate deletion into a diagnostic the author has to chase.
 */
export function removePort(graph, nodeId, side, portId) {
  const nodes = graph.nodes.map((node) => {
    if (node.id !== nodeId) return node;
    const remaining = (node.data[side] ?? []).filter((port) => port.id !== portId);
    return { ...node, data: { ...node.data, [side]: remaining } };
  });
  const bound = (edge) =>
    side === "outputs"
      ? edge.source === nodeId && edge.sourceHandle === portId
      : edge.target === nodeId && edge.targetHandle === portId;
  return {
    nodes,
    edges: graph.edges.filter((edge) => !bound(edge)),
    removedEdges: graph.edges.filter(bound).map((edge) => edge.id),
  };
}

/** The registered handlers a node of this kind may bind to.
 *
 * A manifest declares which node kinds it serves, so offering the whole
 * catalog would invite a binding the compiler refuses. Sorted, because the
 * order a registry happens to seal in is not an order to read.
 */
export function handlersForKind(catalog, kind) {
  return (catalog ?? [])
    .filter((handler) => (handler.node_kinds ?? []).includes(kind))
    .sort((left, right) =>
      left.name.localeCompare(right.name) || left.version.localeCompare(right.version),
    );
}

/** The ports a manifest declares, in the array form a node must write them.
 *
 * The manifest states them as id-to-schema maps and a node declares them as
 * arrays. Asking an author to perform that conversion by hand is asking them
 * to copy data they were already given, and the compiler then rejects the copy
 * for the smallest divergence — the same reason the generator does this for a
 * model rather than describing it in a prompt.
 */
export function portsFromManifest(manifest) {
  const convert = (declared) =>
    Object.entries(declared ?? {})
      .map(([id, schema_id]) => ({ id, schema_id }))
      .sort((left, right) => left.id.localeCompare(right.id));
  return { inputs: convert(manifest?.inputs), outputs: convert(manifest?.outputs) };
}

/** The `{name, version}` a node stores for a chosen handler.
 *
 * Never the fingerprint, even though the catalog reports one: the binding an
 * IR node carries is resolved by `analyze_dsl` from the registry. A document
 * that named its own fingerprint would be choosing what it binds to.
 */
export function handlerRef(manifest) {
  return manifest ? { name: manifest.name, version: manifest.version } : undefined;
}
