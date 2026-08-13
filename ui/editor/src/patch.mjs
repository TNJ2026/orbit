/**
 * The same operations the server applies, applied in the browser.
 *
 * A second implementation of one semantics is the drift this codebase has
 * already been bitten by once, so this is not a port written from the
 * description — it is held to the Python applier by a shared corpus that runs
 * through both and requires the same document out. `tests/test_patch_parity.py`
 * is where that happens; anything added here belongs in that corpus first.
 *
 * Why the editor speaks it at all: an author dragging a node and an Agent
 * adding one then produce the same kind of thing. A change becomes a value
 * that can be shown before it is accepted, listed, undone, or sent — none of
 * which is possible when an edit is "the document, but different now".
 *
 * Positions are not operations. They are how a drawing is read rather than
 * what it means, and they stay in the layout store where nothing about the
 * definition can reach them.
 */

/** The patch was computed against a different version than it was given.
 *
 * Not a `PatchError`, because it is not about an operation. Every operation
 * may be individually applicable and the result still be wrong: that is
 * exactly the mistake `base_version` exists to catch, and the one that cannot
 * be seen by looking at what came out.
 */
class PatchBaseMismatch extends Error {
  constructor(declared, actual) {
    super(`patch was written against v${declared}, but the document is v${actual}`);
    this.declared = declared;
    this.actual = actual;
  }
}

class PatchError extends Error {
  constructor(index, reason) {
    super(`operation ${index}: ${reason}`);
    this.index = index;
    this.reason = reason;
  }
}

const clone = (value) => JSON.parse(JSON.stringify(value));

const indexOf = (items, key, value) =>
  (items ?? []).findIndex((item) => item?.[key] === value);

/** Set a field, or remove it — absent and empty are not the same. */
function assign(target, key, value) {
  if (value === null || value === undefined) delete target[key];
  else target[key] = value;
}

const empty = (value) => (Array.isArray(value) ? value.length === 0 : !value)
  || (typeof value === "object" && value !== null && !Array.isArray(value)
    && Object.keys(value).length === 0);

function requireNode(document, nodeId, index) {
  const position = indexOf(document.nodes, "id", nodeId);
  if (position < 0) throw new PatchError(index, `node '${nodeId}' does not exist`);
  return position;
}

