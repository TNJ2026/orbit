/** What the panel shows, and how often it asks.
 *
 * Separated from the view because the interesting decisions here are not
 * visual: which Runs count as live, how fast to ask while any of them is, and
 * how to stop asking when none is. React only renders the answer.
 */

import type { RunDto } from '../types.js'

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
