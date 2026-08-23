/** What the panel shows, and how often it asks.
 *
 * Separated from the view because the interesting decisions here are not
 * visual: which Runs count as live, how fast to ask while any of them is, and
 * how to stop asking when none is. React only renders the answer.
 */

import type { OutputChunk, RunDto, StepSummary } from '../types.js'

/** Cadence while a Run is moving. */
export const ORBIT_POLL_MS = 2_000
/** Cadence while nothing is. A resident panel costs nothing when idle. */
export const ORBIT_IDLE_MS = 15_000

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'unknown'])

export interface OrbitRunRow {
  readonly runId: string
  readonly goal: string
  readonly workflow: string
  readonly status: string
  readonly live: boolean
  readonly revision: number
  readonly artifactCount: number
  readonly updatedAt: string
}

export function isLive(status: string): boolean {
  return !TERMINAL.has(status)
}

export function toRow(run: RunDto): OrbitRunRow {
  return {
    runId: run.run_id,
    goal: run.goal || run.run_id,
    workflow: `${run.workflow_id}@${String(run.workflow_version)}`,
    status: run.status,
    live: isLive(run.status),
    revision: run.revision,
    artifactCount: run.artifact_count,
    updatedAt: run.updated_at,
  }
}

/** How soon to ask again, given what the last answer contained. */
export function nextInterval(rows: readonly OrbitRunRow[]): number {
  return rows.some(row => row.live) ? ORBIT_POLL_MS : ORBIT_IDLE_MS
}

/** Newest first, with anything still running ahead of anything finished.
 *
 * A resident panel is read at a glance, and the glance is almost always about
 * what is happening now — so recency alone would bury a running Run under a
 * pile of Runs that already have their answer.
 */
export function orderRows(rows: readonly OrbitRunRow[]): OrbitRunRow[] {
  return [...rows].sort((a, b) => {
    if (a.live !== b.live) return a.live ? -1 : 1
    return a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0
  })
}

/** A one-line count for the collapsed badge. */
export function summarise(rows: readonly OrbitRunRow[]): { live: number; total: number } {
  return { live: rows.filter(row => row.live).length, total: rows.length }
}

/** The four states the shell's StateDot draws, from an Orbit status.
 *
 * `unknown` is amber rather than red on purpose: it is the outcome nobody has
 * ruled on yet, and colouring it as a failure would answer a question the
 * Runtime deliberately left open.
 */
export function dotState(status: string): 'done' | 'warning' | 'ongoing' | 'error' {
  if (status === 'completed') return 'done'
  if (status === 'unknown') return 'warning'
  if (status === 'failed' || status === 'cancelled') return 'error'
  return 'ongoing'
}

export interface OrbitStepRow {
  readonly nodeId: string
  readonly label: string
  readonly status: string
  readonly needsPerson: boolean
}

export function toStepRow(step: StepSummary): OrbitStepRow {
  const label = typeof step.label === 'string' && step.label ? step.label : step.node_id
  return {
    nodeId: step.node_id,
    label,
    status: step.status,
    // The one step state a panel must not render as ordinary progress: the
    // Runtime is waiting for a person, and nothing moves until one answers.
    needsPerson: step.resolution?.kind === 'reconciliation_required'
      && step.reconciliation === undefined,
  }
}

/** Join an output page into displayable text, oldest chunk first. */
export function outputText(chunks: readonly OutputChunk[]): string {
  return [...chunks].sort((a, b) => a.chunk_id - b.chunk_id).map(chunk => chunk.text).join('')
}

/** Merge a new page into what is already shown without duplicating a chunk. */
export function mergeChunks(
  previous: readonly OutputChunk[], next: readonly OutputChunk[],
): OutputChunk[] {
  const byId = new Map(previous.map(chunk => [chunk.chunk_id, chunk]))
  for (const chunk of next) byId.set(chunk.chunk_id, chunk)
  return [...byId.values()].sort((a, b) => a.chunk_id - b.chunk_id)
}
