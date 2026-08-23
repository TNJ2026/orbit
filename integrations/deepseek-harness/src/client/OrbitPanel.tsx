/** The resident Orbit panel: what is running, and a way into Orbit itself. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { IconCloseOutline16, IconShareOutline16 } from '@deepseek-ai/dsh-client-ui-primitives'
import type { RunDto, WorkflowSummary } from '../types.js'
import styles from './OrbitPanel.module.css'
import {
  DEFAULT_PANEL_LAYOUT, PANEL_STORAGE_KEY, dragPanel, placePanel, readLayout,
  resizePanel, type PanelBounds, type PanelLayout,
} from './panel-geometry.ts'
import {
  ORBIT_IDLE_MS, nextInterval, orderRows, summarise, toRow,
  type OrbitRunRow as RunRowData,
} from './orbit-model.ts'
import type { OrbitLocaleKey } from './locales.ts'
import { OrbitRunRow } from './OrbitRunRow.tsx'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string

async function hostCall<T>(action: string, args: unknown[], signal: AbortSignal): Promise<T> {
  const response = await fetch('/plugins/dsh-orbit/api', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action, args }), signal,
  })
  const payload = await response.json() as { result?: T; error?: string }
  if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${String(response.status)}`)
  return payload.result as T
}

function storeLayout(layout: PanelLayout): void {
  try { localStorage.setItem(PANEL_STORAGE_KEY, JSON.stringify(layout)) } catch { /* a panel that cannot remember still works */ }
}

function useBounds(): PanelBounds {
  const [bounds, setBounds] = useState<PanelBounds>(() => ({
    width: typeof window === 'undefined' ? 1280 : window.innerWidth,
    height: typeof window === 'undefined' ? 800 : window.innerHeight,
  }))
  useEffect(() => {
    const measure = () => setBounds({ width: window.innerWidth, height: window.innerHeight })
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])
  return bounds
}

export interface OrbitPanelProps {
  t: Translate
  /** `shell.overlay` is root-scoped: it hands over the session *store*, never
   *  a session id. Reading `current` from it is the only way this panel knows
   *  which Workspace it is looking at — a `sessionId` prop would be undefined
   *  forever, and the panel would sit there permanently empty. */
  useSessions: <T>(selector: (state: { current?: string }) => T) => T
}

