import type { AuthoringOutputPage, GenerateAndRunOptions, GenerateAndRunResult, WorkspaceRef, RunDto } from './types.js';
type Fetch = typeof globalThis.fetch;
export interface GatewayDiagnostics {
    discoveryAttempts: number;
    rpcCalls: number;
    transportFailures: number;
    connectedWorkspaces: number;
    lastConnectedAt?: string;
    lastTransportError?: string;
}
/** Connect Harness to Orbit over HTTP MCP; explicit UI entry may start it. */
/**
 * How long any one MCP call may take before the transport gives up on it.
 *
 * Exported because a tool that deliberately blocks — `wait_authoring_request`
 * parks until work arrives — has to ask for less than this. Asking for more
 * does not extend the call: it aborts here, the request is cancelled at the
 * Runtime, and the caller is told about a timeout it chose for itself.
 */
export declare const ORBIT_RPC_TIMEOUT_MS = 60000;
export declare class OrbitGateway {
    private readonly command;
    private readonly commandPrefix;
    private readonly fetchImpl;
    private readonly discoveryRoot;
    private readonly hubUrl;
    private readonly runtimes;
    private readonly telemetry;
    constructor(command?: string, commandPrefix?: readonly string[], fetchImpl?: Fetch, discoveryRoot?: string | undefined, hubUrl?: string);
    diagnostics(): GatewayDiagnostics;
    acquire(workspace: WorkspaceRef, startIfMissing?: boolean): Promise<() => Promise<void>>;
    /**
     * Ask the Runtime serving this Workspace to stop, and forget it.
     *
     * The Runtime's own command, not a signal: it accepts the request, answers,
     * and then exits through its host, so in-flight work is unwound rather than
     * cut. This Host is allowed to ask because the Runtime was started for it —
     * `serve` vouches for `harness:session:` actors exactly when it carries the
     * Harness tool profile, which is the profile a Gateway starts it with.
     *
     * The cached connection goes whatever the answer was. A Runtime that
     * accepted the request is on its way down, and one that refused is a
     * connection worth re-establishing rather than reusing.
     */
    stopRuntime(workspace: WorkspaceRef, sessionId: string): Promise<void>;
    call(workspace: WorkspaceRef, sessionId: string, name: string, args: object): Promise<unknown>;
    /** Stable Hub UI namespace for this Workspace. */
    uiUrl(workspace: WorkspaceRef): Promise<string>;
    /**
     * Read the same durable attempt totals as Orbit's Agent page.
     *
     * This HTTP projection also keeps a newly upgraded Harness compatible with
     * a Runtime process started before `list_agents` grew the aggregate fields.
     */
    handlerAttemptCounts(workspace: WorkspaceRef, sessionId: string): Promise<ReadonlyMap<string, {
        attempt_count: number;
        failed_count: number;
    }>>;
    authoringOutput(workspace: WorkspaceRef, sessionId: string, outputHref: string, after: number): Promise<AuthoringOutputPage>;
    run(workspace: WorkspaceRef, sessionId: string, runId: string): Promise<RunDto>;
    /** Generate a Workflow and execute its Goal through Runtime MCP, without a UI.
     *
     * The authoring and execution calls are deliberately kept in one method so a
     * host can offer one synchronous operation. Runtime remains authoritative:
     * the job's published workflow id is used verbatim, and every mutation gets
     * its own idempotency key. No page routes or guessed URLs are involved.
     */
    generateAndRunGoal(workspace: WorkspaceRef, sessionId: string, prompt: string, goal: string, input?: Record<string, unknown>, options?: GenerateAndRunOptions): Promise<GenerateAndRunResult>;
    private waitForAuthoringJob;
    private waitForRun;
    private runtime;
    private runtimeFor;
    private connect;
    private registerWorkspace;
    private runOrbit;
    private startHub;
    private discover;
    private rpc;
    private actorFrom;
    private callRaw;
}
export {};
//# sourceMappingURL=gateway.d.ts.map