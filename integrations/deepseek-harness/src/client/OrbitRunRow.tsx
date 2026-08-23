/** A Run as a row in a list, and the same Run as the panel's whole body.
 *
 * Two components rather than one disclosure, because a Run's detail does not
 * fit beside its siblings: opening one inline pushed the rest of the list out
 * of a 400px panel, which is the same as losing it.
 */

import { useCallback, useEffect, useState } from 'react'
import { Button, DisclosureRow, StateDot } from '@deepseek-ai/dsh-client-ui-primitives'
import type { OutputChunk, StepSummary } from '../types.js'
import styles from './OrbitPanel.module.css'
import {
  commandRevision, dotState, mergeChunks, outputText, toStepRow,
  type OrbitRunRow as RunRowData,
} from './orbit-model.ts'
import type { OrbitLocaleKey } from './locales.ts'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type HostCall = <T>(action: string, args: unknown[], signal: AbortSignal) => Promise<T>

interface StepProps {
  call: HostCall; t: Translate; sessionId: string; runId: string
  step: ReturnType<typeof toStepRow>; live: boolean
  onSettled: (steps: StepSummary[]) => void
}

function StepDisclosure({ call, t, sessionId, runId, step, live, onSettled }: StepProps) {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [chunks, setChunks] = useState<OutputChunk[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let after = 0
    const tick = async () => {
      try {
        const page = await call<{ chunks: OutputChunk[]; after: number; has_more: boolean }>(
          'getStepOutput', [sessionId, runId, step.nodeId, after], controller.signal,
        )
        if (controller.signal.aborted) return
        after = page.after
        setChunks(current => mergeChunks(current, page.chunks))
        // Follow a step only while it could still say something more.
        if (live || page.has_more) timer = setTimeout(() => { void tick() }, 2_000)
      } catch (reason) {
        if (!controller.signal.aborted) setError(String(reason))
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [open, live, sessionId, runId, step.nodeId, call])
  const text = outputText(chunks)
  return (
    <DisclosureRow
      icon={<StateDot state={step.needsPerson ? 'warning' : dotState(step.status)} size={8} />}
      title={step.label}
      open={open}
      expandable
      expandOnRowClick
      onToggle={() => setOpen(value => !value)}
      collapsedContent={<span className={styles.status}>{step.status}</span>}
      rowClassName={styles.stepRow}
    >
      {step.needsPerson ? (
        <>
          <p className={styles.attention}>{t('needsPerson')}</p>
          {step.delegationId ? (
            <div className={styles.actions}>
              <input
                value={note}
                placeholder={t('note')}
                onChange={event => setNote(event.currentTarget.value)}
              />
              {(['confirmed_succeeded', 'confirmed_failed'] as const).map(outcome => (
                <Button
                  key={outcome}
                  size="sm"
                  variant={outcome === 'confirmed_succeeded' ? 'primary' : 'outline'}
                  disabled={busy}
                  onClick={() => {
                    setBusy(true)
                    void call<{ steps: StepSummary[] }>('reconcileStep', [
                      sessionId, runId, step.delegationId, outcome, note,
                    ], new AbortController().signal)
                      .then(detail => { setNote(''); onSettled(detail.steps) })
                      .catch(reason => setError(String(reason)))
                      .finally(() => setBusy(false))
                  }}
                >
                  {busy ? t('working') : t(outcome === 'confirmed_succeeded' ? 'confirmSucceeded' : 'confirmFailed')}
                </Button>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
      {error ? <p className={styles.error}>{error}</p> : null}
      {text ? <pre className={styles.output}>{text}</pre> : <p className={styles.empty}>{t('noOutput')}</p>}
    </DisclosureRow>
  )
}

/** A Run in a list: what it was for, and how it went. */
export function OrbitRunListRow(
  { t, run, onOpen }: { t: Translate; run: RunRowData; onOpen: () => void },
) {
  return (
    <button type="button" className={styles.listRow} onClick={onOpen}>
      <StateDot state={dotState(run.status)} size={9} className={styles.listDot} />
      <span className={styles.listMain}>
        <span className={styles.listGoal}>{run.goal}</span>
        <span className={styles.meta}>{run.workflow}</span>
      </span>
      <span className={styles.status}>{run.status}</span>
    </button>
  )
}

export interface OrbitRunRowProps {
  call: HostCall; t: Translate; sessionId: string; run: RunRowData
  onBack: () => void
}

export function OrbitRunDetail({ call, t, sessionId, run, onBack }: OrbitRunRowProps) {
  const open = true
  const [steps, setSteps] = useState<StepSummary[] | null>(null)
  const [error, setError] = useState('')
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const cancelAt = commandRevision(run, 'langgraph_run.cancel')
  const resumeAt = commandRevision(run, 'langgraph_run.resume')
  /* The revision comes from what this Run advertises, not from what the panel
     last drew, and the Host refuses the call if Orbit has moved on. A button
     that quietly acted on a newer Run than the one being read would be worse
     than a button that fails. */
  const act = (command: 'langgraph_run.cancel' | 'langgraph_run.resume', revision: number, value: unknown) => {
    setBusy(true); setError('')
    void call<unknown>('runCommand', [sessionId, run.runId, command, revision, value, undefined], new AbortController().signal)
      .then(() => setAnswer(''))
      .catch(reason => setError(String(reason)))
      .finally(() => setBusy(false))
  }
  const load = useCallback((signal: AbortSignal) => {
    call<{ steps: StepSummary[] }>('getRunDetail', [sessionId, run.runId], signal)
      .then(detail => { if (!signal.aborted) { setSteps(detail.steps); setError('') } })
      .catch(reason => { if (!signal.aborted) setError(String(reason)) })
  }, [call, sessionId, run.runId])
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    load(controller.signal)
    // A finished Run's steps are finished too; only a live one is worth re-reading.
    const timer = run.live ? setInterval(() => load(controller.signal), 3_000) : undefined
    return () => { controller.abort(); if (timer !== undefined) clearInterval(timer) }
  }, [open, run.live, load])
  return (
    <div>
      <button type="button" className={styles.back} onClick={onBack}>{t('back')}</button>
      <div className={styles.detailHead}>
        <StateDot state={dotState(run.status)} size={9} />
        <span className={styles.detailGoal}>{run.goal}</span>
      </div>
      <div className={styles.detailMeta}>{run.workflow} · {run.status}</div>
      <div className={styles.actions}>
        {cancelAt !== undefined ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => act('langgraph_run.cancel', cancelAt, undefined)}>
            {busy ? t('working') : t('cancel')}
          </Button>
        ) : null}
        {resumeAt !== undefined ? (
          <>
            <input value={answer} placeholder={t('answer')} onChange={event => setAnswer(event.currentTarget.value)} />
            <Button size="sm" variant="primary" disabled={busy} onClick={() => act('langgraph_run.resume', resumeAt, answer)}>
              {busy ? t('working') : t('resume')}
            </Button>
          </>
        ) : null}
      </div>
      {error ? <p className={styles.error}>{error}</p> : null}
      {!error && steps === null ? <p className={styles.empty}>{t('loading')}</p> : null}
      {steps?.map(step => (
        <StepDisclosure
          key={step.node_id}
          call={call} t={t} sessionId={sessionId} runId={run.runId}
          step={toStepRow(step)} live={run.live} onSettled={setSteps}
        />
      ))}
    </div>
  )
}
