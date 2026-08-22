import type { Context } from '@deepseek-ai/cordis'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import { OrbitGateway } from './gateway.js'
import { OrbitSessionBridge, type OrbitCursorStore } from './session-bridge.js'
import type { Session } from '@deepseek-ai/dsh-session'
import type { AgentRegistry } from '@deepseek-ai/dsh-agent'
import type { SubagentRuntime } from '@deepseek-ai/dsh-subagent'
import type { ArtifactContent, ArtifactSummary, DelegationDto, EdgeSummary, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkspaceRef } from './types.js'
import { defaultDelegationExecutionPorts, executeDelegation } from './delegation-execution.js'

declare module '@deepseek-ai/cordis' { interface Context { orbit: OrbitRemoteService } }

export class OrbitRemoteService extends TypertRemoteService {
  private readonly gateway = new OrbitGateway()
  private readonly host: Context
  constructor(ctx: Context) { super(ctx, 'orbit'); this.host = ctx }

  async bridgeSession(workspace: WorkspaceRef, session: Session, cursor: OrbitCursorStore, signal: AbortSignal, knownRuns: Iterable<string> = []): Promise<void> {
    const bridge = new OrbitSessionBridge(this.gateway, cursor)
    await Promise.all([bridge.run(workspace, String(session.id), {
      append: event => {
        if (event.type === 'orbit/run-started') { const { type: _type, ...data } = event; session.append('orbit/run-started', data) }
        else if (event.type === 'orbit/run-checkpoint') { const { type: _type, ...data } = event; session.append('orbit/run-checkpoint', data) }
        else { const { type: _type, ...data } = event; session.append('orbit/run-ended', data) }
      },
    }, signal, knownRuns), this.runDelegations(workspace, session, signal)])
  }

  private async runDelegations(workspace: WorkspaceRef, session: Session, signal: AbortSignal): Promise<void> {
    const agents = this.host.get('agents') as AgentRegistry | undefined
    const subagents = this.host.get('subagents') as SubagentRuntime | undefined
    if (!agents || !subagents) return
    const sessionId = String(session.id)
    const allowedProviders = subagents.list()
    await this.gateway.call(workspace, sessionId, 'configure_execution_lease', {
      lease_id: `orbit:${sessionId}:${workspace.id}`,
      workspace_id: workspace.id,
      allowed_providers: allowedProviders,
      max_delegations: 100,
      max_wall_seconds: 7200,
      expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
    })
    const workerId = `orbit:${String(session.id)}:${crypto.randomUUID()}`
    while (!signal.aborted) {
      const parent = agents.get(session.id)
      if (!parent) { await new Promise(resolve => setTimeout(resolve, 250)); continue }
      const claimed = await this.gateway.call(workspace, sessionId, 'claim_delegation', { worker_id: workerId, lease_seconds: 30 }) as { delegation: DelegationDto | null }
      if (!claimed.delegation) { await new Promise(resolve => setTimeout(resolve, 250)); continue }
      await this.executeDelegation(workspace, sessionId, workerId, claimed.delegation, parent, subagents, signal)
    }
  }

  private async executeDelegation(workspace: WorkspaceRef, sessionId: string, workerId: string, delegation: DelegationDto, parent: NonNullable<ReturnType<AgentRegistry['get']>>, subagents: SubagentRuntime, outerSignal: AbortSignal): Promise<void> {
    await executeDelegation(workspace, delegation, parent, subagents, outerSignal, {
      ...defaultDelegationExecutionPorts,
      settle: async (result, error) => await this.settleDelegation(
        workspace, sessionId, workerId, delegation.delegation_id, result, error,
      ),
      renew: async () => {
        const value = await this.gateway.call(workspace, sessionId, 'renew_delegation', {
          delegation_id: delegation.delegation_id, worker_id: workerId, lease_seconds: 30,
        }) as { delegation: DelegationDto }
        return value.delegation
      },
    })
  }

  private async settleDelegation(workspace: WorkspaceRef, sessionId: string, workerId: string, delegationId: string, result?: unknown, error?: string): Promise<void> {
    await this.gateway.call(workspace, sessionId, 'complete_delegation', {
      delegation_id: delegationId, worker_id: workerId,
      ...(error === undefined ? { result } : { error }),
    })
  }