export function OrbitPanel({ t, useSessions }: OrbitPanelProps) {
  const sessionId = useSessions(state => state.current)
  const [layout, setLayout] = useState<PanelLayout>(() => {
    try { return readLayout(localStorage.getItem(PANEL_STORAGE_KEY)) } catch { return DEFAULT_PANEL_LAYOUT }
  })
  const [rows, setRows] = useState<RunRowData[] | null>(null)
  const [uiUrl, setUiUrl] = useState('')
  const [workflows, setWorkflows] = useState<readonly WorkflowSummary[]>([])
  const [error, setError] = useState('')
  const bounds = useBounds()
  const drag = useRef<{ x: number; y: number } | null>(null)

  const update = useCallback((next: PanelLayout) => {
    setLayout(next)
    storeLayout(next)
  }, [])

  useEffect(() => {
    const toggle = () => update({
      ...readLayoutSafely(layout), collapsed: !layout.collapsed,
    })
    window.addEventListener('orbit:toggle-panel', toggle)
    return () => window.removeEventListener('orbit:toggle-panel', toggle)
  }, [layout, update])

  // One poll loop, its cadence decided by the last answer: fast while a Run is
  // moving, slow while none is. A resident panel that asked at one rate would
  // either lag the work or bill an idle Workspace for nothing.
  //
  // Folded, it keeps asking at the idle rate rather than stopping. The badge's
  // whole job down there is to say whether anything is running, and a count
  // that stopped updating answers that question wrongly rather than not at all.
  useEffect(() => {
    if (!sessionId) { setRows([]); return }
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const tick = async () => {
      try {
        const state = await hostCall<{
          runs: RunDto[]; uiUrl: string; workflows: readonly WorkflowSummary[]
        }>(
          'getPanelState', [sessionId], controller.signal,
        )
        if (controller.signal.aborted) return
        const next = orderRows(state.runs.map(toRow))
        setRows(next); setUiUrl(state.uiUrl); setWorkflows(state.workflows ?? []); setError('')
        timer = setTimeout(
          () => { void tick() },
          layout.collapsed ? ORBIT_IDLE_MS : nextInterval(next),
        )
      } catch (reason) {
        if (controller.signal.aborted) return
        setError(String(reason))
        timer = setTimeout(() => { void tick() }, 15_000)
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [sessionId, layout.collapsed])

  const counts = summarise(rows ?? [])
  const box = placePanel(layout, bounds)

  if (layout.collapsed) {
    return (
      <button
        type="button"
        className={styles.badge}
        style={{ right: 18, bottom: 24 }}
        onClick={() => update({ ...layout, collapsed: false })}
        aria-label={t('expand')}
      >
        <span className={`${styles.dot} ${counts.live ? styles.live : styles.done}`} />
        {counts.live ? t('liveCount', counts) : t('idleCount', counts)}
      </button>
    )
  }

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    // The whole title area drags, including the text in it — requiring the bar's
    // own background meant the obvious place to grab was the one place that did
    // nothing. Only real controls are excluded, because capturing the pointer
    // from a press on one redirects every following event to the bar and the
    // control is pressed but never clicked.
    if ((event.target as Element).closest('button, a, input')) return
    drag.current = { x: event.clientX, y: event.clientY }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const from = drag.current
    if (!from) return
    drag.current = { x: event.clientX, y: event.clientY }
    setLayout(current => dragPanel(current, event.clientX - from.x, event.clientY - from.y, bounds))
  }
  const onPointerUp = () => { drag.current = null; storeLayout(layout) }

  return (
    <section
      className={styles.panel}
      style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
      role="region"
      aria-label={t('title')}
    >
      <div
        className={styles.bar}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      >
        <span className={styles.title}>{t('title')}</span>
        <span className={styles.count}>
          {counts.live ? t('liveCount', counts) : t('idleCount', counts)}
        </span>
        {uiUrl ? (
          /* An anchor, not a scripted open: the browser's own new-tab
             behaviour comes with it — modified clicks, the middle button, and
             no pop-up blocker deciding whether the press counted. */
          <a
            className={styles.iconButton}
            href={uiUrl}
            target="_blank"
            rel="noopener"
            aria-label={t('openRuntime')}
            title={t('openRuntime')}
          >
            <IconShareOutline16 size={14} />
          </a>
        ) : null}
        <button
          type="button"
          className={styles.iconButton}
          onClick={() => update({ ...layout, collapsed: true })}
          aria-label={t('collapse')}
          title={t('collapse')}
        >
          <IconCloseOutline16 size={14} />
        </button>
      </div>
      <div className={styles.body}>
        {error ? <p className={styles.error}>{error}</p> : null}
        {!error && rows === null ? <p className={styles.empty}>{t('loading')}</p> : null}
        {!error && rows?.length === 0 ? <p className={styles.empty}>{t('empty')}</p> : null}
        {workflows.length ? (
          /* What can be asked for, beside what is already happening. There is
             no start button on purpose: a Run the panel began itself would be
             one the Agent knows nothing about. Naming them is enough — the
             person types the name, the Agent does the rest. */
          <details className={styles.catalog}>
            <summary>{t('runnable', { total: workflows.length })} · {workflows.length}</summary>
            <p className={styles.meta}>{t('runnableHint')}</p>
            {workflows.map(item => (
              <div className={styles.catalogRow} key={item.workflow_id}>
                <span>{item.name || item.workflow_id}</span>
                <code className={styles.meta}>{item.workflow_id}@{String(item.latest_version)}</code>
              </div>
            ))}
          </details>
        ) : null}
        {sessionId ? rows?.map(row => (
          <OrbitRunRow key={row.runId} call={hostCall} t={t} sessionId={sessionId} run={row} />
        )) : null}
      </div>
      <div
        className={styles.resize}
        onPointerDown={onPointerDown}
        onPointerMove={event => {
          const from = drag.current
          if (!from) return
          drag.current = { x: event.clientX, y: event.clientY }
          setLayout(current => resizePanel(current, event.clientX - from.x, event.clientY - from.y, bounds))
        }}
        onPointerUp={onPointerUp}
      />
    </section>
  )
}

function readLayoutSafely(fallback: PanelLayout): PanelLayout {
  try { return readLayout(localStorage.getItem(PANEL_STORAGE_KEY)) } catch { return fallback }
}
