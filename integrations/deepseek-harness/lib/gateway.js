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
    constructor(command = 'orbit', commandPrefix = [], fetchImpl = globalThis.fetch, discoveryRoot = process.env.ORBIT_RUNTIME_ROOT || undefined) {
        this.command = command;
        this.commandPrefix = commandPrefix;
        this.fetchImpl = fetchImpl;
        this.discoveryRoot = discoveryRoot;
    }
    async acquire(workspace) {
        const runtime = await this.runtime(workspace);
        runtime.refs++;
        let released = false;
        return async () => {
            if (released)
                return;
            released = true;
            runtime.refs--;
            // Harness owns only this reference, never the independent Runtime.
        };
    }
    async call(workspace, sessionId, name, args) {
        if (!/^[A-Za-z0-9:_-]{1,180}$/.test(sessionId))
            throw new Error('invalid Harness session id');
        const runtime = await this.runtime(workspace);
        const actor = `harness:session:${sessionId}`;
        let envelope;
        try {
            envelope = await this.rpc(runtime, 'tools/call', {
                name, arguments: args, _meta: { 'orbit/actor': actor },
            });
        }
        catch (error) {
            if (error instanceof OrbitTransportError)
                this.runtimes.delete(await realpath(workspace.canonicalPath));
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
        const key = await realpath(workspace.canonicalPath);
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
        const runtime = { mcpUrl: discovered.mcp_url, nextId: 1, refs: 0, capabilities: {} };
        await this.rpc(runtime, 'initialize', {
            protocolVersion: '2025-06-18', capabilities: {},
            clientInfo: { name: 'dsh-orbit', version: '0.1.0' },
        });
        runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {});
        if (runtime.capabilities.integration_protocol !== 'orbit-harness/1')
            throw new Error('incompatible Orbit integration protocol');
        return runtime;
    }
    async discover(workspaceRoot) {
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
