import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { open, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { decodeRun, decodeToolResult } from './codecs.js';
class OrbitTransportError extends Error {
}
const STARTUP_TIMEOUT_MS = 10_000;
const STARTUP_POLL_MS = 100;
/** Connect Harness to Orbit over HTTP MCP; explicit UI entry may start it. */
/**
 * How long any one MCP call may take before the transport gives up on it.
 *
 * Exported because a tool that deliberately blocks — `wait_authoring_request`
 * parks until work arrives — has to ask for less than this. Asking for more
 * does not extend the call: it aborts here, the request is cancelled at the
 * Runtime, and the caller is told about a timeout it chose for itself.
 */
export const ORBIT_RPC_TIMEOUT_MS = 60_000;
export class OrbitGateway {
    command;
    commandPrefix;
    fetchImpl;
    discoveryRoot;
    hubUrl;
    runtimes = new Map();
    telemetry = {
        discoveryAttempts: 0, rpcCalls: 0, transportFailures: 0,
    };
    constructor(command = 'orbit', commandPrefix = [], fetchImpl = globalThis.fetch, discoveryRoot = process.env.ORBIT_RUNTIME_ROOT || undefined, hubUrl = process.env.ORBIT_HUB_URL || 'http://127.0.0.1:8848') {
        this.command = command;
        this.commandPrefix = commandPrefix;
        this.fetchImpl = fetchImpl;
        this.discoveryRoot = discoveryRoot;
        this.hubUrl = hubUrl;
    }
    diagnostics() {
        return { ...this.telemetry, connectedWorkspaces: this.runtimes.size };
    }
    async acquire(workspace, startIfMissing = false) {
        // Connects and validates, then hands back a no-op release. A Runtime we
        // start is detached and becomes independent immediately: letting go of a
        // panel must not stop work another Session or terminal may still be using.
        //
        // Stopping one is now possible, but only by asking for it — see
        // `stopRuntime`. The distinction is the whole point: a release is this
        // Host finishing with a Runtime, and a shutdown is a person deciding
        // nobody should have it.
        await this.runtime(workspace, startIfMissing);
        return async () => { };
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
    async stopRuntime(workspace, sessionId) {
        if (!/^[A-Za-z0-9:_-]{1,180}$/.test(sessionId))
            throw new Error('invalid Harness session id');
        const key = await realpath(workspace.canonicalPath);
        const runtime = await this.runtimeFor(key);
        if (!runtime.baseUrl)
            throw new Error('this Orbit Runtime published no HTTP address');
        try {
            const response = await this.fetchImpl(`${runtime.baseUrl}/api/v1/runtime/shutdown`, {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    'x-orbit-actor': `harness:session:${sessionId}`,
                    'idempotency-key': crypto.randomUUID(),
                },
                body: JSON.stringify({ expected_version: 0 }),
            });
            if (!response.ok) {
                const detail = await response.text().catch(() => '');
                throw new Error(`Orbit refused to stop: HTTP ${String(response.status)}${detail ? ` ${detail.slice(0, 200)}` : ''}`);
            }
        }
        finally {
            this.runtimes.delete(key);
        }
    }
    async call(workspace, sessionId, name, args) {
        if (!/^[A-Za-z0-9:_-]{1,180}$/.test(sessionId))
            throw new Error('invalid Harness session id');
        // Resolved once, before anything can fail. A removed Workspace directory is
        // a plausible cause of the transport failure handled below, and resolving it
        // again down there would replace that diagnosis with an ENOENT and skip the
        // cache invalidation the failure was supposed to trigger.
        const key = await realpath(workspace.canonicalPath);
        const runtime = await this.runtimeFor(key);
        const actor = `harness:session:${sessionId}`;
        let envelope;
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
            });
        }
        catch (error) {
            if (error instanceof OrbitTransportError) {
                this.telemetry.transportFailures++;
                this.telemetry.lastTransportError = error.message;
                this.runtimes.delete(key);
            }
            throw error;
        }
        if (envelope.isError)
            throw new Error(JSON.stringify(envelope.structuredContent ?? envelope.content));
        return decodeToolResult(name, envelope.structuredContent);
    }
    /** Stable Hub UI namespace for this Workspace. */
    async uiUrl(workspace) {
        const runtime = await this.runtime(workspace);
        return runtime.uiUrl;
    }
    /**
     * Read the same durable attempt totals as Orbit's Agent page.
     *
     * This HTTP projection also keeps a newly upgraded Harness compatible with
     * a Runtime process started before `list_agents` grew the aggregate fields.
     */
    async handlerAttemptCounts(workspace, sessionId) {
        const runtime = await this.runtime(workspace);
        if (!runtime.baseUrl)
            return new Map();
        const response = await this.fetchImpl(`${runtime.baseUrl.replace(/\/$/, '')}/api/v1/handler-catalog`, { headers: { 'x-orbit-actor': `harness:session:${sessionId}` } });
        if (!response.ok)
            throw new OrbitTransportError(`Orbit Handler catalog failed with HTTP ${String(response.status)}`);
        const envelope = await response.json();
        const counts = new Map();
        for (const handler of envelope.data?.handlers ?? []) {
            if (typeof handler.name !== 'string')
                continue;
            counts.set(handler.name, {
                attempt_count: Number(handler.attempt_count ?? 0),
                failed_count: Number(handler.failed_count ?? 0),
            });
        }
        return counts;
    }
    async authoringOutput(workspace, sessionId, outputHref, after) {
        if (!/^\/api\/v1\/workflow-authoring-jobs\/[^/?#]+\/output$/u.test(outputHref)) {
            throw new Error('Orbit returned an invalid authoring output address');
        }
        if (!Number.isSafeInteger(after) || after < 0)
            throw new Error('invalid authoring output cursor');
        const runtime = await this.runtime(workspace);
        if (!runtime.baseUrl)
            throw new Error('Orbit Runtime did not publish a browser address');
        const response = await this.fetchImpl(`${runtime.baseUrl.replace(/\/$/, '')}${outputHref}?after=${String(after)}`, { headers: { 'x-orbit-actor': `harness:session:${sessionId}` } });
        if (!response.ok)
            throw new OrbitTransportError(`Orbit authoring output failed with HTTP ${String(response.status)}`);
        const envelope = await response.json();
        if (!envelope.data || !Array.isArray(envelope.data.chunks)) {
            throw new Error('Orbit authoring output returned invalid JSON');
        }
        return envelope.data;
    }
    async run(workspace, sessionId, runId) {
        return decodeRun(await this.call(workspace, sessionId, 'inspect_run', { run_id: runId }));
    }
    /** Generate a Workflow and execute its Goal through Runtime MCP, without a UI.
     *
     * The authoring and execution calls are deliberately kept in one method so a
     * host can offer one synchronous operation. Runtime remains authoritative:
     * the job's published workflow id is used verbatim, and every mutation gets
     * its own idempotency key. No page routes or guessed URLs are involved.
     */
    async generateAndRunGoal(workspace, sessionId, prompt, goal, input = {}, options = {}) {
        const generated = await this.call(workspace, sessionId, 'generate_workflow', {
            prompt,
            idempotency_key: randomUUID(),
            ...(options.agent === undefined ? {} : { agent: options.agent }),
            ...(options.displayLanguage === undefined ? {} : { display_language: options.displayLanguage }),
        });
        const workflow = await this.waitForAuthoringJob(workspace, sessionId, generated.job_id, options);
        if (workflow.status !== 'done') {
            throw new Error(`Orbit workflow generation ${workflow.status}: ${workflow.error?.message ?? workflow.job_id}`);
        }
        if (typeof workflow.workflow_id !== 'string' || !workflow.workflow_id) {
            throw new Error('Orbit completed workflow generation without a workflow_id');
        }
        const started = await this.call(workspace, sessionId, 'start_run', {
            workflow_id: workflow.workflow_id,
            goal,
            input,
            wait: false,
            idempotency_key: randomUUID(),
        });
        const run = await this.waitForRun(workspace, sessionId, started.run_id, options);
        return { workflow, run };
    }
    async waitForAuthoringJob(workspace, sessionId, jobId, options) {
        const pollMs = options.pollMs ?? 500;
        const deadline = Date.now() + (options.timeoutMs ?? 600_000);
        while (true) {
            const job = await this.call(workspace, sessionId, 'get_authoring_job', { job_id: jobId });
            if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled')
                return job;
            if (Date.now() >= deadline)
                throw new Error(`Orbit workflow generation timed out: ${jobId}`);
            await new Promise(resolve => setTimeout(resolve, pollMs));
        }
    }
    async waitForRun(workspace, sessionId, runId, options) {
        const pollMs = options.pollMs ?? 500;
        const deadline = Date.now() + (options.timeoutMs ?? 600_000);
        while (true) {
            const run = await this.run(workspace, sessionId, runId);
            if (['completed', 'failed', 'cancelled', 'unknown'].includes(run.status))
                return run;
            if (Date.now() >= deadline)
                throw new Error(`Orbit Goal execution timed out: ${runId}`);
            await new Promise(resolve => setTimeout(resolve, pollMs));
        }
    }
    async runtime(workspace, startIfMissing = false) {
        return await this.runtimeFor(await realpath(workspace.canonicalPath), startIfMissing);
    }
    async runtimeFor(key, startIfMissing = false) {
        let promise = this.runtimes.get(key);
        if (promise === undefined) {
            promise = this.connect(key, startIfMissing);
            this.runtimes.set(key, promise);
            promise.catch(() => this.runtimes.delete(key));
        }
        return await promise;
    }
    async connect(workspaceRoot, startIfMissing) {
        const workspaceId = await this.registerWorkspace(workspaceRoot);
        const hub = this.hubUrl.replace(/\/$/, '');
        const mcpUrl = `${hub}/workspaces/${workspaceId}/mcp`;
        const deadline = Date.now() + STARTUP_TIMEOUT_MS;
        let started = false;
        while (true) {
            const discovered = await this.discover(workspaceRoot);
            const runtime = {
                mcpUrl, baseUrl: discovered?.base_url ?? '',
                uiUrl: `${hub}/workspaces/${workspaceId}/ui/`,
                nextId: 1, capabilities: {},
            };
            try {
                await this.rpc(runtime, 'initialize', {
                    protocolVersion: '2025-06-18', capabilities: {},
                    clientInfo: { name: 'dsh-orbit', version: '0.1.0' },
                });
                runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {});
                if (runtime.capabilities.integration_protocol !== 'orbit-harness/1')
                    throw new Error('incompatible Orbit integration protocol');
                this.telemetry.lastConnectedAt = new Date().toISOString();
                this.telemetry.lastTransportError = undefined;
                const ready = await this.discover(workspaceRoot);
                runtime.baseUrl = ready?.base_url ?? runtime.baseUrl;
                return runtime;
            }
            catch (error) {
                if (!startIfMissing || !(error instanceof OrbitTransportError) || Date.now() >= deadline)
                    throw error;
                if (!started) {
                    await this.startHub();
                    started = true;
                }
                await new Promise(resolve => setTimeout(resolve, STARTUP_POLL_MS));
            }
        }
    }
    async registerWorkspace(workspaceRoot) {
        const output = await this.runOrbit(['hub', 'register', workspaceRoot], workspaceRoot);
        try {
            const value = JSON.parse(output);
            if (typeof value.workspace_id === 'string' && value.workspace_id)
                return value.workspace_id;
        }
        catch { }
        throw new Error('Orbit Hub workspace registration returned invalid JSON');
    }
    async runOrbit(args, cwd) {
        return await new Promise((resolve, reject) => {
            const child = spawn(this.command, [...this.commandPrefix, ...args], {
                cwd, stdio: ['ignore', 'pipe', 'pipe'],
            });
            let stdout = '', stderr = '';
            child.stdout.setEncoding('utf8');
            child.stdout.on('data', chunk => { stdout += chunk; });
            child.stderr.setEncoding('utf8');
            child.stderr.on('data', chunk => { stderr += chunk; });
            child.once('error', reject);
            child.once('exit', code => code === 0 ? resolve(stdout) : reject(new Error(`Orbit command failed: ${args.join(' ')} (code ${String(code)})${stderr ? `: ${stderr.trim()}` : ''}`)));
        });
    }
    async startHub() {
        const url = new URL(this.hubUrl);
        if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '::1'].includes(url.hostname)) {
            throw new Error(`Orbit Hub auto-start requires a loopback HTTP URL: ${this.hubUrl}`);
        }
        const port = url.port ? Number(url.port) : 80;
        const log = await open(join(tmpdir(), `dsh-orbit-hub-${String(port)}.log`), 'w');
        try {
            const child = spawn(this.command, [
                ...this.commandPrefix, 'hub', 'serve', '--host', url.hostname, '--port', String(port),
            ], { detached: true, stdio: ['ignore', log.fd, log.fd] });
            await new Promise((resolve, reject) => {
                child.once('spawn', resolve);
                child.once('error', reject);
            });
            child.unref();
        }
        finally {
            await log.close();
        }
    }
    async discover(workspaceRoot) {
        this.telemetry.discoveryAttempts++;
        const output = await new Promise((resolve, reject) => {
            const child = spawn(this.command, [
                ...this.commandPrefix, 'runtimes', '--json',
                ...(this.discoveryRoot ? ['--root', this.discoveryRoot] : []),
            ], {
                cwd: workspaceRoot, stdio: ['ignore', 'pipe', 'pipe'],
            });
            let stdout = '', stderr = '';
            child.stdout.setEncoding('utf8');
            child.stdout.on('data', chunk => { stdout += chunk; });
            child.stderr.setEncoding('utf8');
            child.stderr.on('data', chunk => { stderr += chunk; });
            child.once('error', reject);
            child.once('exit', code => code === 0 ? resolve(stdout) : reject(new Error(`Orbit Runtime discovery failed with code ${String(code)}${stderr ? `: ${stderr.trim()}` : ''}`)));
        });
        let entries;
        try {
            entries = JSON.parse(output);
        }
        catch {
            throw new Error('Orbit Runtime discovery returned invalid JSON');
        }
        if (!Array.isArray(entries))
            throw new Error('Orbit Runtime discovery must return an array');
        const matches = entries.filter(entry => entry.project_root === workspaceRoot && entry.base_url);
        if (matches.length === 0)
            return undefined;
        if (matches.length > 1)
            throw new Error(`Multiple Orbit Runtimes claim Workspace ${workspaceRoot}`);
        return matches[0];
    }
    async rpc(runtime, method, params) {
        this.telemetry.rpcCalls++;
        const id = runtime.nextId++;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), ORBIT_RPC_TIMEOUT_MS);
        try {
            const actor = this.actorFrom(params);
            const response = await this.fetchImpl(runtime.mcpUrl, {
                method: 'POST', headers: {
                    'content-type': 'application/json', ...(actor ? { 'x-orbit-actor': actor } : {}),
                }, signal: controller.signal,
                body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
            });
            if (!response.ok)
                throw new OrbitTransportError(`Orbit MCP HTTP ${String(response.status)}`);
            const message = await response.json();
            if (message.error !== undefined)
                throw new Error(message.error.message || 'Orbit MCP request failed');
            return message.result;
        }
        catch (error) {
            if (controller.signal.aborted)
                throw new OrbitTransportError(`Orbit MCP ${method} timed out`);
            if (error instanceof TypeError)
                throw new OrbitTransportError(`Orbit MCP transport failed: ${error.message}`);
            throw error;
        }
        finally {
            clearTimeout(timer);
        }
    }
    actorFrom(params) {
        const meta = params._meta;
        const actor = meta?.['orbit/actor'];
        return typeof actor === 'string' ? actor : undefined;
    }
    async callRaw(runtime, name, args) {
        const result = await this.rpc(runtime, 'tools/call', { name, arguments: args });
        return result.structuredContent;
    }
}
