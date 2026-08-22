export function reduceOrbitRunCards(state, event) {
    const previous = state[event.runId];
    if (previous !== undefined && previous.sourcePosition >= event.sourcePosition)
        return state;
    const next = event.type === 'orbit/run-started'
        ? { runId: event.runId, sourcePosition: event.sourcePosition, goal: event.goal, workflowId: event.workflowId, workflowVersion: event.workflowVersion, revision: event.revision, status: event.status, artifactCount: 0, updatedAt: event.createdAt, terminal: false }
        : { runId: event.runId, sourcePosition: event.sourcePosition, goal: previous?.goal, workflowId: previous?.workflowId, workflowVersion: previous?.workflowVersion, revision: event.revision, status: event.status, artifactCount: event.artifactCount, updatedAt: event.updatedAt, terminal: event.type === 'orbit/run-ended' };
    return { ...state, [event.runId]: next };
}
