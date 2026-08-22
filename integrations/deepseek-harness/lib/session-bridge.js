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
        const known = new Set(knownRuns);
        let position = await this.cursor.load(workspace.id, sessionId);
        try {
            while (!signal.aborted) {
                const page = await this.gateway.call(workspace, sessionId, 'list_runtime_events', {
                    ...(position === undefined ? {} : { after_position: position }), limit: 200,
                });
                const latest = new Map();
                for (const event of page.events)
                    latest.set(event.run_id, event.position);
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
