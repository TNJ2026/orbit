import type { OrbitSessionEvent, WorkspaceRef } from './types.js';
import { OrbitGateway } from './gateway.js';
export interface OrbitEventSink {
    append(event: OrbitSessionEvent): void | Promise<void>;
}
export interface OrbitCursorStore {
    load(workspaceId: string, sessionId: string): number | undefined | Promise<number | undefined>;
    save(workspaceId: string, sessionId: string, position: number): void | Promise<void>;
}
export interface StoredOrbitEvent {
    type: string;
    data: unknown;
}
export declare function restoredBridgeState(events: readonly StoredOrbitEvent[]): {
    position: number;
    knownRuns: Set<string>;
};
export declare function sessionCanBridge(header: {
    cwd?: string;
    delegationDepth?: number;
}): boolean;
export interface BridgeRetryOptions {
    /**
     * The Session's durable events, read afresh for every attempt.
     *
     * Never hoist this into a value. A Run an earlier attempt already announced
     * is durably recorded here, and that record is the only thing standing
     * between a transient failure and a second announcement of the same Run.
     */
    events: () => readonly StoredOrbitEvent[];
    attempt: (knownRuns: Set<string>) => Promise<void>;
    onWaiting: (message: string) => void;
    signal: AbortSignal;
    retryDelayMs?: number;
}
export declare function bridgeDelay(ms: number, signal: AbortSignal): Promise<void>;
/** Keep attempting a Session Bridge until it finishes or the caller gives up. */
export declare function bridgeWithRetry(options: BridgeRetryOptions): Promise<void>;
export declare class OrbitSessionBridge {
    private readonly gateway;
    private readonly cursor;
    private readonly intervalMs;
    constructor(gateway: OrbitGateway, cursor: OrbitCursorStore, intervalMs?: number);
    run(workspace: WorkspaceRef, sessionId: string, sink: OrbitEventSink, signal: AbortSignal, knownRuns?: Iterable<string>): Promise<void>;
}
//# sourceMappingURL=session-bridge.d.ts.map