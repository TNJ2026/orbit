/** The resident Orbit panel: what is running, and a way into Orbit itself. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { IconChevronDownOutline14, IconCloseOutline16, IconPanelLeftOutline16, IconRefreshOutline16, IconShareOutline16, StateDot } from '@deepseek-ai/dsh-client-ui-primitives'
import { authoringProgress, isProgressMarker, panelError, type PanelError } from '@orbit-runtime/integration-core'
import type { AgentSummary, AuthoringOutputChunk, AuthoringOutputPage, AuthoringSummary, RunDto, StepSummary, WorkflowSummary } from '@orbit-runtime/integration-core'
import styles from './OrbitPanel.module.css'
import {
  DEFAULT_PANEL_LAYOUT, PANEL_STORAGE_KEY, dragPanel, placePanel, readLayout,
  resizePanel, type PanelBounds, type PanelLayout,
} from './panel-geometry.ts'
import {
  ORBIT_IDLE_MS, ORBIT_POLL_MS, goalRuns, nextInterval, orderRows, stepDotState, summarise,
  toRow, type OrbitRunRow as RunRowData,
} from '@orbit-runtime/integration-core'
import type { OrbitLocaleKey } from './locales.ts'
import { OrbitRunDetail, OrbitRunGoalCard, OrbitRunListRow, PanelErrorText } from './OrbitRunRow.tsx'
import { OrbitWorkflowDetail } from './OrbitWorkflowDetail.tsx'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string

/** Orbit's ring-and-satellite mark, kept inline so the folded control is self-contained. */
function OrbitMark() {
  return (
    <svg className={styles.orbitMark} viewBox="0 0 64 64" aria-hidden="true">
      <circle className={styles.orbitBackground} cx="32" cy="32" r="32" />
      <circle className={styles.orbitRing} cx="32" cy="32" r="18" />
      <circle className={styles.orbitSatellite} cx="48" cy="22" r="6" />
    </svg>
  )
}

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

