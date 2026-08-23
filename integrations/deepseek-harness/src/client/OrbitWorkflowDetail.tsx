/** One Workflow as the panel's body: what it needs, what it does, what it did.
 *
 * The steps are listed, not drawn. A graph answers "how do these connect",
 * which needs room this panel does not have; a list answers "what happens and
 * who does it", which is the question a reader has before starting a goal. The
 * header still links out for the picture.
 */

import { useEffect, useState } from 'react'
import type { WorkflowNode, WorkflowSummary } from '../types.js'
import styles from './OrbitPanel.module.css'
import { OrbitRunListRow } from './OrbitRunRow.tsx'
import type { OrbitRunRow as RunRowData } from './orbit-model.ts'
import type { OrbitLocaleKey } from './locales.ts'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type HostCall = <T>(action: string, args: unknown[], signal: AbortSignal) => Promise<T>

/** The input ids a caller has to supply, in the order the Workflow declares them. */
function inputIds(workflow: WorkflowSummary): string[] {
  return Array.isArray(workflow.inputs)
    ? workflow.inputs
      .map(input => (input as { id?: unknown }).id)
      .filter((id): id is string => typeof id === 'string')
    : []
}

/** The kinds that get their own accent; anything else reads as structure. */
const ACCENTED = new Set(['action', 'human', 'decision'])
/** The kinds that carry work, and so are the ones a missing prompt is news about. */
const PROMPTED = new Set(['action', 'human'])

function StepRow({ t, step }: { t: Translate; step: WorkflowNode }) {
  const accent = ACCENTED.has(step.kind) ? step.kind : 'plain'
  const prompt = step.prompt.trim()
  return (
    <div className={`${styles.defnRow} ${styles[`kind_${accent}`] ?? ''}`}>
      <div className={styles.defnHead}>
        <span className={styles.defnName}>{step.label}</span>
        <span className={styles.defnKind}>{step.kind}</span>
        {/* The Agent is the half a reader is choosing between; the `agent.`
            prefix every handler carries is not. */}
        {step.handler
          ? <code className={styles.defnHandler}>{step.handler.replace(/^agent\./, '')}</code>
          : null}
      </div>
      {/* A terminal or a join has no prompt because it does no work — saying so
          on every one of them is noise. An action without one is worth a line. */}
      {prompt
        ? <p className={styles.defnPrompt}>{prompt}</p>
        : PROMPTED.has(step.kind)
          ? <p className={styles.defnNoPrompt}>{t('noPrompt')}</p>
          : null}
    </div>
  )
}

export interface OrbitWorkflowDetailProps {
  call: HostCall
  t: Translate
  sessionId: string
  workflow: WorkflowSummary
  /** Every Run the panel knows of; this one's are picked out here. */
  runs: readonly RunRowData[]
  uiUrl: string
  onBack: () => void
  onOpenRun: (runId: string) => void
}

export function OrbitWorkflowDetail(
  { call, t, sessionId, workflow, runs, uiUrl, onBack, onOpenRun }: OrbitWorkflowDetailProps,
) {
  const inputs = inputIds(workflow)
  const ran = runs.filter(run => run.workflow.startsWith(`${workflow.workflow_id}@`))
  const ready = workflow.goal_readiness === 'ready'
  const [steps, setSteps] = useState<readonly WorkflowNode[] | null>(null)
  const [stepsError, setStepsError] = useState('')

  // Read once per Workflow, and not again: a definition changes only when
  // somebody republishes it, so polling it would ask a settled question.
  useEffect(() => {
    const controller = new AbortController()
    setSteps(null); setStepsError('')
    call<{ nodes: WorkflowNode[] }>(
      'getWorkflowDefinition', [sessionId, workflow.workflow_id], controller.signal,
    )
      .then(detail => { if (!controller.signal.aborted) setSteps(detail.nodes) })
      .catch(reason => { if (!controller.signal.aborted) setStepsError(String(reason)) })
    return () => controller.abort()
  }, [call, sessionId, workflow.workflow_id])

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

      <div className={styles.sectionLabel}>{t('factSteps', { total: steps?.length ?? 0 })}</div>
      {stepsError ? <p className={styles.error}>{stepsError}</p> : null}
      {!stepsError && steps === null ? <p className={styles.empty}>{t('stepsLoading')}</p> : null}
      {steps?.map(step => <StepRow key={step.node_id} t={t} step={step} />)}

      <div className={styles.sectionLabel}>{t('factRuns', { total: ran.length })}</div>
      {ran.length
        ? ran.map(run => (
          <OrbitRunListRow key={run.runId} t={t} run={run} onOpen={() => onOpenRun(run.runId)} />
        ))
        : <p className={styles.empty}>{t('neverRun')}</p>}
    </div>
  )
}
