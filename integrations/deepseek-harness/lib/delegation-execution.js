import { delegationRefusal } from './delegation-policy.js';
import { effectManifest, snapshotWorkspace } from './effects.js';
/** Execute one already-leased job. A rejected published result is deliberately
 * left unsettled: Orbit expires the Job Lease to unknown and never replays it. */
export async function executeDelegation(workspace, delegation, parent, subagents, outerSignal, ports) {
    const config = delegation.request.config;
    const provider = String(config.provider || '');
    const effects = String(config.effects || 'read');
    const refusal = delegationRefusal(workspace, delegation, subagents.list());
    if (refusal !== undefined) {
        await ports.settle(undefined, refusal);
        return;
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    outerSignal.addEventListener('abort', abort, { once: true });
    const wallTimer = setTimeout(abort, Math.max(1, Number(config.max_wall_seconds || 1800)) * 1000);
    let run;
    let renewal;
    let leaseLost = false;
    const before = await ports.snapshot(workspace.canonicalPath);
    try {
        const task = delegation.request.input.task ?? delegation.request.input;
        try {
            run = await subagents.start(provider, {
                label: `Orbit ${delegation.delegation_id.slice(-8)}`,
                prompt: [{ type: 'text', text: typeof task === 'string' ? task : JSON.stringify(task) }],
                parent, signal: controller.signal,
            });
        }
        catch (error) {
            await ports.settle(undefined, String(error));
            return;
        }
        renewal = setInterval(() => {
            void ports.renew().then(item => {
                if (item.cancel_requested)
                    controller.abort();
            }).catch(() => { leaseLost = true; controller.abort(); });
        }, ports.renewalMilliseconds);
        let result;
        try {
            result = await run.result;
        }
        catch {
            return;
        }
        if (leaseLost)
            return;
        const observedEffects = effectManifest(before, await ports.snapshot(workspace.canonicalPath));
        if (effects === 'read' && (observedEffects.changedFiles.length || observedEffects.createdFiles.length || observedEffects.deletedFiles.length)) {
            await ports.settle(undefined, 'read-only delegation modified the workspace');
            return;
        }
        if (result.stopReason === 'completed') {
            await ports.settle({
                answer: result.structured ?? { output: result.output }, effects: observedEffects,
            });
        }
        else {
            await ports.settle(undefined, result.diagnostic || `subagent stopped: ${String(result.stopReason)}`);
        }
    }
    finally {
        if (renewal)
            clearInterval(renewal);
        clearTimeout(wallTimer);
        outerSignal.removeEventListener('abort', abort);
        if (run)
            await run.dispose().catch(() => undefined);
    }
}
export const defaultDelegationExecutionPorts = {
    snapshot: snapshotWorkspace,
    renewalMilliseconds: 10_000,
};
