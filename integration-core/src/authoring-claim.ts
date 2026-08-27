/** Writing a Workflow with the Agent that is already here.
 *
 * Orbit will hand a generation prompt to a connected MCP client rather than
 * fork an Agent CLI for it — but only to a client that has shown up on the
 * queue. Being connected is not enough: the broker counts a client as present
 * because it is *waiting for work*, not because it once called a tool. Nothing
 * in this Host had ever waited, so Orbit forked a CLI every time, and the
 * Agent that wrote the Workflow was one nobody could see working.
 *
 * This is the waiting. The loop lives in the Host; the parts that decide what
 * happens live here, taking their effects as arguments so the policy can be
 * read and tested without a Runtime, a model, or a session.
 */

import { createHash } from 'node:crypto'

/** The name this Host is offered under in Orbit's writer menu. */
export const CLAIM_CLIENT = 'harness'

/** Private, stable writer address for one Harness conversation. */
export function authoringClientForSession(sessionId: string): string {
  const digest = createHash('sha256').update(sessionId).digest('hex').slice(0, 24)
  return `route.${CLAIM_CLIENT}.${digest}`
}

/** Whether an older Runtime is specifically missing one optional MCP tool. */
export function isUnknownToolError(error: unknown, tool: string): boolean {
  const escaped = tool.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`unknown tool[ "']*${escaped}(?:[ "']|$)`, 'iu').test(String(error))
}

/**
 * How long one wait parks for.
 *
 * Long, because waiting is the point: the queue wakes it the moment work
 * arrives, and a short poll only means asking more often for the same silence.
 * But bounded by the transport, which gives up on any call at
 * `ORBIT_RPC_TIMEOUT_MS` in `gateway.ts` — a wait that outlasts it is not a
 * longer wait, it is an aborted request that takes this Host off the queue and
 * reports a timeout of its own making. The margin is for the round trip either
 * side of the park.
 *
 * Written as a number rather than derived from that constant because this
 * module is loaded as source by its tests, which cannot follow a value import
 * out of it. The relationship between the two is held by a test that reads
 * both files instead, so neither can drift without one failing.
 */
export const CLAIM_WAIT_SECONDS = 45

/** How long to leave the queue alone after a round that failed. Unrelated to
 *  the wait: this is about a Runtime that is not answering, not about silence
 *  on a queue that is. */
export const CLAIM_RETRY_MS = 15_000

export interface ClaimedRequest {
  readonly request_id: string
  readonly prompt: string
  readonly job_id?: string | null
  readonly lease_seconds?: number
}

export interface ClaimDeps {
  /** Park on the queue; resolves with work, or null when the wait expires. */
  wait: (timeoutSeconds: number) => Promise<ClaimedRequest | null>
  /** Put the prompt to the session's model and return what it said. */
  ask: (prompt: string) => Promise<string>
  /** Hand the answer back to Orbit, which compiles and publishes it. */
  submit: (requestId: string, dsl: string) => Promise<unknown>
  /** Told what went wrong, and never expected to fix it. */
  report: (stage: 'wait' | 'ask' | 'submit', error: unknown) => void
}

export type ClaimOutcome = 'idle' | 'answered' | 'failed'

/**
 * One turn of the loop: wait, ask, answer.
 *
 * Whatever the model says is submitted, even when it does not look like a
 * document. Orbit extracts and compiles it exactly as it does a CLI's stdout,
 * and a document it refuses comes back as a fresh request carrying the
 * compiler's findings — so a chatty answer costs a round, not the job. Judging
 * the answer here would be a second, worse copy of that validator.
 *
 * A failure to ask is not answered at all. Submitting something the model
 * never said would publish a Workflow nobody wrote; leaving the request alone
 * lets its lease lapse and puts it back on the queue, which is the outcome the
 * broker already knows how to have.
 */
export async function claimOnce(deps: ClaimDeps): Promise<ClaimOutcome> {
  let claimed: ClaimedRequest | null
  try {
    claimed = await deps.wait(CLAIM_WAIT_SECONDS)
  } catch (error) {
    deps.report('wait', error)
    return 'failed'
  }
  if (claimed === null) return 'idle'

  let answer: string
  try {
    answer = await deps.ask(claimed.prompt)
  } catch (error) {
    deps.report('ask', error)
    return 'failed'
  }
  if (!answer.trim()) {
    // A turn that said nothing is not an answer. Same reasoning as a failure
    // to ask: the request goes back on the queue rather than being answered
    // with emptiness the compiler would reject in our name.
    deps.report('ask', new Error('the Agent produced no answer to submit'))
    return 'failed'
  }

  try {
    await deps.submit(claimed.request_id, answer)
  } catch (error) {
    deps.report('submit', error)
    return 'failed'
  }
  return 'answered'
}

interface SessionEvent { readonly type: string; readonly data?: unknown }

/**
 * What the model said, out of the events a turn appended.
 *
 * Only `text` blocks of `assistant/message`. Reasoning blocks are the model
 * thinking rather than answering, and tool calls are it doing something else
 * entirely; including either would hand Orbit a document with the working-out
 * wrapped around it. Every message is taken, not the last, because a turn that
 * used a tool answers across more than one.
 */
export function answerFrom(events: readonly SessionEvent[], afterIndex: number): string {
  const parts: string[] = []
  for (const event of events.slice(Math.max(0, afterIndex))) {
    if (event.type !== 'assistant/message') continue
    const data = event.data as { message?: { content?: unknown } } | undefined
    const content = data?.message?.content
    if (!Array.isArray(content)) continue
    for (const block of content as { type?: unknown; text?: unknown }[]) {
      if (block?.type === 'text' && typeof block.text === 'string') parts.push(block.text)
    }
  }
  return parts.join('\n')
}
