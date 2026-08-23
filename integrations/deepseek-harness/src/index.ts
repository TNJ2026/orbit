import type { Context } from '@deepseek-ai/cordis'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import { OrbitGateway } from './gateway.js'
import { OrbitSessionBridge, restoredBridgeState, sessionCanBridge, type OrbitCursorStore } from './session-bridge.js'
import { OrbitToolBridge } from './orbit-tools.js'
import type { Session, SessionStore } from '@deepseek-ai/dsh-session'
import type { WorkspaceRegistry } from '@deepseek-ai/dsh-workspace'
import type { ArtifactContent, ArtifactSummary, EdgeSummary, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkspaceRef } from './types.js'

declare module '@deepseek-ai/cordis' { interface Context { orbit: OrbitRemoteService } }

export class OrbitRemoteService extends TypertRemoteService {
  static inject = ['sessions', 'workspaceRegistry', 'tools']
  private readonly gateway = new OrbitGateway()
  private readonly bridges = new Map<string, AbortController>()
  private readonly hostSessions: SessionStore
  constructor(ctx: Context) {
    super(ctx, 'orbit')
    this.hostSessions = ctx.get('sessions') as unknown as SessionStore
    new OrbitToolBridge(ctx, this.gateway).register()
    for (const session of this.hostSessions.list()) this.startSessionBridge(ctx, session)
    ctx.on('session/created', session => { this.startSessionBridge(ctx, session) }, { global: true })
    ctx.on('session/disposed', session => { this.stopSessionBridge(String(session.id)) }, { global: true })
    ctx.effect(() => () => {
      for (const controller of this.bridges.values()) controller.abort()
      this.bridges.clear()
    }, 'orbit: stop Session Bridges')
  }

  private startSessionBridge(ctx: Context, session: Session): void {
    const sessionId = String(session.id)
    if (this.bridges.has(sessionId) || !sessionCanBridge(session.header)) return
    const cwd = session.header.cwd
    if (!cwd) return
    const controller = new AbortController()
    this.bridges.set(sessionId, controller)
    void this.runSessionBridge(ctx, session, cwd, controller.signal).finally(() => {
      if (this.bridges.get(sessionId) === controller) this.bridges.delete(sessionId)
    })
  }

  private stopSessionBridge(sessionId: string): void {
    this.bridges.get(sessionId)?.abort()
    this.bridges.delete(sessionId)
  }

  private async runSessionBridge(ctx: Context, session: Session, cwd: string, signal: AbortSignal): Promise<void> {
    const registry = ctx.workspaceRegistry as WorkspaceRegistry
    const registered = await registry.resolveByPath(cwd)
    const workspace: WorkspaceRef = {
      id: registered ? String(registered.id) : `cwd:${cwd}`,
      canonicalPath: registered?.path ?? cwd,
    }
    const restored = restoredBridgeState(session.events)
    const knownRuns = restored.knownRuns
    let cursorPosition = restored.position
    const cursor: OrbitCursorStore = {
      load: () => cursorPosition || undefined,
      save: (_workspaceId, _sessionId, position) => { cursorPosition = position },
    }
    let lastError = ''
    while (!signal.aborted) {
      try {
        await this.bridgeSession(workspace, session, cursor, signal, knownRuns)
        return
      } catch (error) {
        if (signal.aborted) return
        const message = String(error)
        if (message !== lastError) ctx.logger.warn(`Orbit bridge for Session ${String(session.id)} is waiting: ${message}`)
        lastError = message
        await new Promise<void>(resolve => {
          const timer = setTimeout(resolve, 2_000)
          signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
        })
      }
    }
  }

  async bridgeSession(workspace: WorkspaceRef, session: Session, cursor: OrbitCursorStore, signal: AbortSignal, knownRuns: Iterable<string> = []): Promise<void> {
    const bridge = new OrbitSessionBridge(this.gateway, cursor)
    await bridge.run(workspace, String(session.id), {
      append: async event => {
        if (event.type === 'orbit/run-started') { const { type: _type, ...data } = event; session.append('orbit/run-started', data) }
        else if (event.type === 'orbit/run-checkpoint') { const { type: _type, ...data } = event; session.append('orbit/run-checkpoint', data) }
        else { const { type: _type, ...data } = event; session.append('orbit/run-ended', data) }
        await this.hostSessions.flush(session)
      },
    }, signal, knownRuns)
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
