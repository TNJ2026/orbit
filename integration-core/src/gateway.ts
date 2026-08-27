import { spawn, type ChildProcess } from 'node:child_process'
import { createHash } from 'node:crypto'
import { open, realpath, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { AuthoringOutputPage, WorkspaceRef, RunDto } from './types.js'
import { decodeRun, decodeToolResult } from './codecs.js'

interface DiscoveredRuntime { project_root?: string; transport?: string; mcp_url?: string; base_url?: string }
interface Managed { mcpUrl: string; baseUrl: string; nextId: number; capabilities: Record<string, unknown> }
type Fetch = typeof globalThis.fetch
class OrbitTransportError extends Error {}
const STARTUP_TIMEOUT_MS = 10_000
const STARTUP_POLL_MS = 100
/* Enough of the end of a failed start to carry a Python traceback's last
   frames and its exception line, which is the part that says what happened. */
const STARTUP_LOG_TAIL_BYTES = 4096
export interface GatewayDiagnostics {
  discoveryAttempts: number
  rpcCalls: number
  transportFailures: number
  connectedWorkspaces: number
  lastConnectedAt?: string
  lastTransportError?: string
}

interface HandlerAttemptSummary { name: string; attempt_count?: number; failed_count?: number }

/** Connect Harness to Orbit over HTTP MCP; explicit UI entry may start it. */
/**
 * How long any one MCP call may take before the transport gives up on it.
 *
 * Exported because a tool that deliberately blocks — `wait_authoring_request`
 * parks until work arrives — has to ask for less than this. Asking for more
 * does not extend the call: it aborts here, the request is cancelled at the
 * Runtime, and the caller is told about a timeout it chose for itself.
 */
export const ORBIT_RPC_TIMEOUT_MS = 60_000

export class OrbitGateway {
  private readonly runtimes = new Map<string, Promise<Managed>>()
  private readonly telemetry: Omit<GatewayDiagnostics, 'connectedWorkspaces'> = {
    discoveryAttempts: 0, rpcCalls: 0, transportFailures: 0,
  }
  constructor(
    private readonly command = 'orbit',
    private readonly commandPrefix: readonly string[] = [],
    private readonly fetchImpl: Fetch = globalThis.fetch,
    private readonly discoveryRoot = process.env.ORBIT_RUNTIME_ROOT || undefined,
  ) {}

  diagnostics(): GatewayDiagnostics {
    return { ...this.telemetry, connectedWorkspaces: this.runtimes.size }
  }

  async acquire(workspace: WorkspaceRef, startIfMissing = false): Promise<() => Promise<void>> {
    // Connects and validates, then hands back a no-op release. A Runtime we
    // start is detached and becomes independent immediately: letting go of a
    // panel must not stop work another Session or terminal may still be using.
    //
    // Stopping one is now possible, but only by asking for it — see
    // `stopRuntime`. The distinction is the whole point: a release is this
    // Host finishing with a Runtime, and a shutdown is a person deciding
    // nobody should have it.
    await this.runtime(workspace, startIfMissing)
    return async () => {}
  }

  /**
   * Ask the Runtime serving this Workspace to stop, and forget it.
   *
   * The Runtime's own command, not a signal: it accepts the request, answers,
   * and then exits through its host, so in-flight work is unwound rather than
   * cut. This Host is allowed to ask because the Runtime was started for it —
   * `serve` vouches for `harness:session:` actors exactly when it carries the
   * Harness tool profile, which is the profile a Gateway starts it with.
   *
   * The cached connection goes whatever the answer was. A Runtime that
   * accepted the request is on its way down, and one that refused is a
   * connection worth re-establishing rather than reusing.
   */
  async stopRuntime(workspace: WorkspaceRef, sessionId: string): Promise<void> {
    if (!/^[A-Za-z0-9:_-]{1,180}$/.test(sessionId)) throw new Error('invalid Harness session id')
    const key = await realpath(workspace.canonicalPath)
    const runtime = await this.runtimeFor(key)
    if (!runtime.baseUrl) throw new Error('this Orbit Runtime published no HTTP address')
    try {
      const response = await this.fetchImpl(`${runtime.baseUrl}/api/v1/runtime/shutdown`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-orbit-actor': `harness:session:${sessionId}`,
          'idempotency-key': crypto.randomUUID(),
        },
        body: JSON.stringify({ expected_version: 0 }),
      })
      if (!response.ok) {
        const detail = await response.text().catch(() => '')
        throw new Error(
          `Orbit refused to stop: HTTP ${String(response.status)}${detail ? ` ${detail.slice(0, 200)}` : ''}`,
        )
      }
    } finally {
      this.runtimes.delete(key)
    }
  }

  async call(workspace: WorkspaceRef, sessionId: string, name: string, args: object): Promise<unknown> {
    if (!/^[A-Za-z0-9:_-]{1,180}$/.test(sessionId)) throw new Error('invalid Harness session id')
    // Resolved once, before anything can fail. A removed Workspace directory is
    // a plausible cause of the transport failure handled below, and resolving it
    // again down there would replace that diagnosis with an ENOENT and skip the
    // cache invalidation the failure was supposed to trigger.
    const key = await realpath(workspace.canonicalPath)
    const runtime = await this.runtimeFor(key)
    const actor = `harness:session:${sessionId}`
    let envelope: { isError?: boolean; structuredContent?: unknown; content?: unknown }
    try {
      envelope = await this.rpc(runtime, 'tools/call', {
        name, arguments: args, _meta: {
          'orbit/actor': actor,
          'orbit/workspace': {
            id: workspace.id,
            canonicalPath: key,
            ...(workspace.repositoryId ? { repositoryId: workspace.repositoryId } : {}),
            ...(workspace.worktreeId ? { worktreeId: workspace.worktreeId } : {}),
            ...(workspace.baseRevision ? { baseRevision: workspace.baseRevision } : {}),
            ...(workspace.isolationMode ? { isolationMode: workspace.isolationMode } : {}),
          },
        },
      }) as typeof envelope
    } catch (error) {
      if (error instanceof OrbitTransportError) {
        this.telemetry.transportFailures++
        this.telemetry.lastTransportError = error.message
        this.runtimes.delete(key)
      }
      throw error
    }
    if (envelope.isError) throw new Error(JSON.stringify(envelope.structuredContent ?? envelope.content))
    return decodeToolResult(name, envelope.structuredContent)
  }

  /**
   * Where a person reads this Runtime, as the Runtime itself reports it.
   *
   * Never assembled from the MCP endpoint: the two are published together by
   * the process that owns the database, and guessing one from the other would
   * survive exactly until they differ.
   */
  async uiUrl(workspace: WorkspaceRef): Promise<string> {
    const runtime = await this.runtime(workspace)
    if (!runtime.baseUrl) throw new Error('Orbit Runtime did not publish a browser address')
    return `${runtime.baseUrl.replace(/\/$/, '')}/ui/`
  }

  /**
   * Read the same durable attempt totals as Orbit's Agent page.
   *
   * This HTTP projection also keeps a newly upgraded Harness compatible with
   * a Runtime process started before `list_agents` grew the aggregate fields.
   */
  async handlerAttemptCounts(
    workspace: WorkspaceRef, sessionId: string,
  ): Promise<ReadonlyMap<string, { attempt_count: number; failed_count: number }>> {
    const runtime = await this.runtime(workspace)
    if (!runtime.baseUrl) return new Map()
    const response = await this.fetchImpl(
      `${runtime.baseUrl.replace(/\/$/, '')}/api/v1/handler-catalog`,
      { headers: { 'x-orbit-actor': `harness:session:${sessionId}` } },
    )
    if (!response.ok) throw new OrbitTransportError(
      `Orbit Handler catalog failed with HTTP ${String(response.status)}`,
    )
    const envelope = await response.json() as {
      data?: { handlers?: HandlerAttemptSummary[] }
    }
    const counts = new Map<string, { attempt_count: number; failed_count: number }>()
    for (const handler of envelope.data?.handlers ?? []) {
      if (typeof handler.name !== 'string') continue
      counts.set(handler.name, {
        attempt_count: Number(handler.attempt_count ?? 0),
        failed_count: Number(handler.failed_count ?? 0),
      })
    }
    return counts
  }

  async authoringOutput(
    workspace: WorkspaceRef, sessionId: string, outputHref: string, after: number,
  ): Promise<AuthoringOutputPage> {
    if (!/^\/api\/v1\/workflow-authoring-jobs\/[^/?#]+\/output$/u.test(outputHref)) {
      throw new Error('Orbit returned an invalid authoring output address')
    }
    if (!Number.isSafeInteger(after) || after < 0) throw new Error('invalid authoring output cursor')
    const runtime = await this.runtime(workspace)
    if (!runtime.baseUrl) throw new Error('Orbit Runtime did not publish a browser address')
    const response = await this.fetchImpl(
      `${runtime.baseUrl.replace(/\/$/, '')}${outputHref}?after=${String(after)}`,
      { headers: { 'x-orbit-actor': `harness:session:${sessionId}` } },
    )
    if (!response.ok) throw new OrbitTransportError(
      `Orbit authoring output failed with HTTP ${String(response.status)}`,
    )
    const envelope = await response.json() as { data?: AuthoringOutputPage }
    if (!envelope.data || !Array.isArray(envelope.data.chunks)) {
      throw new Error('Orbit authoring output returned invalid JSON')
    }
    return envelope.data
  }

  async run(workspace: WorkspaceRef, sessionId: string, runId: string): Promise<RunDto> {
    return decodeRun(await this.call(workspace, sessionId, 'inspect_run', { run_id: runId }))
  }

  private async runtime(workspace: WorkspaceRef, startIfMissing = false): Promise<Managed> {
    return await this.runtimeFor(await realpath(workspace.canonicalPath), startIfMissing)
  }

  private async runtimeFor(key: string, startIfMissing = false): Promise<Managed> {
    let promise = this.runtimes.get(key)
    if (promise === undefined) {
      promise = this.connect(key, startIfMissing)
      this.runtimes.set(key, promise)
      promise.catch(() => this.runtimes.delete(key))
    }
    return await promise
  }

  private async connect(workspaceRoot: string, startIfMissing: boolean): Promise<Managed> {
    let discovered = await this.discover(workspaceRoot)
      ?? (startIfMissing ? await this.startAndDiscover(workspaceRoot) : undefined)
    if (discovered === undefined) throw new Error(
      `No independent Orbit Runtime is serving Workspace ${workspaceRoot}`,
    )
    const deadline = Date.now() + STARTUP_TIMEOUT_MS
    while (true) {
      const runtime: Managed = {
        mcpUrl: discovered.mcp_url!, baseUrl: discovered.base_url ?? '',
        nextId: 1, capabilities: {},
      }
      try {
        await this.rpc(runtime, 'initialize', {
          protocolVersion: '2025-06-18', capabilities: {},
          clientInfo: { name: 'dsh-orbit', version: '0.1.0' },
        })
        runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {}) as Record<string, unknown>
        if (runtime.capabilities.integration_protocol !== 'orbit-harness/1') throw new Error('incompatible Orbit integration protocol')
        this.telemetry.lastConnectedAt = new Date().toISOString()
        this.telemetry.lastTransportError = undefined
        return runtime
      } catch (error) {
        // The owner publishes its address immediately before HTTP starts
        // accepting connections. Auto-start treats that gap as readiness.
        if (!startIfMissing || !(error instanceof OrbitTransportError) || Date.now() >= deadline) throw error
        await new Promise(resolve => setTimeout(resolve, STARTUP_POLL_MS))
        discovered = await this.discover(workspaceRoot) ?? discovered
      }
    }
  }

  private async discover(workspaceRoot: string): Promise<DiscoveredRuntime | undefined> {
    this.telemetry.discoveryAttempts++
    const output = await new Promise<string>((resolve, reject) => {
      const child = spawn(this.command, [
        ...this.commandPrefix, 'runtimes', '--json',
        ...(this.discoveryRoot ? ['--root', this.discoveryRoot] : []),
      ], {
        cwd: workspaceRoot, stdio: ['ignore', 'pipe', 'pipe'],
      })
      let stdout = '', stderr = ''
      child.stdout.setEncoding('utf8'); child.stdout.on('data', chunk => { stdout += chunk })
      child.stderr.setEncoding('utf8'); child.stderr.on('data', chunk => { stderr += chunk })
      child.once('error', reject)
      child.once('exit', code => code === 0 ? resolve(stdout) : reject(new Error(
        `Orbit Runtime discovery failed with code ${String(code)}${stderr ? `: ${stderr.trim()}` : ''}`,
      )))
    })
    let entries: DiscoveredRuntime[]
    try { entries = JSON.parse(output) as DiscoveredRuntime[] }
    catch { throw new Error('Orbit Runtime discovery returned invalid JSON') }
    if (!Array.isArray(entries)) throw new Error('Orbit Runtime discovery must return an array')
    const matches = entries.filter(entry => entry.project_root === workspaceRoot && entry.mcp_url)
    if (matches.length === 0) return undefined
    if (matches.length > 1) throw new Error(`Multiple Orbit Runtimes claim Workspace ${workspaceRoot}`)
    if (matches[0].transport !== 'http') throw new Error('Orbit Runtime is not reachable over HTTP MCP')
    return matches[0]
  }

  private async startAndDiscover(workspaceRoot: string): Promise<DiscoveredRuntime> {
    const logPath = this.startupLogPath(workspaceRoot)
    const child = await this.startRuntime(workspaceRoot)
    const deadline = Date.now() + STARTUP_TIMEOUT_MS
    while (Date.now() < deadline) {
      // Discover first: the process may have published its endpoint just before
      // an unrelated late shutdown, and that endpoint is still the useful fact.
      const discovered = await this.discover(workspaceRoot)
      if (discovered !== undefined) return discovered
      if (child.exitCode !== null) throw new Error(
        `Orbit Runtime auto-start failed with code ${String(child.exitCode)} `
        + `for Workspace ${workspaceRoot}${await this.startupLogTail(logPath)}`,
      )
      await new Promise(resolve => setTimeout(resolve, STARTUP_POLL_MS))
    }
    // Still running, just not answering: its own output is the only account of
    // what it spent the time on.
    throw new Error(
      `Orbit Runtime auto-start timed out for Workspace ${workspaceRoot}`
      + `${await this.startupLogTail(logPath)}`,
    )
  }

  /**
   * Where a starting Runtime's stderr goes.
   *
   * Named for the Workspace so a second Workspace starting at the same moment
   * writes somewhere else, and truncated on each attempt so what is read back
   * is this start's output rather than a previous one's.
   */
  private startupLogPath(workspaceRoot: string): string {
    const stem = createHash('sha256').update(workspaceRoot).digest('hex').slice(0, 12)
    return join(tmpdir(), `dsh-orbit-runtime-${stem}.log`)
  }

  /**
   * The end of a failed start, or nothing.
   *
   * Nothing is a real answer here: the file may not exist, may be empty, or
   * may be unreadable, and none of those is worth replacing the exit code with
   * an error about reading a log file.
   */
  private async startupLogTail(logPath: string): Promise<string> {
    try {
      const { size } = await stat(logPath)
      if (size === 0) return ''
      const handle = await open(logPath, 'r')
      try {
        const length = Math.min(size, STARTUP_LOG_TAIL_BYTES)
        const buffer = Buffer.alloc(length)
        await handle.read(buffer, 0, length, size - length)
        const text = buffer.toString('utf8').trim()
        return text ? `: ${text}` : ''
      } finally { await handle.close() }
    } catch { return '' }
  }

  /**
   * Start a Runtime for this Workspace, keeping what it says on the way out.
   *
   * stderr goes to a file rather than to `'ignore'` or to a pipe. Discarding
   * it left a failed start with nothing but an exit code — the panel could
   * only say that something went wrong. A pipe would carry the text, but this
   * child is detached and outlives the Host: nobody would be draining the pipe
   * afterwards, and a Runtime that filled it would block on its own logging,
   * or take an EPIPE when the Host exited. A file has neither problem, and the
   * child holds its own descriptor once spawn has duplicated it.
   */
  private async startRuntime(workspaceRoot: string): Promise<ChildProcess> {
    const log = await open(this.startupLogPath(workspaceRoot), 'w')
    try {
      const child = spawn(this.command, [
        ...this.commandPrefix, 'serve', '--port', '0',
        // `harness`, and written here rather than passed in: the only caller
        // is the Harness. A Runtime's profile also decides whether it honours
        // `x-orbit-actor` at all — `harness` installs the scoped authenticator
        // under the `harness:session:` prefix, `full` ignores the header — so
        // this is not a knob to turn without deciding about actors too.
        '--project-root', workspaceRoot, '--mcp-tool-profile', 'harness',
      ], {
        cwd: workspaceRoot,
        detached: true,
        stdio: ['ignore', log.fd, log.fd],
      })
      await new Promise<void>((resolve, reject) => {
        child.once('spawn', resolve)
        child.once('error', reject)
      })
      child.unref()
      return child
    } finally { await log.close() }
  }

  private async rpc(runtime: Managed, method: string, params: object): Promise<unknown> {
    this.telemetry.rpcCalls++
    const id = runtime.nextId++
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), ORBIT_RPC_TIMEOUT_MS)
    try {
      const actor = this.actorFrom(params)
      const response = await this.fetchImpl(runtime.mcpUrl, {
        method: 'POST', headers: {
          'content-type': 'application/json', ...(actor ? { 'x-orbit-actor': actor } : {}),
        }, signal: controller.signal,
        body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
      })
      if (!response.ok) throw new OrbitTransportError(`Orbit MCP HTTP ${String(response.status)}`)
      const message = await response.json() as { result?: unknown; error?: { message?: string } }
      if (message.error !== undefined) throw new Error(message.error.message || 'Orbit MCP request failed')
      return message.result
    } catch (error) {
      if (controller.signal.aborted) throw new OrbitTransportError(`Orbit MCP ${method} timed out`)
      if (error instanceof TypeError) throw new OrbitTransportError(`Orbit MCP transport failed: ${error.message}`)
      throw error
    } finally { clearTimeout(timer) }
  }

  private actorFrom(params: object): string | undefined {
    const meta = (params as { _meta?: { 'orbit/actor'?: unknown } })._meta
    const actor = meta?.['orbit/actor']
    return typeof actor === 'string' ? actor : undefined
  }

  private async callRaw(runtime: Managed, name: string, args: object): Promise<unknown> {
    const result = await this.rpc(runtime, 'tools/call', { name, arguments: args }) as { structuredContent?: unknown }
    return result.structuredContent
  }
}
