/** What went wrong, said to a person rather than to whoever wrote the throw.
 *
 * Everything the panel calls travels Host → Gateway → MCP → Runtime, and each
 * layer wraps the last: what reaches a reader is `Error: {"error":"workflow
 * version not found: workflow:wf_7ba…@2"}` — four layers of packaging around
 * one fact, and the fact is not the one the reader needs. They need to know
 * whether to press the button again, go and start something, or fetch someone.
 *
 * So the raw text is classified rather than printed. It is kept, though, on
 * the element's `title`: a message nobody can quote is a message nobody can
 * get help with, and the classification is a guess that can be wrong.
 */

/**
 * Everything that can be said to have gone wrong.
 *
 * The vocabulary lives here rather than in a host's dictionary because it is
 * decided here: the table below is what turns a raw failure into one of these,
 * and a host that adds a key nothing classifies has written a sentence nobody
 * will ever see. A host supplies the wording — `errNoRuntime` reads differently
 * in a panel and in a terminal — and must supply it for every key.
 */
export const ORBIT_ERROR_KEYS = [
  'errAborted',
  'errArtifactTooLarge',
  'errAuthoringActive',
  'errDiscoveryFailed',
  'errGoalActive',
  'errHostGone',
  'errImageOnly',
  'errNoAgent',
  'errNoAnswer',
  'errNoRuntime',
  'errNoSession',
  'errNoWorkspace',
  'errNotAllowed',
  'errPromptLength',
  'errProtocol',
  'errRequestTooLarge',
  'errRunElsewhere',
  'errRunGone',
  'errRunMoved',
  'errRuntimeAddress',
  'errRuntimeConflict',
  'errStartFailed',
  'errStopRefused',
  'errTimeout',
  'errUnknown',
  'errUnreachable',
  'errVersionMismatch',
  'errWorkflowDeleted',
  'errWorkflowGone',
  'errWorkspaceMismatch',
] as const

export type OrbitErrorKey = (typeof ORBIT_ERROR_KEYS)[number]

export interface PanelError {
  /** The sentence to show. */
  readonly key: OrbitErrorKey
  /** What it was that failed, when the sentence has a place for it. */
  readonly values?: Record<string, string | number>
  /** The original text, for a tooltip and for quoting into a bug report. */
  readonly detail: string
}

/* Matched in order, because these overlap: a stopped Runtime answers a tool
   call with a transport failure, and a workflow that was deleted is also a
   workflow that is not found. The more specific reading goes first. */
