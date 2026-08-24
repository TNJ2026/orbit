const JSON_OUTPUT = {
    schema: {},
    render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
};
const object = (properties, required = []) => ({
    type: 'object', properties, ...(required.length ? { required } : {}), additionalProperties: false,
});
function args(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value))
        throw new Error('Orbit tool arguments must be an object');
    return value;
}
export class OrbitToolBridge {
    ctx;
    gateway;
    watch;
    tools;
    registry;
    constructor(ctx, gateway, watch = () => { }) {
        this.ctx = ctx;
        this.gateway = gateway;
        this.watch = watch;
        this.tools = ctx.get('tools');
        this.registry = ctx.get('workspaceRegistry');
    }
    register() {
        for (const definition of this.definitions())
            this.tools.register(definition);
    }
    definitions() {
        return [
            this.definition('orbit_list_workflows', 'List published Orbit workflows available in this Session Workspace.', object({ ready_only: { type: 'boolean' } }), 'list_workflows', true),
            this.definition('orbit_list_runs', 'List Orbit workflow runs owned by this Harness Session.', object({ status: { type: 'string' }, limit: { type: 'integer', minimum: 1, maximum: 200 } }), 'list_runs', true),
            this.definition('orbit_inspect_run', 'Inspect one Orbit Run, including status, revision, interrupts and allowed commands.', object({ run_id: { type: 'string' } }, ['run_id']), 'inspect_run', true),
            {
                name: 'orbit_start_run',
                description: 'Start a published Orbit workflow in the current Workspace. Returns immediately so progress appears in the Orbit Run Card.',
                parameters: object({
                    workflow_id: { type: 'string' }, workflow_version: { type: 'integer' },
                    input: { type: 'object' }, goal: { type: 'string', maxLength: 4000 },
                }, ['workflow_id']), output: JSON_OUTPUT, timeoutMs: 60_000,
                execute: async (value, exec) => {
                    const input = args(value);
                    return await this.call(exec, 'start_run', {
                        ...input, wait: false, idempotency_key: crypto.randomUUID(),
                    });
                },
            },
            {
                name: 'orbit_generate_workflow',
                description: 'Draft a new Orbit workflow from a description and publish it if the compiler accepts it. '
                    + 'Returns a job immediately — authoring takes a while — so poll orbit_get_authoring_job '
                    + 'with the job_id until its status leaves queued/running. Nothing is published until the '
                    + 'compiler accepts the draft, so a failed job has changed nothing. Progress also appears '
                    + 'in the Orbit panel.',
                parameters: object({
                    prompt: { type: 'string', maxLength: 4000 },
                    agent: { type: 'string', description: 'Which Agent writes it; the Runtime picks one if omitted.' },
                }, ['prompt']), output: JSON_OUTPUT, timeoutMs: 60_000,
                execute: async (value, exec) => {
                    const input = args(value);
                    const { workspace, session } = await this.route(exec);
                    const job = await this.gateway.call(workspace, String(session.id), 'generate_workflow', {
                        prompt: String(input.prompt),
                        ...(input.agent === undefined ? {} : { agent: String(input.agent) }),
                        idempotency_key: crypto.randomUUID(),
                    });
                    // The panel is told before the model is: a person watching it should
                    // not have to wait for the Agent's next turn to learn work started.
                    this.watch(workspace, String(session.id), job);
                    return job;
                },
            },
            this.definition('orbit_get_authoring_job', 'Check an Orbit authoring job started by orbit_generate_workflow. Status queued or running '
                + 'means it is still going; done carries the published workflow, failed carries why.', object({ job_id: { type: 'string' } }, ['job_id']), 'get_authoring_job', true),
            {
                name: 'orbit_cancel_run',
                description: 'Cancel an Orbit Run if its latest server-advertised commands allow cancellation.',
                parameters: object({ run_id: { type: 'string' } }, ['run_id']), output: JSON_OUTPUT, timeoutMs: 60_000,
                execute: async (value, exec) => {
                    const runId = String(args(value).run_id);
                    return await this.command(exec, runId, 'langgraph_run.cancel');
                },
            },
            {
                name: 'orbit_resume_run',
                description: 'Resume an interrupted Orbit Run using its latest server-advertised revision.',
                parameters: object({ run_id: { type: 'string' }, value: {}, interrupt_id: { type: 'string' } }, ['run_id']),
                output: JSON_OUTPUT, timeoutMs: 60_000,
                execute: async (value, exec) => {
                    const input = args(value), runId = String(input.run_id);
                    return await this.command(exec, runId, 'langgraph_run.resume', input.value, input.interrupt_id);
                },
            },
        ];
    }
    definition(name, description, parameters, wireName, concurrencySafe) {
        return {
            name, description, parameters, output: JSON_OUTPUT, timeoutMs: 60_000,
            isConcurrencySafe: concurrencySafe ? () => true : undefined,
            execute: async (value, exec) => await this.call(exec, wireName, args(value)),
        };
    }
    async command(exec, runId, command, value, interruptId) {
        const { workspace, session } = await this.route(exec);
        const run = await this.gateway.run(workspace, String(session.id), runId);
        const advertised = run.allowed_commands.find(item => item.command === command);
        if (!advertised)
            throw new Error(`Orbit no longer advertises ${command} for Run ${runId}`);
        return await this.gateway.call(workspace, String(session.id), command === 'langgraph_run.cancel' ? 'cancel_run' : 'resume_run', {
            run_id: runId, expected_version: advertised.expected_version,
            idempotency_key: crypto.randomUUID(), ...(value === undefined ? {} : { value }),
            ...(interruptId === undefined ? {} : { interrupt_id: interruptId }),
        });
    }
    async call(exec, name, input) {
        const { workspace, session } = await this.route(exec);
        return await this.gateway.call(workspace, String(session.id), name, input);
    }
    async route(exec) {
        const session = exec.agent?.session;
        if (!session)
            throw new Error('Orbit tools require a live Harness Agent Session');
        const cwd = session.header.cwd;
        if (!cwd)
            throw new Error('Orbit tools require the Session to have a Workspace cwd');
        const registered = await this.registry.resolveByPath(cwd);
        return { session, workspace: {
                id: registered ? String(registered.id) : `cwd:${cwd}`,
                canonicalPath: registered?.path ?? cwd,
            } };
    }
}
