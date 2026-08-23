/** The Workflows the input menu offers, shared with the panel that fetches them.
 *
 * Module state rather than React state because the two readers live on
 * different sides of the plugin: the panel is a component, the trigger source
 * is registered at apply time and outside any tree. Nothing here fetches — the
 * panel already asks the Host every poll, and this remembers the last answer
 * for the menu to read synchronously.
 */

import type { WorkflowSummary } from '../types.js'

/** Beyond this the menu stops being a shortlist and becomes a catalogue. */
export const MENU_LIMIT = 12

let known: readonly WorkflowSummary[] = []

export function rememberWorkflows(workflows: readonly WorkflowSummary[]): void {
  known = workflows
}

/**
 * The Workflows worth offering for what has been typed after the command.
 *
 * Matched on the name alone. The id is in the row for the Agent to act on, but
 * nobody searches by it — a person reaching for one of these is remembering
 * what it was called, and matching ids too would surface rows whose visible
 * text has nothing to do with what they typed.
 */
export function matchWorkflows(query: string): readonly WorkflowSummary[] {
  const needle = query.trim().toLowerCase()
  const hits = needle
    ? known.filter(item => (item.name || '').toLowerCase().includes(needle))
    : known
  return hits.slice(0, MENU_LIMIT)
}