const READINGS: readonly (readonly [RegExp, OrbitErrorKey])[] = [
  // The Runtime is not there. Nothing else can be true at the same time, and
  // it is the one a person can fix by opening the panel again.
  [/No independent Orbit Runtime is serving/i, 'errNoRuntime'],
  // Before the plain timeout below: a start that ran out of time is a failed
  // start, and the message now carries the Runtime's own last words. Those
  // words are arbitrary text, so this has to win before anything reads them.
  [/auto-start (failed|timed out)/i, 'errStartFailed'],
  // The `orbit` command itself, rather than the Runtime it was asked about.
  [/Runtime discovery (failed|returned invalid JSON|must return an array)/i, 'errDiscoveryFailed'],
  [/Multiple Orbit Runtimes claim/i, 'errRuntimeConflict'],
  [/not reachable over HTTP MCP|published no HTTP address|did not publish a browser address/i,
    'errRuntimeAddress'],
  [/incompatible Orbit integration protocol/i, 'errVersionMismatch'],

  // Refused rather than failed. Above the transport readings and above the
  // refusal below, because these carry status codes of their own: a stop that
  // came back 403 is a permission fact, which is the actionable half, while
  // the same sentence with a 5xx is only "it would not stop".
  [/only a Runtime operator|valid actor credentials|HTTP 40[13]/i, 'errNotAllowed'],
  [/refused to stop/i, 'errStopRefused'],

  // It is there and did not answer in time, or did not answer at all.
  [/timed out/i, 'errTimeout'],
  [/transport failed|MCP HTTP|HTTP 5\d\d|failed with HTTP/i, 'errUnreachable'],

  // The host this surface lives in is gone — not Orbit, the thing that draws
  // the panel. Below the readings above on purpose: `Orbit MCP transport
  // failed: fetch failed` is a browser's network wording wrapped around a
  // fact about Orbit, and it is Orbit that is unreachable there. What is left
  // by the time it reaches here names nothing — a bare `Failed to fetch`, or
  // the 404 a plugin route answers with when the plugin did not load — and
  // that is the panel's own server having stopped. Both used to fall through
  // to "something went wrong" while the reader looked at a dead page.
  [/Failed to fetch|fetch failed|NetworkError|Load failed|ECONNREFUSED|HTTP 40[04]/i,
    'errHostGone'],

  // Nothing is wrong with Orbit; the panel has no Session or no project.
  // `cwd` first: "requires the Harness Session to have a Workspace cwd" is
  // both of these sentences, and the missing folder is the actionable half.
  [/Workspace cwd/i, 'errNoWorkspace'],
  [/requires? a live Harness|invalid Harness session id/i, 'errNoSession'],
  [/Workspace does not match the Harness Session|has not registered/i, 'errWorkspaceMismatch'],

  // The thing being acted on has moved or gone.
  [/workflow was deleted/i, 'errWorkflowDeleted'],
  [/workflow (version )?not found/i, 'errWorkflowGone'],
  [/no longer (offers|advertis(es|ed))/i, 'errRunMoved'],
  [/run not found|LangGraph run not found/i, 'errRunGone'],

  // Something else already has the slot.
  [/ActiveGoalExists|active_goal_exists/i, 'errGoalActive'],
  [/workflow_generation_already_active/i, 'errAuthoringActive'],

  // Bigger than the content proxy will carry, which is a fact about the file
  // rather than a fault — and the only one on this list a reader cannot act on.
  [/too large for MCP content proxy/i, 'errArtifactTooLarge'],

  // The writer this Harness offers is not available to take the work.
  [/no live Agent for Session|exposes no Agent registry/i, 'errNoAgent'],

  [/was started elsewhere/i, 'errRunElsewhere'],

  // Limits, which are facts about the request rather than faults.
  [/exceeds 256 KiB/i, 'errRequestTooLarge'],
  [/must be 1-20000 characters/i, 'errPromptLength'],
  [/supports images only/i, 'errImageOnly'],

  // Nobody did anything wrong and there is nothing to retry.
  // `AbortError: This operation was aborted` is what the platform says, and it
  // says nothing about who aborted or why. Read as a cancellation because that
  // is what an abort nobody labelled is: code that aborts on a deadline knows
  // it did, and says so in words of its own before this is reached.
  [/request aborted|operation was aborted|AbortError/i, 'errAborted'],
  [/produced no answer to submit/i, 'errNoAnswer'],

  // Last, because it is the widest: something arrived that could not be read.
  // A reader cannot act on any of these, but "this is a bug" is still a more
  // useful thing to be told than "something went wrong".
  [/invalid Orbit DTO|not canonical base64|arguments must be an object/i, 'errProtocol'],
  [/Orbit workflow generation (?:failed|\$\{workflow\.status\})|completed workflow generation without/i, 'errProtocol'],
  [/Unknown Orbit client action|requires action and args|Workflow id is required/i, 'errProtocol'],
  [/invalid authoring output (cursor|address)|authoring output returned invalid JSON/i, 'errProtocol'],
  [/returned an invalid authoring output address/i, 'errProtocol'],
]

/**
 * Read one failure into something worth showing.
 *
 * An unrecognised failure is not dressed up as a known one: it says that
 * something went wrong and carries its own text, which is honest and still
 * lets the reader copy it. Guessing here would be worse than saying nothing —
 * a wrong diagnosis sends somebody to fix the wrong thing.
 */
export function panelError(reason: unknown): PanelError {
  const detail = textOf(reason)
  for (const [pattern, key] of READINGS) {
    if (pattern.test(detail)) return { key, detail }
  }
  return { key: 'errUnknown', detail }
}

function textOf(reason: unknown): string {
  if (typeof reason === 'string') return reason
  if (reason instanceof Error) return reason.message
  if (reason !== null && typeof reason === 'object') {
    const held = (reason as { error?: unknown; message?: unknown })
    for (const value of [held.error, held.message]) {
      if (typeof value === 'string' && value) return value
    }
  }
  return String(reason)
}
