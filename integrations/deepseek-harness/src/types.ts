export interface WorkspaceRef { id: string; canonicalPath: string; repositoryId?: string; worktreeId?: string; baseRevision?: string; isolationMode?: 'shared' | 'exclusive' | 'worktree' | 'snapshot' }
export interface RuntimeSummary { workspaceId: string; state: 'ready' | 'stopped'; capabilities: Record<string, unknown> }
export interface OrbitCommandRequest { workspace: WorkspaceRef; sessionId: string; runId: string; command: 'langgraph_run.cancel' | 'langgraph_run.resume'; expectedVersion: number; idempotencyKey: string; value?: unknown; interruptId?: string }
export type RunDto = Record<string, unknown> & { run_id: string; goal: string; workflow_id: string; workflow_version: number; status: string; revision: number; artifact_count: number; result?: unknown; error?: string | null; created_at: string; updated_at: string; interrupts: unknown[]; allowed_commands: Array<{ command: string; expected_version: number }> }
export interface StepSummary { node_id: string; status: string; resolution?: { kind: 'reconciliation_required'; delegation_id?: string }; reconciliation?: { outcome: 'confirmed_succeeded' | 'confirmed_failed'; note: string; created_at: string }; [key: string]: unknown }
export interface RunGraph { [key: string]: unknown }
export interface EdgeSummary { edge_id: string; source_node: string; target_node: string; status: string; [key: string]: unknown }
export interface OutputChunk { chunk_id: number; node_id: string; attempt_id: string; stream: 'stdout' | 'stderr'; text: string; created_at: string }
export interface OutputPage { chunks: OutputChunk[]; after: number; has_more: boolean }
export interface ArtifactSummary { artifact_id: string; run_id: string; [key: string]: unknown }
export interface ArtifactContent { artifact: ArtifactSummary; encoding: 'base64'; content: string }
export interface DelegationDto { delegation_id: string; status: string; request: { input: Record<string, unknown>; config: Record<string, unknown> }; result?: unknown; error?: string | null; lease_expires_at?: string; cancel_requested: boolean }
export interface RuntimeEventHint { position: number; run_id: string; event_type: string; revision: number; occurred_at: string; node_id?: string; attempt_id?: string }
export interface RuntimeEventPage { events: RuntimeEventHint[]; next_position: number }
export interface OrbitRunStarted { type: 'orbit/run-started'; sourcePosition: number; runId: string; workspaceId: string; goal: string; workflowId: string; workflowVersion: number; revision: number; status: string; createdAt: string }
export interface OrbitRunCheckpoint { type: 'orbit/run-checkpoint'; sourcePosition: number; runId: string; revision: number; status: string; currentSteps: StepSummary[]; stepCounts: Record<string, number>; artifactCount: number; updatedAt: string }
export interface OrbitRunEnded { type: 'orbit/run-ended'; sourcePosition: number; runId: string; revision: number; status: string; resultSummary?: string; errorSummary?: string; artifactCount: number; updatedAt: string }
export type OrbitSessionEvent = OrbitRunStarted | OrbitRunCheckpoint | OrbitRunEnded
