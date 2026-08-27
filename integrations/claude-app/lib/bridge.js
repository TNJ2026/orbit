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
import { ORBIT_RPC_TIMEOUT_MS, panelError, } from '@orbit-runtime/integration-core';
import { sentenceFor } from './messages.js';
import { INTERNAL_ERROR, errorReply } from './stdio.js';
/** The default name. Not `claude`-plus-something: see `actor` above. */
export const DEFAULT_ACTOR = 'claude';
export class OrbitBridge {
    options;
    fetchImpl;
    endpoint;
    constructor(options) {
        this.options = options;
        this.fetchImpl = options.fetchImpl ?? globalThis.fetch;
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
    async forward(message) {
        try {
            const url = this.endpoint ?? await this.resolve();
            // A Runtime that stops answering must not stop this server answering.
            // Without a deadline the client waits on a promise nothing will settle,
            // and a stdio peer has no other way to notice.
            const controller = new AbortController();
            let expired = false;
            const deadline = this.options.timeoutMs ?? ORBIT_RPC_TIMEOUT_MS;
            const timer = setTimeout(() => { expired = true; controller.abort(); }, deadline);
            let response;
            try {
                response = await this.fetchImpl(url, {
                    method: 'POST',
                    headers: {
                        'content-type': 'application/json',
                        'x-orbit-actor': this.options.actor ?? DEFAULT_ACTOR,
                    },
                    body: JSON.stringify(message),
                    signal: controller.signal,
                });
            }
            catch (reason) {
                // An abort says "this operation was aborted" and nothing about why.
                // Only this code knows the deadline fired, and a timeout is a
                // different thing to be told than a cancellation — one means Orbit is
                // still working, the other that nobody is waiting any more.
                if (expired)
                    throw new Error(`Orbit MCP call timed out after ${String(deadline)}ms`);
                throw reason;
            }
            finally {
                clearTimeout(timer);
            }
            if (!response.ok) {
                throw new Error(`Orbit MCP HTTP ${String(response.status)}`);
            }
            return await response.json();
        }
        catch (reason) {
            // The endpoint may have been the stale half of the failure — a Runtime
            // that stopped between two calls — so the next message resolves again
            // rather than retrying an address that has already failed once.
            this.endpoint = undefined;
            // The sentence is what a reader acts on; the raw text is what they
            // quote into a bug report. Both travel, in the places JSON-RPC has for
            // them, because a classification is a guess that can be wrong.
            const reading = panelError(reason);
            const reply = errorReply(message.id, INTERNAL_ERROR, sentenceFor(reading.key));
            return { ...reply, error: { ...reply.error, data: { detail: reading.detail } } };
        }
    }
    /** Find or start the Runtime, and remember where it answers. */
    async resolve() {
        const { mcpUrl } = await this.options.gateway.endpoint(this.options.workspace, this.options.startIfMissing ?? true);
        this.endpoint = mcpUrl;
        return mcpUrl;
    }
}
