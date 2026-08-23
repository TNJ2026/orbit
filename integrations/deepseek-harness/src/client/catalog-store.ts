/** The Workflows the input menu can offer, shared with the panel that fetches them.
 *
 * Module state rather than React state because the two readers live on
 * different sides of the plugin: the panel is a component and the trigger
 * source is registered at apply time, outside any tree. The panel already asks
 * the Host for this list every poll, so nothing here fetches anything — it only
 * remembers the last answer for the menu to read synchronously.
 */

import type { WorkflowSummary } from '../types.js'

/** Beyond this the menu stops being a shortlist and starts being a catalogue. */
export const MENU_LIMIT = 10

let known: readonly WorkflowSummary[] = []

export function rememberWorkflows(workflows: readonly WorkflowSummary[]): void {
  known = workflows
}

export function knownWorkflows(): readonly WorkflowSummary[] {
  return known
}

/**
 * The Workflows worth offering for what has been typed so far.
 *
 * Matched on the name and the id together: a person reaching for one of these
 * knows it by whichever of the two they last saw, and the menu should not make
 * them guess which.
 */
export function matchWorkflows(query: string): readonly WorkflowSummary[] {
  const needle = query.trim().toLowerCase()
  const hits = needle
    ? known.filter(item => `${item.name} ${item.workflow_id}`.toLowerCase().includes(needle))
    : known
  return hits.slice(0, MENU_LIMIT)
}
