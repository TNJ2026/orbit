import type { Context } from '@deepseek-ai/cordis'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import { OrbitGateway } from './gateway.js'
import { OrbitSessionBridge, bridgeWithRetry, restoredBridgeState, sessionCanBridge, type OrbitCursorStore, type StoredOrbitEvent } from './session-bridge.js'
import { OrbitToolBridge } from './orbit-tools.js'
import type { Session, SessionStore } from '@deepseek-ai/dsh-session'
import type { WorkspaceRegistry } from '@deepseek-ai/dsh-workspace'
import type { AttachmentStore } from '@deepseek-ai/dsh-attachment'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { artifactImageInput } from './artifact-import.js'
import { advertisedAt, commandTool, type OrbitRunCommand } from './commands.js'
import { WorkflowCatalog } from './workflow-catalog.js'
import type { AgentSummary } from './types.js'
import type { ArtifactContent, ArtifactSummary, AuthoringJob, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowNode, WorkflowSummary, WorkspaceRef } from './types.js'

declare module '@deepseek-ai/cordis' { interface Context { orbit: OrbitRemoteService } }

interface OrbitWebServer {
  register(route: { kind: 'exact'; path: string; handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void> }): () => void
}

export class OrbitRemoteService extends TypertRemoteService {
  static inject = ['sessions', 'workspaceRegistry', 'tools', 'attachments', 'systemPrompt']
  private readonly gateway = new OrbitGateway()
  private readonly catalog = new WorkflowCatalog()
  /** Agent handlers per Workspace. The Runtime seals its registry at startup,
   *  so one read answers for as long as that Runtime is up. */
  private readonly agentsByWorkspace = new Map<string, readonly AgentSummary[]>()
  private readonly bridges = new Map<string, AbortController>()
  /** One entry per live Bridge: the Workspaces worth knowing the Workflows of. */
  private readonly bridgedWorkspaces = new Map<string, WorkspaceRef>()
  private readonly bridgeDiagnostics = new Map<string, { state: string; cursorPosition: number; lastError?: string; updatedAt: string }>()
  private readonly hostSessions: SessionStore
  private readonly attachments: AttachmentStore
  private readonly workspaceRegistry: WorkspaceRegistry
  constructor(ctx: Context) {
    super(ctx, 'orbit')
    this.hostSessions = ctx.get('sessions') as unknown as SessionStore
    this.attachments = ctx.get('attachments') as unknown as AttachmentStore
    this.workspaceRegistry = ctx.get('workspaceRegistry') as unknown as WorkspaceRegistry
    new OrbitToolBridge(ctx, this.gateway).register()
    this.registerWebApi(ctx)
    this.tellTheModelWhatCanRun(ctx)
    for (const session of this.hostSessions.list()) this.startSessionBridge(ctx, session)
    ctx.on('session/created', session => { this.startSessionBridge(ctx, session) }, { global: true })
    ctx.on('session/disposed', session => { this.stopSessionBridge(String(session.id)) }, { global: true })
    ctx.effect(() => () => {
      for (const controller of this.bridges.values()) controller.abort()
      this.bridges.clear()
    }, 'orbit: stop Session Bridges')
  }

  /**
   * Name the runnable Workflows in the model's context, so it does not have to
   * ask before it can tell whether Orbit is relevant to what was just said.
   *
   * The contribution is read synchronously at every assembly, so it can only
   * ever report what has already been fetched: a stale entry answers now and
   * refreshes for next time. The alternative — blocking assembly on a Runtime
   * that may not be running — would make a missing Orbit everyone's problem.
   */
  private tellTheModelWhatCanRun(ctx: Context): void {
    const systemPrompt = ctx.get('systemPrompt') as unknown as {
      context(entry: { name: string; order: number; text: () => string }): () => void
    } | undefined
    if (!systemPrompt) return
    ctx.effect(() => systemPrompt.context({
      name: 'orbit-workflows',
      // After the tool guidance it belongs with: this says which Workflows the
      // tools above can be pointed at.
      order: 190,
      text: () => {
        for (const workspace of this.bridgedWorkspaces.values()) {
          if (this.catalog.stale(workspace.canonicalPath)) this.refreshCatalog(workspace)
        }
        return this.catalog.render()
      },
    }), 'orbit: runnable Workflows in the model context')
  }

