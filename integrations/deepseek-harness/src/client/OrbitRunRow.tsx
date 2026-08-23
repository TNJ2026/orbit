/** One Run in the panel, and what opening it shows. */

import { useCallback, useEffect, useState } from 'react'
import { DisclosureRow, StateDot } from '@deepseek-ai/dsh-client-ui-primitives'
import type { OutputChunk, StepSummary } from '../types.js'
import styles from './OrbitPanel.module.css'
import {
  dotState, mergeChunks, outputText, toStepRow, type OrbitRunRow as RunRowData,
} from './orbit-model.ts'
import type { OrbitLocaleKey } from './locales.ts'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type HostCall = <T>(action: string, args: unknown[], signal: AbortSignal) => Promise<T>

interface StepProps {
  call: HostCall; t: Translate; sessionId: string; runId: string
  step: ReturnType<typeof toStepRow>; live: boolean
}

function StepDisclosure({ call, t, sessionId, runId, step, live }: StepProps) {
  const [open, setOpen] = useState(false)
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
      {step.needsPerson ? <p className={styles.attention}>{t('needsPerson')}</p> : null}
      {error ? <p className={styles.error}>{error}</p> : null}
      {text ? <pre className={styles.output}>{text}</pre> : <p className={styles.empty}>{t('noOutput')}</p>}
    </DisclosureRow>
  )
}

export interface OrbitRunRowProps {
  call: HostCall; t: Translate; sessionId: string; run: RunRowData
}

export function OrbitRunRow({ call, t, sessionId, run }: OrbitRunRowProps) {
  const [open, setOpen] = useState(false)
  const [steps, setSteps] = useState<StepSummary[] | null>(null)
  const [error, setError] = useState('')
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
    <DisclosureRow
      icon={<StateDot state={dotState(run.status)} size={10} />}
      title={run.goal}
      open={open}
      expandable
      expandOnRowClick
      previewChevron
      onToggle={() => setOpen(value => !value)}
      collapsedContent={<span className={styles.status}>{run.status}</span>}
      rowClassName={styles.row}
    >
      <div className={styles.meta}>{run.workflow}</div>
      {error ? <p className={styles.error}>{error}</p> : null}
      {!error && steps === null ? <p className={styles.empty}>{t('loading')}</p> : null}
      {steps?.map(step => (
        <StepDisclosure
          key={step.node_id}
          call={call} t={t} sessionId={sessionId} runId={run.runId}
          step={toStepRow(step)} live={run.live}
        />
      ))}
    </DisclosureRow>
  )
}