const SHAPE_LIMIT = 8
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
function AuthoringRow({ t, job, sessionId }: { t: Translate; job: AuthoringSummary; sessionId: string }) {
  const [open, setOpen] = useState(false)
  const [chunks, setChunks] = useState<AuthoringOutputChunk[]>([])
  /** The markers, kept apart: they are the ladder, not console text. */
  const [markers, setMarkers] = useState<AuthoringOutputChunk[]>([])
  const [outputError, setOutputError] = useState<PanelError | null>(null)
  const settled = job.status === 'done' || job.status === 'failed'
  const state = job.status === 'done' ? 'done' : job.status === 'failed' ? 'error' : 'ongoing'
  const progress = authoringProgress(markers, job.status)
  const label = job.status === 'done' ? t('authoringDone')
    : job.status === 'failed' ? t('authoringFailed')
      : job.status === 'queued' ? t('authoringQueued') : t('authoringRunning')
  useEffect(() => {
    const outputHref = job.output_href
    // Not gated on `open`. The progress markers live in this stream, so the
    // ladder below only moves while it is being read — a job whose console
    // nobody expanded used to show one unchanging line for its whole run.
    if (!outputHref) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let after = 0
    const tick = async () => {
      try {
        const page = await hostCall<AuthoringOutputPage>(
          'getAuthoringOutput', [sessionId, outputHref, after], controller.signal,
        )
        if (controller.signal.aborted) return
        // Markers are control data and never console text; they are kept out
        // of what is shown and read as the ladder instead.
        if (page.chunks.length) after = Math.max(...page.chunks.map(chunk => chunk.chunk_id))
        const merge = (into: AuthoringOutputChunk[], added: AuthoringOutputChunk[]) => {
          const byId = new Map(into.map(chunk => [chunk.chunk_id, chunk]))
          for (const chunk of added) byId.set(chunk.chunk_id, chunk)
          return [...byId.values()].sort((a, b) => a.chunk_id - b.chunk_id)
        }
        const [found, visible] = [
          page.chunks.filter(isProgressMarker),
          page.chunks.filter(chunk => !isProgressMarker(chunk)),
        ]
        setChunks(current => merge(current, visible))
        if (found.length) setMarkers(current => merge(current, found))
        setOutputError(null)
        if (!settled || page.has_more) timer = setTimeout(() => { void tick() }, 1_000)
      } catch (reason) {
        if (!controller.signal.aborted) setOutputError(panelError(reason))
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [settled, sessionId, job.output_href])
  return (
    <div className={styles.authoringRow} data-open={open || undefined}>
      <div className={styles.authoringSummary}>
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
      {job.output_href ? (
        <button
          type="button" className={styles.authoringOutputToggle}
          aria-expanded={open} title={t(open ? 'hideAgentOutput' : 'showAgentOutput')}
          onClick={() => setOpen(value => !value)}
        >
          <IconChevronDownOutline14 className={open ? styles.authoringChevronOpen : ''} />
        </button>
      ) : null}
      </div>
      {/* What it is doing, the way a Goal says what it is doing: the stages in
          order, each with the state it is in. Drawn from markers the Runtime
          has always written and nothing had ever read. */}
      <div className={styles.authoringStages}>
        {progress.stages.map(stage => (
          <div key={stage.stage} className={styles.stepDisclosure}>
            <div className={styles.stepRow}>
              <span className={`${styles.stepDot} ${styles[`stepDot_${stepDotState(stage.status)}`]}`} aria-hidden="true" />
              <span className={styles.stepTitle}>{t(`stage_${stage.stage}`)}</span>
              {/* Only when it has gone round again: on the first try the count
                  is noise on every row, and the retry is the whole news. */}
              {stage.status === 'running' && progress.attempt > 1 ? (
                <span className={styles.status}>
                  {t('stageAttempt', { attempt: progress.attempt, max: progress.maxAttempts })}
                </span>
              ) : <span className={styles.status}>{stage.status}</span>}
            </div>
          </div>
        ))}
      </div>
      {open ? (
        <div className={styles.authoringOutput}>
          <PanelErrorText t={t} error={outputError} />
          {chunks.length ? <pre>{chunks.map(chunk => chunk.text).join('')}</pre>
            : <p className={styles.empty}>{t('agentOutputWaiting')}</p>}
        </div>
      ) : null}
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
  /** Whether the close button has asked, and whether the answer is in flight. */
  const [confirmingStop, setConfirmingStop] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState<PanelError | null>(null)
  /** Steps of the Runs still moving, so a list line can say where each one is. */
  const [steps, setSteps] = useState<Record<string, StepSummary[]>>({})
  // The Runtime's own four: what is running, what could, what did, and who by.
  const [tab, setTab] = useState<'goal' | 'workflows' | 'history' | 'agents'>('goal')
  // One Run at a time, filling the panel. Selection is cleared by changing page
  // rather than surviving it: a detail left behind a tab is a place a reader
  // returns to without meaning to.
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedFlow, setSelectedFlow] = useState<string | null>(null)
  const [error, setError] = useState<PanelError | null>(null)
  const [connecting, setConnecting] = useState(false)
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
    const toggle = () => update(
      layout.dismissed
        // Toggling the fold of something that is not on screen would look like
        // the command did nothing. Put away, `/orbit` means "bring it back".
        ? { ...readLayoutSafely(layout), dismissed: false, collapsed: false }
        : { ...readLayoutSafely(layout), collapsed: !layout.collapsed },
    )
    window.addEventListener('orbit:toggle-panel', toggle)
    return () => window.removeEventListener('orbit:toggle-panel', toggle)
  }, [layout, update])

  useEffect(() => {
    const show = (event: Event) => {
      const detail = (event as CustomEvent<{ tab?: string }>).detail
      update({ ...readLayoutSafely(layout), dismissed: false, collapsed: false })
      if (detail?.tab === 'workflows') {
        setSelected(null); setSelectedFlow(null); setTab('workflows')
      }
    }
    window.addEventListener('orbit:show-panel', show)
    return () => window.removeEventListener('orbit:show-panel', show)
  }, [layout, update])

  // One poll loop, its cadence decided by the last answer: fast while a Run is
  // moving, slow while none is. A resident panel that asked at one rate would
  // either lag the work or bill an idle Workspace for nothing.
  //
  // Folded, it keeps asking at the idle rate rather than stopping, so opening
  // the icon restores a current panel rather than a stale snapshot.
  useEffect(() => {
    if (!sessionId || layout.dismissed) { setRows([]); return }
    const awaitingFirstConnection = !layout.collapsed && rows === null
    if (awaitingFirstConnection) { setConnecting(true); setError(null) }
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
          steps: Record<string, StepSummary[]>
        }>(
          // A folded resident badge may observe an existing Runtime, but only
          // an explicit open — `/orbit` or the badge — may start a new one.
          'getPanelState', [sessionId, forced, !layout.collapsed], controller.signal,
        )
        if (controller.signal.aborted) return
        const workflowNames = new Map(
          (state.workflows ?? []).map(item => [item.workflow_id, item.name || item.workflow_id]),
        )
        const next = orderRows(state.runs.map(run => toRow(run, workflowNames.get(run.workflow_id))))
        setRows(next); setUiUrl(state.uiUrl); setError(null)
        setWorkflows(state.workflows ?? []); setAgents(state.agents ?? [])
        // Held rather than merged into the rows: a Run whose steps this answer
        // could not read keeps the list it had, instead of the Goal page losing
        // its steps for one poll and getting them back on the next. A Run that
        // has finished is dropped, so this does not grow by one entry per Run
        // for the life of the panel — the Goal page draws only what moves.
        setSteps(current => {
          const held: Record<string, StepSummary[]> = {}
          for (const row of goalRuns(next)) {
            const kept = current[row.runId]
            if (kept) held[row.runId] = kept
          }
          return { ...held, ...state.steps }
        })
        setAuthoring(state.authoring ?? [])
        setAsking(false); setConnecting(false)
        const authoringLive = state.authoring.some(
          job => job.status === 'queued' || job.status === 'running',
        )
        timer = setTimeout(
          () => { void tick() },
          layout.collapsed ? ORBIT_IDLE_MS : authoringLive ? ORBIT_POLL_MS : nextInterval(next),
        )
      } catch (reason) {
        if (controller.signal.aborted) return
        setConnecting(false)
        setError(panelError(reason))
        setAsking(false)
        timer = setTimeout(() => { void tick() }, 15_000)
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [sessionId, layout.collapsed, layout.dismissed, asked])

  const counts = summarise(rows ?? [])
  // Split once: the Runtime's own pages read as "what is happening" and "what
  // happened", and a Run belongs to exactly one of them.
  const chosen = (rows ?? []).find(row => row.runId === selected)
  const chosenFlow = workflows.find(item => item.workflow_id === selectedFlow)
  // Not `filter(live)`: a Goal that has finished holds the page until the next
  // one starts, so the reader who watched it run also sees how it ended.
  const goal = goalRuns(rows ?? [])
  const settled = (rows ?? []).filter(row => !row.live)
  const box = placePanel(layout, bounds)

  // Put away means gone: no panel, and no badge offering to reopen a page
  // about a Runtime the same press stopped. `/orbit` is the way back.
  if (layout.dismissed) return null

  if (layout.collapsed) {
    return (
      <button
        type="button"
        className={styles.badge}
        onClick={() => update({ ...layout, collapsed: false })}
        aria-label={t('expand')}
      >
        <OrbitMark />
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
          <IconPanelLeftOutline16 size={14} />
        </button>
        {/* Last, and it asks first. Every control to its left is reversible —
            fold the panel, open a tab, poll again — and this one stops a
            service other Sessions, Orbit's own UI and any Run in flight are
            using. A press that cannot be undone does not belong in that row
            without a question between it and the effect. */}
        <button
          type="button"
          className={`${styles.iconButton} ${styles.stopButton}`}
          disabled={stopping}
          onClick={() => setConfirmingStop(true)}
          aria-label={t('stopRuntime')}
          title={t('stopRuntime')}
        >
          <IconCloseOutline16 size={14} />
        </button>
      </div>
      {confirmingStop ? (
        <div className={styles.confirmBar} role="alertdialog" aria-label={t('stopRuntime')}>
          <span className={styles.confirmText}>{t('stopRuntimeAsk')}</span>
          <div className={styles.confirmActions}>
            <button
              type="button" className={styles.confirmCancel}
              onClick={() => setConfirmingStop(false)}
            >
              {t('stopCancel')}
            </button>
            <button
              type="button" className={styles.confirmGo} disabled={stopping}
              onClick={() => {
                setStopping(true); setStopError(null)
                void hostCall<{ stopped: true }>('stopRuntime', [sessionId], new AbortController().signal)
                  .then(() => {
                    // It is a close button, so it closes. Stopping the Runtime
                    // and leaving its panel open would leave a page describing
                    // a service that is no longer there — and the next poll
                    // would fill it with the error of finding that out.
                    setConfirmingStop(false); setRows([]); setSteps({})
                    update({ ...readLayoutSafely(layout), dismissed: true })
                  })
                  .catch(reason => setStopError(panelError(reason)))
                  .finally(() => setStopping(false))
              }}
            >
              {stopping ? t('working') : t('stopConfirm')}
            </button>
          </div>
          <PanelErrorText t={t} error={stopError} />
        </div>
      ) : null}
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
        {connecting ? (
          <div className={styles.connecting} role="status">
            <span className={styles.connectSpinner} aria-hidden="true" />
            <span>{t('connectingRuntime')}</span>
          </div>
        ) : <PanelErrorText t={t} error={error} />}
        {!connecting && error === null && rows === null ? <p className={styles.empty}>{t('loading')}</p> : null}

        {!connecting && error === null && rows !== null && sessionId !== undefined && tab === 'goal' ? (
          goal.length ? goal.map(row => (
            <OrbitRunGoalCard
              key={row.runId} call={hostCall} t={t} sessionId={sessionId}
              run={row} steps={steps[row.runId]}
              // A person ruling on a step changes the step list this page is
              // drawn from, and the poll is up to two seconds away. Take the
              // answer straight from the call that produced it.
              onSettled={settled => setSteps(current => ({ ...current, [row.runId]: settled }))}
            />
          )) : <p className={styles.empty}>{t('emptyGoal')}</p>
        ) : null}

        {!connecting && error === null && rows !== null && tab === 'history' ? (
          settled.length ? settled.map(row => (
            <OrbitRunListRow key={row.runId} t={t} run={row} onOpen={() => setSelected(row.runId)} />
          )) : <p className={styles.empty}>{t('emptyHistory')}</p>
        ) : null}

        {!connecting && error === null && sessionId && tab === 'workflows'
          ? authoring.map(job => <AuthoringRow key={job.job_id} t={t} job={job} sessionId={sessionId} />)
          : null}
        {!connecting && error === null && tab === 'workflows' ? (
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

        {!connecting && error === null && tab === 'agents' ? (
          agents.length ? <div className={styles.agentsGrid}>{agents.map(item => {
            const mark = agentMark(item.name)
            const attempts = item.attempt_count ?? 0
            const failed = item.failed_count ?? 0
            return (
              <article className={styles.agentCard} key={item.name}>
                <div className={styles.agentHead}>
                  <span className={styles.avatar} style={mark.style} aria-hidden>{mark.initials}</span>
                  <span className={styles.agentIdentity}>
                    <span className={styles.agentName}>{item.name.replace(/^agent\./u, '')}</span>
                    <span className={styles.agentVersion}>{item.version}</span>
                  </span>
                </div>
                <div className={styles.agentStat}>
                  <span className={styles.agentStatLabel}>{t('agentRunsLabel')}</span>
                  <span className={styles.agentStatPill}>{t('agentRuns', { count: attempts })}</span>
                </div>
                {failed > 0 ? (
                  <div className={styles.agentStat}>
                    <span className={styles.agentStatLabel}>{t('agentFailedLabel')}</span>
                    <span className={`${styles.agentStatPill} ${styles.agentStatError}`}>
                      {t('agentFailed', { count: failed })}
                    </span>
                  </div>
                ) : null}
              </article>
            )
          })}</div> : <p className={styles.empty}>{t('emptyAgents')}</p>
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