  @Remote('getRuntime')
  async getRuntime(workspace: WorkspaceRef, signal: AbortSignal): Promise<RuntimeSummary> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try {
      const capabilities = await this.gateway.call(workspace, 'probe', 'get_capabilities', {}) as Record<string, unknown>
      return { workspaceId: workspace.id, state: 'ready', capabilities }
    } finally { await release() }
  }

  @Remote('getRun')
  async getRun(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunDto> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try { return await this.gateway.run(workspace, sessionId, runId) }
    finally { await release() }
  }

  @Remote('getSteps')
  async getSteps(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<StepSummary[]> {
    return await this.readRunField<StepSummary[]>(workspace, sessionId, runId, 'get_run_steps', 'steps', signal)
  }

  @Remote('getGraph')
  async getGraph(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunGraph> {
    return await this.readRunField<RunGraph>(workspace, sessionId, runId, 'get_run_graph', 'graph', signal)
  }

  @Remote('getEdges')
  async getEdges(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<EdgeSummary[]> {
    return await this.readRunField<EdgeSummary[]>(workspace, sessionId, runId, 'get_run_edges', 'edges', signal)
  }

  @Remote('readOutput')
  async readOutput(workspace: WorkspaceRef, sessionId: string, runId: string, after: number, nodeId: string | undefined, signal: AbortSignal): Promise<OutputPage> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try {
      return await this.gateway.call(workspace, sessionId, 'read_run_output', {
        run_id: runId, after, ...(nodeId ? { node_id: nodeId } : {}),
      }) as OutputPage
    } finally { await release() }
  }

  @Remote('listArtifacts')
  async listArtifacts(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<ArtifactSummary[]> {
    return await this.readRunField<ArtifactSummary[]>(workspace, sessionId, runId, 'list_artifacts', 'artifacts', signal)
  }

  @Remote('getArtifact')
  async getArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactSummary> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try { return await this.gateway.call(workspace, sessionId, 'read_artifact', { artifact_id: artifactId }) as ArtifactSummary }
    finally { await release() }
  }

  @Remote('getArtifactContent')
  async getArtifactContent(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactContent> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try { return await this.gateway.call(workspace, sessionId, 'read_artifact_content', { artifact_id: artifactId }) as ArtifactContent }
    finally { await release() }
  }

  @Remote('reconcileDelegation')
  async reconcileDelegation(workspace: WorkspaceRef, sessionId: string, runId: string, delegationId: string, outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal): Promise<StepSummary[]> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try {
      await this.gateway.call(workspace, sessionId, 'reconcile_delegation', {
        delegation_id: delegationId, outcome, note,
        idempotency_key: crypto.randomUUID(),
      })
      const result = await this.gateway.call(workspace, sessionId, 'get_run_steps', { run_id: runId }) as { steps: StepSummary[] }
      return result.steps
    } finally { await release() }
  }

  private async readRunField<T>(workspace: WorkspaceRef, sessionId: string, runId: string, tool: string, field: string, signal: AbortSignal): Promise<T> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try {
      const result = await this.gateway.call(workspace, sessionId, tool, { run_id: runId }) as Record<string, unknown>
      return result[field] as T
    } finally { await release() }
  }

  @Remote('executeCommand')
  async executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(request.workspace)
    try {
      const run = await this.gateway.run(request.workspace, request.sessionId, request.runId)
      const advertised = run.allowed_commands.find(item => item.command === request.command && item.expected_version === request.expectedVersion)
      if (advertised === undefined) throw new Error('Orbit command is no longer advertised at this revision')
      const tool = request.command === 'langgraph_run.cancel' ? 'cancel_run' : 'resume_run'
      return await this.gateway.call(request.workspace, request.sessionId, tool, {
        run_id: request.runId, expected_version: request.expectedVersion,
        idempotency_key: request.idempotencyKey, value: request.value,
        interrupt_id: request.interruptId,
      }) as RunDto
    } finally { await release() }
  }
}

export default OrbitRemoteService
