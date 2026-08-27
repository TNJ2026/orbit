/** How far a Workflow being written has got, read from what it printed.
 *
 * Authoring has stages the way a Run has steps — it drafts, it compiles what
 * came back, it goes round again when the compiler refuses, and it publishes —
 * and the Runtime has always said so: `AuthoringJobService` writes a marker
 * into the job's console at each turn. Nothing read them. The panel matched
 * them only to drop them, so a job that spent a minute on its second attempt
 * showed one unchanging line, and the one question a person watching has —
 * *is it stuck or is it working* — had no answer on the page.
 *
 * A marker is a whole chunk whose text is the sentinel followed by JSON, so
 * this reads chunks rather than scanning text: an Agent that prints the
 * sentinel itself is printing inside a chunk of its own output, not writing a
 * marker, and must not be able to move the ladder.
 */

const SENTINEL = '\x1eorbit-progress:'

/** The three things authoring does. Repairing is not among them — see below. */
export const AUTHORING_STAGES = ['generating', 'validating', 'publishing'] as const

export type AuthoringStageName = (typeof AUTHORING_STAGES)[number]

/* Where each marker the Runtime emits leaves the ladder.
 *
 * `validated` and `repairing` are the two that are not stages: they are what
 * the compiler said. Validation passing moves to publishing; validation
 * failing sends the draft back to be written again, which is the stage the
 * ladder shows — with the attempt count as the thing that changed. A fourth
 * rung that appears only on failure would make the ladder a different shape
 * depending on how the job went, and a reader cannot follow a ladder that
 * grows a step behind them. */
const LANDS_ON: Record<string, number> = {
  generating: 0, repairing: 0, validating: 1, validated: 2, publishing: 2,
}

export type AuthoringStageStatus = 'succeeded' | 'running' | 'failed' | 'not_reached'

export interface AuthoringStage {
  readonly stage: AuthoringStageName
  readonly status: AuthoringStageStatus
}

export interface AuthoringProgress {
  readonly stages: readonly AuthoringStage[]
  /** Which try this is, or 0 when the job has not said yet. */
  readonly attempt: number
  /** How many it is allowed, or 0 when unknown. */
  readonly maxAttempts: number
}

interface Chunk { readonly text: string }

/** Whether this chunk is a progress marker rather than Agent output. */
export function isProgressMarker(chunk: Chunk): boolean {
  return chunk.text.startsWith(SENTINEL)
}

interface Marker { stage: string; attempt: number; maxAttempts: number }

function marker(chunk: Chunk): Marker | null {
  if (!isProgressMarker(chunk)) return null
  try {
    const value = JSON.parse(chunk.text.slice(SENTINEL.length)) as Record<string, unknown>
    const stage = typeof value.stage === 'string' ? value.stage : ''
    if (!(stage in LANDS_ON)) return null
    return {
      stage,
      attempt: typeof value.attempt === 'number' ? value.attempt : 0,
      maxAttempts: typeof value.max_attempts === 'number' ? value.max_attempts : 0,
    }
  } catch {
    // A truncated or malformed marker is a chunk this cannot read, not a
    // reason to lose the ladder built from the ones it could.
    return null
  }
}

/**
 * The ladder, from the markers a job has printed and the state it is in.
 *
 * The job's own status has the last word on the stages: a job that failed
 * failed at whatever rung it had reached, and one that is done reached all of
 * them — the markers stop when the process does, so a job killed mid-stage
 * would otherwise show that stage running for as long as anyone looked at it.
 */
export function authoringProgress(
  chunks: readonly Chunk[], jobStatus: string,
): AuthoringProgress {
  let reached = -1
  let attempt = 0
  let maxAttempts = 0
  for (const chunk of chunks) {
    const found = marker(chunk)
    if (found === null) continue
    // The rung it is on now, not the furthest it has been: a job on its second
    // attempt is drafting again, and a ladder that only ever moved forward
    // would show it validating while it writes.
    reached = LANDS_ON[found.stage] ?? reached
    if (found.attempt) attempt = found.attempt
    if (found.maxAttempts) maxAttempts = found.maxAttempts
  }
  const settled = jobStatus === 'done' || jobStatus === 'failed' || jobStatus === 'cancelled'
  const stages = AUTHORING_STAGES.map((stage, index): AuthoringStage => {
    if (jobStatus === 'done') return { stage, status: 'succeeded' }
    if (index < reached) return { stage, status: 'succeeded' }
    if (index > reached) return { stage, status: 'not_reached' }
    return { stage, status: settled ? 'failed' : 'running' }
  })
  return { stages, attempt, maxAttempts }
}
