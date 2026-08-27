/** Orbit, in front of a host that speaks MCP for itself.
 *
 * Claude connects to MCP servers; Orbit is one, over HTTP, once a Runtime is
 * running for the project. The gap is everything in between: which Runtime
 * serves this directory, starting one when none does, refusing one whose
 * protocol this code does not know, and saying something useful when any of
 * that fails. That is exactly what the shared core already does for the
 * DeepSeek-Harness, so this is thin on purpose — it resolves an endpoint and
 * forwards, and the interesting parts are borrowed.
 */

import {
  ORBIT_RPC_TIMEOUT_MS, OrbitGateway, panelError, type WorkspaceRef,
} from '@orbit-runtime/integration-core'
import { sentenceFor } from './messages.js'
import { INTERNAL_ERROR, errorReply, type JsonRpcMessage } from './stdio.js'

type Fetch = typeof globalThis.fetch

export interface BridgeOptions {
  /** The project this server serves. One Runtime serves one Workspace, so one
   *  of these serves one project — named at launch, never per request. */
  readonly workspace: WorkspaceRef
  readonly gateway: OrbitGateway
  /** Start a Runtime when none is serving the project. On by default: a person
   *  who pointed their client at this project asked for Orbit there. */
  readonly startIfMissing?: boolean
  /**
   * Who Orbit records as having done this.
   *
   * Orbit scopes every write by actor: who cancelled a Run is the point of
   * recording who cancelled it, and the one-goal-at-a-time slot is per-actor
   * so one caller cannot block another. Without a name every loopback caller
   * is `local`, which makes this server indistinguishable from a person at a
   * terminal in the same project.
   *
   * Stable across restarts on purpose. A name with a pid in it would leave a
   * goal slot held by an actor that no longer exists, and would make it
   * impossible to cancel a Run this server had started a minute earlier.
   */
  readonly actor?: string
  /** How long one forwarded call may take. Defaults to the shared transport
   *  ceiling; injectable so the deadline can be shown to work without waiting
   *  a minute for it. */
  readonly timeoutMs?: number
  readonly fetchImpl?: Fetch
}

/** The default name. Not `claude`-plus-something: see `actor` above. */
export const DEFAULT_ACTOR = 'claude'

export class OrbitBridge {
  private readonly fetchImpl: Fetch
  private endpoint: string | undefined

  constructor(private readonly options: BridgeOptions) {
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch
  }

  /**
   * Forward one message and return what came back, or a JSON-RPC error saying
   * why not.
   *
   * Nothing is thrown at the caller. A stdio server has one pipe and one peer:
   * a throw ends the session, and every failure here is one the peer could act
   * on if only it were told. So each is classified by the shared reader and
   * turned into this host's own sentence.
   */
  async forward(message: JsonRpcMessage): Promise<JsonRpcMessage> {
    try {
      const url = this.endpoint ?? await this.resolve()
      // A Runtime that stops answering must not stop this server answering.
      // Without a deadline the client waits on a promise nothing will settle,
      // and a stdio peer has no other way to notice.
      const controller = new AbortController()
      let expired = false
      const deadline = this.options.timeoutMs ?? ORBIT_RPC_TIMEOUT_MS
      const timer = setTimeout(() => { expired = true; controller.abort() }, deadline)
      let response: Awaited<ReturnType<Fetch>>
      try {
        response = await this.fetchImpl(url, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            'x-orbit-actor': this.options.actor ?? DEFAULT_ACTOR,
          },
          body: JSON.stringify(message),
          signal: controller.signal,
        })
      } catch (reason) {
        // An abort says "this operation was aborted" and nothing about why.
        // Only this code knows the deadline fired, and a timeout is a
        // different thing to be told than a cancellation — one means Orbit is
        // still working, the other that nobody is waiting any more.
        if (expired) throw new Error(`Orbit MCP call timed out after ${String(deadline)}ms`)
        throw reason
      } finally { clearTimeout(timer) }
      if (!response.ok) {
        throw new Error(`Orbit MCP HTTP ${String(response.status)}`)
      }
      return await response.json() as JsonRpcMessage
    } catch (reason) {
      // The endpoint may have been the stale half of the failure — a Runtime
      // that stopped between two calls — so the next message resolves again
      // rather than retrying an address that has already failed once.
      this.endpoint = undefined
      // The sentence is what a reader acts on; the raw text is what they
      // quote into a bug report. Both travel, in the places JSON-RPC has for
      // them, because a classification is a guess that can be wrong.
      const reading = panelError(reason)
      const reply = errorReply(message.id, INTERNAL_ERROR, sentenceFor(reading.key))
      return { ...reply, error: { ...reply.error!, data: { detail: reading.detail } } }
    }
  }

  /** Find or start the Runtime, and remember where it answers. */
  private async resolve(): Promise<string> {
    const { mcpUrl } = await this.options.gateway.endpoint(
      this.options.workspace, this.options.startIfMissing ?? true,
    )
    this.endpoint = mcpUrl
    return mcpUrl
  }
}
