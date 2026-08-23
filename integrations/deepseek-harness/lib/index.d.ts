import type { Context } from '@deepseek-ai/cordis';
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol';
import { type OrbitCursorStore } from './session-bridge.js';
import type { Session } from '@deepseek-ai/dsh-session';
import type { ArtifactContent, ArtifactSummary, AuthoringJob, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowSummary, WorkspaceRef } from './types.js';
declare module '@deepseek-ai/cordis' {
    interface Context {
        orbit: OrbitRemoteService;
    }
}
export declare class OrbitRemoteService extends TypertRemoteService {
    static inject: string[];
    private readonly gateway;
    private readonly bridges;
    private readonly bridgeDiagnostics;
    private readonly hostSessions;
    private readonly attachments;
    constructor(ctx: Context);
    private registerWebApi;
    private dispatchWebApi;
    private startSessionBridge;
    private stopSessionBridge;
    private runSessionBridge;
    bridgeSession(workspace: WorkspaceRef, session: Session, cursor: OrbitCursorStore, signal: AbortSignal, knownRuns?: Iterable<string>): Promise<void>;
    getRuntime(workspace: WorkspaceRef, signal: AbortSignal): Promise<RuntimeSummary>;
    getDiagnostics(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<IntegrationDiagnostics>;
    listWorkflows(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<WorkflowSummary[]>;
    listRuns(workspace: WorkspaceRef, sessionId: string, status: string | undefined, signal: AbortSignal): Promise<RunDto[]>;
    startWorkflowSelection(workspace: WorkspaceRef, sessionId: string, selectionId: string, workflowId: string, workflowVersion: number, input: Record<string, unknown>, signal: AbortSignal): Promise<RunDto>;
    private beginWorkflowSelection;
    private getPendingWorkflowSelection;
    cancelWorkflowSelection(sessionId: string, selectionId: string, signal: AbortSignal): Promise<void>;
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
    private selectionSession;
    private selectionGoal;
    private settleSelection;
    executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto>;
}
export default OrbitRemoteService;
