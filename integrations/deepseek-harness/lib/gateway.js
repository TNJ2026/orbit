import { spawn } from 'node:child_process';
import { realpath } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { decodeRun, decodeToolResult } from './codecs.js';
export class OrbitGateway {
    command;
    commandPrefix;
    runtimes = new Map();
    constructor(command = 'orbit', commandPrefix = []) {
        this.command = command;
        this.commandPrefix = commandPrefix;
    }
    async acquire(workspace) {
        const runtime = await this.runtime(workspace);
        runtime.refs++;
        let released = false;
        return async () => {
            if (released)
                return;
            released = true;
            if (--runtime.refs === 0)
                await this.stop(workspace, runtime);
        };
    }
    async call(workspace, sessionId, name, args) {
        const runtime = await this.runtime(workspace);
        const actor = `harness:session:${sessionId}`;
        if (!/^[A-Za-z0-9:_-]{1,200}$/.test(actor))
            throw new Error('invalid Harness session id');
        const envelope = await this.rpc(runtime, 'tools/call', {
            name, arguments: args, _meta: { 'orbit/actor': actor },
        });
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
            promise = this.start(key);
            this.runtimes.set(key, promise);
            promise.catch(() => this.runtimes.delete(key));
        }
        return await promise;
    }
    async start(cwd) {
        const child = spawn(this.command, [...this.commandPrefix, 'mcp', '--mcp-tool-profile', 'harness', '--actor', 'harness:gateway', '--actor-prefix', 'harness:session:'], { cwd, stdio: ['pipe', 'pipe', 'pipe'] });
        const runtime = { child, pending: new Map(), nextId: 1, refs: 0, capabilities: {} };
        child.stderr.resume();
        createInterface({ input: child.stdout }).on('line', line => {
            let message;
            try {
                message = JSON.parse(line);
            }
            catch {
                child.kill();
                return;
            }
            if (message.id === undefined)
                return;
            const pending = runtime.pending.get(message.id);
            if (pending === undefined)
                return;
            runtime.pending.delete(message.id);
            clearTimeout(pending.timer);
            message.error === undefined ? pending.resolve(message.result) : pending.reject(new Error(message.error.message));
        });
        const rejectPending = (reason) => {
            for (const pending of runtime.pending.values()) {
                clearTimeout(pending.timer);
                pending.reject(new Error(reason));
            }
            runtime.pending.clear();
        };
        child.once('error', error => rejectPending(error.message));
        child.once('exit', (code, signal) => rejectPending(`Orbit Runtime exited${code === null ? ` by ${signal || 'signal'}` : ` with code ${String(code)}`}`));
        try {
            await this.rpc(runtime, 'initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'dsh-orbit', version: '0.1.0' } });
            runtime.capabilities = await this.callRaw(runtime, 'get_capabilities', {});
            if (runtime.capabilities.integration_protocol !== 'orbit-harness/1')
                throw new Error('incompatible Orbit integration protocol');
            return runtime;
        }
        catch (error) {
            child.kill();
            throw error;
        }
    }
    async rpc(runtime, method, params) {
        const id = runtime.nextId++;
        const result = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                runtime.pending.delete(id);
                reject(new Error(`Orbit MCP ${method} timed out`));
            }, 60_000);
            runtime.pending.set(id, { resolve, reject, timer });
        });
        runtime.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
        return await result;
    }
    async callRaw(runtime, name, args) {
        const result = await this.rpc(runtime, 'tools/call', { name, arguments: args });
        return result.structuredContent;
    }
    async stop(workspace, runtime) {
        const key = await realpath(workspace.canonicalPath);
        this.runtimes.delete(key);
        runtime.child.stdin.end();
    }
}
