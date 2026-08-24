/** The resident Orbit panel: what is running, and a way into Orbit itself. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { IconCloseOutline16, IconRefreshOutline16, IconShareOutline16, StateDot } from '@deepseek-ai/dsh-client-ui-primitives'
import type { AgentSummary, AuthoringSummary, RunDto, WorkflowSummary } from '../types.js'
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
import { OrbitRunDetail, OrbitRunListRow } from './OrbitRunRow.tsx'
import { OrbitWorkflowDetail } from './OrbitWorkflowDetail.tsx'

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

/** Two letters and a colour, derived so the same Agent always looks the same.
 *
 * Orbit gives each Agent a coloured mark; this reproduces the idea without
 * shipping a palette that would drift from it. The hue is the name's own, and
 * the colours stay inside the shell's theme by being expressed as one.
 */
function agentMark(name: string): { initials: string; style: React.CSSProperties } {
  const bare = name.replace(/^agent\./u, '')
  let hash = 0
  for (const ch of bare) hash = (hash * 31 + ch.codePointAt(0)!) % 360
  return {
    initials: bare.slice(0, 2),
    style: {
      background: `color-mix(in oklab, hsl(${String(hash)} 70% 55%) 22%, transparent)`,
      color: `hsl(${String(hash)} 70% 45%)`,
    },
  }
}

/** One page's heading, the line under it, and anything it counts. */
/** The letter a step wears in the chain, as Orbit's own card assigns it. */
function glyph(kind: string): string {
  if (kind === 'terminal') return '✓'
  if (kind === 'human') return 'H'
  if (kind === 'decision') return '?'
  return kind.slice(0, 1).toUpperCase()
}

const SHAPE_LIMIT = 5
/* Where each kind sits in the chain. The tally arrives in whatever order the
   nodes were authored in, and a chain drawn from that order can open with a
   terminal — a picture that reads left to right saying the workflow ends
   first. Ranking them puts the work up front and the ends at the end. */
const SHAPE_ORDER: Record<string, number> = {
  action: 0, human: 1, decision: 2, join: 3, terminal: 5,
}
const shapeRank = (kind: string): number => SHAPE_ORDER[kind] ?? 4

/**
 * A workflow's shape: its first steps as a connected chain, then how many more.
 *
 * Grouped by kind rather than laid out in graph order, which is what the
 * listing knows — a tally of kinds costs one small object, and the order would
 * cost the graph. It reads as "three actions, a decision, an end", which is
 * the size and character a reader is choosing by; the real order is one click
 * away on the detail page, where the steps are listed as they happen.
 */
function WorkflowShape({ kinds, total }: { kinds: Record<string, number>; total: number }) {
  const shown: string[] = []
  const ranked = Object.entries(kinds)
    .sort(([left], [right]) => shapeRank(left) - shapeRank(right))
  for (const [kind, count] of ranked) {
    for (let index = 0; index < count && shown.length < SHAPE_LIMIT; index += 1) {
      shown.push(kind)
    }
    if (shown.length === SHAPE_LIMIT) break
  }
  if (!shown.length) return null
  return (
    <div className={styles.shape} aria-hidden="true">
      {shown.map((kind, index) => (
        <span key={index} className={`${styles.shapeNode} ${styles[`node_${kind}`] ?? ''}`}>
          {glyph(kind)}
        </span>
      ))}
      {total > shown.length
        ? <span className={`${styles.shapeNode} ${styles.node_more}`}>+{total - shown.length}</span>
        : null}
    </div>
  )
}

/**
 * One authoring job, on the page where its result will land.
 *
 * Not the Goal page: authoring is not a Run, and a job among the Runs would
 * make "what is this Workspace working on" answer with two different kinds of
 * thing. It sits above the catalog it is about to change.
 */
