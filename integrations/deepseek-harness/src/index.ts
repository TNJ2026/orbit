import type { Context } from '@deepseek-ai/cordis'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import { OrbitGateway } from './gateway.js'
import { OrbitSessionBridge, restoredBridgeState, sessionCanBridge, type OrbitCursorStore } from './session-bridge.js'
import { OrbitToolBridge } from './orbit-tools.js'
import type { Session, SessionStore } from '@deepseek-ai/dsh-session'
import type { WorkspaceRegistry } from '@deepseek-ai/dsh-workspace'
import type { AttachmentStore } from '@deepseek-ai/dsh-attachment'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { artifactImageInput } from './artifact-import.js'
import { ORBIT_COMMAND, ORBIT_SELECTION_PENDING_TEXT, orbitGoal } from './orbit-command.js'
import type { ArtifactContent, ArtifactSummary, AuthoringJob, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowSummary, WorkspaceRef } from './types.js'

declare module '@deepseek-ai/cordis' { interface Context { orbit: OrbitRemoteService } }

interface OrbitWebServer {
  register(route: { kind: 'exact'; path: string; handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void> }): () => void
}

export class OrbitRemoteService extends TypertRemoteService {
  static inject = ['sessions', 'workspaceRegistry', 'tools', 'attachments']
  private readonly gateway = new OrbitGateway()
  private readonly bridges = new Map<string, AbortController>()
  private readonly bridgeDiagnostics = new Map<string, { state: string; cursorPosition: number; lastError?: string; updatedAt: string }>()
  private readonly hostSessions: SessionStore
  private readonly attachments: AttachmentStore
  constructor(ctx: Context) {
    super(ctx, 'orbit')
    this.hostSessions = ctx.get('sessions') as unknown as SessionStore
    this.attachments = ctx.get('attachments') as unknown as AttachmentStore
    new OrbitToolBridge(ctx, this.gateway).register()
    this.registerWebApi(ctx)
    for (const session of this.hostSessions.list()) this.startSessionBridge(ctx, session)
    ctx.on('session/created', session => { this.startSessionBridge(ctx, session) }, { global: true })
    ctx.on('session/disposed', session => { this.stopSessionBridge(String(session.id)) }, { global: true })
    ctx.effect(() => () => {
      for (const controller of this.bridges.values()) controller.abort()
      this.bridges.clear()
    }, 'orbit: stop Session Bridges')
  }

