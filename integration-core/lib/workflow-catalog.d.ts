/** What can be run here, kept ready for the model to be told without asking.
 *
 * The Agent has `orbit_list_workflows`, but a tool it must call first cannot
 * help with the turn where someone says "clean this up" — by then the model has
 * already had to decide whether Orbit is even relevant. Naming the Workflows in
 * the prompt turns that decision into a reading.
 */
import type { WorkflowSummary } from './types.js';
/** Beyond this the list stops being context and starts being noise. */
export declare const CATALOG_LIMIT = 20;
/** How long a remembered catalog is still worth telling the model about. */
export declare const CATALOG_TTL_MS: number;
export declare class WorkflowCatalog {
    private readonly now;
    private readonly byWorkspace;
    constructor(now?: () => number);
    remember(canonicalPath: string, workflows: readonly WorkflowSummary[]): void;
    forget(canonicalPath: string): void;
    /**
     * Everything remembered for one Workspace, in the order it was read.
     *
     * All of it, not the runnable subset: this answers the panel, where a person
     * is reading a catalog and a Workflow that has gone unrunnable is something
     * they need to see — it is the one they have to go and fix. `render`, which
     * answers the model, keeps the filter: there a name is an offer to run.
     */
    list(canonicalPath: string): readonly WorkflowSummary[];
    /** Whether a workspace's entry is missing or old enough to re-read. */
    stale(canonicalPath: string): boolean;
    /**
     * The prompt contribution, or an empty string when there is nothing to say.
     *
     * Empty rather than a sentence explaining the emptiness: a contribution that
     * says "no Workflows are known" costs the same tokens every turn and tells
     * the model nothing it could not infer from the absence.
     */
    render(): string;
}
//# sourceMappingURL=workflow-catalog.d.ts.map