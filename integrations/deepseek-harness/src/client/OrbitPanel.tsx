/** The resident Orbit panel: what is running, and a way into Orbit itself. */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { RunDto } from '../types.js'
import styles from './OrbitPanel.module.css'
import {
  DEFAULT_PANEL_LAYOUT, PANEL_STORAGE_KEY, dragPanel, placePanel, readLayout,
  resizePanel, type PanelBounds, type PanelLayout,
} from './panel-geometry.ts'
import { nextInterval, orderRows, summarise, toRow, type OrbitRunRow } from './orbit-model.ts'
import type { OrbitLocaleKey } from './locales.ts'

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

function statusClass(row: OrbitRunRow): string {
  if (row.live) return styles.live
  if (row.status === 'completed') return styles.done
  if (row.status === 'unknown') return styles.unknown
  return styles.failed
}

export interface OrbitPanelProps { t: Translate; sessionId?: string }

export function OrbitPanel({ t, sessionId }: OrbitPanelProps) {
  const [layout, setLayout] = useState<PanelLayout>(() => {
    try { return readLayout(localStorage.getItem(PANEL_STORAGE_KEY)) } catch { return DEFAULT_PANEL_LAYOUT }
  })
  const [rows, setRows] = useState<OrbitRunRow[] | null>(null)
  const [uiUrl, setUiUrl] = useState('')
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
  useEffect(() => {
    if (layout.collapsed && rows !== null) return
    if (!sessionId) { setRows([]); return }
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const tick = async () => {
      try {
        const state = await hostCall<{ runs: RunDto[]; uiUrl: string }>(
          'getPanelState', [sessionId], controller.signal,
        )
        if (controller.signal.aborted) return
        const next = orderRows(state.runs.map(toRow))
        setRows(next); setUiUrl(state.uiUrl); setError('')
        timer = setTimeout(() => { void tick() }, nextInterval(next))
      } catch (reason) {
        if (controller.signal.aborted) return
        setError(String(reason))
        timer = setTimeout(() => { void tick() }, 15_000)
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [sessionId, layout.collapsed, rows === null])

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
        {uiUrl ? <a href={uiUrl} target="_blank" rel="noopener">{t('openRuntime')}</a> : null}
        <button type="button" onClick={() => update({ ...layout, collapsed: true })} aria-label={t('collapse')}>
          ×
        </button>
      </div>
      <div className={styles.body}>
        {error ? <p className={styles.error}>{error}</p> : null}
        {!error && rows === null ? <p className={styles.empty}>{t('loading')}</p> : null}
        {!error && rows?.length === 0 ? <p className={styles.empty}>{t('empty')}</p> : null}
        {rows?.map(row => (
          <article className={styles.row} key={row.runId}>
            <span className={`${styles.dot} ${statusClass(row)}`} />
            <div>
              <div className={styles.goal}>{row.goal}</div>
              <div className={styles.meta}>{row.workflow}</div>
            </div>
            <span className={styles.status}>{row.status}</span>
          </article>
        ))}
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
