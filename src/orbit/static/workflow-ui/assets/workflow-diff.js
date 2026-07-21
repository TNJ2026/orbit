function stableValue(value) {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, stableValue(value[key])]),
    );
  }
  return value;
}

function equalValue(left, right) {
  return JSON.stringify(stableValue(left)) === JSON.stringify(stableValue(right));
}

function changedFields(before, after, ignored = new Set()) {
  return [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .filter((field) => !ignored.has(field) && !equalValue(before[field], after[field]))
    .sort();
}

function edgeIdentity(edge) {
  if (edge.id) return String(edge.id);
  const from = edge.from || {};
  const to = edge.to || {};
  return `${from.node || "?"}:${from.port || "?"}->${to.node || "?"}:${to.port || "?"}`;
}

function byIdentity(items, identity) {
  return new Map((Array.isArray(items) ? items : []).map((item) => [identity(item), item]));
}

/** Compare two compiler-facing Workflow DSL documents by stable graph identity.
 *
 * Object-key order and source formatting are intentionally ignored. A null
 * result means one of the sources is not JSON and the caller should retain its
 * raw-source comparison fallback.
 */
export function semanticWorkflowDiff(beforeSource, afterSource) {
  let before;
  let after;
  try {
    before = JSON.parse(beforeSource);
    after = JSON.parse(afterSource);
  } catch {
    return null;
  }
  if (!before || !after || typeof before !== "object" || typeof after !== "object") {
    return null;
  }

  const beforeNodes = byIdentity(before.nodes, (node) => String(node.id));
  const afterNodes = byIdentity(after.nodes, (node) => String(node.id));
  const beforeEdges = byIdentity(before.edges, edgeIdentity);
  const afterEdges = byIdentity(after.edges, edgeIdentity);

  const addedNodes = [...afterNodes.keys()].filter((id) => !beforeNodes.has(id)).sort();
  const removedNodes = [...beforeNodes.keys()].filter((id) => !afterNodes.has(id)).sort();
  const changedNodes = [...beforeNodes.keys()]
    .filter((id) => afterNodes.has(id))
    .map((id) => ({
      id,
      fields: changedFields(beforeNodes.get(id), afterNodes.get(id), new Set(["id"])),
    }))
    .filter((item) => item.fields.length)
    .sort((left, right) => left.id.localeCompare(right.id));

  const addedEdges = [...afterEdges.keys()].filter((id) => !beforeEdges.has(id)).sort();
  const removedEdges = [...beforeEdges.keys()].filter((id) => !afterEdges.has(id)).sort();
  const changedEdges = [...beforeEdges.keys()]
    .filter((id) => afterEdges.has(id))
    .map((id) => ({
      id,
      fields: changedFields(beforeEdges.get(id), afterEdges.get(id), new Set(["id"])),
    }))
    .filter((item) => item.fields.length)
    .sort((left, right) => left.id.localeCompare(right.id));
  const workflowFields = changedFields(before, after, new Set(["nodes", "edges"]));

  return {
    addedNodes,
    removedNodes,
    changedNodes,
    addedEdges,
    removedEdges,
    changedEdges,
    workflowFields,
    changeCount: addedNodes.length + removedNodes.length + changedNodes.length
      + addedEdges.length + removedEdges.length + changedEdges.length
      + workflowFields.length,
  };
}
