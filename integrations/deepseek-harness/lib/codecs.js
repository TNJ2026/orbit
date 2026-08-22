function object(value, path) {
    if (value === null || typeof value !== 'object' || Array.isArray(value))
        throw new Error(`invalid Orbit DTO at ${path}: expected object`);
    return value;
}
function string(value, path) { if (typeof value !== 'string')
    throw new Error(`invalid Orbit DTO at ${path}: expected string`); return value; }
function number(value, path) { if (typeof value !== 'number' || !Number.isFinite(value))
    throw new Error(`invalid Orbit DTO at ${path}: expected number`); return value; }
function boolean(value, path) { if (typeof value !== 'boolean')
    throw new Error(`invalid Orbit DTO at ${path}: expected boolean`); return value; }
function array(value, path) { if (!Array.isArray(value))
    throw new Error(`invalid Orbit DTO at ${path}: expected array`); return value; }
export function decodeRun(value) {
    const item = object(value, 'run');
    for (const key of ['run_id', 'goal', 'workflow_id', 'status', 'created_at', 'updated_at'])
        string(item[key], `run.${key}`);
    for (const key of ['workflow_version', 'revision', 'artifact_count'])
        number(item[key], `run.${key}`);
    array(item.interrupts, 'run.interrupts');
    for (const [index, command] of array(item.allowed_commands, 'run.allowed_commands').entries()) {
        const entry = object(command, `run.allowed_commands[${index}]`);
        string(entry.command, `run.allowed_commands[${index}].command`);
        number(entry.expected_version, `run.allowed_commands[${index}].expected_version`);
    }
    return item;
}
function decodeStep(value, index) {
    const item = object(value, `steps[${index}]`);
    string(item.node_id, `steps[${index}].node_id`);
    string(item.status, `steps[${index}].status`);
    if (item.resolution !== undefined && item.resolution !== null) {
        const resolution = object(item.resolution, `steps[${index}].resolution`);
        if (string(resolution.kind, `steps[${index}].resolution.kind`) !== 'reconciliation_required')
            throw new Error(`invalid Orbit DTO at steps[${index}].resolution.kind`);
        if (resolution.delegation_id !== undefined && resolution.delegation_id !== null)
            string(resolution.delegation_id, `steps[${index}].resolution.delegation_id`);
    }
    if (item.reconciliation !== undefined && item.reconciliation !== null) {
        const decision = object(item.reconciliation, `steps[${index}].reconciliation`);
        const outcome = string(decision.outcome, `steps[${index}].reconciliation.outcome`);
        if (!['confirmed_succeeded', 'confirmed_failed'].includes(outcome))
            throw new Error(`invalid Orbit DTO at steps[${index}].reconciliation.outcome`);
        string(decision.note, `steps[${index}].reconciliation.note`);
        string(decision.created_at, `steps[${index}].reconciliation.created_at`);
    }
    return item;
}
function decodeDelegation(value) {
    const item = object(value, 'delegation');
    string(item.delegation_id, 'delegation.delegation_id');
    string(item.status, 'delegation.status');
    boolean(item.cancel_requested, 'delegation.cancel_requested');
    const request = object(item.request, 'delegation.request');
    object(request.input, 'delegation.request.input');
    object(request.config, 'delegation.request.config');
    return item;
}
export function decodeToolResult(name, value) {
    if (['inspect_run', 'start_run', 'resume_run', 'cancel_run'].includes(name))
        return decodeRun(value);
    const item = object(value, name);
    if (name === 'get_run_steps')
        return { ...item, steps: array(item.steps, 'steps').map(decodeStep) };
    if (name === 'get_run_graph') {
        object(item.graph, 'graph');
        return item;
    }
    if (name === 'get_run_edges') {
        for (const [index, edge] of array(item.edges, 'edges').entries()) {
            const entry = object(edge, `edges[${index}]`);
            for (const key of ['edge_id', 'source_node', 'target_node', 'status'])
                string(entry[key], `edges[${index}].${key}`);
        }
        return item;
    }
    if (name === 'read_run_output') {
        array(item.chunks, 'output.chunks');
        number(item.after, 'output.after');
        boolean(item.has_more, 'output.has_more');
        return item;
    }
    if (name === 'list_artifacts') {
        array(item.artifacts, 'artifacts').forEach((v, i) => { const a = object(v, `artifacts[${i}]`); string(a.artifact_id, `artifacts[${i}].artifact_id`); string(a.run_id, `artifacts[${i}].run_id`); });
        return item;
    }
    if (name === 'read_artifact') {
        string(item.artifact_id, 'artifact.artifact_id');
        string(item.run_id, 'artifact.run_id');
        return item;
    }
    if (name === 'read_artifact_content') {
        object(item.artifact, 'artifact_content.artifact');
        if (string(item.encoding, 'artifact_content.encoding') !== 'base64')
            throw new Error('invalid Orbit DTO at artifact_content.encoding');
        string(item.content, 'artifact_content.content');
        return item;
    }
    if (['claim_delegation', 'renew_delegation', 'complete_delegation'].includes(name))
        return { ...item, delegation: item.delegation === null ? null : decodeDelegation(item.delegation) };
    if (name === 'list_runtime_events') {
        array(item.events, 'events');
        number(item.next_position, 'next_position');
        return item;
    }
    return item;
}
