import { spawn } from 'node:child_process';
import { realpath } from 'node:fs/promises';
import { decodeRun, decodeToolResult } from './codecs.js';
class OrbitTransportError extends Error {
}
/** Connect Harness to an already-running Orbit Runtime over HTTP MCP. */
export class OrbitGateway {
    command;
    commandPrefix;
    fetchImpl;
    discoveryRoot;
    runtimes = new Map();
    telemetry = {
        discoveryAttempts: 0, rpcCalls: 0, transportFailures: 0,
    };
    constructor(command = 'orbit', commandPrefix = [], fetchImpl = globalThis.fetch, discoveryRoot = process.env.ORBIT_RUNTIME_ROOT || undefined) {
        this.command = command;
        this.commandPrefix = commandPrefix;
        this.fetchImpl = fetchImpl;
        this.discoveryRoot = discoveryRoot;
    }
    diagnostics() {
        return { ...this.telemetry, connectedWorkspaces: this.runtimes.size };
    }
    async acquire(workspace) {
        // Connects and validates, then hands back a no-op release. Harness owns no
        // part of the Runtime's lifecycle, so there is nothing for a release to
        // reclaim — the call exists so a caller fails early, at acquire, rather
        // than midway through a sequence of reads.
        await this.runtime(workspace);
        return async () => { };
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
    async run(workspace, sessionId, runId) {
        return decodeRun(await this.call(workspace, sessionId, 'inspect_run', { run_id: runId }));
    }
    async runtime(workspace) {
        return await this.runtimeFor(await realpath(workspace.canonicalPath));
    }
    async runtimeFor(key) {
        let promise = this.runtimes.get(key);
        if (promise === undefined) {
            promise = this.connect(key);
            this.runtimes.set(key, promise);
            promise.catch(() => this.runtimes.delete(key));
        }
        return await promise;
    }
    async connect(workspaceRoot) {
        const discovered = await this.discover(workspaceRoot);
        const runtime = { mcpUrl: discovered.mcp_url, nextId: 1, capabilities: {} };
        await this.rpc(runtime, 'initialize', {
            protocolVersion: '2025-06-18', capabilities: {},
            clientInfo: { name: 'dsh-orbit', version: '0.1.0' },
        });
        runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {});
        if (runtime.capabilities.integration_protocol !== 'orbit-harness/1')
            throw new Error('incompatible Orbit integration protocol');
        this.telemetry.lastConnectedAt = new Date().toISOString();
        this.telemetry.lastTransportError = undefined;
        return runtime;
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
        const matches = entries.filter(entry => entry.project_root === workspaceRoot && entry.mcp_url);
        if (matches.length === 0)
            throw new Error(`No independent Orbit Runtime is serving Workspace ${workspaceRoot}; start it with orbit serve --project-root ${workspaceRoot}`);
        if (matches.length > 1)
            throw new Error(`Multiple Orbit Runtimes claim Workspace ${workspaceRoot}`);
        if (matches[0].transport !== 'http')
            throw new Error('Orbit Runtime is not reachable over HTTP MCP');
        return matches[0];
    }
    async rpc(runtime, method, params) {
        this.telemetry.rpcCalls++;
        const id = runtime.nextId++;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 60_000);
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
