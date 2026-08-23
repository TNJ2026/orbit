var __runInitializers = (this && this.__runInitializers) || function (thisArg, initializers, value) {
    var useValue = arguments.length > 2;
    for (var i = 0; i < initializers.length; i++) {
        value = useValue ? initializers[i].call(thisArg, value) : initializers[i].call(thisArg);
    }
    return useValue ? value : void 0;
};
var __esDecorate = (this && this.__esDecorate) || function (ctor, descriptorIn, decorators, contextIn, initializers, extraInitializers) {
    function accept(f) { if (f !== void 0 && typeof f !== "function") throw new TypeError("Function expected"); return f; }
    var kind = contextIn.kind, key = kind === "getter" ? "get" : kind === "setter" ? "set" : "value";
    var target = !descriptorIn && ctor ? contextIn["static"] ? ctor : ctor.prototype : null;
    var descriptor = descriptorIn || (target ? Object.getOwnPropertyDescriptor(target, contextIn.name) : {});
    var _, done = false;
    for (var i = decorators.length - 1; i >= 0; i--) {
        var context = {};
        for (var p in contextIn) context[p] = p === "access" ? {} : contextIn[p];
        for (var p in contextIn.access) context.access[p] = contextIn.access[p];
        context.addInitializer = function (f) { if (done) throw new TypeError("Cannot add initializers after decoration has completed"); extraInitializers.push(accept(f || null)); };
        var result = (0, decorators[i])(kind === "accessor" ? { get: descriptor.get, set: descriptor.set } : descriptor[key], context);
        if (kind === "accessor") {
            if (result === void 0) continue;
            if (result === null || typeof result !== "object") throw new TypeError("Object expected");
            if (_ = accept(result.get)) descriptor.get = _;
            if (_ = accept(result.set)) descriptor.set = _;
            if (_ = accept(result.init)) initializers.unshift(_);
        }
        else if (_ = accept(result)) {
            if (kind === "field") initializers.unshift(_);
            else descriptor[key] = _;
        }
    }
    if (target) Object.defineProperty(target, contextIn.name, descriptor);
    done = true;
};
import { TypertRemoteService, Remote } from '@deepseek-ai/dsh-typert-protocol';
import { OrbitGateway } from './gateway.js';
import { OrbitSessionBridge, restoredBridgeState, sessionCanBridge } from './session-bridge.js';
import { OrbitToolBridge } from './orbit-tools.js';
let OrbitRemoteService = (() => {
    let _classSuper = TypertRemoteService;
    let _instanceExtraInitializers = [];
    let _getRuntime_decorators;
    let _getRun_decorators;
    let _getSteps_decorators;
    let _getGraph_decorators;
    let _getEdges_decorators;
    let _readOutput_decorators;
    let _listArtifacts_decorators;
    let _getArtifact_decorators;
    let _getArtifactContent_decorators;
    let _reconcileDelegation_decorators;
    let _executeCommand_decorators;
    return class OrbitRemoteService extends _classSuper {
        static {
            const _metadata = typeof Symbol === "function" && Symbol.metadata ? Object.create(_classSuper[Symbol.metadata] ?? null) : void 0;
            _getRuntime_decorators = [Remote('getRuntime')];
            _getRun_decorators = [Remote('getRun')];
            _getSteps_decorators = [Remote('getSteps')];
            _getGraph_decorators = [Remote('getGraph')];
            _getEdges_decorators = [Remote('getEdges')];
            _readOutput_decorators = [Remote('readOutput')];
            _listArtifacts_decorators = [Remote('listArtifacts')];
            _getArtifact_decorators = [Remote('getArtifact')];
            _getArtifactContent_decorators = [Remote('getArtifactContent')];
            _reconcileDelegation_decorators = [Remote('reconcileDelegation')];
            _executeCommand_decorators = [Remote('executeCommand')];
            __esDecorate(this, null, _getRuntime_decorators, { kind: "method", name: "getRuntime", static: false, private: false, access: { has: obj => "getRuntime" in obj, get: obj => obj.getRuntime }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getRun_decorators, { kind: "method", name: "getRun", static: false, private: false, access: { has: obj => "getRun" in obj, get: obj => obj.getRun }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getSteps_decorators, { kind: "method", name: "getSteps", static: false, private: false, access: { has: obj => "getSteps" in obj, get: obj => obj.getSteps }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getGraph_decorators, { kind: "method", name: "getGraph", static: false, private: false, access: { has: obj => "getGraph" in obj, get: obj => obj.getGraph }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getEdges_decorators, { kind: "method", name: "getEdges", static: false, private: false, access: { has: obj => "getEdges" in obj, get: obj => obj.getEdges }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _readOutput_decorators, { kind: "method", name: "readOutput", static: false, private: false, access: { has: obj => "readOutput" in obj, get: obj => obj.readOutput }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _listArtifacts_decorators, { kind: "method", name: "listArtifacts", static: false, private: false, access: { has: obj => "listArtifacts" in obj, get: obj => obj.listArtifacts }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getArtifact_decorators, { kind: "method", name: "getArtifact", static: false, private: false, access: { has: obj => "getArtifact" in obj, get: obj => obj.getArtifact }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getArtifactContent_decorators, { kind: "method", name: "getArtifactContent", static: false, private: false, access: { has: obj => "getArtifactContent" in obj, get: obj => obj.getArtifactContent }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _reconcileDelegation_decorators, { kind: "method", name: "reconcileDelegation", static: false, private: false, access: { has: obj => "reconcileDelegation" in obj, get: obj => obj.reconcileDelegation }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _executeCommand_decorators, { kind: "method", name: "executeCommand", static: false, private: false, access: { has: obj => "executeCommand" in obj, get: obj => obj.executeCommand }, metadata: _metadata }, null, _instanceExtraInitializers);
            if (_metadata) Object.defineProperty(this, Symbol.metadata, { enumerable: true, configurable: true, writable: true, value: _metadata });
        }
        static inject = ['sessions', 'workspaceRegistry', 'tools'];
        gateway = (__runInitializers(this, _instanceExtraInitializers), new OrbitGateway());
        bridges = new Map();
        hostSessions;
        constructor(ctx) {
            super(ctx, 'orbit');
            this.hostSessions = ctx.get('sessions');
            new OrbitToolBridge(ctx, this.gateway).register();
            for (const session of this.hostSessions.list())
                this.startSessionBridge(ctx, session);
            ctx.on('session/created', session => { this.startSessionBridge(ctx, session); }, { global: true });
            ctx.on('session/disposed', session => { this.stopSessionBridge(String(session.id)); }, { global: true });
            ctx.effect(() => () => {
                for (const controller of this.bridges.values())
                    controller.abort();
                this.bridges.clear();
            }, 'orbit: stop Session Bridges');
        }
        startSessionBridge(ctx, session) {
            const sessionId = String(session.id);
            if (this.bridges.has(sessionId) || !sessionCanBridge(session.header))
                return;
            const cwd = session.header.cwd;
            if (!cwd)
                return;
            const controller = new AbortController();
            this.bridges.set(sessionId, controller);
            void this.runSessionBridge(ctx, session, cwd, controller.signal).finally(() => {
                if (this.bridges.get(sessionId) === controller)
                    this.bridges.delete(sessionId);
            });
        }
        stopSessionBridge(sessionId) {
            this.bridges.get(sessionId)?.abort();
            this.bridges.delete(sessionId);
        }
        async runSessionBridge(ctx, session, cwd, signal) {
            const registry = ctx.workspaceRegistry;
            const registered = await registry.resolveByPath(cwd);
            const workspace = {
                id: registered ? String(registered.id) : `cwd:${cwd}`,
                canonicalPath: registered?.path ?? cwd,
            };
            const restored = restoredBridgeState(session.events);
            const knownRuns = restored.knownRuns;
            let cursorPosition = restored.position;
            const cursor = {
                load: () => cursorPosition || undefined,
                save: (_workspaceId, _sessionId, position) => { cursorPosition = position; },
            };
            let lastError = '';
            while (!signal.aborted) {
                try {
                    await this.bridgeSession(workspace, session, cursor, signal, knownRuns);
                    return;
                }
                catch (error) {
                    if (signal.aborted)
                        return;
                    const message = String(error);
                    if (message !== lastError)
                        ctx.logger.warn(`Orbit bridge for Session ${String(session.id)} is waiting: ${message}`);
                    lastError = message;
                    await new Promise(resolve => {
                        const timer = setTimeout(resolve, 2_000);
                        signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
                    });
                }
            }
        }
        async bridgeSession(workspace, session, cursor, signal, knownRuns = []) {
            const bridge = new OrbitSessionBridge(this.gateway, cursor);
            await bridge.run(workspace, String(session.id), {
                append: async (event) => {
                    if (event.type === 'orbit/run-started') {
                        const { type: _type, ...data } = event;
                        session.append('orbit/run-started', data);
                    }
                    else if (event.type === 'orbit/run-checkpoint') {
                        const { type: _type, ...data } = event;
                        session.append('orbit/run-checkpoint', data);
                    }
                    else {
                        const { type: _type, ...data } = event;
                        session.append('orbit/run-ended', data);
                    }
                    await this.hostSessions.flush(session);
                },
            }, signal, knownRuns);
        }
        async getRuntime(workspace, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                const capabilities = await this.gateway.call(workspace, 'probe', 'get_capabilities', {});
                return { workspaceId: workspace.id, state: 'ready', capabilities };
            }
            finally {
                await release();
            }
        }
        async getRun(workspace, sessionId, runId, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                return await this.gateway.run(workspace, sessionId, runId);
            }
            finally {
                await release();
            }
        }
        async getSteps(workspace, sessionId, runId, signal) {
            return await this.readRunField(workspace, sessionId, runId, 'get_run_steps', 'steps', signal);
        }
        async getGraph(workspace, sessionId, runId, signal) {
            return await this.readRunField(workspace, sessionId, runId, 'get_run_graph', 'graph', signal);
        }
        async getEdges(workspace, sessionId, runId, signal) {
            return await this.readRunField(workspace, sessionId, runId, 'get_run_edges', 'edges', signal);
        }
        async readOutput(workspace, sessionId, runId, after, nodeId, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                return await this.gateway.call(workspace, sessionId, 'read_run_output', {
                    run_id: runId, after, ...(nodeId ? { node_id: nodeId } : {}),
                });
            }
            finally {
                await release();
            }
        }
        async listArtifacts(workspace, sessionId, runId, signal) {
            return await this.readRunField(workspace, sessionId, runId, 'list_artifacts', 'artifacts', signal);
        }
        async getArtifact(workspace, sessionId, artifactId, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                return await this.gateway.call(workspace, sessionId, 'read_artifact', { artifact_id: artifactId });
            }
            finally {
                await release();
            }
        }
        async getArtifactContent(workspace, sessionId, artifactId, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                return await this.gateway.call(workspace, sessionId, 'read_artifact_content', { artifact_id: artifactId });
            }
            finally {
                await release();
            }
        }
        async reconcileDelegation(workspace, sessionId, runId, delegationId, outcome, note, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                await this.gateway.call(workspace, sessionId, 'reconcile_delegation', {
                    delegation_id: delegationId, outcome, note,
                    idempotency_key: crypto.randomUUID(),
                });
                const result = await this.gateway.call(workspace, sessionId, 'get_run_steps', { run_id: runId });
                return result.steps;
            }
            finally {
                await release();
            }
        }
        async readRunField(workspace, sessionId, runId, tool, field, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                const result = await this.gateway.call(workspace, sessionId, tool, { run_id: runId });
                return result[field];
            }
            finally {
                await release();
            }
        }
        async executeCommand(request, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(request.workspace);
            try {
                const run = await this.gateway.run(request.workspace, request.sessionId, request.runId);
                const advertised = run.allowed_commands.find(item => item.command === request.command && item.expected_version === request.expectedVersion);
                if (advertised === undefined)
                    throw new Error('Orbit command is no longer advertised at this revision');
                const tool = request.command === 'langgraph_run.cancel' ? 'cancel_run' : 'resume_run';
                return await this.gateway.call(request.workspace, request.sessionId, tool, {
                    run_id: request.runId, expected_version: request.expectedVersion,
                    idempotency_key: request.idempotencyKey, value: request.value,
                    interrupt_id: request.interruptId,
                });
            }
            finally {
                await release();
            }
        }
    };
})();
export { OrbitRemoteService };
export default OrbitRemoteService;
