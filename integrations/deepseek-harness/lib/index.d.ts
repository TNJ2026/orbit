import type { Context } from '@deepseek-ai/cordis';
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol';
import { type OrbitCursorStore } from './session-bridge.js';
import type { Session } from '@deepseek-ai/dsh-session';
import type { ArtifactContent, ArtifactSummary, EdgeSummary, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkspaceRef } from './types.js';
declare module '@deepseek-ai/cordis' {
    interface Context {
        orbit: OrbitRemoteService;
    }
}
export declare class OrbitRemoteService extends TypertRemoteService {
    private readonly gateway;
    private readonly host;
    constructor(ctx: Context);
    bridgeSession(workspace: WorkspaceRef, session: Session, cursor: OrbitCursorStore, signal: AbortSignal, knownRuns?: Iterable<string>): Promise<void>;
    private runDelegations;
    private executeDelegation;
    private settleDelegation;
    getRuntime(workspace: WorkspaceRef, signal: AbortSignal): Promise<RuntimeSummary>;
    getRun(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunDto>;
    getSteps(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<StepSummary[]>;
    getGraph(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunGraph>;
    getEdges(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<EdgeSummary[]>;
    readOutput(workspace: WorkspaceRef, sessionId: string, runId: string, after: number, nodeId: string | undefined, signal: AbortSignal): Promise<OutputPage>;
    listArtifacts(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<ArtifactSummary[]>;
    getArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactSummary>;
    getArtifactContent(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactContent>;
    reconcileDelegation(workspace: WorkspaceRef, sessionId: string, runId: string, delegationId: string, outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal): Promise<StepSummary[]>;
    private readRunField;
    executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto>;
}
export default OrbitRemoteService;
