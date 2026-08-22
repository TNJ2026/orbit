import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { realpath } from 'node:fs/promises'
import { createInterface } from 'node:readline'
import type { WorkspaceRef, RunDto } from './types.js'
import { decodeRun, decodeToolResult } from './codecs.js'

interface Pending { resolve(value: unknown): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }
interface Managed { child: ChildProcessWithoutNullStreams; pending: Map<number, Pending>; nextId: number; refs: number; capabilities: Record<string, unknown> }

export class OrbitGateway {
  private readonly runtimes = new Map<string, Promise<Managed>>()
  constructor(
    private readonly command = 'orbit',
    private readonly commandPrefix: readonly string[] = [],
  ) {}

  async acquire(workspace: WorkspaceRef): Promise<() => Promise<void>> {
    const runtime = await this.runtime(workspace)
    runtime.refs++
    let released = false
    return async () => {
      if (released) return
      released = true
      if (--runtime.refs === 0) await this.stop(workspace, runtime)
    }
  }

  async call(workspace: WorkspaceRef, sessionId: string, name: string, args: object): Promise<unknown> {
    const runtime = await this.runtime(workspace)
    const actor = `harness:session:${sessionId}`
    if (!/^[A-Za-z0-9:_-]{1,200}$/.test(actor)) throw new Error('invalid Harness session id')
    const envelope = await this.rpc(runtime, 'tools/call', {
      name, arguments: args, _meta: { 'orbit/actor': actor },
    }) as { isError?: boolean; structuredContent?: unknown; content?: unknown }
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
      promise = this.start(key)
      this.runtimes.set(key, promise)
      promise.catch(() => this.runtimes.delete(key))
    }
    return await promise
  }

  private async start(cwd: string): Promise<Managed> {
    const child = spawn(this.command, [...this.commandPrefix, 'mcp', '--mcp-tool-profile', 'harness', '--actor', 'harness:gateway', '--actor-prefix', 'harness:session:'], { cwd, stdio: ['pipe', 'pipe', 'pipe'] })
    const runtime: Managed = { child, pending: new Map(), nextId: 1, refs: 0, capabilities: {} }
    child.stderr.resume()
    createInterface({ input: child.stdout }).on('line', line => {
      let message: { id?: number; result?: unknown; error?: { message: string } }
      try { message = JSON.parse(line) as typeof message }
      catch { child.kill(); return }
      if (message.id === undefined) return
      const pending = runtime.pending.get(message.id)
      if (pending === undefined) return
      runtime.pending.delete(message.id)
      clearTimeout(pending.timer)
      message.error === undefined ? pending.resolve(message.result) : pending.reject(new Error(message.error.message))
    })
    const rejectPending = (reason: string): void => {
      for (const pending of runtime.pending.values()) {
        clearTimeout(pending.timer)
        pending.reject(new Error(reason))
      }
      runtime.pending.clear()
    }
    child.once('error', error => rejectPending(error.message))
    child.once('exit', (code, signal) => rejectPending(
      `Orbit Runtime exited${code === null ? ` by ${signal || 'signal'}` : ` with code ${String(code)}`}`,
    ))
    try {
      await this.rpc(runtime, 'initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'dsh-orbit', version: '0.1.0' } })
      runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {}) as Record<string, unknown>
      if (runtime.capabilities.integration_protocol !== 'orbit-harness/1') throw new Error('incompatible Orbit integration protocol')
      return runtime
    } catch (error) {
      child.kill()
      throw error
    }
  }

  private async rpc(runtime: Managed, method: string, params: object): Promise<unknown> {
    const id = runtime.nextId++
    const result = new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        runtime.pending.delete(id)
        reject(new Error(`Orbit MCP ${method} timed out`))
      }, 60_000)
      runtime.pending.set(id, { resolve, reject, timer })
    })
    runtime.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
    return await result
  }

  private async callRaw(runtime: Managed, name: string, args: object): Promise<unknown> {
    const result = await this.rpc(runtime, 'tools/call', { name, arguments: args }) as { structuredContent?: unknown }
    return result.structuredContent
  }

  private async stop(workspace: WorkspaceRef, runtime: Managed): Promise<void> {
    const key = await realpath(workspace.canonicalPath)
    this.runtimes.delete(key)
    runtime.child.stdin.end()
  }
}
