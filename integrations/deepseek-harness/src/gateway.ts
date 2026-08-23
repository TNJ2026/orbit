import { spawn } from 'node:child_process'
import { realpath } from 'node:fs/promises'
import type { WorkspaceRef, RunDto } from './types.js'
import { decodeRun, decodeToolResult } from './codecs.js'

interface DiscoveredRuntime { project_root?: string; transport?: string; mcp_url?: string; base_url?: string }
interface Managed { mcpUrl: string; baseUrl: string; nextId: number; capabilities: Record<string, unknown> }
type Fetch = typeof globalThis.fetch
class OrbitTransportError extends Error {}
export interface GatewayDiagnostics {
  discoveryAttempts: number
  rpcCalls: number
  transportFailures: number
  connectedWorkspaces: number
  lastConnectedAt?: string
  lastTransportError?: string
}

/** Connect Harness to an already-running Orbit Runtime over HTTP MCP. */
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

  async acquire(workspace: WorkspaceRef): Promise<() => Promise<void>> {
    // Connects and validates, then hands back a no-op release. Harness owns no
    // part of the Runtime's lifecycle, so there is nothing for a release to
    // reclaim — the call exists so a caller fails early, at acquire, rather
    // than midway through a sequence of reads.
    await this.runtime(workspace)
    return async () => {}
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

  async run(workspace: WorkspaceRef, sessionId: string, runId: string): Promise<RunDto> {
    return decodeRun(await this.call(workspace, sessionId, 'inspect_run', { run_id: runId }))
  }

  private async runtime(workspace: WorkspaceRef): Promise<Managed> {
    return await this.runtimeFor(await realpath(workspace.canonicalPath))
  }

  private async runtimeFor(key: string): Promise<Managed> {
    let promise = this.runtimes.get(key)
    if (promise === undefined) {
      promise = this.connect(key)
      this.runtimes.set(key, promise)
      promise.catch(() => this.runtimes.delete(key))
    }
    return await promise
  }

  private async connect(workspaceRoot: string): Promise<Managed> {
    const discovered = await this.discover(workspaceRoot)
    const runtime: Managed = {
      mcpUrl: discovered.mcp_url!, baseUrl: discovered.base_url ?? '',
      nextId: 1, capabilities: {},
    }
    await this.rpc(runtime, 'initialize', {
      protocolVersion: '2025-06-18', capabilities: {},
      clientInfo: { name: 'dsh-orbit', version: '0.1.0' },
    })
    runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {}) as Record<string, unknown>
    if (runtime.capabilities.integration_protocol !== 'orbit-harness/1') throw new Error('incompatible Orbit integration protocol')
    this.telemetry.lastConnectedAt = new Date().toISOString()
    this.telemetry.lastTransportError = undefined
    return runtime
  }

  private async discover(workspaceRoot: string): Promise<DiscoveredRuntime> {
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
    if (matches.length === 0) throw new Error(
      `No independent Orbit Runtime is serving Workspace ${workspaceRoot}; start it with orbit serve --project-root ${workspaceRoot}`,
    )
    if (matches.length > 1) throw new Error(`Multiple Orbit Runtimes claim Workspace ${workspaceRoot}`)
    if (matches[0].transport !== 'http') throw new Error('Orbit Runtime is not reachable over HTTP MCP')
    return matches[0]
  }

  private async rpc(runtime: Managed, method: string, params: object): Promise<unknown> {
    this.telemetry.rpcCalls++
    const id = runtime.nextId++
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 60_000)
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
