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
    private readonly runtimes;
    private readonly telemetry;
    constructor(command?: string, commandPrefix?: readonly string[], fetchImpl?: Fetch, discoveryRoot?: string | undefined);
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
    /**
     * Where a person reads this Runtime, as the Runtime itself reports it.
     *
     * Never assembled from the MCP endpoint: the two are published together by
     * the process that owns the database, and guessing one from the other would
     * survive exactly until they differ.
     */
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
    private discover;
    private startAndDiscover;
    /**
     * Where a starting Runtime's stderr goes.
     *
     * Named for the Workspace so a second Workspace starting at the same moment
     * writes somewhere else, and truncated on each attempt so what is read back
     * is this start's output rather than a previous one's.
     */
    private startupLogPath;
    /**
     * The end of a failed start, or nothing.
     *
     * Nothing is a real answer here: the file may not exist, may be empty, or
     * may be unreadable, and none of those is worth replacing the exit code with
     * an error about reading a log file.
     */
    private startupLogTail;
    /**
     * Start a Runtime for this Workspace, keeping what it says on the way out.
     *
     * stderr goes to a file rather than to `'ignore'` or to a pipe. Discarding
     * it left a failed start with nothing but an exit code — the panel could
     * only say that something went wrong. A pipe would carry the text, but this
     * child is detached and outlives the Host: nobody would be draining the pipe
     * afterwards, and a Runtime that filled it would block on its own logging,
     * or take an EPIPE when the Host exited. A file has neither problem, and the
     * child holds its own descriptor once spawn has duplicated it.
     */
    private startRuntime;
    private rpc;
    private actorFrom;
    private callRaw;
}
export {};
//# sourceMappingURL=gateway.d.ts.map