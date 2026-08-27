/** How far a Run has got, derived once for everywhere that says it.
 *
 * The panel answers "where is this now" in three places — the Host, when it
 * decides which Runs are worth reading steps for; the list line under a Run;
 * the bar above its steps — and two of those disagreeing is worse than either
 * being absent, because a reader has no way to tell which one lied. So the
 * vocabulary of statuses lives here, once, beside the arithmetic that reads it.
 */

import type { StepSummary } from './types.js'

/** Run statuses there is no coming back from. */
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'unknown'])

/** Whether a Run could still do something. */
export function isLive(status: string): boolean {
  return !TERMINAL.has(status)
}

/** Steps that have had their turn. `answered` is one: a person replied. */
const SETTLED = new Set(['succeeded', 'answered'])

/* What a reader means by "the step it is on", in the order they would look:
   something working beats something waiting on a person, and both beat a step
   whose outcome nobody has ruled on. A Run can be in more than one of these at
   once — a superstep runs several nodes — and naming the first is a choice to
   say something short rather than something complete. */
const CURRENT_ORDER = ['running', 'waiting', 'unknown'] as const

export interface RunProgress {
  /** Steps settled, out of the whole definition. */
  readonly done: number
  readonly total: number
  /** What it is on, when it is on something nameable. */
  readonly current?: string
  /** Whether that step is stuck rather than working — it needs a person. */
  readonly blocked: boolean
}

/** The step's authored name, or the node id it was authored without one. */
export function labelOf(step: StepSummary): string {
  return typeof step.label === 'string' && step.label ? step.label : step.node_id
}

/**
 * A count and a name, from the steps of one Run.
 *
 * `done` counts only what actually finished, so a Run that failed at its third
 * step reads `2/6` rather than `3/6`: two steps produced something, and the
 * row's own red status is what says the third did not.
 */
export function progressOf(steps: readonly StepSummary[]): RunProgress {
  const total = steps.length
  const done = steps.filter(step => SETTLED.has(step.status)).length
  for (const status of CURRENT_ORDER) {
    const step = steps.find(item => item.status === status)
    if (step) return { done, total, current: labelOf(step), blocked: status !== 'running' }
  }
  return { done, total, blocked: false }
}

/** The little a Run has to say for the Goal page to decide whether to draw it. */
export interface GoalCandidate {
  readonly live: boolean
  readonly updatedAt: string
}

/**
 * The Runs the Goal page shows: everything still moving, or the one that
 * moved last when nothing is.
 *
 * A Goal reaching its end is the moment its result matters most, and dropping
 * it from the page right then answered "what happened" with an empty page —
 * the reader watched four steps go green and was left looking at "nothing is
 * running here". So a finished Goal stays, with its steps and its outcome,
 * until the next one starts and takes the page.
 *
 * Shared because the Host reads steps for exactly the Runs this page draws. A
 * Host that kept its own idea of that would go on serving a settled Run's last
 * *running* step forever, since the step read stops with the Run.
 */
export function goalRuns<T extends GoalCandidate>(rows: readonly T[]): T[] {
  const live = rows.filter(row => row.live)
  if (live.length) return live
  const latest = rows.reduce<T | undefined>(
    (best, row) => best === undefined || row.updatedAt > best.updatedAt ? row : best,
    undefined,
  )
  return latest === undefined ? [] : [latest]
}