const HANDLERS = {
  add_node(document, op, index) {
    if (indexOf(document.nodes, "id", op.node.id) >= 0) {
      throw new PatchError(index, `node '${op.node.id}' already exists`);
    }
    document.nodes.push(clone(op.node));
  },

  remove_node(document, op, index) {
    document.nodes.splice(requireNode(document, op.node_id, index), 1);
    document.edges = (document.edges ?? []).filter(
      (edge) => edge.from?.node !== op.node_id && edge.to?.node !== op.node_id,
    );
    for (const key of ["entry", "terminals"]) {
      if (Array.isArray(document[key])) {
        document[key] = document[key].filter((id) => id !== op.node_id);
      }
    }
    if (document.result?.node === op.node_id) delete document.result;
  },

  set_node_label(document, op, index) {
    const node = { ...document.nodes[requireNode(document, op.node_id, index)] };
    assign(node, "label", op.label ?? null);
    document.nodes[indexOf(document.nodes, "id", op.node_id)] = node;
  },

  set_node_config(document, op, index) {
    const node = { ...document.nodes[requireNode(document, op.node_id, index)] };
    assign(node, "config", empty(op.config) ? null : op.config);
    document.nodes[indexOf(document.nodes, "id", op.node_id)] = node;
  },

  set_node_ports(document, op, index) {
    const node = { ...document.nodes[requireNode(document, op.node_id, index)] };
    assign(node, op.side, op.ports?.length ? clone(op.ports) : null);
    document.nodes[indexOf(document.nodes, "id", op.node_id)] = node;
  },

  set_node_policies(document, op, index) {
    const node = { ...document.nodes[requireNode(document, op.node_id, index)] };
    assign(node, "policies", op.policies?.length ? [...op.policies].sort() : null);
    document.nodes[indexOf(document.nodes, "id", op.node_id)] = node;
  },

  set_node_handler(document, op, index) {
    const node = { ...document.nodes[requireNode(document, op.node_id, index)] };
    assign(node, "handler", op.handler ?? null);
    document.nodes[indexOf(document.nodes, "id", op.node_id)] = node;
  },

  set_node_kind(document, op, index) {
    const node = { ...document.nodes[requireNode(document, op.node_id, index)] };
    node.kind = op.kind;
    document.nodes[indexOf(document.nodes, "id", op.node_id)] = node;
  },

  add_edge(document, op, index) {
    if (indexOf(document.edges, "id", op.edge.id) >= 0) {
      throw new PatchError(index, `edge '${op.edge.id}' already exists`);
    }
    document.edges.push(clone(op.edge));
  },

  remove_edge(document, op, index) {
    const position = indexOf(document.edges, "id", op.edge_id);
    if (position < 0) throw new PatchError(index, `edge '${op.edge_id}' does not exist`);
    document.edges.splice(position, 1);
  },

  set_edge(document, op, index) {
    const position = indexOf(document.edges, "id", op.edge.id);
    if (position < 0) throw new PatchError(index, `edge '${op.edge.id}' does not exist`);
    document.edges[position] = clone(op.edge);
  },

  add_policy(document, op, index) {
    document.policies = document.policies ?? [];
    if (indexOf(document.policies, "id", op.policy.id) >= 0) {
      throw new PatchError(index, `policy '${op.policy.id}' already exists`);
    }
    document.policies.push(clone(op.policy));
  },

  set_policy_config(document, op, index) {
    const policies = document.policies ?? [];
    const position = indexOf(policies, "id", op.policy_id);
    if (position < 0) {
      throw new PatchError(index, `policy '${op.policy_id}' does not exist`);
    }
    policies[position] = { ...policies[position], config: clone(op.config) };
  },

  remove_policy(document, op, index) {
    const policies = document.policies ?? [];
    const position = indexOf(policies, "id", op.policy_id);
    if (position < 0) {
      throw new PatchError(index, `policy '${op.policy_id}' does not exist`);
    }
    policies.splice(position, 1);
    if (!policies.length) delete document.policies;
    for (const node of document.nodes ?? []) {
      if (Array.isArray(node.policies) && node.policies.includes(op.policy_id)) {
        const remaining = node.policies.filter((item) => item !== op.policy_id);
        assign(node, "policies", remaining.length ? remaining : null);
      }
    }
    for (const edge of document.edges ?? []) {
      if (edge.policy === op.policy_id) delete edge.policy;
    }
  },

  set_result(document, op) {
    document.result = clone(op.result);
  },

  set_entry(document, op, index) {
    membership(document, "entry", op.entry, index);
  },

  set_terminals(document, op, index) {
    membership(document, "terminals", op.terminals, index);
  },

  set_metadata(document, op) {
    const metadata = { ...(document.metadata ?? {}) };
    // The id is never an operand: publishing an edit onto a different
    // aggregate is not an edit.
    if (op.name !== undefined && op.name !== null) {
      assign(metadata, "name", op.name.trim() || null);
    }
    if (op.description !== undefined && op.description !== null) {
      assign(metadata, "description", op.description.trim() || null);
    }
    document.metadata = metadata;
  },

  set_inputs(document, op) {
    assign(document, "inputs", op.inputs?.length ? clone(op.inputs) : null);
  },
};

function membership(document, key, named, index) {
  const missing = (named ?? []).filter(
    (id) => indexOf(document.nodes, "id", id) < 0,
  );
  if (missing.length) {
    throw new PatchError(index, `${key} names unknown node(s): ${missing.join(", ")}`);
  }
  document[key] = [...named];
}

/** The document these operations describe, applied in order.
 *
 * The argument is never mutated, which is what lets a caller hold what it had
 * and show the change before accepting it.
 *
 * `baseVersion` is the version `document` actually is. Given one, the patch's
 * own claim must match it. A caller that does not know the version passes
 * none: a check against a number nobody has is not a check.
 */
export function applyPatch(document, operations, baseVersion = null) {
  const declared = Array.isArray(operations) ? null : operations?.base_version;
  if (baseVersion !== null && declared !== undefined && declared !== baseVersion) {
    throw new PatchBaseMismatch(declared, baseVersion);
  }
  const result = clone(document);
  result.nodes = result.nodes ?? [];
  result.edges = result.edges ?? [];
  const list = Array.isArray(operations)
    ? operations : (operations?.operations ?? []);
  list.forEach((op, index) => {
    const handler = HANDLERS[op?.op];
    if (!handler) throw new PatchError(index, `unsupported operation '${op?.op}'`);
    handler(result, op, index);
  });
  return result;
}

export { PatchBaseMismatch, PatchError };