  /**
   * Read a Workspace's Workflows into the catalog; a failure leaves the last
   * answer standing.
   *
   * The parameter is a `scope` and not a `workspace` because that is what it
   * is: every caller derived it from a Session. The name is also what the
   * bundle's guard reads, so calling it anything else is how this stops being
   * checked.
   */
  private refreshCatalog(scope: WorkspaceRef): Promise<void> {
    return this.gateway.call(scope, 'catalog', 'list_workflows', { ready_only: true })
      .then(result => {
        this.catalog.remember(
          scope.canonicalPath,
          (result as { workflows: WorkflowSummary[] }).workflows,
        )
      })
      .catch(() => { /* an unreachable Runtime is not worth a turn's worth of noise */ })
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
              if (size > 256 * 1024) {
                req.destroy()
                throw new Error('Orbit client request exceeds 256 KiB')
              }
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
      case 'getRuntimeUi': return await this.getRuntimeUi(String(args[0]), signal)
      case 'getPanelState': return await this.getPanelState(String(args[0]), signal)
      case 'getRunDetail': return await this.getRunDetail(String(args[0]), String(args[1]), signal)
      case 'getWorkflowDefinition': return await this.getWorkflowDefinition(String(args[0]), String(args[1]), signal)
      case 'getStepOutput': return await this.getStepOutput(String(args[0]), String(args[1]), String(args[2]), Number(args[3]), signal)
      case 'runCommand': return await this.runCommand(String(args[0]), String(args[1]), args[2] as 'langgraph_run.cancel' | 'langgraph_run.resume', Number(args[3]), args[4], args[5] === undefined ? undefined : String(args[5]), signal)
      case 'reconcileStep': return await this.reconcileStep(String(args[0]), String(args[1]), String(args[2]), args[3] as 'confirmed_succeeded' | 'confirmed_failed', String(args[4]), signal)
      case 'getDiagnostics': return await this.getDiagnostics(args[0] as WorkspaceRef, String(args[1]), signal)
      case 'listWorkflows': return await this.listWorkflows(args[0] as WorkspaceRef, String(args[1]), signal)
      case 'listRuns': return await this.listRuns(args[0] as WorkspaceRef, String(args[1]), args[2] === undefined ? undefined : String(args[2]), signal)
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

  /**
   * Turn a caller-supplied Workspace into one this Host vouches for.
   *
   * The browser sends a Workspace with every call, and a browser is not an
   * authority on which directory a Session belongs to: the Session is. A
   * mismatch is refused rather than quietly corrected, because the two
   * disagreeing at all means the caller is describing a Session it is not in.
   */
  private async verified(claimed: WorkspaceRef, sessionId: string): Promise<WorkspaceRef> {
    const actual = await this.workspaceForSession(this.liveSession(sessionId))
    if (claimed.id !== actual.id || claimed.canonicalPath !== actual.canonicalPath) {
      throw new Error('Orbit request Workspace does not match the Harness Session')
    }
    return actual
  }

  /**
   * The same guarantee for the Settings panel, which has a Workspace but no
   * Session. Its authority is the Workspace registry: a path nobody registered
   * is not somewhere this Host will go looking for a Runtime.
   */
  private async registered(claimed: WorkspaceRef): Promise<WorkspaceRef> {
    const found = await this.workspaceRegistry.resolveByPath(claimed.canonicalPath)
    if (!found || String(found.id) !== claimed.id || found.path !== claimed.canonicalPath) {
      throw new Error('Orbit request names a Workspace this Harness has not registered')
    }
    return { id: String(found.id), canonicalPath: found.path }
  }

  /**
   * The Workspace of a Session, derived and never claimed.
   *
   * Stronger than `verified`: there is no caller-supplied value to disagree
   * with, so there is nothing to check.
   */
  private async sessionWorkspace(sessionId: string): Promise<WorkspaceRef> {
    return await this.workspaceForSession(this.liveSession(sessionId))
  }

  private liveSession(sessionId: string): Session {
    const session = this.hostSessions.list().find(item => String(item.id) === sessionId)
    if (!session) throw new Error('Orbit requires a live Harness Session')
    return session
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
    // Only a disposed Session stops a Bridge, and nothing can ask a disposed
    // Session for its diagnostics. Keeping the entry would grow this map by one
    // for every Session the Host ever opened.
    this.bridgeDiagnostics.delete(sessionId)
    this.bridgedWorkspaces.delete(sessionId)
    this.agentsByWorkspace.clear()
  }

  private async runSessionBridge(ctx: Context, session: Session, cwd: string, signal: AbortSignal): Promise<void> {
    const registry = ctx.workspaceRegistry as WorkspaceRegistry
    const registered = await registry.resolveByPath(cwd)
    const workspace: WorkspaceRef = {
      id: registered ? String(registered.id) : `cwd:${cwd}`,
      canonicalPath: registered?.path ?? cwd,
    }
    this.bridgedWorkspaces.set(String(session.id), workspace)
    // Warm it before the first turn asks: a Bridge starts when the Session is
    // created, and the person types afterwards.
    this.refreshCatalog(workspace)
    let cursorPosition = restoredBridgeState(session.events).position
    const cursor: OrbitCursorStore = {
      load: () => cursorPosition || undefined,
      save: (_workspaceId, _sessionId, position) => {
        cursorPosition = position
        this.bridgeDiagnostics.set(String(session.id), {
          state: 'connected', cursorPosition: position, updatedAt: new Date().toISOString(),
        })
      },
    }
    await bridgeWithRetry({
      events: () => session.events as readonly StoredOrbitEvent[],
      attempt: async knownRuns => {
        await this.bridgeSession(workspace, session, cursor, signal, knownRuns)
      },
      onWaiting: message => {
        this.bridgeDiagnostics.set(String(session.id), {
          state: 'waiting', cursorPosition, lastError: message, updatedAt: new Date().toISOString(),
        })
        ctx.logger.warn(`Orbit bridge for Session ${String(session.id)} is waiting: ${message}`)
      },
      signal,
    })
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
    const scope = await this.registered(workspace)
    const release = await this.gateway.acquire(scope)
    try {
      const capabilities = await this.gateway.call(scope, 'probe', 'get_capabilities', {}) as Record<string, unknown>
      return { workspaceId: scope.id, state: 'ready', capabilities }
    } finally { await release() }
  }

  @Remote('getRuntimeUi')
  async getRuntimeUi(sessionId: string, signal: AbortSignal): Promise<string> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try { return await this.gateway.uiUrl(scope) }
    finally { await release() }
  }

