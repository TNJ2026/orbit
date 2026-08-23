import type { Context } from '@deepseek-ai/cordis';
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol';
import { type OrbitCursorStore } from './session-bridge.js';
import type { Session } from '@deepseek-ai/dsh-session';
import { type OrbitRunCommand } from './commands.js';
import type { AgentSummary } from './types.js';
import type { ArtifactContent, ArtifactSummary, AuthoringJob, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowSummary, WorkspaceRef } from './types.js';
declare module '@deepseek-ai/cordis' {
    interface Context {
        orbit: OrbitRemoteService;
    }
}
export declare class OrbitRemoteService extends TypertRemoteService {
    static inject: string[];
    private readonly gateway;
    private readonly catalog;
    /** Agent handlers per Workspace. The Runtime seals its registry at startup,
     *  so one read answers for as long as that Runtime is up. */
    private readonly agentsByWorkspace;
    private readonly bridges;
    /** One entry per live Bridge: the Workspaces worth knowing the Workflows of. */
    private readonly bridgedWorkspaces;
    private readonly bridgeDiagnostics;
    private readonly hostSessions;
    private readonly attachments;
    private readonly workspaceRegistry;
    constructor(ctx: Context);
    /**
     * Name the runnable Workflows in the model's context, so it does not have to
     * ask before it can tell whether Orbit is relevant to what was just said.
     *
     * The contribution is read synchronously at every assembly, so it can only
     * ever report what has already been fetched: a stale entry answers now and
     * refreshes for next time. The alternative — blocking assembly on a Runtime
     * that may not be running — would make a missing Orbit everyone's problem.
     */
    private tellTheModelWhatCanRun;
    /**
     * Read a Workspace's Workflows into the catalog; a failure leaves the last
     * answer standing.
     *
     * The parameter is a `scope` and not a `workspace` because that is what it
     * is: every caller derived it from a Session. The name is also what the
     * bundle's guard reads, so calling it anything else is how this stops being
     * checked.
     */
    private refreshCatalog;
    private registerWebApi;
    private dispatchWebApi;
    /**
     * Turn a caller-supplied Workspace into one this Host vouches for.
     *
     * The browser sends a Workspace with every call, and a browser is not an
     * authority on which directory a Session belongs to: the Session is. A
     * mismatch is refused rather than quietly corrected, because the two
     * disagreeing at all means the caller is describing a Session it is not in.
     */
    private verified;
    /**
     * The same guarantee for the Settings panel, which has a Workspace but no
     * Session. Its authority is the Workspace registry: a path nobody registered
     * is not somewhere this Host will go looking for a Runtime.
     */
    private registered;
    /**
     * The Workspace of a Session, derived and never claimed.
     *
     * Stronger than `verified`: there is no caller-supplied value to disagree
     * with, so there is nothing to check.
     */
    private sessionWorkspace;
    private liveSession;
    private startSessionBridge;
    private stopSessionBridge;
    private runSessionBridge;
    bridgeSession(workspace: WorkspaceRef, session: Session, cursor: OrbitCursorStore, signal: AbortSignal, knownRuns?: Iterable<string>): Promise<void>;
    getRuntime(workspace: WorkspaceRef, signal: AbortSignal): Promise<RuntimeSummary>;
    getRuntimeUi(sessionId: string, signal: AbortSignal): Promise<string>;
    /**
     * Everything the resident panel draws, in one round trip.
     *
     * It takes a Session and derives the Workspace, so a poller that runs every
     * couple of seconds carries no claim the Host has to check — and the panel
     * never has to know what a Workspace is.
     */
    getPanelState(sessionId: string, signal: AbortSignal): Promise<{
        runs: RunDto[];
        uiUrl: string;
        workflows: readonly WorkflowSummary[];
        agents: readonly AgentSummary[];
    }>;
    /**
     * The steps of one Run, for a panel row the reader opened.
     *
     * Session-scoped like the panel's poll: a Run id is not a capability, so the
     * Workspace it is read in comes from the Session rather than from the caller.
     */
    getRunDetail(sessionId: string, runId: string, signal: AbortSignal): Promise<{
        steps: StepSummary[];
    }>;
    getStepOutput(sessionId: string, runId: string, nodeId: string, after: number, signal: AbortSignal): Promise<OutputPage>;
    /**
     * Cancel or resume a Run from the panel.
     *
     * `expectedRevision` is what the panel had on screen, and it must still be
     * what Orbit advertises. Re-reading here would make the call succeed against
     * a Run that changed under the reader — the refusal is the point: whoever
     * pressed the button was looking at something else.
     */
    runCommand(sessionId: string, runId: string, command: OrbitRunCommand, expectedRevision: number, value: unknown, interruptId: string | undefined, signal: AbortSignal): Promise<RunDto>;
    /** Record a person's ruling on what an external Agent actually did. */
    reconcileStep(sessionId: string, runId: string, delegationId: string, outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal): Promise<{
        steps: StepSummary[];
    }>;
    getDiagnostics(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<IntegrationDiagnostics>;
    listWorkflows(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<WorkflowSummary[]>;
    listRuns(workspace: WorkspaceRef, sessionId: string, status: string | undefined, signal: AbortSignal): Promise<RunDto[]>;
    private workspaceForSession;
    generateWorkflow(workspace: WorkspaceRef, sessionId: string, prompt: string, signal: AbortSignal): Promise<AuthoringJob>;
    modifyWorkflow(workspace: WorkspaceRef, sessionId: string, workflowId: string, prompt: string, regenerate: boolean, signal: AbortSignal): Promise<AuthoringJob>;
    getAuthoringJob(workspace: WorkspaceRef, sessionId: string, jobId: string, signal: AbortSignal): Promise<AuthoringJob>;
    getRun(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunDto>;
    getSteps(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<StepSummary[]>;
    getGraph(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunGraph>;
    getEdges(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<EdgeSummary[]>;
    readOutput(workspace: WorkspaceRef, sessionId: string, runId: string, after: number, nodeId: string | undefined, signal: AbortSignal): Promise<OutputPage>;
    listArtifacts(workspace: WorkspaceRef, sessionId: string, runId: string | undefined, signal: AbortSignal): Promise<ArtifactSummary[]>;
    getArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactSummary>;
    getArtifactContent(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactContent>;
    importArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ImportedArtifact>;
    reconcileDelegation(workspace: WorkspaceRef, sessionId: string, runId: string, delegationId: string, outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal): Promise<StepSummary[]>;
    private readRunField;
    private readListField;
    executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto>;
}
export default OrbitRemoteService;
