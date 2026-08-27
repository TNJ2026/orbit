/** One Workflow as the panel's body: what it needs, what it does, what it did.
 *
 * The steps are listed, not drawn. A graph answers "how do these connect",
 * which needs room this panel does not have; a list answers "what happens and
 * who does it", which is the question a reader has before starting a goal. The
 * header still links out for the picture.
 */

import { useEffect, useState } from 'react'
import { panelError, type PanelError } from '@orbit-runtime/integration-core'
import type { OrbitRunRow as RunRowData, WorkflowNode, WorkflowSummary } from '@orbit-runtime/integration-core'
import styles from './OrbitPanel.module.css'
import { BackButton, OrbitRunListRow, PanelErrorText } from './OrbitRunRow.tsx'
import type { OrbitLocaleKey } from './locales.ts'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type HostCall = <T>(action: string, args: unknown[], signal: AbortSignal) => Promise<T>

/** The kinds that carry work, and so are the ones a missing prompt is news about. */
const PROMPTED = new Set(['action', 'human'])

function StepRow({ t, step }: { t: Translate; step: WorkflowNode }) {
  const prompt = step.prompt.trim()
  // Every step gets a stripe, and its kind picks the colour. A kind the
  // stylesheet has no rule for still gets one — `.defnRow`'s own border-left
  // is the neutral, so an unrecognised kind reads as "a step of some other
  // sort" rather than silently losing its leading edge.
  return (
    <div className={`${styles.defnRow} ${styles[`kind_${step.kind}`] ?? ''}`}>
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
  const ran = runs.filter(run => run.workflow.startsWith(`${workflow.workflow_id}@`))
  const [steps, setSteps] = useState<readonly WorkflowNode[] | null>(null)
  const [stepsError, setStepsError] = useState<PanelError | null>(null)

  // Read once per Workflow, and not again: a definition changes only when
  // somebody republishes it, so polling it would ask a settled question.
  useEffect(() => {
    const controller = new AbortController()
    setSteps(null); setStepsError(null)
    call<{ nodes: WorkflowNode[] }>(
      'getWorkflowDefinition', [sessionId, workflow.workflow_id], controller.signal,
    )
      .then(detail => { if (!controller.signal.aborted) setSteps(detail.nodes) })
      .catch(reason => { if (!controller.signal.aborted) setStepsError(panelError(reason)) })
    return () => controller.abort()
  }, [call, sessionId, workflow.workflow_id])

  return (
    <div>
      <BackButton t={t} onBack={onBack} />
      <div className={styles.detailHead}>
        <span className={styles.detailGoal}>{workflow.name || workflow.workflow_id}</span>
      </div>
      <div className={styles.detailMeta}>
        {workflow.workflow_id}@{String(workflow.latest_version)}
      </div>

      {workflow.description ? <p className={styles.prose}>{workflow.description}</p> : null}

      <a className={styles.outLink} href={`${uiUrl}#/workflows/${encodeURIComponent(workflow.workflow_id)}`} target="_blank" rel="noopener">
        {t('openThisInOrbit')}
      </a>

      <div className={styles.sectionLabel}>{t('factSteps', { total: steps?.length ?? 0 })}</div>
      <PanelErrorText t={t} error={stepsError} />
      {stepsError === null && steps === null ? <p className={styles.empty}>{t('stepsLoading')}</p> : null}
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
