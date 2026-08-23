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
import { artifactImageInput } from './artifact-import.js';
let OrbitRemoteService = (() => {
    let _classSuper = TypertRemoteService;
    let _instanceExtraInitializers = [];
    let _getRuntime_decorators;
    let _getDiagnostics_decorators;
    let _listWorkflows_decorators;
    let _listRuns_decorators;
    let _generateWorkflow_decorators;
    let _modifyWorkflow_decorators;
    let _getAuthoringJob_decorators;
    let _getRun_decorators;
    let _getSteps_decorators;
    let _getGraph_decorators;
    let _getEdges_decorators;
    let _readOutput_decorators;
    let _listArtifacts_decorators;
    let _getArtifact_decorators;
    let _getArtifactContent_decorators;
    let _importArtifact_decorators;
    let _reconcileDelegation_decorators;
    let _executeCommand_decorators;
    return class OrbitRemoteService extends _classSuper {
        static {
            const _metadata = typeof Symbol === "function" && Symbol.metadata ? Object.create(_classSuper[Symbol.metadata] ?? null) : void 0;
            _getRuntime_decorators = [Remote('getRuntime')];
            _getDiagnostics_decorators = [Remote('getDiagnostics')];
            _listWorkflows_decorators = [Remote('listWorkflows')];
            _listRuns_decorators = [Remote('listRuns')];
            _generateWorkflow_decorators = [Remote('generateWorkflow')];
            _modifyWorkflow_decorators = [Remote('modifyWorkflow')];
            _getAuthoringJob_decorators = [Remote('getAuthoringJob')];
            _getRun_decorators = [Remote('getRun')];
            _getSteps_decorators = [Remote('getSteps')];
            _getGraph_decorators = [Remote('getGraph')];
            _getEdges_decorators = [Remote('getEdges')];
            _readOutput_decorators = [Remote('readOutput')];
            _listArtifacts_decorators = [Remote('listArtifacts')];
            _getArtifact_decorators = [Remote('getArtifact')];
            _getArtifactContent_decorators = [Remote('getArtifactContent')];
            _importArtifact_decorators = [Remote('importArtifact')];
            _reconcileDelegation_decorators = [Remote('reconcileDelegation')];
            _executeCommand_decorators = [Remote('executeCommand')];
            __esDecorate(this, null, _getRuntime_decorators, { kind: "method", name: "getRuntime", static: false, private: false, access: { has: obj => "getRuntime" in obj, get: obj => obj.getRuntime }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getDiagnostics_decorators, { kind: "method", name: "getDiagnostics", static: false, private: false, access: { has: obj => "getDiagnostics" in obj, get: obj => obj.getDiagnostics }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _listWorkflows_decorators, { kind: "method", name: "listWorkflows", static: false, private: false, access: { has: obj => "listWorkflows" in obj, get: obj => obj.listWorkflows }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _listRuns_decorators, { kind: "method", name: "listRuns", static: false, private: false, access: { has: obj => "listRuns" in obj, get: obj => obj.listRuns }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _generateWorkflow_decorators, { kind: "method", name: "generateWorkflow", static: false, private: false, access: { has: obj => "generateWorkflow" in obj, get: obj => obj.generateWorkflow }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _modifyWorkflow_decorators, { kind: "method", name: "modifyWorkflow", static: false, private: false, access: { has: obj => "modifyWorkflow" in obj, get: obj => obj.modifyWorkflow }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getAuthoringJob_decorators, { kind: "method", name: "getAuthoringJob", static: false, private: false, access: { has: obj => "getAuthoringJob" in obj, get: obj => obj.getAuthoringJob }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getRun_decorators, { kind: "method", name: "getRun", static: false, private: false, access: { has: obj => "getRun" in obj, get: obj => obj.getRun }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getSteps_decorators, { kind: "method", name: "getSteps", static: false, private: false, access: { has: obj => "getSteps" in obj, get: obj => obj.getSteps }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getGraph_decorators, { kind: "method", name: "getGraph", static: false, private: false, access: { has: obj => "getGraph" in obj, get: obj => obj.getGraph }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getEdges_decorators, { kind: "method", name: "getEdges", static: false, private: false, access: { has: obj => "getEdges" in obj, get: obj => obj.getEdges }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _readOutput_decorators, { kind: "method", name: "readOutput", static: false, private: false, access: { has: obj => "readOutput" in obj, get: obj => obj.readOutput }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _listArtifacts_decorators, { kind: "method", name: "listArtifacts", static: false, private: false, access: { has: obj => "listArtifacts" in obj, get: obj => obj.listArtifacts }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getArtifact_decorators, { kind: "method", name: "getArtifact", static: false, private: false, access: { has: obj => "getArtifact" in obj, get: obj => obj.getArtifact }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getArtifactContent_decorators, { kind: "method", name: "getArtifactContent", static: false, private: false, access: { has: obj => "getArtifactContent" in obj, get: obj => obj.getArtifactContent }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _importArtifact_decorators, { kind: "method", name: "importArtifact", static: false, private: false, access: { has: obj => "importArtifact" in obj, get: obj => obj.importArtifact }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _reconcileDelegation_decorators, { kind: "method", name: "reconcileDelegation", static: false, private: false, access: { has: obj => "reconcileDelegation" in obj, get: obj => obj.reconcileDelegation }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _executeCommand_decorators, { kind: "method", name: "executeCommand", static: false, private: false, access: { has: obj => "executeCommand" in obj, get: obj => obj.executeCommand }, metadata: _metadata }, null, _instanceExtraInitializers);
            if (_metadata) Object.defineProperty(this, Symbol.metadata, { enumerable: true, configurable: true, writable: true, value: _metadata });
        }
        static inject = ['sessions', 'workspaceRegistry', 'tools', 'attachments'];
        gateway = (__runInitializers(this, _instanceExtraInitializers), new OrbitGateway());
        bridges = new Map();
        bridgeDiagnostics = new Map();
        hostSessions;
        attachments;
        constructor(ctx) {
            super(ctx, 'orbit');
            this.hostSessions = ctx.get('sessions');
            this.attachments = ctx.get('attachments');
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
            this.bridgeDiagnostics.set(sessionId, { state: 'connecting', cursorPosition: 0, updatedAt: new Date().toISOString() });
            void this.runSessionBridge(ctx, session, cwd, controller.signal).finally(() => {
                if (this.bridges.get(sessionId) === controller)
                    this.bridges.delete(sessionId);
            });
        }
        stopSessionBridge(sessionId) {
            this.bridges.get(sessionId)?.abort();
            this.bridges.delete(sessionId);
            const previous = this.bridgeDiagnostics.get(sessionId);
            this.bridgeDiagnostics.set(sessionId, { state: 'stopped', cursorPosition: previous?.cursorPosition || 0, updatedAt: new Date().toISOString() });
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
                save: (_workspaceId, _sessionId, position) => {
                    cursorPosition = position;
                    this.bridgeDiagnostics.set(String(session.id), {
                        state: 'connected', cursorPosition: position, updatedAt: new Date().toISOString(),
                    });
                },
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
                    this.bridgeDiagnostics.set(String(session.id), {
                        state: 'waiting', cursorPosition, lastError: message, updatedAt: new Date().toISOString(),
                    });
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
        async getDiagnostics(workspace, sessionId, signal) {
            const runtime = await this.getRuntime(workspace, signal);
            return {
                generated_at: new Date().toISOString(), workspace_id: workspace.id,
                session_id: sessionId, runtime, gateway: this.gateway.diagnostics(),
                bridge: this.bridgeDiagnostics.get(sessionId) || null,
            };
        }
        async listWorkflows(workspace, sessionId, signal) {
            return await this.readListField(workspace, sessionId, 'list_workflows', 'workflows', {}, signal);
        }
        async listRuns(workspace, sessionId, status, signal) {
            return await this.readListField(workspace, sessionId, 'list_runs', 'runs', {
                limit: 100, ...(status ? { status } : {}),
            }, signal);
        }
        async generateWorkflow(workspace, sessionId, prompt, signal) {
            signal.throwIfAborted();
            if (!prompt.trim() || prompt.length > 20_000)
                throw new Error('Workflow prompt must be 1-20000 characters');
            return await this.gateway.call(workspace, sessionId, 'generate_workflow', {
                prompt: prompt.trim(), display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
            });
        }
        async modifyWorkflow(workspace, sessionId, workflowId, prompt, regenerate, signal) {
            signal.throwIfAborted();
            if (!workflowId.trim())
                throw new Error('Workflow id is required');
            if (!prompt.trim() || prompt.length > 20_000)
                throw new Error('Workflow prompt must be 1-20000 characters');
            return await this.gateway.call(workspace, sessionId, 'modify_workflow', {
                workflow_id: workflowId, prompt: prompt.trim(), mode: regenerate ? 'regenerate' : 'modify',
                display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
            });
        }
        async getAuthoringJob(workspace, sessionId, jobId, signal) {
            signal.throwIfAborted();
            return await this.gateway.call(workspace, sessionId, 'get_authoring_job', { job_id: jobId });
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
            return await this.readListField(workspace, sessionId, 'list_artifacts', 'artifacts', {
                limit: 100, ...(runId ? { run_id: runId } : {}),
            }, signal);
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
        async importArtifact(workspace, sessionId, artifactId, signal) {
            const content = await this.getArtifactContent(workspace, sessionId, artifactId, signal);
            return await this.attachments.saveImage(artifactImageInput(content));
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
        async readListField(workspace, sessionId, tool, field, arguments_, signal) {
            signal.throwIfAborted();
            const release = await this.gateway.acquire(workspace);
            try {
                const result = await this.gateway.call(workspace, sessionId, tool, arguments_);
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
