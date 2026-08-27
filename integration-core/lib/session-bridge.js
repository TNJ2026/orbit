export function restoredBridgeState(events) {
    const prior = events.flatMap(event => {
        if (event.type !== 'orbit/run-started' && event.type !== 'orbit/run-checkpoint' && event.type !== 'orbit/run-ended')
            return [];
        if (event.data === null || typeof event.data !== 'object' || Array.isArray(event.data))
            return [];
        const data = event.data;
        const runId = typeof data.runId === 'string' ? data.runId : '';
        const sourcePosition = Number(data.sourcePosition);
        return runId && Number.isSafeInteger(sourcePosition) && sourcePosition >= 0 ? [{ runId, sourcePosition }] : [];
    });
    return {
        knownRuns: new Set(prior.map(event => event.runId)),
        position: prior.reduce((latest, event) => Math.max(latest, event.sourcePosition), 0),
    };
}
export function sessionCanBridge(header) {
    return Boolean(header.cwd) && (header.delegationDepth ?? 0) === 0;
}
export function bridgeDelay(ms, signal) {
    return new Promise(resolve => {
        const timer = setTimeout(resolve, ms);
        signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
    });
}
/** Keep attempting a Session Bridge until it finishes or the caller gives up. */
export async function bridgeWithRetry(options) {
    let lastError = '';
    while (!options.signal.aborted) {
        try {
            await options.attempt(restoredBridgeState(options.events()).knownRuns);
            return;
        }
        catch (error) {
            if (options.signal.aborted)
                return;
            const message = String(error);
            if (message !== lastError)
                options.onWaiting(message);
            lastError = message;
            await bridgeDelay(options.retryDelayMs ?? 2_000, options.signal);
        }
    }
}
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'unknown']);
export class OrbitSessionBridge {
    gateway;
    cursor;
    intervalMs;
    constructor(gateway, cursor, intervalMs = 500) {
        this.gateway = gateway;
        this.cursor = cursor;
        this.intervalMs = intervalMs;
    }
    async run(workspace, sessionId, sink, signal, knownRuns = []) {
        const release = await this.gateway.acquire(workspace);
        try {
            const known = new Set(knownRuns);
            let position = await this.cursor.load(workspace.id, sessionId);
            while (!signal.aborted) {
                const page = await this.gateway.call(workspace, sessionId, 'list_runtime_events', {
                    ...(position === undefined ? {} : { after_position: position }), limit: 200,
                });
                const latest = new Map();
                for (const event of page.events)
                    latest.set(event.run_id, Math.max(latest.get(event.run_id) ?? 0, event.position));
                for (const [runId, sourcePosition] of latest) {
                    const run = await this.gateway.run(workspace, sessionId, runId);
                    const steps = await this.gateway.call(workspace, sessionId, 'get_run_steps', { run_id: runId });
                    if (!known.has(runId)) {
                        await sink.append({ type: 'orbit/run-started', sourcePosition, runId, workspaceId: workspace.id, goal: run.goal, workflowId: run.workflow_id, workflowVersion: run.workflow_version, revision: run.revision, status: run.status, createdAt: run.created_at });
                        known.add(runId);
                    }
                    const counts = {};
                    for (const step of steps.steps)
                        counts[step.status] = (counts[step.status] ?? 0) + 1;
                    if (TERMINAL.has(run.status)) {
                        await sink.append({ type: 'orbit/run-ended', sourcePosition, runId, revision: run.revision, status: run.status, artifactCount: run.artifact_count, updatedAt: run.updated_at });
                        known.add(runId);
                    }
                    else {
                        await sink.append({ type: 'orbit/run-checkpoint', sourcePosition, runId, revision: run.revision, status: run.status, currentSteps: steps.steps, stepCounts: counts, artifactCount: run.artifact_count, updatedAt: run.updated_at });
                    }
                }
                position = page.next_position;
                await this.cursor.save(workspace.id, sessionId, position);
                if (page.events.length === 0)
                    await new Promise(resolve => { const timer = setTimeout(resolve, this.intervalMs); signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true }); });
            }
        }
        finally {
            await release();
        }
    }
}
