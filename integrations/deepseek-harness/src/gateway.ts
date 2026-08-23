import { spawn } from 'node:child_process'
import { realpath } from 'node:fs/promises'
import type { WorkspaceRef, RunDto } from './types.js'
import { decodeRun, decodeToolResult } from './codecs.js'

interface DiscoveredRuntime { project_root?: string; transport?: string; mcp_url?: string }
interface Managed { mcpUrl: string; nextId: number; refs: number; capabilities: Record<string, unknown> }
type Fetch = typeof globalThis.fetch
class OrbitTransportError extends Error {}

/** Connect Harness to an already-running Orbit Runtime over HTTP MCP. */
export class OrbitGateway {
  private readonly runtimes = new Map<string, Promise<Managed>>()
  constructor(
    private readonly command = 'orbit',
    private readonly commandPrefix: readonly string[] = [],
    private readonly fetchImpl: Fetch = globalThis.fetch,
    private readonly discoveryRoot = process.env.ORBIT_RUNTIME_ROOT || undefined,
  ) {}

  async acquire(workspace: WorkspaceRef): Promise<() => Promise<void>> {
    const runtime = await this.runtime(workspace)
    runtime.refs++
    let released = false
    return async () => {
      if (released) return
      released = true
      runtime.refs--
      // Harness owns only this reference, never the independent Runtime.
    }
  }

  async call(workspace: WorkspaceRef, sessionId: string, name: string, args: object): Promise<unknown> {
    if (!/^[A-Za-z0-9:_-]{1,180}$/.test(sessionId)) throw new Error('invalid Harness session id')
    const runtime = await this.runtime(workspace)
    const actor = `harness:session:${sessionId}`
    let envelope: { isError?: boolean; structuredContent?: unknown; content?: unknown }
    try {
      envelope = await this.rpc(runtime, 'tools/call', {
        name, arguments: args, _meta: { 'orbit/actor': actor },
      }) as typeof envelope
    } catch (error) {
      if (error instanceof OrbitTransportError) this.runtimes.delete(await realpath(workspace.canonicalPath))
      throw error
    }
    if (envelope.isError) throw new Error(JSON.stringify(envelope.structuredContent ?? envelope.content))
    return decodeToolResult(name, envelope.structuredContent)
  }

  async run(workspace: WorkspaceRef, sessionId: string, runId: string): Promise<RunDto> {
    return decodeRun(await this.call(workspace, sessionId, 'inspect_run', { run_id: runId }))
  }

  private async runtime(workspace: WorkspaceRef): Promise<Managed> {
    const key = await realpath(workspace.canonicalPath)
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
    const runtime: Managed = { mcpUrl: discovered.mcp_url!, nextId: 1, refs: 0, capabilities: {} }
    await this.rpc(runtime, 'initialize', {
      protocolVersion: '2025-06-18', capabilities: {},
      clientInfo: { name: 'dsh-orbit', version: '0.1.0' },
    })
    runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {}) as Record<string, unknown>
    if (runtime.capabilities.integration_protocol !== 'orbit-harness/1') throw new Error('incompatible Orbit integration protocol')
    return runtime
  }

  private async discover(workspaceRoot: string): Promise<DiscoveredRuntime> {
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