  private registerWebApi(ctx: Context): void {
    let registered = false
    const mount = () => {
      if (registered) return
      const webServer = (ctx.get('webServer') ?? ctx.get('httpServer')) as OrbitWebServer | undefined
      if (!webServer) return
      registered = true
      ctx.effect(() => webServer.register({
        kind: 'exact', path: '/plugins/dsh-orbit/api',
        handler: async (req, res) => {
          if (req.method !== 'POST') { res.writeHead(405, { allow: 'POST' }); res.end(); return }
          const controller = new AbortController()
          req.once('aborted', () => controller.abort(new Error('Orbit client request aborted')))
          try {
            const chunks: Buffer[] = []; let size = 0
            for await (const chunk of req) {
              const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
              size += buffer.length
              if (size > 256 * 1024) throw new Error('Orbit client request exceeds 256 KiB')
              chunks.push(buffer)
            }
            const body = JSON.parse(Buffer.concat(chunks).toString('utf8')) as { action?: unknown; args?: unknown }
            if (typeof body.action !== 'string' || !Array.isArray(body.args)) throw new Error('Orbit client request requires action and args')
            const result = await this.dispatchWebApi(body.action, body.args, controller.signal)
            res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
            res.end(JSON.stringify({ result: result === undefined ? null : result }))
          } catch (error) {
            res.writeHead(400, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
            res.end(JSON.stringify({ error: String(error) }))
          }
        },
      }), 'orbit: browser Host API')
    }
    mount()
    ctx.on('internal/service', name => { if (name === 'webServer' || name === 'httpServer') mount() })
  }

  private async dispatchWebApi(action: string, args: unknown[], signal: AbortSignal): Promise<unknown> {
    switch (action) {
      case 'getRuntime': return await this.getRuntime(args[0] as WorkspaceRef, signal)
      case 'getDiagnostics': return await this.getDiagnostics(args[0] as WorkspaceRef, String(args[1]), signal)
      case 'listWorkflows': return await this.listWorkflows(args[0] as WorkspaceRef, String(args[1]), signal)
      case 'listRuns': return await this.listRuns(args[0] as WorkspaceRef, String(args[1]), args[2] === undefined ? undefined : String(args[2]), signal)
      case 'beginWorkflowSelection': return this.beginWorkflowSelection(String(args[0]), String(args[1]), signal)
      case 'getPendingWorkflowSelection': return this.getPendingWorkflowSelection(String(args[0]), signal)
      case 'startWorkflowSelection': return await this.startWorkflowSelection(args[0] as WorkspaceRef, String(args[1]), String(args[2]), String(args[3]), Number(args[4]), args[5] as Record<string, unknown>, signal)
      case 'cancelWorkflowSelection': return await this.cancelWorkflowSelection(String(args[0]), String(args[1]), signal)
      case 'generateWorkflow': return await this.generateWorkflow(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'modifyWorkflow': return await this.modifyWorkflow(args[0] as WorkspaceRef, String(args[1]), String(args[2]), String(args[3]), Boolean(args[4]), signal)
      case 'getAuthoringJob': return await this.getAuthoringJob(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'getRun': return await this.getRun(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'getSteps': return await this.getSteps(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'getGraph': return await this.getGraph(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'getEdges': return await this.getEdges(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'readOutput': return await this.readOutput(args[0] as WorkspaceRef, String(args[1]), String(args[2]), Number(args[3]), args[4] === undefined ? undefined : String(args[4]), signal)
      case 'listArtifacts': return await this.listArtifacts(args[0] as WorkspaceRef, String(args[1]), args[2] === undefined ? undefined : String(args[2]), signal)
      case 'getArtifactContent': return await this.getArtifactContent(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'importArtifact': return await this.importArtifact(args[0] as WorkspaceRef, String(args[1]), String(args[2]), signal)
      case 'executeCommand': return await this.executeCommand(args[0] as OrbitCommandRequest, signal)
      case 'reconcileDelegation': return await this.reconcileDelegation(args[0] as WorkspaceRef, String(args[1]), String(args[2]), String(args[3]), args[4] as 'confirmed_succeeded' | 'confirmed_failed', String(args[5]), signal)
      default: throw new Error(`Unknown Orbit client action: ${action}`)
    }
  }

  private startSessionBridge(ctx: Context, session: Session): void {
    const sessionId = String(session.id)
    if (this.bridges.has(sessionId) || !sessionCanBridge(session.header)) return
    const cwd = session.header.cwd
    if (!cwd) return
    const controller = new AbortController()
    this.bridges.set(sessionId, controller)
    this.bridgeDiagnostics.set(sessionId, { state: 'connecting', cursorPosition: 0, updatedAt: new Date().toISOString() })
    void this.runSessionBridge(ctx, session, cwd, controller.signal).finally(() => {
      if (this.bridges.get(sessionId) === controller) this.bridges.delete(sessionId)
    })
  }

  private stopSessionBridge(sessionId: string): void {
    this.bridges.get(sessionId)?.abort()
    this.bridges.delete(sessionId)
    const previous = this.bridgeDiagnostics.get(sessionId)
    this.bridgeDiagnostics.set(sessionId, { state: 'stopped', cursorPosition: previous?.cursorPosition || 0, updatedAt: new Date().toISOString() })
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
      save: (_workspaceId, _sessionId, position) => {
        cursorPosition = position
        this.bridgeDiagnostics.set(String(session.id), {
          state: 'connected', cursorPosition: position, updatedAt: new Date().toISOString(),
        })
      },
    }
    let lastError = ''
    while (!signal.aborted) {
      try {
        await this.bridgeSession(workspace, session, cursor, signal, knownRuns)
        return
      } catch (error) {
        if (signal.aborted) return
        const message = String(error)
        this.bridgeDiagnostics.set(String(session.id), {
          state: 'waiting', cursorPosition, lastError: message, updatedAt: new Date().toISOString(),
        })
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

  @Remote('getDiagnostics')
  async getDiagnostics(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<IntegrationDiagnostics> {
    const runtime = await this.getRuntime(workspace, signal)
    return {
      generated_at: new Date().toISOString(), workspace_id: workspace.id,
      session_id: sessionId, runtime, gateway: this.gateway.diagnostics(),
      bridge: this.bridgeDiagnostics.get(sessionId) || null,
    }
  }

  @Remote('listWorkflows')
  async listWorkflows(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<WorkflowSummary[]> {
    return await this.readListField<WorkflowSummary>(workspace, sessionId, 'list_workflows', 'workflows', {}, signal)
  }

  @Remote('listRuns')
  async listRuns(workspace: WorkspaceRef, sessionId: string, status: string | undefined, signal: AbortSignal): Promise<RunDto[]> {
    return await this.readListField<RunDto>(workspace, sessionId, 'list_runs', 'runs', {
      limit: 100, ...(status ? { status } : {}),
    }, signal)
  }

  @Remote('startWorkflowSelection')
  async startWorkflowSelection(workspace: WorkspaceRef, sessionId: string, selectionId: string, workflowId: string, workflowVersion: number, input: Record<string, unknown>, signal: AbortSignal): Promise<RunDto> {
    signal.throwIfAborted()
    if (input === null || typeof input !== 'object' || Array.isArray(input)) throw new Error('Workflow input must be a JSON object')
    const session = this.selectionSession(sessionId, selectionId)
    const workflows = await this.readListField<WorkflowSummary>(workspace, sessionId, 'list_workflows', 'workflows', { ready_only: true }, signal)
    const workflow = workflows.find(item => item.workflow_id === workflowId && item.latest_version === workflowVersion)
    if (!workflow) throw new Error('Selected Workflow/version is not published and ready in this Workspace')
    const goal = this.selectionGoal(session, selectionId)
    const run = await this.gateway.call(workspace, sessionId, 'start_run', {
      workflow_id: workflowId, workflow_version: workflowVersion, input, goal,
      wait: false, idempotency_key: crypto.randomUUID(),
    }) as RunDto
    this.settleSelection(session, selectionId, `Started ${workflowId}@${String(workflowVersion)} as Run ${run.run_id}`)
    return run
  }

  private beginWorkflowSelection(sessionId: string, rawGoal: string, signal: AbortSignal): { selectionId: string } {
    signal.throwIfAborted()
    const session = this.hostSessions.list().find(item => String(item.id) === sessionId)
    if (!session?.header.cwd) throw new Error('Orbit requires a live Harness Session with a Workspace')
    const goal = orbitGoal(rawGoal)
    if (!goal || goal.length > 4000) throw new Error('Orbit goal must be 1-4000 characters')
    const selectionId = `orbit-${crypto.randomUUID()}`
    session.append.bind(session)('command/run', {
      commandId: selectionId, name: ORBIT_COMMAND, args: ` ${goal}`, source: { kind: 'user' },
    } as never)
    return { selectionId }
  }

  private getPendingWorkflowSelection(sessionId: string, signal: AbortSignal): { selectionId: string; goal: string } | null {
    signal.throwIfAborted()
    const session = this.hostSessions.list().find(item => String(item.id) === sessionId)
    if (!session) return null
    const settled = new Set(session.events.filter(event => event.type === 'command/done')
      .map(event => String((event.data as { commandId?: unknown }).commandId)))
    const requested = [...session.events].reverse().find(event => event.type === 'command/run'
      && (event.data as { name?: unknown }).name === ORBIT_COMMAND
      && !settled.has(String((event.data as { commandId?: unknown }).commandId)))
    if (!requested) return null
    return {
      selectionId: String((requested.data as { commandId?: unknown }).commandId),
      goal: orbitGoal(String((requested.data as { args?: unknown }).args || '')),
    }
  }

  @Remote('cancelWorkflowSelection')
  async cancelWorkflowSelection(sessionId: string, selectionId: string, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted()
    const session = this.selectionSession(sessionId, selectionId)
    this.settleSelection(session, selectionId, 'Orbit Workflow selection cancelled.')
  }

  @Remote('generateWorkflow')
  async generateWorkflow(workspace: WorkspaceRef, sessionId: string, prompt: string, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    if (!prompt.trim() || prompt.length > 20_000) throw new Error('Workflow prompt must be 1-20000 characters')
    return await this.gateway.call(workspace, sessionId, 'generate_workflow', {
      prompt: prompt.trim(), display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
    }) as AuthoringJob
  }

  @Remote('modifyWorkflow')
  async modifyWorkflow(workspace: WorkspaceRef, sessionId: string, workflowId: string, prompt: string, regenerate: boolean, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    if (!workflowId.trim()) throw new Error('Workflow id is required')
    if (!prompt.trim() || prompt.length > 20_000) throw new Error('Workflow prompt must be 1-20000 characters')
    return await this.gateway.call(workspace, sessionId, 'modify_workflow', {
      workflow_id: workflowId, prompt: prompt.trim(), mode: regenerate ? 'regenerate' : 'modify',
      display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
    }) as AuthoringJob
  }

  @Remote('getAuthoringJob')
  async getAuthoringJob(workspace: WorkspaceRef, sessionId: string, jobId: string, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    return await this.gateway.call(workspace, sessionId, 'get_authoring_job', { job_id: jobId }) as AuthoringJob
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
  async listArtifacts(workspace: WorkspaceRef, sessionId: string, runId: string | undefined, signal: AbortSignal): Promise<ArtifactSummary[]> {
    return await this.readListField<ArtifactSummary>(workspace, sessionId, 'list_artifacts', 'artifacts', {
      limit: 100, ...(runId ? { run_id: runId } : {}),
    }, signal)
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

  @Remote('importArtifact')
  async importArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ImportedArtifact> {
    const content = await this.getArtifactContent(workspace, sessionId, artifactId, signal)
    return await this.attachments.saveImage(artifactImageInput(content)) as unknown as ImportedArtifact
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

  private async readListField<T>(workspace: WorkspaceRef, sessionId: string, tool: string, field: string, arguments_: object, signal: AbortSignal): Promise<T[]> {
    signal.throwIfAborted()
    const release = await this.gateway.acquire(workspace)
    try {
      const result = await this.gateway.call(workspace, sessionId, tool, arguments_) as Record<string, unknown>
      return result[field] as T[]
    } finally { await release() }
  }

  private selectionSession(sessionId: string, selectionId: string): Session {
    const session = this.hostSessions.list().find(item => String(item.id) === sessionId)
    if (!session) throw new Error('Orbit Workflow selection requires a live Harness Session')
    this.selectionGoal(session, selectionId)
    const settled = session.events.some(event => event.type === 'command/done'
      && String((event.data as { commandId?: unknown }).commandId) === selectionId
      && String((event.data as { text?: unknown }).text || '') !== ORBIT_SELECTION_PENDING_TEXT)
    if (settled) throw new Error('Orbit Workflow selection is already settled')
    return session
  }

  private selectionGoal(session: Session, selectionId: string): string {
    const requested = [...session.events].reverse().find(event => (
      event.type === 'command/run'
      && String((event.data as { commandId?: unknown }).commandId) === selectionId
      && (event.data as { name?: unknown }).name === 'orbit'
    ))
    if (!requested) throw new Error('Orbit Workflow selection request was not found in this Session')
    const goal = String((requested.data as { args?: unknown }).args || '').trim()
    if (!goal) throw new Error('Orbit Workflow selection requires a non-empty goal')
    return goal
  }

  private settleSelection(session: Session, selectionId: string, text: string): void {
    session.append.bind(session)('command/done', { commandId: selectionId, kind: 'success', text } as never)
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
