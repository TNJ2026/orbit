import type { OrbitSessionEvent, WorkspaceRef } from './types.js';
import { OrbitGateway } from './gateway.js';
export interface OrbitEventSink {
    append(event: OrbitSessionEvent): void | Promise<void>;
}
export interface OrbitCursorStore {
    load(workspaceId: string, sessionId: string): number | undefined | Promise<number | undefined>;
    save(workspaceId: string, sessionId: string, position: number): void | Promise<void>;
}
export declare class OrbitSessionBridge {
    private readonly gateway;
    private readonly cursor;
    private readonly intervalMs;
    constructor(gateway: OrbitGateway, cursor: OrbitCursorStore, intervalMs?: number);
    run(workspace: WorkspaceRef, sessionId: string, sink: OrbitEventSink, signal: AbortSignal, knownRuns?: Iterable<string>): Promise<void>;
}
