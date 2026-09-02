import type { Context } from '@deepseek-ai/cordis'
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol'
import { OrbitGateway, OrbitSessionBridge, WorkflowCatalog, advertisedAt, artifactFilename, commandTool, goalRuns, isLive, readableAsText, sessionCanBridge, type OrbitCursorStore, type OrbitRunCommand } from '@orbit-runtime/integration-core'
import type { AgentSummary, ArtifactContent, ArtifactSummary, AuthoringJob, AuthoringOutputPage, AuthoringSummary, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowNode, WorkflowSummary, WorkspaceRef } from '@orbit-runtime/integration-core'
import { OrbitToolBridge } from './orbit-tools.js'
import type { Session, SessionStore } from '@deepseek-ai/dsh-session'
import type { WorkspaceRegistry } from '@deepseek-ai/dsh-workspace'
import type { AttachmentStore } from '@deepseek-ai/dsh-attachment'
import type { AssembleContext } from '@deepseek-ai/dsh-system-prompt'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { mkdir, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { createUserMessage, type UserMessage } from '@deepseek-ai/dsh-llm'
import { artifactImageInput } from './artifact-import.js'
import {
  CLAIM_RETRY_MS, CLAIM_WAIT_SECONDS, answerFrom, authoringClientForSession,
  claimOnce, isUnknownToolError,
  type ClaimedRequest,
} from '@orbit-runtime/integration-core'

declare module '@deepseek-ai/cordis' { interface Context { orbit: OrbitRemoteService } }

/** The slice of the Agent registry this Host uses to drive one Session. */
interface AgentLookup {
  get(id: string): {
    session: { events: readonly { type: string; data?: unknown }[] }
    /** Queue an ordinary turn and wake the driver. On the Agent, not its
     *  inbox — the inbox is the durable projection, the Agent is what runs. */
    followup(message: UserMessage): void
    whenIdle(): Promise<void>
  } | undefined
}

/** How long to let one authoring turn run before giving the request back.
 *  Under the broker's own lease, so this Host stops waiting before Orbit
 *  stops expecting it to. */
const AUTHORING_TURN_MS = 240_000

interface OrbitWebServer {
  register(route: { kind: 'exact'; path: string; handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void> }): () => void
}

/** How long a settled job stays on the panel before it stops being news. */
const AUTHORING_LINGER_MS = 60_000

/**
 * How many Runs the panel poll will read steps for.
 *
 * Steps are one extra Runtime read per Run, on a two-second poll, so this is
 * not free the way the Run list is — the list is one read whatever its length.
 * A Workspace with more than a handful of Runs moving at once has a different
 * problem than a missing progress line, and the ones past the cap are the ones
 * furthest down a list ordered by recency.
 */
const LIVE_STEP_LIMIT = 6

interface TrackedAuthoring {
  /** The Session that started it; `get_authoring_job` answers only to it. */
  readonly sessionId: string
  job: AuthoringJob
  /** When it settled, or undefined while it is still going. */
  settledAt?: number
  /** Set only after the newly published Workflow was actually read back. */
  catalogRefreshed?: boolean
}

export class OrbitRemoteService extends TypertRemoteService {
  static inject = ['sessions', 'workspaceRegistry', 'tools', 'attachments', 'systemPrompt', 'agents']
  private readonly gateway = new OrbitGateway()
  private readonly agents: AgentLookup | undefined
  private readonly catalog = new WorkflowCatalog()
  /** Authoring jobs started from this Harness, per Workspace.
   *
   *  Held here because there is nothing to ask: a job is addressed by an id
   *  the starter was handed, and `get_authoring_job` is scoped to the actor
   *  that created it. Jobs started in Orbit's own UI are shown by Orbit's own
   *  UI, which has the whole authoring surface. */
  private readonly authoringByWorkspace = new Map<string, Map<string, TrackedAuthoring>>()
  private readonly bridges = new Map<string, AbortController>()
  /** One entry per live Bridge: the Workspaces worth knowing the Workflows of. */
  private readonly bridgedWorkspaces = new Map<string, WorkspaceRef>()
  /** Live authoring consumers, keyed by the exact Harness Session they drive. */
  private readonly authoringWaiters = new Map<string, AbortController>()
  /** The last thing that went wrong while writing a Workflow here. */
  private readonly authoringTrouble = new Map<
    string, { stage: string; error: string; at: string }
  >()
  private readonly bridgeDiagnostics = new Map<string, { state: string; cursorPosition: number; lastError?: string; updatedAt: string }>()
  /** Deleted Workflows the panel still has to name, by Workspace and id. */
  private readonly retiredNames = new Map<string, string | null>()
  private readonly hostSessions: SessionStore
  private readonly attachments: AttachmentStore
  private readonly workspaceRegistry: WorkspaceRegistry
  constructor(ctx: Context) {
    super(ctx, 'orbit')
    this.hostSessions = ctx.get('sessions') as unknown as SessionStore
    this.attachments = ctx.get('attachments') as unknown as AttachmentStore
    this.workspaceRegistry = ctx.get('workspaceRegistry') as unknown as WorkspaceRegistry
    // Captured here like every other service, rather than read from `this.ctx`
    // at the moment it is needed: a service this Host cannot reach is a fact
    // about how it was composed, and finding that out at the instant a
    // Workflow needs writing is finding out in the worst place.
    this.agents = ctx.get('agents') as unknown as AgentLookup | undefined
    new OrbitToolBridge(ctx, this.gateway, (workspace, sessionId, job) => {
      this.watchAuthoring(workspace, sessionId, job)
    }).register()
    this.registerWebApi(ctx)
    this.tellTheModelWhatCanRun(ctx)
    for (const session of this.hostSessions.list()) this.startSessionBridge(ctx, session)
    ctx.on('session/created', session => { this.startSessionBridge(ctx, session) }, { global: true })
    ctx.on('session/disposed', session => { this.stopSessionBridge(String(session.id)) }, { global: true })
    ctx.effect(() => () => {
      for (const controller of this.bridges.values()) controller.abort()
      this.bridges.clear()
      for (const controller of this.authoringWaiters.values()) controller.abort()
      this.authoringWaiters.clear()
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
      context(entry: { name: string; order: number; text: (context: AssembleContext) => string }): () => void
    } | undefined
    if (!systemPrompt) return
    ctx.effect(() => systemPrompt.context({
      name: 'orbit-workflows',
      // After the tool guidance it belongs with: this says which Workflows the
      // tools above can be pointed at.
      order: 190,
      text: context => {
        const sessionId = context.agent === undefined
          ? undefined : String(context.agent.session.id)
        const workspace = sessionId === undefined
          ? undefined : this.bridgedWorkspaces.get(sessionId)
        if (workspace === undefined) return ''
        if (this.catalog.stale(workspace.canonicalPath)) this.refreshCatalog(workspace)
        return this.catalog.render(workspace.canonicalPath)
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
  private watchAuthoring(workspace: WorkspaceRef, sessionId: string, job: AuthoringJob): void {
    const held = this.authoringByWorkspace.get(workspace.canonicalPath)
      ?? new Map<string, TrackedAuthoring>()
    held.set(job.job_id, { sessionId, job })
    this.authoringByWorkspace.set(workspace.canonicalPath, held)
  }

  /**
   * Bring the tracked jobs up to date, and say which of them are worth drawing.
   *
   * Reports whether one of them has just published, which is the one case
   * where this Host knows the catalog it holds is out of date without being
   * told — so the caller re-reads rather than making a person press refresh
   * for a Workflow they asked for and watched arrive.
   */
  private async readAuthoring(
    scope: WorkspaceRef,
  ): Promise<{ jobs: AuthoringSummary[]; published: boolean }> {
    const held = this.authoringByWorkspace.get(scope.canonicalPath)
    if (!held?.size) return { jobs: [], published: false }
    let published = false
    const now = Date.now()
    for (const [jobId, tracked] of [...held]) {
      if (tracked.settledAt !== undefined) {
        if (now - tracked.settledAt > AUTHORING_LINGER_MS) held.delete(jobId)
        else if (tracked.job.status === 'done' && !tracked.catalogRefreshed) published = true
        continue
      }
      try {
        tracked.job = await this.gateway.call(
          scope, tracked.sessionId, 'get_authoring_job', { job_id: jobId },
        ) as AuthoringJob
      } catch {
        // An unreachable Runtime is not an outcome. The row stands as it was
        // and the next poll asks again.
        continue
      }
      if (tracked.job.status === 'queued' || tracked.job.status === 'running') continue
      tracked.settledAt = now
      if (tracked.job.status === 'done') published = true
    }
    return { published, jobs: [...held.values()].map(({ job }) => ({
      job_id: job.job_id, status: job.status, prompt: job.prompt,
      requested_agent: job.requested_agent ?? null,
      workflow_id: job.workflow_id ?? null,
      error: job.error?.message ?? null,
      output_href: job.output_href ?? null,
    })) }
  }

  private markPublishedCatalogRefreshed(scope: WorkspaceRef): void {
    for (const tracked of this.authoringByWorkspace.get(scope.canonicalPath)?.values() ?? []) {
      if (tracked.job.status === 'done') tracked.catalogRefreshed = true
    }
  }

  @Remote('getAuthoringOutput')
  async getAuthoringOutput(
    sessionId: string, outputHref: string, after: number, signal: AbortSignal,
  ): Promise<AuthoringOutputPage> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    return await this.gateway.authoringOutput(scope, sessionId, outputHref, after)
  }

  private refreshCatalog(scope: WorkspaceRef): Promise<boolean> {
    // Everything, not `ready_only`: two readers share this, and they want
    // different halves. The model is offered only what it can start, which
    // `WorkflowCatalog.render` filters for; the panel lists the catalog as it
    // is, including the entries someone has to go and fix.
    return this.gateway.call(scope, 'catalog', 'list_workflows', {})
      .then(result => {
        this.catalog.remember(
          scope.canonicalPath,
          (result as { workflows: WorkflowSummary[] }).workflows,
        )
        return true
      })
      .catch(() => false) // Background warming retries when the entry stays stale.
  }

  private registerWebApi(ctx: Context): void {
    let registered = false
    const mount = () => {
      if (registered) return
      const webServer = (ctx.get('webServer') ?? ctx.get('httpServer')) as OrbitWebServer | undefined
      if (!webServer) return
      registered = true
      /**
       * Hand a browser the bytes of one Artifact.
       *
       * A GET, because a link is what a person clicks and a browser is what
       * renders the result. It exists because Orbit's own address for an
       * Artifact cannot serve one: Artifacts are owned by the actor that
       * produced them, a browser reaching `/api/v1` on loopback is `local`,
       * and the Runs this panel starts belong to `harness:session:<id>`. So
       * the link was a 404 for every Artifact this Harness ever made.
       *
       * This route is that identity. It reads the Artifact as the Session that
       * owns it and passes the bytes through unchanged — no gallery, no
       * viewer, no second drawing of anything Orbit draws. The browser opens
       * what it was given, exactly as it would have from Orbit's own URL.
       */
      ctx.effect(() => webServer.register({
        kind: 'exact', path: '/plugins/dsh-orbit/artifact',
        handler: async (req, res) => {
          const send = (status: number, body: string) => {
            res.writeHead(status, {
              'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store',
            })
            res.end(body)
          }
          try {
            if (req.method !== 'GET') return send(405, 'GET only')
            const query = new URL(req.url ?? '', 'http://localhost').searchParams
            const sessionId = query.get('session') ?? ''
            const artifactId = query.get('id') ?? ''
            if (!sessionId || !artifactId) return send(400, 'session and id are required')
            const scope = await this.sessionWorkspace(sessionId)
            const held = await this.gateway.call(
              scope, sessionId, 'read_artifact_content', { artifact_id: artifactId },
            ) as ArtifactContent
            const bytes = Buffer.from(held.content, 'base64')
            res.writeHead(200, {
              // What Orbit recorded it as, so the browser treats it the way it
              // would have coming from Orbit. A missing type is bytes, not a
              // guess: guessing is how a text file renders as a download and a
              // script renders as a script.
              'content-type': String(held.artifact.content_type || 'application/octet-stream'),
              'content-length': String(bytes.length),
              'cache-control': 'no-store',
              // Never inline into this page's origin. The bytes are whatever a
              // workflow wrote, and this origin is the Harness the person is
              // signed in to.
              'content-security-policy': "sandbox; default-src 'none'",
              'x-content-type-options': 'nosniff',
              ...(typeof held.artifact.filename === 'string' && held.artifact.filename
                ? { 'content-disposition': `inline; filename*=UTF-8''${encodeURIComponent(held.artifact.filename)}` }
                : {}),
            })
            res.end(bytes)
          } catch (error) {
            send(404, String(error))
          }
        },
      }), 'orbit: Artifact bytes for a browser link')
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
      case 'getPanelState': return await this.getPanelState(String(args[0]), Boolean(args[1]), Boolean(args[2]), signal)
      case 'generateWorkflowForSession': return await this.generateWorkflowForSession(String(args[0]), String(args[1]), signal)
      case 'getAuthoringOutput': return await this.getAuthoringOutput(String(args[0]), String(args[1]), Number(args[2]), signal)
      case 'getRunDetail': return await this.getRunDetail(String(args[0]), String(args[1]), signal)
      case 'getWorkflowDefinition': return await this.getWorkflowDefinition(String(args[0]), String(args[1]), signal)
      case 'getStepOutput': return await this.getStepOutput(String(args[0]), String(args[1]), String(args[2]), Number(args[3]), signal)
      case 'runCommand': return await this.runCommand(String(args[0]), String(args[1]), args[2] as 'langgraph_run.cancel' | 'langgraph_run.resume', Number(args[3]), args[4], args[5] === undefined ? undefined : String(args[5]), signal)
      case 'reconcileStep': return await this.reconcileStep(String(args[0]), String(args[1]), String(args[2]), args[3] as 'confirmed_succeeded' | 'confirmed_failed', String(args[4]), signal)
      case 'exportArtifact': return await this.exportArtifact(String(args[0]), String(args[1]), signal)
      case 'readArtifactText': return await this.readArtifactText(String(args[0]), String(args[1]), signal)
      case 'getDiagnostics': return await this.getDiagnostics(args[0] as WorkspaceRef, String(args[1]), signal)
      case 'stopRuntime': return await this.stopRuntime(String(args[0]), signal)
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
  private async sessionWorkspace(
    sessionId: string, allowPersisted = false,
  ): Promise<WorkspaceRef> {
    const live = this.hostSessions.list().find(item => String(item.id) === sessionId)
    if (live) return await this.workspaceForSession(live)
    if (allowPersisted) {
      // Since Harness rc.6, opening a persisted conversation in the browser
      // doesn't necessarily enter its Host Session until an Agent turn needs
      // it. popupSelect is deliberately client-owned, so its options request
      // can arrive in that interval. The durable Workspace registry is still
      // authoritative for the Session's cwd and is safe for this read-only
      // panel projection; mutations continue through liveSession()/verified().
      const matches = this.workspaceRegistry.list().filter(workspace =>
        workspace.sessionIds.some(id => String(id) === sessionId),
      )
      if (matches.length === 1) {
        return { id: String(matches[0].id), canonicalPath: matches[0].path }
      }
      if (matches.length > 1) {
        // An inconsistent durable index grants no authority. Keep the same
        // classified failure as an absent live Session rather than exposing
        // registry internals in the panel.
        throw new Error('Orbit requires a live Harness Session')
      }
    }
    throw new Error('Orbit requires a live Harness Session')
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
    void this.bindSessionWorkspace(ctx, session, cwd).finally(() => {
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
    const workspace = this.bridgedWorkspaces.get(sessionId)
    this.bridgedWorkspaces.delete(sessionId)
    if (
      workspace !== undefined
      && ![...this.bridgedWorkspaces.values()].some(
        item => item.canonicalPath === workspace.canonicalPath,
      )
    ) this.catalog.forget(workspace.canonicalPath)
    this.authoringWaiters.get(sessionId)?.abort()
    this.authoringWaiters.delete(sessionId)
  }

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
  private async bindSessionWorkspace(ctx: Context, session: Session, cwd: string): Promise<void> {
    const registry = ctx.workspaceRegistry as WorkspaceRegistry
    const registered = await registry.resolveByPath(cwd)
    const workspace: WorkspaceRef = {
      id: registered ? String(registered.id) : `cwd:${cwd}`,
      canonicalPath: registered?.path ?? cwd,
    }
    this.bridgedWorkspaces.set(String(session.id), workspace)
    this.refreshCatalog(workspace)
    this.bridgeDiagnostics.set(String(session.id), {
      state: 'bound', cursorPosition: 0, updatedAt: new Date().toISOString(),
    })
    this.waitForAuthoring(ctx, workspace, String(session.id))
  }

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
  private waitForAuthoring(ctx: Context, scope: WorkspaceRef, sessionId: string): void {
    if (this.authoringWaiters.has(sessionId)) return
    const controller = new AbortController()
    this.authoringWaiters.set(sessionId, controller)
    const client = authoringClientForSession(sessionId)
    const loop = async () => {
      while (!controller.signal.aborted) {
        const outcome = await claimOnce({
          wait: async seconds => await this.gateway.call(
            scope, sessionId, 'wait_authoring_request',
            { client, timeout_seconds: seconds },
          ).then(result => (result as { request: ClaimedRequest | null }).request),
          ask: async prompt => await this.askTheSession(sessionId, prompt),
          submit: async (requestId, dsl) => await this.gateway.call(
            scope, sessionId, 'submit_authoring_response',
            { request_id: requestId, dsl },
          ),
          report: (stage, error) => {
            // Logged and kept. The log goes to whichever terminal started the
            // Harness, which is nowhere a person debugging the panel is
            // looking; the diagnostics endpoint is.
            this.authoringTrouble.set(scope.canonicalPath, {
              stage, error: String(error), at: new Date().toISOString(),
            })
            ctx.logger.warn(
              `Orbit authoring ${stage} failed in ${scope.canonicalPath}: ${String(error)}`,
            )
          },
        })
        if (controller.signal.aborted) return
        // A failed round waits before asking again; an expired one is the
        // queue working as intended and goes straight back on it.
        if (outcome === 'failed') {
          await new Promise<void>(resolve => {
            const timer = setTimeout(resolve, CLAIM_RETRY_MS)
            controller.signal.addEventListener(
              'abort', () => { clearTimeout(timer); resolve() }, { once: true },
            )
          })
        }
      }
    }
    void loop().finally(() => {
      if (this.authoringWaiters.get(sessionId) === controller) {
        this.authoringWaiters.delete(sessionId)
      }
    })
  }

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
  private async askTheSession(sessionId: string, prompt: string): Promise<string> {
    if (this.agents === undefined) throw new Error('this Harness exposes no Agent registry')
    const agent = this.agents.get(sessionId)
    if (agent === undefined) throw new Error(`no live Agent for Session ${sessionId}`)
    const mark = agent.session.events.length
    agent.followup(createUserMessage({
      content: [{ type: 'text', text: prompt }],
      source: { kind: 'plugin', plugin: 'orbit' },
    }))
    /* `whenIdle` follows whole-agent quiescence, not this message: a Session
       busy with the person's own turn reaches idle when *that* turn ends,
       which can be before the queued one has run. So idle is a prompt to look
       rather than an answer, and looking is asking whether anything was said.
       Bounded, because an Agent that never answers must give the request back
       while Orbit is still willing to re-offer it. */
    const deadline = Date.now() + AUTHORING_TURN_MS
    for (;;) {
      await agent.whenIdle()
      const said = answerFrom(agent.session.events, mark)
      if (said.trim()) return said
      if (Date.now() >= deadline) return ''
      await new Promise<void>(resolve => setTimeout(resolve, 1_000))
    }
  }

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
    const scope = await this.sessionWorkspace(sessionId, true)
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
  /**
   * Everything the panel draws, in one call.
   *
   * `force` is a person pressing refresh, and it is the difference between a
   * poll and an answer: the catalog and the Agent registry are both held
   * deliberately — one behind a TTL, one for the life of the Runtime — because
   * a two-second poll must not re-ask questions that change when somebody
   * publishes. A press is exactly the case where they may have.
   */
  @Remote('getPanelState')
  async getPanelState(sessionId: string, force: boolean, startIfMissing: boolean, signal: AbortSignal): Promise<{
    runs: RunDto[]; uiUrl: string
    workflows: readonly WorkflowSummary[]; agents: readonly AgentSummary[]
    retiredWorkflowNames: Record<string, string>
    authoring: readonly AuthoringSummary[]
    steps: Record<string, StepSummary[]>
  }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId, true)
    const release = await this.gateway.acquire(scope, startIfMissing)
    try {
      // The panel is a view of the Workspace, not of one chat: a Run started in
      // Orbit's own UI is the same Run, and a History that hid it would sit
      // empty beside a Runtime full of work. It used to say so with
      // `owner: 'workspace'`; the Runtime says it now, because a Runtime
      // serves one Workspace and that is the whole of what a read may see.
      const result = await this.gateway.call(scope, sessionId, 'list_runs', {
        limit: 50,
      }) as { runs: RunDto[] }
      // Before the catalog, so a publish is known in time to re-read it below.
      const authoring = await this.readAuthoring(scope)
      // Awaited whenever this answer is meant to carry the new list: a press,
      // or a publish someone just watched happen. Forgetting the entry and
      // letting a later poll fill it would empty the page at the very moment
      // the Workflow they asked for was supposed to appear on it.
      if (force || authoring.published) {
        const refreshed = await this.refreshCatalog(scope)
        if (authoring.published && refreshed) this.markPublishedCatalogRefreshed(scope)
      }
      else if (this.catalog.stale(scope.canonicalPath)) this.refreshCatalog(scope)
      // Identity is fixed for the Runtime, but attempt totals are not. This is
      // one grouped read and stays beside the Runs it counts on every poll.
      const listed = await this.gateway.call(scope, sessionId, 'list_agents', {}) as {
        agents: AgentSummary[]
      }
      // A Runtime already alive during a Harness upgrade may still expose the
      // older identity-only MCP shape. Orbit's HTTP Agent page has always held
      // these totals, so merge that same projection instead of silently
      // rendering a missing value as zero until somebody restarts Runtime.
      const needsAttemptCounts = listed.agents.some(
        agent => agent.attempt_count === undefined || agent.failed_count === undefined,
      )
      const attemptCounts = needsAttemptCounts
        ? await this.gateway.handlerAttemptCounts(scope, sessionId)
        : undefined
      const agents = listed.agents.map(agent => ({
        ...agent,
        ...(attemptCounts?.get(agent.name) ?? {}),
      }))
      const workflows = this.catalog.list(scope.canonicalPath)
      const retired = await this.retiredWorkflowNames(
        scope, sessionId, result.runs, workflows,
      )
      // A retained definition means the Workflow was deliberately retired and
      // the Run is still a real piece of History. No definition at all means
      // the row is orphaned (for example, from an old or replaced database),
      // and presenting its stale `running` flag as a current Goal invents work
      // the Runtime can no longer inspect or operate.
      const runs = result.runs.filter(run => !retired.missing.has(run.workflow_id))
      return {
        runs,
        uiUrl: await this.gateway.uiUrl(scope),
        workflows,
        agents,
        retiredWorkflowNames: retired.names,
        authoring: authoring.jobs,
        steps: await this.liveSteps(scope, sessionId, runs),
      }
    } finally { await release() }
  }

  /**
   * Names for the Workflows a Run ran and the catalog no longer offers.
   *
   * Deleting a Workflow retires its id; it does not retract the Runs that
   * carry it, which go on executing and being opened. The catalog is the
   * wrong place to look one of those up — a catalog is what can be started —
   * so the panel had nothing to name them by and printed the id, which reads
   * as a Goal pointed at something that is not there. Orbit keeps the
   * definition for exactly this, so ask it.
   *
   * Read once per id and remembered, negative answers included: a retired id
   * is never reissued, so neither answer can go out of date, and a poll that
   * runs every couple of seconds must not re-ask either one.
   */
  private async retiredWorkflowNames(
    scope: WorkspaceRef, sessionId: string,
    runs: readonly RunDto[], listed: readonly WorkflowSummary[],
  ): Promise<{ names: Record<string, string>; missing: Set<string> }> {
    const offered = new Set(listed.map(item => item.workflow_id))
    const retired = [...new Set(runs.map(run => run.workflow_id))]
      .filter(id => !offered.has(id))
    await Promise.all(
      retired
        .filter(id => !this.retiredNames.has(this.retiredKey(scope, id)))
        .map(async id => {
          try {
            const definition = await this.gateway.call(
              scope, sessionId, 'inspect_workflow_definition', { workflow_id: id },
            ) as { name?: string }
            this.retiredNames.set(this.retiredKey(scope, id), definition.name || null)
          } catch (reason) {
            const detail = reason instanceof Error ? reason.message : String(reason)
            if (/workflow (?:version )?not found/iu.test(detail)) {
              // A genuine negative is immutable: retired ids are never reused.
              this.retiredNames.set(this.retiredKey(scope, id), null)
              return
            }
            // Transport, authentication and protocol failures say nothing about
            // whether the definition exists. Leave them uncached so the next
            // panel poll retries instead of printing the id until Host restart.
            throw reason
          }
        }),
    )
    const names: Record<string, string> = {}
    const missing = new Set<string>()
    for (const id of retired) {
      const held = this.retiredNames.get(this.retiredKey(scope, id))
      if (held) names[id] = held
      else if (held === null) missing.add(id)
    }
    return { names, missing }
  }

  private retiredKey(scope: WorkspaceRef, workflowId: string): string {
    return `${scope.canonicalPath}\n${workflowId}`
  }

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
  private async liveSteps(
    scope: WorkspaceRef, sessionId: string, runs: readonly RunDto[],
  ): Promise<Record<string, StepSummary[]>> {
    // Exactly the Runs the Goal page draws, by the same rule it draws them: a
    // Goal that has just finished is still on that page, and its steps have to
    // be re-read once more or it keeps the last step it was seen *running*.
    const drawn = goalRuns(
      runs.map(run => ({ live: isLive(run.status), updatedAt: run.updated_at, run })),
    ).slice(0, LIVE_STEP_LIMIT)
    const read = await Promise.all(drawn.map(async ({ run }) => {
      try {
        const detail = await this.gateway.call(scope, sessionId, 'get_run_steps', {
          run_id: run.run_id,
        }) as { steps: StepSummary[] }
        return [run.run_id, detail.steps.map(step => ({
          node_id: step.node_id, label: step.label, status: step.status,
          has_output: step.has_output, resolution: step.resolution,
          reconciliation: step.reconciliation,
        }))] as const
      } catch { return null }
    }))
    return Object.fromEntries(read.filter(entry => entry !== null))
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
        run_id: runId,
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
        run_id: runId, after, node_id: nodeId,
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
  @Remote('stopRuntime')
  async stopRuntime(sessionId: string, signal: AbortSignal): Promise<{ stopped: true }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    this.authoringWaiters.get(scope.canonicalPath)?.abort()
    this.authoringWaiters.delete(scope.canonicalPath)
    await this.gateway.stopRuntime(scope, sessionId)
    return { stopped: true }
  }

  @Remote('getDiagnostics')
  async getDiagnostics(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<IntegrationDiagnostics> {
    const runtime = await this.getRuntime(workspace, signal)
    return {
      generated_at: new Date().toISOString(), workspace_id: workspace.id,
      session_id: sessionId, runtime, gateway: this.gateway.diagnostics(),
      bridge: this.bridgeDiagnostics.get(sessionId) || null,
      authoring: {
        waiting: this.authoringWaiters.has(sessionId),
        driving: this.authoringWaiters.has(sessionId) ? sessionId : null,
        agentRegistry: this.agents !== undefined,
        lastError: this.authoringTrouble.get(workspace.canonicalPath) ?? null,
      },
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
    const agent = await this.prepareAuthoringRoute(scope, sessionId)
    const job = await this.gateway.call(scope, sessionId, 'generate_workflow', {
      prompt: prompt.trim(), display_language: 'zh-CN', agent,
      idempotency_key: crypto.randomUUID(),
    }) as AuthoringJob
    this.watchAuthoring(scope, sessionId, job)
    return job
  }

  /** Start authoring from a Slash command whose only authority is its Session. */
  @Remote('generateWorkflowForSession')
  async generateWorkflowForSession(
    sessionId: string, prompt: string, signal: AbortSignal,
  ): Promise<AuthoringJob> {
    signal.throwIfAborted()
    if (!prompt.trim() || prompt.length > 20_000) throw new Error(
      'Workflow prompt must be 1-20000 characters',
    )
    const scope = await this.sessionWorkspace(sessionId)
    const release = await this.gateway.acquire(scope, true)
    try {
      const agent = await this.prepareAuthoringRoute(scope, sessionId)
      const job = await this.gateway.call(scope, sessionId, 'generate_workflow', {
        prompt: prompt.trim(), display_language: 'zh-CN',
        agent, idempotency_key: crypto.randomUUID(),
      }) as AuthoringJob
      this.watchAuthoring(scope, sessionId, job)
      return job
    } finally { await release() }
  }

  /** Register this exact Session route before asking Orbit to address work to it. */
  private async prepareAuthoringRoute(
    scope: WorkspaceRef, sessionId: string,
  ): Promise<string> {
    const client = authoringClientForSession(sessionId)
    try {
      await this.gateway.call(scope, sessionId, 'register_authoring_client', { client })
    } catch (error) {
      // Runtimes from before the presence-only registration tool still learn
      // this route from the Session's standing wait_authoring_request call.
      // Ignore only that one protocol-version miss; authorization, transport
      // and every other registration failure remain actionable errors.
      if (!isUnknownToolError(error, 'register_authoring_client')) throw error
    }
    return client
  }

  @Remote('modifyWorkflow')
  async modifyWorkflow(workspace: WorkspaceRef, sessionId: string, workflowId: string, prompt: string, regenerate: boolean, signal: AbortSignal): Promise<AuthoringJob> {
    signal.throwIfAborted()
    if (!workflowId.trim()) throw new Error('Workflow id is required')
    if (!prompt.trim() || prompt.length > 20_000) throw new Error('Workflow prompt must be 1-20000 characters')
    const scope = await this.verified(workspace, sessionId)
    const agent = await this.prepareAuthoringRoute(scope, sessionId)
    return await this.gateway.call(scope, sessionId, 'modify_workflow', {
      workflow_id: workflowId, prompt: prompt.trim(), mode: regenerate ? 'regenerate' : 'modify',
      display_language: 'zh-CN', agent, idempotency_key: crypto.randomUUID(),
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
  @Remote('readArtifactText')
  async readArtifactText(
    sessionId: string, artifactId: string, signal: AbortSignal,
  ): Promise<{ contentType: string; sizeBytes: number; text: string | null }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const meta = await this.gateway.call(
      scope, sessionId, 'read_artifact', { artifact_id: artifactId },
    ) as ArtifactSummary
    const contentType = String(meta.content_type ?? '')
    const sizeBytes = Number(meta.size_bytes ?? 0)
    if (!readableAsText(contentType, sizeBytes)) return { contentType, sizeBytes, text: null }
    const held = await this.gateway.call(
      scope, sessionId, 'read_artifact_content', { artifact_id: artifactId },
    ) as ArtifactContent
    return { contentType, sizeBytes, text: Buffer.from(held.content, 'base64').toString('utf8') }
  }

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
  @Remote('exportArtifact')
  async exportArtifact(
    sessionId: string, artifactId: string, signal: AbortSignal,
  ): Promise<{ path: string }> {
    signal.throwIfAborted()
    const scope = await this.sessionWorkspace(sessionId)
    const held = await this.gateway.call(
      scope, sessionId, 'read_artifact_content', { artifact_id: artifactId },
    ) as ArtifactContent
    const target = join(
      homedir(), 'Downloads',
      artifactFilename(artifactId, held.artifact.content_type, held.artifact.filename),
    )
    await mkdir(dirname(target), { recursive: true })
    // Rewritten rather than skipped when it exists: the name carries the
    // digest, so a file already at that path holds these exact bytes — and one
    // truncated by an interrupted write would otherwise stand forever.
    await writeFile(target, Buffer.from(held.content, 'base64'))
    return { path: target }
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
