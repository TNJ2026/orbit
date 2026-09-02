/** What can be run here, kept ready for the model to be told without asking.
 *
 * The Agent has `orbit_list_workflows`, but a tool it must call first cannot
 * help with the turn where someone says "clean this up" — by then the model has
 * already had to decide whether Orbit is even relevant. Naming the Workflows in
 * the prompt turns that decision into a reading.
 */

import type { WorkflowSummary } from './types.js'

/** Beyond this the list stops being context and starts being noise. */
export const CATALOG_LIMIT = 20
/** How long a remembered catalog is still worth telling the model about. */
export const CATALOG_TTL_MS = 5 * 60_000

interface Remembered {
  readonly workflows: readonly WorkflowSummary[]
  readonly at: number
}

/** One line per Workflow: what to name it, and what it needs. */
function line(workflow: WorkflowSummary): string {
  const inputs = Array.isArray(workflow.inputs)
    ? workflow.inputs
      .map(input => (input as { id?: unknown }).id)
      .filter((id): id is string => typeof id === 'string')
    : []
  const needs = inputs.length ? ` (input: ${inputs.join(', ')})` : ''
  const name = workflow.name || workflow.workflow_id
  return `- ${workflow.workflow_id}@${String(workflow.latest_version)} — ${name}${needs}`
}

export class WorkflowCatalog {
  private readonly byWorkspace = new Map<string, Remembered>()
  constructor(private readonly now: () => number = Date.now) {}

  remember(canonicalPath: string, workflows: readonly WorkflowSummary[]): void {
    this.byWorkspace.set(canonicalPath, { workflows, at: this.now() })
  }

  forget(canonicalPath: string): void {
    this.byWorkspace.delete(canonicalPath)
  }

  /**
   * Everything remembered for one Workspace, in the order it was read.
   *
   * All of it, not the runnable subset: this answers the panel, where a person
   * is reading a catalog and a Workflow that has gone unrunnable is something
   * they need to see — it is the one they have to go and fix. `render`, which
   * answers the model, keeps the filter: there a name is an offer to run.
   */
  list(canonicalPath: string): readonly WorkflowSummary[] {
    return this.byWorkspace.get(canonicalPath)?.workflows ?? []
  }

  /**
   * The entry for a workspace while it is still worth speaking for.
   *
   * The TTL is stated here and nowhere else. `stale` and `render` are the two
   * questions asked about it — "should this be re-read" and "may this be put
   * in front of the model" — and they were each spelling the comparison out,
   * which is two places to edit and one poll doing the arithmetic twice.
   */
  private fresh(canonicalPath: string): Remembered | undefined {
    const entry = this.byWorkspace.get(canonicalPath)
    if (entry === undefined || this.now() - entry.at > CATALOG_TTL_MS) return undefined
    return entry
  }

  /** Whether a workspace's entry is missing or old enough to re-read. */
  stale(canonicalPath: string): boolean {
    return this.fresh(canonicalPath) === undefined
  }

  /**
   * The prompt contribution, or an empty string when there is nothing to say.
   *
   * Empty rather than a sentence explaining the emptiness: a contribution that
   * says "no Workflows are known" costs the same tokens every turn and tells
   * the model nothing it could not infer from the absence.
   */
  render(canonicalPath: string): string {
    // A model-facing name is an offer to execute it. Once its verification has
    // expired, hide it until refresh succeeds; retaining an old panel snapshot
    // is useful to a person, but offering it to a tool-calling model is not.
    const entry = this.fresh(canonicalPath)
    if (entry === undefined) return ''
    const ready = entry.workflows.filter(item => item.goal_readiness === 'ready')
    if (!ready.length) return ''
    const shown = ready.slice(0, CATALOG_LIMIT).map(line)
    const omitted = ready.length - shown.length
    return [
      `Orbit Workflows ready in ${canonicalPath}:`,
      ...shown,
      ...(omitted > 0 ? [`- …and ${String(omitted)} more; call orbit_list_workflows for the rest.`] : []),
      'Start one with orbit_start_run. Progress appears in the Orbit panel.',
    ].join('\n')
  }
}
