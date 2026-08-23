import type { Context } from '@deepseek-ai/cordis'
import type { Session } from '@deepseek-ai/dsh-session'
import type { ToolDefinition, ToolRunContext, ToolRuntime, JsonValue } from '@deepseek-ai/dsh-tools'
import type { WorkspaceRegistry } from '@deepseek-ai/dsh-workspace'
import { OrbitGateway } from './gateway.js'
import type { RunDto, WorkspaceRef } from './types.js'

const JSON_OUTPUT = {
  schema: {} as const,
  render: (_args: unknown, value: JsonValue) => [{ type: 'text' as const, text: JSON.stringify(value, null, 2) }],
}

const object = (properties: Record<string, unknown>, required: string[] = []) => ({
  type: 'object' as const, properties, ...(required.length ? { required } : {}), additionalProperties: false,
})

function args(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('Orbit tool arguments must be an object')
  return value as Record<string, unknown>
}

export class OrbitToolBridge {
  private readonly tools: ToolRuntime
  private readonly registry: WorkspaceRegistry
  constructor(private readonly ctx: Context, private readonly gateway: OrbitGateway) {
    this.tools = ctx.get('tools') as unknown as ToolRuntime
    this.registry = ctx.get('workspaceRegistry') as unknown as WorkspaceRegistry
  }

  register(): void {
    for (const definition of this.definitions()) this.tools.register(definition)
  }

  private definitions(): ToolDefinition[] {
    return [
      this.definition('orbit_list_workflows', 'List published Orbit workflows available in this Session Workspace.',
        object({ ready_only: { type: 'boolean' } }), 'list_workflows', true),
      this.definition('orbit_list_runs', 'List Orbit workflow runs owned by this Harness Session.',
        object({ status: { type: 'string' }, limit: { type: 'integer', minimum: 1, maximum: 200 } }), 'list_runs', true),
      this.definition('orbit_inspect_run', 'Inspect one Orbit Run, including status, revision, interrupts and allowed commands.',
        object({ run_id: { type: 'string' } }, ['run_id']), 'inspect_run', true),
      {
        name: 'orbit_start_run',
        description: 'Start a published Orbit workflow in the current Workspace. Returns immediately so progress appears in the Orbit Run Card.',
        parameters: object({
          workflow_id: { type: 'string' }, workflow_version: { type: 'integer' },
          input: { type: 'object' }, goal: { type: 'string', maxLength: 4000 },
        }, ['workflow_id']), output: JSON_OUTPUT, timeoutMs: 60_000,
        execute: async (value, exec) => {
          const input = args(value)
          return await this.call(exec, 'start_run', {
            ...input, wait: false, idempotency_key: crypto.randomUUID(),
          })
        },
      },
      {
        name: 'orbit_cancel_run',
        description: 'Cancel an Orbit Run if its latest server-advertised commands allow cancellation.',
        parameters: object({ run_id: { type: 'string' } }, ['run_id']), output: JSON_OUTPUT, timeoutMs: 60_000,
        execute: async (value, exec) => {
          const runId = String(args(value).run_id)
          return await this.command(exec, runId, 'langgraph_run.cancel')
        },
      },
      {
        name: 'orbit_resume_run',
        description: 'Resume an interrupted Orbit Run using its latest server-advertised revision.',
        parameters: object({ run_id: { type: 'string' }, value: {}, interrupt_id: { type: 'string' } }, ['run_id']),
        output: JSON_OUTPUT, timeoutMs: 60_000,
        execute: async (value, exec) => {
          const input = args(value), runId = String(input.run_id)
          return await this.command(exec, runId, 'langgraph_run.resume', input.value, input.interrupt_id)
        },
      },
    ]
  }

  private definition(name: string, description: string, parameters: ToolDefinition['parameters'], wireName: string, concurrencySafe: boolean): ToolDefinition {
    return {
      name, description, parameters, output: JSON_OUTPUT, timeoutMs: 60_000,
      isConcurrencySafe: concurrencySafe ? () => true : undefined,
      execute: async (value, exec) => await this.call(exec, wireName, args(value)),
    }
  }

  private async command(exec: ToolRunContext, runId: string, command: 'langgraph_run.cancel' | 'langgraph_run.resume', value?: unknown, interruptId?: unknown): Promise<RunDto> {
    const { workspace, session } = await this.route(exec)
    const run = await this.gateway.run(workspace, String(session.id), runId)
    const advertised = run.allowed_commands.find(item => item.command === command)
    if (!advertised) throw new Error(`Orbit no longer advertises ${command} for Run ${runId}`)
    return await this.gateway.call(workspace, String(session.id), command === 'langgraph_run.cancel' ? 'cancel_run' : 'resume_run', {
      run_id: runId, expected_version: advertised.expected_version,
      idempotency_key: crypto.randomUUID(), ...(value === undefined ? {} : { value }),
      ...(interruptId === undefined ? {} : { interrupt_id: interruptId }),
    }) as RunDto
  }

  private async call(exec: ToolRunContext, name: string, input: object): Promise<JsonValue> {
    const { workspace, session } = await this.route(exec)
    return await this.gateway.call(workspace, String(session.id), name, input) as JsonValue
  }

  private async route(exec: ToolRunContext): Promise<{ workspace: WorkspaceRef; session: Session }> {
    const session = exec.agent?.session
    if (!session) throw new Error('Orbit tools require a live Harness Agent Session')
    const cwd = session.header.cwd
    if (!cwd) throw new Error('Orbit tools require the Session to have a Workspace cwd')
    const registered = await this.registry.resolveByPath(cwd)
    return { session, workspace: {
      id: registered ? String(registered.id) : `cwd:${cwd}`,
      canonicalPath: registered?.path ?? cwd,
    } }
  }
}
