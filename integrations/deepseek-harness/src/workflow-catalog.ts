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

  /** What is remembered for one Workspace, ready-only, in the order shown. */
  list(canonicalPath: string): readonly WorkflowSummary[] {
    const entry = this.byWorkspace.get(canonicalPath)
    if (!entry) return []
    return entry.workflows.filter(item => item.goal_readiness === 'ready')
  }

  /** Whether a workspace's entry is missing or old enough to re-read. */
  stale(canonicalPath: string): boolean {
    const entry = this.byWorkspace.get(canonicalPath)
    return entry === undefined || this.now() - entry.at > CATALOG_TTL_MS
  }

  /**
   * The prompt contribution, or an empty string when there is nothing to say.
   *
   * Empty rather than a sentence explaining the emptiness: a contribution that
   * says "no Workflows are known" costs the same tokens every turn and tells
   * the model nothing it could not infer from the absence.
   */
  render(): string {
    const parts: string[] = []
    for (const [path, entry] of [...this.byWorkspace].sort()) {
      const ready = entry.workflows.filter(item => item.goal_readiness === 'ready')
      if (!ready.length) continue
      const shown = ready.slice(0, CATALOG_LIMIT).map(line)
      const omitted = ready.length - shown.length
      parts.push([
        `Orbit Workflows ready in ${path}:`,
        ...shown,
        ...(omitted > 0 ? [`- …and ${String(omitted)} more; call orbit_list_workflows for the rest.`] : []),
      ].join('\n'))
    }
    if (!parts.length) return ''
    return [
      ...parts,
      'Start one with orbit_start_run. Progress appears in the Orbit panel.',
    ].join('\n\n')
  }
}
