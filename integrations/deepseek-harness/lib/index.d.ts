import type { Context } from '@deepseek-ai/cordis';
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol';
import { type OrbitCursorStore } from './session-bridge.js';
import type { Session } from '@deepseek-ai/dsh-session';
import { type OrbitRunCommand } from './commands.js';
import type { AgentSummary } from './types.js';
import type { ArtifactContent, ArtifactSummary, AuthoringJob, AuthoringOutputPage, AuthoringSummary, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowNode, WorkflowSummary, WorkspaceRef } from './types.js';
declare module '@deepseek-ai/cordis' {
    interface Context {
        orbit: OrbitRemoteService;
    }
}
export declare class OrbitRemoteService extends TypertRemoteService {
    static inject: string[];
    private readonly gateway;
    private readonly agents;
    private readonly catalog;
    /** Authoring jobs started from this Harness, per Workspace.
     *
     *  Held here because there is nothing to ask: a job is addressed by an id
     *  the starter was handed, and `get_authoring_job` is scoped to the actor
     *  that created it. Jobs started in Orbit's own UI are shown by Orbit's own
     *  UI, which has the whole authoring surface. */
    private readonly authoringByWorkspace;
    private readonly bridges;
    /** One entry per live Bridge: the Workspaces worth knowing the Workflows of. */
    private readonly bridgedWorkspaces;
    /** Live authoring consumers, keyed by the exact Harness Session they drive. */
    private readonly authoringWaiters;
    /** The last thing that went wrong while writing a Workflow here. */
    private readonly authoringTrouble;
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
    private watchAuthoring;
    /**
     * Bring the tracked jobs up to date, and say which of them are worth drawing.
     *
     * Reports whether one of them has just published, which is the one case
     * where this Host knows the catalog it holds is out of date without being
     * told — so the caller re-reads rather than making a person press refresh
     * for a Workflow they asked for and watched arrive.
     */
    private readAuthoring;
    private markPublishedCatalogRefreshed;
    getAuthoringOutput(sessionId: string, outputHref: string, after: number, signal: AbortSignal): Promise<AuthoringOutputPage>;
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
    /**
     * Bind a Session to the Workspace it is working in, and warm that catalog.
     *
     * Warmed before the first turn asks, because this runs when the Session is
     * created and the person types afterwards. The binding is what
     * `tellTheModelWhatCanRun` reads, so every Session that can reach a Runtime
     * is registered here whether or not anything else happens.
     *
     * This is all that happens at Session start now. It used to also run
     * `OrbitSessionBridge`, which recorded each Run into the Session log as
     * `orbit/run-started` / `-checkpoint` / `-ended`; see `stopSessionBridge`
     * and the note on why that stopped.
     */
    private bindSessionWorkspace;
    /**
     * Stand on Orbit's authoring queue for this Workspace, and write what comes.
     *
     * Being on the queue is what makes this Host a writer Orbit will choose:
     * `_connected_client_first` prefers a connected client over forking an Agent
     * CLI, and it counts a client as connected because it is waiting here. A
     * Host that only ever called tools was never on the queue, so the preference
     * had nothing to prefer and every Workflow was written by a forked CLI whose
     * work nobody could watch.
     *
     * Restarted after every wait, including after a failure, because the wait is
     * how the Host stays addressable — stopping on the first unreachable Runtime
     * would take this Workspace off the menu until the Session was recreated.
     */
    private waitForAuthoring;
    /**
     * Put the prompt to the Workspace's Session and return what its model said.
     *
     * `followup` rather than anything quieter: the whole point is that the work
     * happens in the conversation, where a person can watch it and where the
     * Agent has the context the request came out of. It becomes an ordinary turn
     * and queues behind whatever the person is doing, so this never interrupts
     * one — it waits for one to end.
     *
     * The answer is read back out of the Session's own log rather than returned
     * by the call, because the log is what actually happened: a turn that used
     * tools answers across several messages, and the log has all of them in
     * order.
     */
    private askTheSession;
    /**
     * Drive the Session Bridge for one Session, writing each Run into its log.
     *
     * NOT called at Session start, and must not be until the Harness can accept
     * the events it writes. `orbit/run-*` are not in the Harness's own event
     * vocabulary, and `Session.append` offers no way to set the envelope's
     * `ignorable` marker — the one thing that lets a reader skip a type it does
     * not know. So every Session this ran in became unreadable on reload:
     *
     *   session "…" contains event type "orbit/run-started" (seq 964) unknown to
     *   this harness and not marked ignorable; refusing to interpret the log
     *
     * Kept rather than deleted because nothing here is wrong except where the
     * record is put. `@deepseek-ai/dsh-session` says a registration surface for
     * out-of-repo plugin events "is deferred until such a consumer exists"; this
     * is that consumer. When `append` can mark an event ignorable, or the
     * vocabulary can be extended, calling this from `bindSessionWorkspace`
     * restores the account of what ran.
     */
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
    /**
     * Everything the panel draws, in one call.
     *
     * `force` is a person pressing refresh, and it is the difference between a
     * poll and an answer: the catalog and the Agent registry are both held
     * deliberately — one behind a TTL, one for the life of the Runtime — because
     * a two-second poll must not re-ask questions that change when somebody
     * publishes. A press is exactly the case where they may have.
     */
    getPanelState(sessionId: string, force: boolean, startIfMissing: boolean, signal: AbortSignal): Promise<{
        runs: RunDto[];
        uiUrl: string;
        workflows: readonly WorkflowSummary[];
        agents: readonly AgentSummary[];
        authoring: readonly AuthoringSummary[];
        steps: Record<string, StepSummary[]>;
    }>;
    /**
     * The steps of the Runs that are still moving, so the Goal page can draw them.
     *
     * Only the live ones, and only what that page draws: the name and status of
     * each step, whether it has output to offer, and whether it is waiting on a
     * person. The rest of a StepSummary — the prompt it was authored with, its
     * handler, its timestamps — is detail nobody reads here, and sending the
     * whole thing on a two-second poll would put a page of JSON on the wire per
     * Run to render a list of names.
     *
     * A Run whose steps cannot be read loses its progress line and keeps its
     * row. The alternative is a panel that goes blank because one Run out of six
     * answered badly, which trades the thing a reader came for against a detail
     * they did not.
     */
    private liveSteps;
    /**
     * The steps of one Run, for a panel row the reader opened.
     *
     * Session-scoped like the panel's poll: a Run id is not a capability, so the
     * Workspace it is read in comes from the Session rather than from the caller.
     */
    getRunDetail(sessionId: string, runId: string, signal: AbortSignal): Promise<{
        steps: StepSummary[];
    }>;
    /**
     * The steps one Workflow is published with — read on demand, never polled.
     *
     * A definition changes only when someone republishes it, so this is fetched
     * when a reader opens a Workflow and not again.
     */
    getWorkflowDefinition(sessionId: string, workflowId: string, signal: AbortSignal): Promise<{
        nodes: WorkflowNode[];
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
    /**
     * Stop the Orbit Runtime serving this Session's Workspace.
     *
     * Session-scoped like every other call here: the Workspace is derived from
     * the Session rather than taken from the caller, so this can only ever stop
     * the Runtime the person is actually looking at.
     *
     * The waiter goes first. It is parked on that Runtime's authoring queue, and
     * leaving it there would have it discover the shutdown as a transport error
     * and log one — a failure report about something that was asked for.
     */
    stopRuntime(sessionId: string, signal: AbortSignal): Promise<{
        stopped: true;
    }>;
    getDiagnostics(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<IntegrationDiagnostics>;
    listWorkflows(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<WorkflowSummary[]>;
    listRuns(workspace: WorkspaceRef, sessionId: string, status: string | undefined, signal: AbortSignal): Promise<RunDto[]>;
    private workspaceForSession;
    generateWorkflow(workspace: WorkspaceRef, sessionId: string, prompt: string, signal: AbortSignal): Promise<AuthoringJob>;
    /** Start authoring from a Slash command whose only authority is its Session. */
    generateWorkflowForSession(sessionId: string, prompt: string, signal: AbortSignal): Promise<AuthoringJob>;
    /** Register this exact Session route before asking Orbit to address work to it. */
    private prepareAuthoringRoute;
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
    /**
     * What an Artifact is, and its text when its text is the answer.
     *
     * Metadata first, always: a workflow that writes its reply as markdown has
     * written the reply, and making a reader click through to it charges them a
     * click for the thing they asked for. But asking for a 2 MiB PDF in order to
     * discover it is a 2 MiB PDF is the round trip this ordering avoids, so the
     * bytes are fetched only once the recorded type and size say they are worth
     * fetching.
     */
    readArtifactText(sessionId: string, artifactId: string, signal: AbortSignal): Promise<{
        contentType: string;
        sizeBytes: number;
        text: string | null;
    }>;
    /**
     * Write one Artifact out as an ordinary file and say where it went.
     *
     * Not the path it already has. Orbit stores Artifacts content-addressed: the
     * file on disk is named by the sha256 of its own bytes, has no extension, is
     * shared by every Artifact with identical content, and is collected when
     * nothing references it. Handing that path to a person invites them to open
     * it in an editor and save — and saving corrupts every Artifact sharing
     * those bytes. So they get a copy that is theirs.
     *
     * Session-scoped like everything else here, and for the same reason twice
     * over: an Artifact belongs to the actor that produced it, so the Session is
     * both which Workspace to look in and the only identity allowed to read it.
     */
    exportArtifact(sessionId: string, artifactId: string, signal: AbortSignal): Promise<{
        path: string;
    }>;
    importArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ImportedArtifact>;
    reconcileDelegation(workspace: WorkspaceRef, sessionId: string, runId: string, delegationId: string, outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal): Promise<StepSummary[]>;
    private readRunField;
    private readListField;
    executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto>;
}
export default OrbitRemoteService;
