/**
 * Where an author's arrangement of a graph is kept.
 *
 * Not in the definition. `definition_hash` is a content hash over the IR and
 * it is the workflow's version identity, so a coordinate in there would mean
 * that nudging a node publishes a new version — and that two people who
 * arranged the same workflow differently would hold two different workflows.
 * Positions are how a drawing is read, not what it means.
 *
 * So they live beside it, in the browser, keyed by workflow. That is a real
 * limitation and worth stating plainly: an arrangement is this person's, on
 * this machine. A shared one would need a store on the server, which is a
 * different thing to build and not one to fake with a cache.
 *
 * Storage is injected rather than reached for, so this is testable without a
 * DOM — and because `localStorage` is not always there to be reached for: it
 * throws on access in some privacy modes and on write when a quota is full,
 * and losing a layout must never be the reason an editor fails to open.
 */

const PREFIX = "orbit.editor.layout.";

export const layoutKey = (workflowId) => `${PREFIX}${workflowId}`;

/** Positions for nodes that still exist, and only those.
 *
 * A workflow outlives the nodes drawn in it, so without this the store grows
 * a coordinate for every node anyone ever deleted.
 */
export function pruneLayout(positions, nodeIds) {
  const present = new Set(nodeIds);
  return Object.fromEntries(
    Object.entries(positions ?? {}).filter(([id]) => present.has(id)),
  );
}

/** Whether a stored value is a layout and not something else's key collision. */
function usable(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return Object.values(value).every(
    (point) =>
      point && typeof point === "object"
      && Number.isFinite(point.x) && Number.isFinite(point.y),
  );
}

/** The arrangement stored for this workflow, or none.
 *
 * Anything unreadable is treated as none: a layout is a convenience, and
 * refusing to open a workflow because its saved positions were corrupt would
 * trade a small loss for a total one.
 */
export function readLayout(storage, workflowId) {
  try {
    const raw = storage?.getItem(layoutKey(workflowId));
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return usable(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Store this arrangement, or give up quietly.
 *
 * Returns whether it was written, for a caller that wants to know; nothing in
 * the editor should stop working because it was not.
 */
export function writeLayout(storage, workflowId, positions, nodeIds) {
  try {
    const pruned = pruneLayout(positions, nodeIds);
    if (Object.keys(pruned).length === 0) {
      storage?.removeItem(layoutKey(workflowId));
      return true;
    }
    storage?.setItem(layoutKey(workflowId), JSON.stringify(pruned));
    return true;
  } catch {
    return false;
  }
}

export function clearLayout(storage, workflowId) {
  try {
    storage?.removeItem(layoutKey(workflowId));
    return true;
  } catch {
    return false;
  }
}
