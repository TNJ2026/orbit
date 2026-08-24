export interface WorkspaceRef {
    id: string;
    canonicalPath: string;
    repositoryId?: string;
    worktreeId?: string;
    baseRevision?: string;
    isolationMode?: 'shared' | 'exclusive' | 'worktree' | 'snapshot';
}
export interface RuntimeSummary {
    workspaceId: string;
    state: 'ready' | 'stopped';
    capabilities: Record<string, unknown>;
}
export interface WorkflowSummary {
    workflow_id: string;
    name: string;
    description: string;
    latest_version: number;
    goal_readiness: string;
    readiness_reason?: string | null;
    input_mode?: string;
    inputs?: unknown[];
    goal_binding?: unknown;
    node_count?: number;
    node_kinds?: Record<string, number>;
}
/** One published step, as a reader meets it: what it does and who runs it. */
export interface WorkflowNode {
    node_id: string;
    label: string;
    kind: string;
    handler: string | null;
    prompt: string;
}
export interface AuthoringJob {
    job_id: string;
    type: string;
    workflow_id?: string | null;
    prompt: string;
    status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
    requested_agent?: string | null;
    attempts?: number | null;
    result?: unknown;
    error?: {
        code: string;
        message: string;
        diagnostics?: unknown[];
    } | null;
    created_at: string;
    updated_at: string;
}
/** An authoring job as the panel draws it: what was asked, and how it went. */
export interface AuthoringSummary {
    job_id: string;
    status: string;
    prompt: string;
    requested_agent?: string | null;
    workflow_id?: string | null;
    error?: string | null;
}
export interface OrbitCommandRequest {
    workspace: WorkspaceRef;
    sessionId: string;
    runId: string;
    command: 'langgraph_run.cancel' | 'langgraph_run.resume';
    expectedVersion: number;
    idempotencyKey: string;
    value?: unknown;
    interruptId?: string;
}
export type RunDto = Record<string, unknown> & {
    run_id: string;
    goal: string;
    workflow_id: string;
    workflow_version: number;
    status: string;
    revision: number;
    artifact_count: number;
    result?: unknown;
    error?: string | null;
    created_at: string;
    updated_at: string;
    interrupts: unknown[];
    allowed_commands: Array<{
        command: string;
        expected_version: number;
    }>;
};
export interface StepSummary {
    node_id: string;
    status: string;
    resolution?: {
        kind: 'reconciliation_required';
        delegation_id?: string;
    };
    reconciliation?: {
        outcome: 'confirmed_succeeded' | 'confirmed_failed';
        note: string;
        created_at: string;
    };
    [key: string]: unknown;
}
export interface AgentSummary {
    name: string;
    version: string;
    node_kinds: string[];
}
export interface RunGraph {
    [key: string]: unknown;
}
export interface EdgeSummary {
    edge_id: string;
    source_node: string;
    target_node: string;
    status: string;
    [key: string]: unknown;
}
export interface OutputChunk {
    chunk_id: number;
    node_id: string;
    attempt_id: string;
    stream: 'stdout' | 'stderr';
    text: string;
    created_at: string;
}
export interface OutputPage {
    chunks: OutputChunk[];
    after: number;
    has_more: boolean;
}
export interface ArtifactSummary {
    artifact_id: string;
    run_id: string;
    content_type?: string;
    size_bytes?: number;
    [key: string]: unknown;
}
export interface ImportedArtifact {
    attachmentId: string;
    mediaType: string;
    bytes: number;
    width: number;
    height: number;
    name?: string;
}
export interface IntegrationDiagnostics {
    generated_at: string;
    workspace_id: string;
    session_id: string;
    runtime: RuntimeSummary;
    gateway: {
        discoveryAttempts: number;
        rpcCalls: number;
        transportFailures: number;
        connectedWorkspaces: number;
        lastConnectedAt?: string;
        lastTransportError?: string;
    };
    bridge: {
        state: string;
        cursorPosition: number;
        lastError?: string;
        updatedAt: string;
    } | null;
}
export interface ArtifactContent {
    artifact: ArtifactSummary;
    encoding: 'base64';
    content: string;
}
export interface RuntimeEventHint {
    position: number;
    run_id: string;
    event_type: string;
    revision: number;
    occurred_at: string;
    node_id?: string;
    attempt_id?: string;
}
export interface RuntimeEventPage {
    events: RuntimeEventHint[];
    next_position: number;
}
export interface OrbitRunStarted {
    type: 'orbit/run-started';
    sourcePosition: number;
    runId: string;
    workspaceId: string;
    goal: string;
    workflowId: string;
    workflowVersion: number;
    revision: number;
    status: string;
    createdAt: string;
}
export interface OrbitRunCheckpoint {
    type: 'orbit/run-checkpoint';
    sourcePosition: number;
    runId: string;
    revision: number;
    status: string;
    currentSteps: StepSummary[];
    stepCounts: Record<string, number>;
    artifactCount: number;
    updatedAt: string;
}
export interface OrbitRunEnded {
    type: 'orbit/run-ended';
    sourcePosition: number;
    runId: string;
    revision: number;
    status: string;
    resultSummary?: string;
    errorSummary?: string;
    artifactCount: number;
    updatedAt: string;
}
export type OrbitSessionEvent = OrbitRunStarted | OrbitRunCheckpoint | OrbitRunEnded;
/**
 * The Session event shapes this integration appends.
 *
 * Declared beside the shapes themselves rather than in a UI module: the Bridge
 * records these whether or not anything renders them, and a record's type
 * should not depend on the existence of a view.
 */
declare module '@deepseek-ai/dsh-session/types' {
    interface SessionEventMap {
        'orbit/run-started': Omit<OrbitRunStarted, 'type'>;
        'orbit/run-checkpoint': Omit<OrbitRunCheckpoint, 'type'>;
        'orbit/run-ended': Omit<OrbitRunEnded, 'type'>;
    }
}
