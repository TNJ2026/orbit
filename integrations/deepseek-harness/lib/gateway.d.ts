import type { WorkspaceRef, RunDto } from './types.js';
type Fetch = typeof globalThis.fetch;
export interface GatewayDiagnostics {
    discoveryAttempts: number;
    rpcCalls: number;
    transportFailures: number;
    connectedWorkspaces: number;
    lastConnectedAt?: string;
    lastTransportError?: string;
}
/** Connect Harness to an already-running Orbit Runtime over HTTP MCP. */
export declare class OrbitGateway {
    private readonly command;
    private readonly commandPrefix;
    private readonly fetchImpl;
    private readonly discoveryRoot;
    private readonly runtimes;
    private readonly telemetry;
    constructor(command?: string, commandPrefix?: readonly string[], fetchImpl?: Fetch, discoveryRoot?: string | undefined);
    diagnostics(): GatewayDiagnostics;
    acquire(workspace: WorkspaceRef): Promise<() => Promise<void>>;
    call(workspace: WorkspaceRef, sessionId: string, name: string, args: object): Promise<unknown>;
    /**
     * Where a person reads this Runtime, as the Runtime itself reports it.
     *
     * Never assembled from the MCP endpoint: the two are published together by
     * the process that owns the database, and guessing one from the other would
     * survive exactly until they differ.
     */
    uiUrl(workspace: WorkspaceRef): Promise<string>;
    run(workspace: WorkspaceRef, sessionId: string, runId: string): Promise<RunDto>;
    private runtime;
    private runtimeFor;
    private connect;
    private discover;
    private rpc;
    private actorFrom;
    private callRaw;
}
export {};
