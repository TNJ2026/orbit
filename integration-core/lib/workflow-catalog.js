/** What can be run here, kept ready for the model to be told without asking.
 *
 * The Agent has `orbit_list_workflows`, but a tool it must call first cannot
 * help with the turn where someone says "clean this up" — by then the model has
 * already had to decide whether Orbit is even relevant. Naming the Workflows in
 * the prompt turns that decision into a reading.
 */
/** Beyond this the list stops being context and starts being noise. */
export const CATALOG_LIMIT = 20;
/** How long a remembered catalog is still worth telling the model about. */
export const CATALOG_TTL_MS = 5 * 60_000;
/** One line per Workflow: what to name it, and what it needs. */
function line(workflow) {
    const inputs = Array.isArray(workflow.inputs)
        ? workflow.inputs
            .map(input => input.id)
            .filter((id) => typeof id === 'string')
        : [];
    const needs = inputs.length ? ` (input: ${inputs.join(', ')})` : '';
    const name = workflow.name || workflow.workflow_id;
    return `- ${workflow.workflow_id}@${String(workflow.latest_version)} — ${name}${needs}`;
}
export class WorkflowCatalog {
    now;
    byWorkspace = new Map();
    constructor(now = Date.now) {
        this.now = now;
    }
    remember(canonicalPath, workflows) {
        this.byWorkspace.set(canonicalPath, { workflows, at: this.now() });
    }
    forget(canonicalPath) {
        this.byWorkspace.delete(canonicalPath);
    }
    /**
     * Everything remembered for one Workspace, in the order it was read.
     *
     * All of it, not the runnable subset: this answers the panel, where a person
     * is reading a catalog and a Workflow that has gone unrunnable is something
     * they need to see — it is the one they have to go and fix. `render`, which
     * answers the model, keeps the filter: there a name is an offer to run.
     */
    list(canonicalPath) {
        return this.byWorkspace.get(canonicalPath)?.workflows ?? [];
    }
    /** Whether a workspace's entry is missing or old enough to re-read. */
    stale(canonicalPath) {
        const entry = this.byWorkspace.get(canonicalPath);
        return entry === undefined || this.now() - entry.at > CATALOG_TTL_MS;
    }
    /**
     * The prompt contribution, or an empty string when there is nothing to say.
     *
     * Empty rather than a sentence explaining the emptiness: a contribution that
     * says "no Workflows are known" costs the same tokens every turn and tells
     * the model nothing it could not infer from the absence.
     */
    render() {
        const parts = [];
        for (const [path, entry] of [...this.byWorkspace].sort()) {
            const ready = entry.workflows.filter(item => item.goal_readiness === 'ready');
            if (!ready.length)
                continue;
            const shown = ready.slice(0, CATALOG_LIMIT).map(line);
            const omitted = ready.length - shown.length;
            parts.push([
                `Orbit Workflows ready in ${path}:`,
                ...shown,
                ...(omitted > 0 ? [`- …and ${String(omitted)} more; call orbit_list_workflows for the rest.`] : []),
            ].join('\n'));
        }
        if (!parts.length)
            return '';
        return [
            ...parts,
            'Start one with orbit_start_run. Progress appears in the Orbit panel.',
        ].join('\n\n');
    }
}
