/** Host-observable policy checks that must pass before a Provider is started. */
export function delegationRefusal(workspace, delegation, registeredProviders) {
    const config = delegation.request.config;
    const provider = String(config.provider || '');
    const effects = String(config.effects || 'read');
    const requestedIsolation = String(config.isolation_mode || 'shared');
    const actualIsolation = workspace.isolationMode || 'shared';
    if (!registeredProviders.includes(provider)) {
        return `Harness subagent provider is not registered: ${provider}`;
    }
    if (Number(config.max_concurrency || 1) !== 1) {
        return 'Orbit delegation worker supports max_concurrency=1';
    }
    if (effects === 'write' && !['exclusive', 'worktree'].includes(actualIsolation)) {
        return `write delegation refused in ${actualIsolation} workspace`;
    }
    if (requestedIsolation !== actualIsolation) {
        return `workspace isolation mismatch: requested ${requestedIsolation}, actual ${actualIsolation}`;
    }
    return undefined;
}
