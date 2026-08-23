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
import { OrbitSessionBridge, bridgeWithRetry, restoredBridgeState, sessionCanBridge } from './session-bridge.js';
import { OrbitToolBridge } from './orbit-tools.js';
import { artifactImageInput } from './artifact-import.js';
import { advertisedAt, commandTool } from './commands.js';
import { WorkflowCatalog } from './workflow-catalog.js';
let OrbitRemoteService = (() => {
    let _classSuper = TypertRemoteService;
    let _instanceExtraInitializers = [];
    let _getRuntime_decorators;
    let _getRuntimeUi_decorators;
    let _getPanelState_decorators;
    let _getRunDetail_decorators;
    let _getStepOutput_decorators;
    let _runCommand_decorators;
    let _reconcileStep_decorators;
    let _listRunnable_decorators;
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
            _getRuntimeUi_decorators = [Remote('getRuntimeUi')];
            _getPanelState_decorators = [Remote('getPanelState')];
            _getRunDetail_decorators = [Remote('getRunDetail')];
            _getStepOutput_decorators = [Remote('getStepOutput')];
            _runCommand_decorators = [Remote('runCommand')];
            _reconcileStep_decorators = [Remote('reconcileStep')];
            _listRunnable_decorators = [Remote('listRunnable')];
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
            __esDecorate(this, null, _getRuntimeUi_decorators, { kind: "method", name: "getRuntimeUi", static: false, private: false, access: { has: obj => "getRuntimeUi" in obj, get: obj => obj.getRuntimeUi }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getPanelState_decorators, { kind: "method", name: "getPanelState", static: false, private: false, access: { has: obj => "getPanelState" in obj, get: obj => obj.getPanelState }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getRunDetail_decorators, { kind: "method", name: "getRunDetail", static: false, private: false, access: { has: obj => "getRunDetail" in obj, get: obj => obj.getRunDetail }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _getStepOutput_decorators, { kind: "method", name: "getStepOutput", static: false, private: false, access: { has: obj => "getStepOutput" in obj, get: obj => obj.getStepOutput }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _runCommand_decorators, { kind: "method", name: "runCommand", static: false, private: false, access: { has: obj => "runCommand" in obj, get: obj => obj.runCommand }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _reconcileStep_decorators, { kind: "method", name: "reconcileStep", static: false, private: false, access: { has: obj => "reconcileStep" in obj, get: obj => obj.reconcileStep }, metadata: _metadata }, null, _instanceExtraInitializers);
            __esDecorate(this, null, _listRunnable_decorators, { kind: "method", name: "listRunnable", static: false, private: false, access: { has: obj => "listRunnable" in obj, get: obj => obj.listRunnable }, metadata: _metadata }, null, _instanceExtraInitializers);
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
        static inject = ['sessions', 'workspaceRegistry', 'tools', 'attachments', 'systemPrompt'];
        gateway = (__runInitializers(this, _instanceExtraInitializers), new OrbitGateway());
        catalog = new WorkflowCatalog();
        bridges = new Map();
        /** One entry per live Bridge: the Workspaces worth knowing the Workflows of. */
        bridgedWorkspaces = new Map();
        bridgeDiagnostics = new Map();
        hostSessions;
        attachments;
        workspaceRegistry;
        constructor(ctx) {
            super(ctx, 'orbit');
            this.hostSessions = ctx.get('sessions');
            this.attachments = ctx.get('attachments');
            this.workspaceRegistry = ctx.get('workspaceRegistry');
            new OrbitToolBridge(ctx, this.gateway).register();
            this.registerWebApi(ctx);
            this.tellTheModelWhatCanRun(ctx);
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
        /**
         * Name the runnable Workflows in the model's context, so it does not have to
         * ask before it can tell whether Orbit is relevant to what was just said.
         *
         * The contribution is read synchronously at every assembly, so it can only
         * ever report what has already been fetched: a stale entry answers now and
         * refreshes for next time. The alternative — blocking assembly on a Runtime
         * that may not be running — would make a missing Orbit everyone's problem.
         */
        tellTheModelWhatCanRun(ctx) {
            const systemPrompt = ctx.get('systemPrompt');
            if (!systemPrompt)
                return;
            ctx.effect(() => systemPrompt.context({
                name: 'orbit-workflows',
                // After the tool guidance it belongs with: this says which Workflows the
                // tools above can be pointed at.
                order: 190,
                text: () => {
                    for (const workspace of this.bridgedWorkspaces.values()) {
                        if (this.catalog.stale(workspace.canonicalPath))
                            this.refreshCatalog(workspace);
                    }
                    return this.catalog.render();
                },
            }), 'orbit: runnable Workflows in the model context');
        }
        /**
         * Read a Workspace's Workflows into the catalog; a failure leaves the last
         * answer standing.
         *
         * The parameter is a `scope` and not a `workspace` because that is what it
         * is: every caller derived it from a Session. The name is also what the
         * bundle's guard reads, so calling it anything else is how this stops being
         * checked.
         */
        refreshCatalog(scope) {
            return this.gateway.call(scope, 'catalog', 'list_workflows', { ready_only: true })
                .then(result => {
                this.catalog.remember(scope.canonicalPath, result.workflows);
            })
                .catch(() => { });
        }
        registerWebApi(ctx) {
            let registered = false;
            const mount = () => {
                if (registered)
                    return;
                const webServer = (ctx.get('webServer') ?? ctx.get('httpServer'));
                if (!webServer)
                    return;
                registered = true;
                ctx.effect(() => webServer.register({
                    kind: 'exact', path: '/plugins/dsh-orbit/api',
                    handler: async (req, res) => {
                        if (req.method !== 'POST') {
                            res.writeHead(405, { allow: 'POST' });
                            res.end();
                            return;
                        }
                        const controller = new AbortController();
                        req.once('aborted', () => controller.abort(new Error('Orbit client request aborted')));
                        try {
                            const chunks = [];
                            let size = 0;
                            for await (const chunk of req) {
                                const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
                                size += buffer.length;
                                if (size > 256 * 1024) {
                                    req.destroy();
                                    throw new Error('Orbit client request exceeds 256 KiB');
                                }
                                chunks.push(buffer);
                            }
                            const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
                            if (typeof body.action !== 'string' || !Array.isArray(body.args))
                                throw new Error('Orbit client request requires action and args');
                            const result = await this.dispatchWebApi(body.action, body.args, controller.signal);
                            res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
                            res.end(JSON.stringify({ result: result === undefined ? null : result }));
                        }
                        catch (error) {
                            res.writeHead(400, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
                            res.end(JSON.stringify({ error: String(error) }));
                        }
                    },
                }), 'orbit: browser Host API');
            };
            mount();
            ctx.on('internal/service', name => { if (name === 'webServer' || name === 'httpServer')
                mount(); });
        }
        async dispatchWebApi(action, args, signal) {
            switch (action) {
                case 'getRuntime': return await this.getRuntime(args[0], signal);
                case 'getRuntimeUi': return await this.getRuntimeUi(String(args[0]), signal);
                case 'listRunnable': return await this.listRunnable(String(args[0]), signal);
                case 'getPanelState': return await this.getPanelState(String(args[0]), signal);
                case 'getRunDetail': return await this.getRunDetail(String(args[0]), String(args[1]), signal);
                case 'getStepOutput': return await this.getStepOutput(String(args[0]), String(args[1]), String(args[2]), Number(args[3]), signal);
                case 'runCommand': return await this.runCommand(String(args[0]), String(args[1]), args[2], Number(args[3]), args[4], args[5] === undefined ? undefined : String(args[5]), signal);
                case 'reconcileStep': return await this.reconcileStep(String(args[0]), String(args[1]), String(args[2]), args[3], String(args[4]), signal);
                case 'getDiagnostics': return await this.getDiagnostics(args[0], String(args[1]), signal);
                case 'listWorkflows': return await this.listWorkflows(args[0], String(args[1]), signal);
                case 'listRuns': return await this.listRuns(args[0], String(args[1]), args[2] === undefined ? undefined : String(args[2]), signal);
                case 'generateWorkflow': return await this.generateWorkflow(args[0], String(args[1]), String(args[2]), signal);
                case 'modifyWorkflow': return await this.modifyWorkflow(args[0], String(args[1]), String(args[2]), String(args[3]), Boolean(args[4]), signal);
                case 'getAuthoringJob': return await this.getAuthoringJob(args[0], String(args[1]), String(args[2]), signal);
                case 'getRun': return await this.getRun(args[0], String(args[1]), String(args[2]), signal);
                case 'getSteps': return await this.getSteps(args[0], String(args[1]), String(args[2]), signal);
                case 'getGraph': return await this.getGraph(args[0], String(args[1]), String(args[2]), signal);
                case 'getEdges': return await this.getEdges(args[0], String(args[1]), String(args[2]), signal);
                case 'readOutput': return await this.readOutput(args[0], String(args[1]), String(args[2]), Number(args[3]), args[4] === undefined ? undefined : String(args[4]), signal);
                case 'listArtifacts': return await this.listArtifacts(args[0], String(args[1]), args[2] === undefined ? undefined : String(args[2]), signal);
                case 'getArtifactContent': return await this.getArtifactContent(args[0], String(args[1]), String(args[2]), signal);
                case 'importArtifact': return await this.importArtifact(args[0], String(args[1]), String(args[2]), signal);
                case 'executeCommand': return await this.executeCommand(args[0], signal);
                case 'reconcileDelegation': return await this.reconcileDelegation(args[0], String(args[1]), String(args[2]), String(args[3]), args[4], String(args[5]), signal);
                default: throw new Error(`Unknown Orbit client action: ${action}`);
            }
        }
        /**
         * Turn a caller-supplied Workspace into one this Host vouches for.
         *
         * The browser sends a Workspace with every call, and a browser is not an
         * authority on which directory a Session belongs to: the Session is. A
         * mismatch is refused rather than quietly corrected, because the two
         * disagreeing at all means the caller is describing a Session it is not in.
         */
        async verified(claimed, sessionId) {
            const actual = await this.workspaceForSession(this.liveSession(sessionId));
            if (claimed.id !== actual.id || claimed.canonicalPath !== actual.canonicalPath) {
                throw new Error('Orbit request Workspace does not match the Harness Session');
            }
            return actual;
        }
        /**
         * The same guarantee for the Settings panel, which has a Workspace but no
         * Session. Its authority is the Workspace registry: a path nobody registered
         * is not somewhere this Host will go looking for a Runtime.
         */
        async registered(claimed) {
            const found = await this.workspaceRegistry.resolveByPath(claimed.canonicalPath);
            if (!found || String(found.id) !== claimed.id || found.path !== claimed.canonicalPath) {
                throw new Error('Orbit request names a Workspace this Harness has not registered');
            }
            return { id: String(found.id), canonicalPath: found.path };
        }
        /**
         * The Workspace of a Session, derived and never claimed.
         *
         * Stronger than `verified`: there is no caller-supplied value to disagree
         * with, so there is nothing to check.
         */
        async sessionWorkspace(sessionId) {
            return await this.workspaceForSession(this.liveSession(sessionId));
        }
        liveSession(sessionId) {
            const session = this.hostSessions.list().find(item => String(item.id) === sessionId);
            if (!session)
                throw new Error('Orbit requires a live Harness Session');
            return session;
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
            // Only a disposed Session stops a Bridge, and nothing can ask a disposed
            // Session for its diagnostics. Keeping the entry would grow this map by one
            // for every Session the Host ever opened.
            this.bridgeDiagnostics.delete(sessionId);
            this.bridgedWorkspaces.delete(sessionId);
        }
        async runSessionBridge(ctx, session, cwd, signal) {
            const registry = ctx.workspaceRegistry;
            const registered = await registry.resolveByPath(cwd);
            const workspace = {
                id: registered ? String(registered.id) : `cwd:${cwd}`,
                canonicalPath: registered?.path ?? cwd,
            };
            this.bridgedWorkspaces.set(String(session.id), workspace);
            // Warm it before the first turn asks: a Bridge starts when the Session is
            // created, and the person types afterwards.
            this.refreshCatalog(workspace);
            let cursorPosition = restoredBridgeState(session.events).position;
            const cursor = {
                load: () => cursorPosition || undefined,
                save: (_workspaceId, _sessionId, position) => {
                    cursorPosition = position;
                    this.bridgeDiagnostics.set(String(session.id), {
                        state: 'connected', cursorPosition: position, updatedAt: new Date().toISOString(),
                    });
                },
            };
            await bridgeWithRetry({
                events: () => session.events,
                attempt: async (knownRuns) => {
                    await this.bridgeSession(workspace, session, cursor, signal, knownRuns);
                },
                onWaiting: message => {
                    this.bridgeDiagnostics.set(String(session.id), {
                        state: 'waiting', cursorPosition, lastError: message, updatedAt: new Date().toISOString(),
                    });
                    ctx.logger.warn(`Orbit bridge for Session ${String(session.id)} is waiting: ${message}`);
                },
                signal,
            });
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
            const scope = await this.registered(workspace);
            const release = await this.gateway.acquire(scope);
            try {
                const capabilities = await this.gateway.call(scope, 'probe', 'get_capabilities', {});
                return { workspaceId: scope.id, state: 'ready', capabilities };
            }
            finally {
                await release();
            }
        }
        async getRuntimeUi(sessionId, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.uiUrl(scope);
            }
            finally {
                await release();
            }
        }
        /**
         * Everything the resident panel draws, in one round trip.
         *
         * It takes a Session and derives the Workspace, so a poller that runs every
         * couple of seconds carries no claim the Host has to check — and the panel
         * never has to know what a Workspace is.
         */
        async getPanelState(sessionId, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                const result = await this.gateway.call(scope, sessionId, 'list_runs', {
                    limit: 50,
                });
                // The catalog the model is told about, read rather than fetched again:
                // a poll running every two seconds should not ask twice for something
                // that changes when someone publishes a Workflow.
                if (this.catalog.stale(scope.canonicalPath))
                    this.refreshCatalog(scope);
                return {
                    runs: result.runs,
                    uiUrl: await this.gateway.uiUrl(scope),
                    workflows: this.catalog.list(scope.canonicalPath),
                };
            }
            finally {
                await release();
            }
        }
        /**
         * The steps of one Run, for a panel row the reader opened.
         *
         * Session-scoped like the panel's poll: a Run id is not a capability, so the
         * Workspace it is read in comes from the Session rather than from the caller.
         */
        async getRunDetail(sessionId, runId, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.call(scope, sessionId, 'get_run_steps', {
                    run_id: runId,
                });
            }
            finally {
                await release();
            }
        }
        async getStepOutput(sessionId, runId, nodeId, after, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.call(scope, sessionId, 'read_run_output', {
                    run_id: runId, after, node_id: nodeId,
                });
            }
            finally {
                await release();
            }
        }
        /**
         * Cancel or resume a Run from the panel.
         *
         * `expectedRevision` is what the panel had on screen, and it must still be
         * what Orbit advertises. Re-reading here would make the call succeed against
         * a Run that changed under the reader — the refusal is the point: whoever
         * pressed the button was looking at something else.
         */
        async runCommand(sessionId, runId, command, expectedRevision, value, interruptId, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                const run = await this.gateway.run(scope, sessionId, runId);
                const advertised = advertisedAt(run, command, expectedRevision);
                if (advertised === undefined) {
                    throw new Error(`Orbit no longer offers ${command} at revision ${String(expectedRevision)}`);
                }
                return await this.gateway.call(scope, sessionId, commandTool(command), {
                    run_id: runId, expected_version: advertised.expected_version,
                    idempotency_key: crypto.randomUUID(),
                    ...(value === undefined ? {} : { value }),
                    ...(interruptId === undefined ? {} : { interrupt_id: interruptId }),
                });
            }
            finally {
                await release();
            }
        }
        /** Record a person's ruling on what an external Agent actually did. */
        async reconcileStep(sessionId, runId, delegationId, outcome, note, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                await this.gateway.call(scope, sessionId, 'reconcile_delegation', {
                    delegation_id: delegationId, outcome, note,
                    idempotency_key: crypto.randomUUID(),
                });
                return await this.gateway.call(scope, sessionId, 'get_run_steps', {
                    run_id: runId,
                });
            }
            finally {
                await release();
            }
        }
        /**
         * The runnable Workflows, for a person who just asked.
         *
         * Refreshed before answering rather than served stale: a command is pressed
         * because someone wants to know now, and the reason the prompt contribution
         * cannot wait — assembly is synchronous — does not apply here.
         */
        async listRunnable(sessionId, signal) {
            signal.throwIfAborted();
            const scope = await this.sessionWorkspace(sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                await this.refreshCatalog(scope);
                return this.catalog.list(scope.canonicalPath);
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
        async workspaceForSession(session) {
            const cwd = session.header.cwd;
            if (!cwd)
                throw new Error('Orbit requires the Harness Session to have a Workspace cwd');
            const registered = await this.workspaceRegistry.resolveByPath(cwd);
            return {
                id: registered ? String(registered.id) : `cwd:${cwd}`,
                canonicalPath: registered?.path ?? cwd,
            };
        }
        async generateWorkflow(workspace, sessionId, prompt, signal) {
            signal.throwIfAborted();
            if (!prompt.trim() || prompt.length > 20_000)
                throw new Error('Workflow prompt must be 1-20000 characters');
            const scope = await this.verified(workspace, sessionId);
            return await this.gateway.call(scope, sessionId, 'generate_workflow', {
                prompt: prompt.trim(), display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
            });
        }
        async modifyWorkflow(workspace, sessionId, workflowId, prompt, regenerate, signal) {
            signal.throwIfAborted();
            if (!workflowId.trim())
                throw new Error('Workflow id is required');
            if (!prompt.trim() || prompt.length > 20_000)
                throw new Error('Workflow prompt must be 1-20000 characters');
            const scope = await this.verified(workspace, sessionId);
            return await this.gateway.call(scope, sessionId, 'modify_workflow', {
                workflow_id: workflowId, prompt: prompt.trim(), mode: regenerate ? 'regenerate' : 'modify',
                display_language: 'zh-CN', idempotency_key: crypto.randomUUID(),
            });
        }
        async getAuthoringJob(workspace, sessionId, jobId, signal) {
            signal.throwIfAborted();
            const scope = await this.verified(workspace, sessionId);
            return await this.gateway.call(scope, sessionId, 'get_authoring_job', { job_id: jobId });
        }
        async getRun(workspace, sessionId, runId, signal) {
            signal.throwIfAborted();
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.run(scope, sessionId, runId);
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
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.call(scope, sessionId, 'read_run_output', {
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
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.call(scope, sessionId, 'read_artifact', { artifact_id: artifactId });
            }
            finally {
                await release();
            }
        }
        async getArtifactContent(workspace, sessionId, artifactId, signal) {
            signal.throwIfAborted();
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                return await this.gateway.call(scope, sessionId, 'read_artifact_content', { artifact_id: artifactId });
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
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                await this.gateway.call(scope, sessionId, 'reconcile_delegation', {
                    delegation_id: delegationId, outcome, note,
                    idempotency_key: crypto.randomUUID(),
                });
                const result = await this.gateway.call(scope, sessionId, 'get_run_steps', { run_id: runId });
                return result.steps;
            }
            finally {
                await release();
            }
        }
        async readRunField(workspace, sessionId, runId, tool, field, signal) {
            signal.throwIfAborted();
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                const result = await this.gateway.call(scope, sessionId, tool, { run_id: runId });
                return result[field];
            }
            finally {
                await release();
            }
        }
        async readListField(workspace, sessionId, tool, field, arguments_, signal) {
            signal.throwIfAborted();
            const scope = await this.verified(workspace, sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                const result = await this.gateway.call(scope, sessionId, tool, arguments_);
                return result[field];
            }
            finally {
                await release();
            }
        }
        async executeCommand(request, signal) {
            signal.throwIfAborted();
            const scope = await this.verified(request.workspace, request.sessionId);
            const release = await this.gateway.acquire(scope);
            try {
                const run = await this.gateway.run(scope, request.sessionId, request.runId);
                const advertised = advertisedAt(run, request.command, request.expectedVersion);
                if (advertised === undefined)
                    throw new Error('Orbit command is no longer advertised at this revision');
                const tool = commandTool(request.command);
                return await this.gateway.call(scope, request.sessionId, tool, {
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