function AuthoringRow({ t, job }: { t: Translate; job: AuthoringSummary }) {
  const settled = job.status === 'done' || job.status === 'failed'
  const state = job.status === 'done' ? 'done' : job.status === 'failed' ? 'error' : 'ongoing'
  const label = job.status === 'done' ? t('authoringDone')
    : job.status === 'failed' ? t('authoringFailed')
      : job.status === 'queued' ? t('authoringQueued') : t('authoringRunning')
  return (
    <div className={styles.authoringRow}>
      <StateDot state={state} size={8} />
      <div className={styles.authoringMain}>
        <div className={styles.authoringLabel}>
          {label}
          {job.requested_agent
            ? <span className={styles.meta}> · {t('authoringBy', { agent: job.requested_agent })}</span>
            : null}
        </div>
        {/* The prompt is how a reader tells two jobs apart, and after it lands
            it is the only record here of what was asked for. */}
        <div className={styles.authoringPrompt}>
          {settled && job.status === 'failed' && job.error ? job.error : job.prompt}
        </div>
      </div>
    </div>
  )
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
  const [agents, setAgents] = useState<readonly AgentSummary[]>([])
  const [authoring, setAuthoring] = useState<readonly AuthoringSummary[]>([])
  // The Runtime's own four: what is running, what could, what did, and who by.
  const [tab, setTab] = useState<'goal' | 'workflows' | 'history' | 'agents'>('goal')
  // One Run at a time, filling the panel. Selection is cleared by changing page
  // rather than surviving it: a detail left behind a tab is a place a reader
  // returns to without meaning to.
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedFlow, setSelectedFlow] = useState<string | null>(null)
  const [error, setError] = useState('')
  // Asking again, now. Every page is drawn from one poll, so a refresh is that
  // poll brought forward rather than four per-tab reloads — and bumping this
  // aborts the sleep the loop is sitting in, which a call into it could not.
  const [asked, setAsked] = useState(0)
  const [asking, setAsking] = useState(false)
  /* Only the tick the press starts is forced. The timer ticks that follow it
     are ordinary polls, and forcing those would re-read the whole catalog
     every two seconds for the rest of the session. */
  const forceNext = useRef(false)
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
        const forced = forceNext.current
        forceNext.current = false
        const state = await hostCall<{
          runs: RunDto[]; uiUrl: string
          workflows: readonly WorkflowSummary[]; agents: readonly AgentSummary[]
          authoring: readonly AuthoringSummary[]
        }>(
          'getPanelState', [sessionId, forced], controller.signal,
        )
        if (controller.signal.aborted) return
        const next = orderRows(state.runs.map(toRow))
        setRows(next); setUiUrl(state.uiUrl); setError('')
        setWorkflows(state.workflows ?? []); setAgents(state.agents ?? [])
        setAuthoring(state.authoring ?? [])
        setAsking(false)
        timer = setTimeout(
          () => { void tick() },
          layout.collapsed ? ORBIT_IDLE_MS : nextInterval(next),
        )
      } catch (reason) {
        if (controller.signal.aborted) return
        setError(String(reason))
        setAsking(false)
        timer = setTimeout(() => { void tick() }, 15_000)
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [sessionId, layout.collapsed, asked])

  const counts = summarise(rows ?? [])
  // Split once: the Runtime's own pages read as "what is happening" and "what
  // happened", and a Run belongs to exactly one of them.
  const chosen = (rows ?? []).find(row => row.runId === selected)
  const chosenFlow = workflows.find(item => item.workflow_id === selectedFlow)
  const live = (rows ?? []).filter(row => row.live)
  const settled = (rows ?? []).filter(row => !row.live)
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
        <button
          type="button"
          className={styles.iconButton}
          disabled={asking}
          onClick={() => { forceNext.current = true; setAsking(true); setAsked(count => count + 1) }}
          aria-label={t('refresh')}
          title={t('refresh')}
        >
          <IconRefreshOutline16 size={14} />
        </button>
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
      <nav className={styles.tabs} aria-label={t('title')}>
        {([
          ['goal', 'tabGoal'], ['workflows', 'tabWorkflows'],
          ['history', 'tabHistory'], ['agents', 'tabAgents'],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={key === tab ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            aria-pressed={key === tab}
            onClick={() => { setSelected(null); setSelectedFlow(null); setTab(key) }}
          >
            {t(label)}
          </button>
        ))}
      </nav>
      <div className={styles.body}>
        {chosen !== undefined && sessionId !== undefined ? (
          <OrbitRunDetail
            call={hostCall} t={t} sessionId={sessionId} run={chosen}
            onBack={() => setSelected(null)}
          />
        ) : chosenFlow !== undefined && sessionId !== undefined ? (
          <OrbitWorkflowDetail
            call={hostCall} t={t} sessionId={sessionId}
            workflow={chosenFlow} runs={rows ?? []} uiUrl={uiUrl}
            onBack={() => setSelectedFlow(null)}
            onOpenRun={runId => setSelected(runId)}
          />
        ) : <>
        {error ? <p className={styles.error}>{error}</p> : null}
        {!error && rows === null ? <p className={styles.empty}>{t('loading')}</p> : null}

        {!error && rows !== null && tab === 'goal' ? (
          live.length ? live.map(row => (
            <OrbitRunListRow key={row.runId} t={t} run={row} onOpen={() => setSelected(row.runId)} />
          )) : <p className={styles.empty}>{t('emptyGoal')}</p>
        ) : null}

        {!error && rows !== null && tab === 'history' ? (
          settled.length ? settled.map(row => (
            <OrbitRunListRow key={row.runId} t={t} run={row} onOpen={() => setSelected(row.runId)} />
          )) : <p className={styles.empty}>{t('emptyHistory')}</p>
        ) : null}

        {!error && tab === 'workflows'
          ? authoring.map(job => <AuthoringRow key={job.job_id} t={t} job={job} />)
          : null}
        {!error && tab === 'workflows' ? (
          workflows.length ? workflows.map(item => (
            <button
              type="button"
              className={`${styles.flowRow} ${styles.flowButton}`}
              key={item.workflow_id}
              onClick={() => setSelectedFlow(item.workflow_id)}
            >
              {/* Name and shape, as Orbit's own card reads: the id is how a
                  machine addresses this, and it is a line above the only thing
                  a person is choosing by. Both are on the detail page. */}
              <div className={styles.flowName}>
                {item.name || item.workflow_id}
                {/* The list carries the whole catalog now, so it has to say
                    which of these a goal cannot actually be started from —
                    otherwise the ones needing work read as ready. */}
                {item.goal_readiness === 'ready' ? null : (
                  <span className={styles.flowBlocked}>
                    {t(item.goal_readiness === 'needs_migration'
                      ? 'needsMigration' : 'needsUpgrade')}
                  </span>
                )}
              </div>
              <WorkflowShape
                kinds={item.node_kinds ?? {}} total={item.node_count ?? 0}
              />
            </button>
          )) : <p className={styles.empty}>{t('emptyWorkflows')}</p>
        ) : null}

        {!error && tab === 'agents' ? (
          agents.length ? agents.map(item => {
            const mark = agentMark(item.name)
            return (
              <div className={styles.agentRow} key={item.name}>
                <span className={styles.avatar} style={mark.style} aria-hidden>{mark.initials}</span>
                <span>
                  <div className={styles.agentName}>{item.name.replace(/^agent\./u, '')}</div>
                  <div className={styles.agentVersion}>{item.version}</div>
                </span>
              </div>
            )
          }) : <p className={styles.empty}>{t('emptyAgents')}</p>
        ) : null}
        </>}
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
