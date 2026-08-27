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
import { OrbitGateway, type WorkspaceRef } from '@orbit-runtime/integration-core';
import { type JsonRpcMessage } from './stdio.js';
type Fetch = typeof globalThis.fetch;
export interface BridgeOptions {
    /** The project this server serves. One Runtime serves one Workspace, so one
     *  of these serves one project — named at launch, never per request. */
    readonly workspace: WorkspaceRef;
    readonly gateway: OrbitGateway;
    /** Start a Runtime when none is serving the project. On by default: a person
     *  who pointed their client at this project asked for Orbit there. */
    readonly startIfMissing?: boolean;
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
    readonly actor?: string;
    /** How long one forwarded call may take. Defaults to the shared transport
     *  ceiling; injectable so the deadline can be shown to work without waiting
     *  a minute for it. */
    readonly timeoutMs?: number;
    readonly fetchImpl?: Fetch;
}
/** The default name. Not `claude`-plus-something: see `actor` above. */
export declare const DEFAULT_ACTOR = "claude";
export declare class OrbitBridge {
    private readonly options;
    private readonly fetchImpl;
    private endpoint;
    constructor(options: BridgeOptions);
    /**
     * Forward one message and return what came back, or a JSON-RPC error saying
     * why not.
     *
     * Nothing is thrown at the caller. A stdio server has one pipe and one peer:
     * a throw ends the session, and every failure here is one the peer could act
     * on if only it were told. So each is classified by the shared reader and
     * turned into this host's own sentence.
     */
    forward(message: JsonRpcMessage): Promise<JsonRpcMessage>;
    /** Find or start the Runtime, and remember where it answers. */
    private resolve;
}
export {};