  /**
   * Everything the resident panel draws, in one round trip.
   *
   * It takes a Session and derives the Workspace, so a poller that runs every
   * couple of seconds carries no claim the Host has to check — and the panel
   * never has to know what a Workspace is.
   */
  @Remote('getPanelState')
  async getPanelState(sessionId: string, signal: AbortSignal): Promise<{
    runs: RunDto[]; uiUrl: string
    workflows: readonly WorkflowSummary[]; agents: readonly AgentSummary[]
  }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      // The panel is a view of the Workspace, not of one chat: a Run started in
      // Orbit's own UI is the same Run, and a History that hid it would sit
      // empty beside a Runtime full of work.
      const result = await this.gateway.call(scope, sessionId, 'list_runs', {
        limit: 50, owner: 'workspace',
      }) as { runs: RunDto[] }
      // The catalog the model is told about, read rather than fetched again:
      // a poll running every two seconds should not ask twice for something
      // that changes when someone publishes a Workflow.
      if (this.catalog.stale(scope.canonicalPath)) this.refreshCatalog(scope)
      if (!this.agentsByWorkspace.has(scope.canonicalPath)) {
        // Read once and kept: a sealed registry cannot change under us, and a
        // poll every two seconds should not keep asking a question whose answer
        // is fixed for the life of the Runtime.
        const listed = await this.gateway.call(scope, sessionId, 'list_agents', {}) as {
          agents: AgentSummary[]
        }
        this.agentsByWorkspace.set(scope.canonicalPath, listed.agents)
      }
      return {
        runs: result.runs,
        uiUrl: await this.gateway.uiUrl(scope),
        workflows: this.catalog.list(scope.canonicalPath),
        agents: this.agentsByWorkspace.get(scope.canonicalPath) ?? [],
      }
    } finally { await release() }
  }

  /**
   * The steps of one Run, for a panel row the reader opened.
   *
   * Session-scoped like the panel's poll: a Run id is not a capability, so the
   * Workspace it is read in comes from the Session rather than from the caller.
   */
  @Remote('getRunDetail')
  async getRunDetail(sessionId: string, runId: string, signal: AbortSignal): Promise<{
    steps: StepSummary[]
  }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      return await this.gateway.call(scope, sessionId, 'get_run_steps', {
        run_id: runId, owner: 'workspace',
      }) as { steps: StepSummary[] }
    } finally { await release() }
  }

  /**
   * The steps one Workflow is published with — read on demand, never polled.
   *
   * A definition changes only when someone republishes it, so this is fetched
   * when a reader opens a Workflow and not again.
   */
  @Remote('getWorkflowDefinition')
  async getWorkflowDefinition(
    sessionId: string, workflowId: string, signal: AbortSignal,
  ): Promise<{ nodes: WorkflowNode[] }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      return await this.gateway.call(scope, sessionId, 'get_workflow_definition', {
        workflow_id: workflowId,
      }) as { nodes: WorkflowNode[] }
    } finally { await release() }
  }

  @Remote('getStepOutput')
  async getStepOutput(
    sessionId: string, runId: string, nodeId: string, after: number, signal: AbortSignal,
  ): Promise<OutputPage> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      return await this.gateway.call(scope, sessionId, 'read_run_output', {
        run_id: runId, after, node_id: nodeId, owner: 'workspace',
      }) as OutputPage
    } finally { await release() }
  }

  /**
   * Cancel or resume a Run from the panel.
   *
   * `expectedRevision` is what the panel had on screen, and it must still be
   * what Orbit advertises. Re-reading here would make the call succeed against
   * a Run that changed under the reader — the refusal is the point: whoever
   * pressed the button was looking at something else.
   */
  @Remote('runCommand')
  async runCommand(
    sessionId: string, runId: string,
    command: OrbitRunCommand,
    expectedRevision: number, value: unknown, interruptId: string | undefined,
    signal: AbortSignal,
  ): Promise<RunDto> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      // Deliberately the caller's scope, unlike the reads above: a write also
      // records who acted, so acting on a Run started elsewhere would file it
      // under this Session. Orbit answers "not found" for one it does not own,
      // which is true and useless here — the panel showed the Run, so the
      // reader knows it exists.
      const run = await this.gateway.run(scope, sessionId, runId).catch((reason: unknown) => {
        if (/not found/i.test(String(reason))) {
          throw new Error('This Run was started elsewhere; act on it where it began, or in Orbit')
        }
        throw reason
      })
      const advertised = advertisedAt(run, command, expectedRevision)
      if (advertised === undefined) {
        throw new Error(`Orbit no longer offers ${command} at revision ${String(expectedRevision)}`)
      }
      return await this.gateway.call(
        scope, sessionId, commandTool(command),
        {
          run_id: runId, expected_version: advertised.expected_version,
          idempotency_key: crypto.randomUUID(),
          ...(value === undefined ? {} : { value }),
          ...(interruptId === undefined ? {} : { interrupt_id: interruptId }),
        },
      ) as RunDto
    } finally { await release() }
  }

  /** Record a person's ruling on what an external Agent actually did. */
  @Remote('reconcileStep')
  async reconcileStep(
    sessionId: string, runId: string, delegationId: string,
    outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal,
  ): Promise<{ steps: StepSummary[] }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      await this.gateway.call(scope, sessionId, 'reconcile_delegation', {
        delegation_id: delegationId, outcome, note,
        idempotency_key: crypto.randomUUID(),
      })
      return await this.gateway.call(scope, sessionId, 'get_run_steps', {
        run_id: runId,
      }) as { steps: StepSummary[] }
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

  private async workspaceForSession(session: Session): Promise<WorkspaceRef> {
    const cwd = session.header.cwd
    if (!cwd) throw new Error('Orbit requires the Harness Session to have a Workspace cwd')
    const registered = await this.workspaceRegistry.resolveByPath(cwd)
    return {
      id: registered ? String(registered.id) : `cwd:${cwd}`,
      canonicalPath: registered?.path ?? cwd,
    }
  }

  @Remote('generateWorkflow')
  async generateWorkflow(workspace: WorkspaceRef, sessionId: string, prompt: string, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    if (!prompt.trim() || prompt.length > 20_000) throw new Error('Workflow prompt must be 1-20000 characters')
    const scope = await this.verified(workspace, sessionId)
    return await this.gateway.call(scope, sessionId, 'generate_workflow', {
      prompt: prompt.trim(), display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
    }) as AuthoringJob
  }

  @Remote('modifyWorkflow')
  async modifyWorkflow(workspace: WorkspaceRef, sessionId: string, workflowId: string, prompt: string, regenerate: boolean, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    if (!workflowId.trim()) throw new Error('Workflow id is required')
    if (!prompt.trim() || prompt.length > 20_000) throw new Error('Workflow prompt must be 1-20000 characters')
    const scope = await this.verified(workspace, sessionId)
    return await this.gateway.call(scope, sessionId, 'modify_workflow', {
      workflow_id: workflowId, prompt: prompt.trim(), mode: regenerate ? 'regenerate' : 'modify',
      display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
    }) as AuthoringJob
  }

  @Remote('getAuthoringJob')
  async getAuthoringJob(workspace: WorkspaceRef, sessionId: string, jobId: string, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    const scope = await this.verified(workspace, sessionId)
    return await this.gateway.call(scope, sessionId, 'get_authoring_job', { job_id: jobId }) as AuthoringJob
  }

  @Remote('getRun')
  async getRun(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunDto> {
    signal.throwIfAborted()
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try { return await this.gateway.run(scope, sessionId, runId) }
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
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      return await this.gateway.call(scope, sessionId, 'read_run_output', {
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
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try { return await this.gateway.call(scope, sessionId, 'read_artifact', { artifact_id: artifactId }) as ArtifactSummary }
    finally { await release() }
  }

  @Remote('getArtifactContent')
  async getArtifactContent(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactContent> {
    signal.throwIfAborted()
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try { return await this.gateway.call(scope, sessionId, 'read_artifact_content', { artifact_id: artifactId }) as ArtifactContent }
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
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      await this.gateway.call(scope, sessionId, 'reconcile_delegation', {
        delegation_id: delegationId, outcome, note,
        idempotency_key: crypto.randomUUID(),
      })
      const result = await this.gateway.call(scope, sessionId, 'get_run_steps', { run_id: runId }) as { steps: StepSummary[] }
      return result.steps
    } finally { await release() }
  }

  private async readRunField<T>(workspace: WorkspaceRef, sessionId: string, runId: string, tool: string, field: string, signal: AbortSignal): Promise<T> {
    signal.throwIfAborted()
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      const result = await this.gateway.call(scope, sessionId, tool, { run_id: runId }) as Record<string, unknown>
      return result[field] as T
    } finally { await release() }
  }

  private async readListField<T>(workspace: WorkspaceRef, sessionId: string, tool: string, field: string, arguments_: object, signal: AbortSignal): Promise<T[]> {
    signal.throwIfAborted()
    const scope = await this.verified(workspace, sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      const result = await this.gateway.call(scope, sessionId, tool, arguments_) as Record<string, unknown>
      return result[field] as T[]
    } finally { await release() }
  }

  @Remote('executeCommand')
  async executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto> {
    signal.throwIfAborted()
    const scope = await this.verified(request.workspace, request.sessionId)
    const release = await this.gateway.acquire(scope)
    try {
      const run = await this.gateway.run(scope, request.sessionId, request.runId)
      const advertised = advertisedAt(run, request.command, request.expectedVersion)
      if (advertised === undefined) throw new Error('Orbit command is no longer advertised at this revision')
      const tool = commandTool(request.command)
      return await this.gateway.call(scope, request.sessionId, tool, {
        run_id: request.runId, expected_version: request.expectedVersion,
        idempotency_key: request.idempotencyKey, value: request.value,
        interrupt_id: request.interruptId,
      }) as RunDto
    } finally { await release() }
  }
}

export default OrbitRemoteService
