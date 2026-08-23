/** One Workflow as the panel's body: what it needs, and what it has done.
 *
 * Deliberately not its graph or its definition. Those are drawn by Orbit, in a
 * frame built for them, and a smaller second drawing here would be a worse
 * answer to a question already answered — so the header links out instead.
 */

import type { WorkflowSummary } from '../types.js'
import styles from './OrbitPanel.module.css'
import { OrbitRunListRow } from './OrbitRunRow.tsx'
import type { OrbitRunRow as RunRowData } from './orbit-model.ts'
import type { OrbitLocaleKey } from './locales.ts'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string

/** The input ids a caller has to supply, in the order the Workflow declares them. */
function inputIds(workflow: WorkflowSummary): string[] {
  return Array.isArray(workflow.inputs)
    ? workflow.inputs
      .map(input => (input as { id?: unknown }).id)
      .filter((id): id is string => typeof id === 'string')
    : []
}

export interface OrbitWorkflowDetailProps {
  t: Translate
  workflow: WorkflowSummary
  /** Every Run the panel knows of; this one's are picked out here. */
  runs: readonly RunRowData[]
  uiUrl: string
  onBack: () => void
  onOpenRun: (runId: string) => void
}

export function OrbitWorkflowDetail(
  { t, workflow, runs, uiUrl, onBack, onOpenRun }: OrbitWorkflowDetailProps,
) {
  const inputs = inputIds(workflow)
  const ran = runs.filter(run => run.workflow.startsWith(`${workflow.workflow_id}@`))
  const ready = workflow.goal_readiness === 'ready'
  return (
    <div>
      <button type="button" className={styles.back} onClick={onBack}>{t('back')}</button>
      <div className={styles.detailHead}>
        <span className={styles.detailGoal}>{workflow.name || workflow.workflow_id}</span>
      </div>
      <div className={styles.detailMeta}>
        {workflow.workflow_id}@{String(workflow.latest_version)}
      </div>

      {workflow.description ? <p className={styles.prose}>{workflow.description}</p> : null}

      <dl className={styles.facts}>
        <dt>{t('factReadiness')}</dt>
        <dd>
          {ready ? t('readyYes') : t('readyNo')}
          {/* The reason is the actionable half: "not ready" alone sends a person
              to Orbit to find out what this line already knows. */}
          {!ready && workflow.readiness_reason
            ? <span className={styles.meta}> · {workflow.readiness_reason}</span> : null}
        </dd>
        <dt>{t('factInputs')}</dt>
        <dd>{inputs.length ? inputs.join(', ') : t('factNone')}</dd>
      </dl>

      <a className={styles.outLink} href={`${uiUrl}#/workflows/${encodeURIComponent(workflow.workflow_id)}`} target="_blank" rel="noopener">
        {t('openThisInOrbit')}
      </a>

      <div className={styles.sectionLabel}>{t('factRuns', { total: ran.length })}</div>
      {ran.length
        ? ran.map(run => (
          <OrbitRunListRow key={run.runId} t={t} run={run} onOpen={() => onOpenRun(run.runId)} />
        ))
        : <p className={styles.empty}>{t('neverRun')}</p>}
    </div>
  )
}
