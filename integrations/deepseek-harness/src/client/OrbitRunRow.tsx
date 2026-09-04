/** A Run as a row in a list, and the same Run as the panel's whole body.
 *
 * Two components rather than one disclosure, because a Run's detail does not
 * fit beside its siblings: opening one inline pushed the rest of the list out
 * of a 400px panel, which is the same as losing it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, IconChevronDownOutline14, StateDot } from '@deepseek-ai/dsh-client-ui-primitives'
import { panelError, type PanelError } from '@orbit-runtime/integration-core'
import type { OutputChunk, StepSummary } from '@orbit-runtime/integration-core'
import styles from './OrbitPanel.module.css'
import {
  approvalValue, artifactHref, artifactLabel, commandRevision, dotState, mergeChunks,
  outputText, resultOutcome, stepDotState, toStepRow, type OrbitRunRow as RunRowData,
} from '@orbit-runtime/integration-core'
import type { OrbitLocaleKey } from './locales.ts'

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type HostCall = <T>(action: string, args: unknown[], signal: AbortSignal) => Promise<T>

interface StepProps {
  call: HostCall; t: Translate; sessionId: string; runId: string
  step: ReturnType<typeof toStepRow>; live: boolean
  onSettled: (steps: StepSummary[]) => void
}

function StepDisclosure({ call, t, sessionId, runId, step, live, onSettled }: StepProps) {
  // Reconciliation is actionable content even when the external Agent left no
  // console output; hiding it would make the required decision unreachable.
  const expandable = step.hasOutput || step.needsPerson
  /* Open because it has something to say, not because anyone asked. What a
     step printed is the answer to "what is it doing", and a panel that makes
     the reader click each step to find out is a panel they read by clicking
     four times. The toggle stays, inverted: it is now how a noisy step is shut
     up rather than how a quiet one is opened.

     Held as an override rather than as the open state itself, because
     `hasOutput` arrives on a later poll than the step does — seeding
     `useState` from it would leave the step shut when its first line lands. */
  const [override, setOverride] = useState<boolean | undefined>(undefined)
  const open = override ?? expandable
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [chunks, setChunks] = useState<OutputChunk[]>([])
  const [error, setError] = useState<PanelError | null>(null)
  // A settled step's output is as settled as it is: read once. Only the step
  // actually working is followed, so an open ladder is one poll, not one per
  // step per poll.
  const working = live && step.status === 'running'
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
        if (working || page.has_more) timer = setTimeout(() => { void tick() }, 2_000)
      } catch (reason) {
        if (!controller.signal.aborted) setError(panelError(reason))
      }
    }
    void tick()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
  }, [open, working, sessionId, runId, step.nodeId, call])
  const text = outputText(chunks)
  const indicator = stepDotState(step.status)
  return (
    <div className={styles.stepDisclosure} data-open={open || undefined}>
      <button
        type="button"
        className={styles.stepRow}
        disabled={!expandable}
        aria-expanded={expandable ? open : undefined}
        onClick={() => { if (expandable) setOverride(!open) }}
      >
        <span className={`${styles.stepDot} ${styles[`stepDot_${indicator}`]}`} aria-hidden="true" />
        <span className={styles.stepTitle}>{step.label}</span>
        <span className={styles.status}>{step.status}</span>
        {expandable ? (
          <IconChevronDownOutline14
            className={`${styles.stepChevron} ${open ? styles.stepChevronOpen : ''}`}
          />
        ) : null}
      </button>
      {open ? <div className={styles.stepContent}>
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
                      .catch(reason => setError(panelError(reason)))
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
      <PanelErrorText t={t} error={error} />
      {text
        ? <FoldedText t={t} text={text} lines={OUTPUT_LINES} />
        : <p className={styles.empty}>{t('noOutput')}</p>}
      </div> : null}
    </div>
  )
}

/**
 * A failure, said in words, with its own text kept where it can be quoted.
 *
 * Every page shows failures the same way because they arrive the same way —
 * through the Host, from whatever layer actually broke. The reading is on the
 * page; the original is on the element, so a person can hover it, copy it into
 * a bug report, and correct the reading when it is wrong.
 */
export function PanelErrorText({ t, error }: { t: Translate; error: PanelError | null }) {
  if (error === null) return null
  return (
    <p className={styles.error} title={error.detail}>
      {t(error.key, error.values)}
    </p>
  )
}

/**
 * The way out of a detail page, on every page that has one.
 *
 * Shared rather than spelled twice: the two detail pages are reached the same
 * way and left the same way, and a back control that differs between them
 * reads as two different affordances for one idea. The arrow is its own
 * element so it can carry its own size — glued into the label it could only be
 * as big as the word, and the word is not what the eye lands on.
 */
export function BackButton({ t, onBack }: { t: Translate; onBack: () => void }) {
  return (
    <button type="button" className={styles.back} onClick={onBack}>
      <span className={styles.backArrow} aria-hidden="true">←</span>
      {/* The word carries its own element so it can carry its own underline:
          `text-decoration` on a flex container is not propagated to its items,
          and a bare text node here is an anonymous one. Underlining the label
          rather than the whole control also leaves the arrow alone, which is
          what an underlined arrow was never going to look right as. */}
      <span className={styles.backLabel}>{t('back')}</span>
    </button>
  )
}

/**
 * A Run's steps, top to bottom, each opening onto what it printed.
 *
 * One component for both places a Run is read. The Goal page draws it under a
 * running Run so the steps are simply there — that page answers "what is
 * happening", and an answer a reader has to click for is not on the page. The
 * detail page draws the same list under the Run's controls.
 */
export function OrbitStepList(
  { call, t, sessionId, runId, steps, live, onSettled }: {
    call: HostCall; t: Translate; sessionId: string; runId: string
    steps: readonly StepSummary[]; live: boolean
    onSettled: (steps: StepSummary[]) => void
  },
) {
  return (
    <>
      {steps.map(step => (
        <StepDisclosure
          key={step.node_id}
          call={call} t={t} sessionId={sessionId} runId={runId}
          step={toStepRow(step)} live={live} onSettled={onSettled}
        />
      ))}
    </>
  )
}

/**
 * A Run in a list: what it was for, and how it went.
 *
 * The History page and a Workflow's run list, both of which list Runs that are
 * over. It briefly grew a progress line for the Goal page; the Goal page draws
 * the whole Run now, and no caller here ever had steps to give it.
 */
export function OrbitRunListRow(
  { t, run, onOpen }: { t: Translate; run: RunRowData; onOpen: () => void },
) {
  return (
    <button type="button" className={styles.listRow} onClick={onOpen}>
      <span className={styles.listMain}>
        <span className={styles.listGoal}>{run.goal}</span>
        {/* What was asked for, not which definition answered it. Two Runs of
            one Workflow are told apart by their request; the id was the same
            string on both, and it is on the Run's own page for whoever wants
            it. Nothing at all when there was no request to show — a row is
            better short than padded with an empty line. */}
        {run.prompt ? <span className={styles.listPrompt}>{run.prompt}</span> : null}
      </span>
      <span className={styles.status}>{run.status}</span>
    </button>
  )
}

/**
 * A Run on the Goal page: what it is for, how far it is, and every step of it.
 *
 * The steps are not behind a click here. This page exists to answer what is
 * happening now, and a Run that is happening now has one thing worth reading —
 * which step it is on and what that step is saying. The list row this replaces
 * could only say that in a summary line, and the reader had to leave the page
 * to see the rest.
 *
 * The heading is a heading and not a link. There is nowhere left for it to
 * go: everything the detail page had — the steps, the output, the answer, the
 * buttons that cancel or resume — is on this card. A control that navigates to
 * a copy of what the reader is already looking at is a control that wastes the
 * one click they were willing to spend.
 */
export function OrbitRunGoalCard(
  { call, t, sessionId, run, steps, onSettled }: {
    call: HostCall; t: Translate; sessionId: string; run: RunRowData
    steps?: readonly StepSummary[]
    onSettled: (steps: StepSummary[]) => void
  },
) {
  return (
    <section className={styles.goalCard}>
      <div className={styles.goalHead}>
        <StateDot state={dotState(run.status)} size={9} className={styles.listDot} />
        <span className={styles.listMain}>
          <span className={styles.goalTitle}>{run.goal}</span>
          {/* The request under the label put on it. Neither the Workflow's id
              nor a second copy of the status: the id names a definition the
              reader did not choose by name, and the status is already on every
              step below. What is not anywhere else is what was asked for. */}
          <FoldedText t={t} text={run.prompt} lines={PROMPT_LINES} />
        </span>
      </div>
      <RunControls call={call} t={t} sessionId={sessionId} run={run} />
      {steps?.length ? (
        <div className={styles.goalSteps}>
          <OrbitStepList
            call={call} t={t} sessionId={sessionId} runId={run.runId}
            steps={steps} live={run.live} onSettled={onSettled}
          />
        </div>
      ) : null}
      <RunResult t={t} run={run} sessionId={sessionId} call={call} />
    </section>
  )
}

/** Past this many lines a request stops being a heading and becomes a wall. */
const PROMPT_LINES = 5
/** A step's console is read further than a heading is, and reached on purpose. */
const OUTPUT_LINES = 10

/**
 * Text from somewhere else, folded to a few lines with a way to see the rest.
 *
 * One widget for the two places the panel shows writing it did not produce —
 * the request a Run was given, and what a step printed. They are the same
 * thing to a reader, and they were the same thing here twice: a scroll box for
 * one and a fold for the other, so the same gesture meant different things a
 * few pixels apart.
 *
 * Folded by line box rather than by counting characters, so it folds where the
 * reader's panel actually wraps it — the same paste is two lines wide and six
 * lines narrow. The toggle is drawn only when there is something folded, which
 * cannot be known until the block has been laid out: a measurement taken on
 * first render compares 0 against 0 and hides a control the reader needs. A
 * resize observer answers on first layout and again whenever a narrowing panel
 * could have changed the answer.
 */
function FoldedText(
  { t, text, lines }: { t: Translate; text: string; lines: number },
) {
  const [open, setOpen] = useState(false)
  const [folded, setFolded] = useState(false)
  const clamp = useRef<HTMLPreElement | null>(null)
  useEffect(() => {
    const node = clamp.current
    if (node === null) return
    const measure = () => { setFolded(node.scrollHeight > node.clientHeight + 1) }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [text, open])
  if (!text) return null
  return (
    // The card is the wrapper, not the text: the control that unfolds a block
    // belongs to it, and one sitting outside read as a separate thing that
    // happened to be about the block above it.
    <div className={styles.goalPromptCard}>
      <pre
        ref={clamp}
        className={styles.goalPrompt}
        style={open ? undefined : { WebkitLineClamp: lines }}
        data-open={open || undefined}
      >
        {text}
      </pre>
      {folded || open ? (
        <button type="button" className={styles.goalPromptToggle} onClick={() => setOpen(value => !value)}>
          <IconChevronDownOutline14
            className={open ? styles.goalPromptChevronOpen : undefined}
          />
          {t(open ? 'promptCollapse' : 'promptExpand')}
        </button>
      ) : null}
    </div>
  )
}



/**
 * Cancel it, or answer what it is waiting for.
 *
 * Beside the Run wherever the Run is read. It used to be on the detail page
 * only, which was reachable from the Goal page; when that stopped being a
 * link, these were the thing that would have gone with it — a running Goal
 * with no way to stop it.
 *
 * Draws nothing when Orbit advertises neither command, which is most of the
 * time: a Run that is not interrupted cannot be resumed, and a finished one
 * cannot be cancelled.
 */
function RunControls(
  { call, t, sessionId, run }: {
    call: HostCall; t: Translate; sessionId: string; run: RunRowData
  },
) {
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<PanelError | null>(null)
  const cancelAt = commandRevision(run, 'langgraph_run.cancel')
  const resumeAt = commandRevision(run, 'langgraph_run.resume')
  const approval = run.interrupts.find(item => item.taskKind === 'approval')
  /* The revision comes from what this Run advertises, not from what the panel
     last drew, and the Host refuses the call if Orbit has moved on. A button
     that quietly acted on a newer Run than the one being read would be worse
     than a button that fails. */
  const act = (
    command: 'langgraph_run.cancel' | 'langgraph_run.resume', revision: number,
    value: unknown, interruptId?: string,
  ) => {
    setBusy(true); setError(null)
    void call<unknown>('runCommand', [sessionId, run.runId, command, revision, value, interruptId], new AbortController().signal)
      .then(() => setAnswer(''))
      .catch(reason => setError(panelError(reason)))
      .finally(() => setBusy(false))
  }
  if (cancelAt === undefined && resumeAt === undefined) return null
  return (
    <>
      {/* Centred, and under the request rather than beside the heading: it is
          the one thing to do to a Run that is still going, and a control on
          the left edge of a card reads as a footnote to the line above it. */}
      <div className={`${styles.actions} ${styles.runActions}`}>
        {cancelAt !== undefined ? (
          // The shell's own highlight, not a colour written here: `primary` is
          // backed by the `--dsw-alias-button-primary-*` family and follows the
          // theme, which a hand-mixed accent would not.
          <Button size="sm" variant="primary" disabled={busy} onClick={() => act('langgraph_run.cancel', cancelAt, undefined)}>
            {busy ? t('working') : t('cancel')}
          </Button>
        ) : null}
        {resumeAt === undefined ? null : approval !== undefined ? (
          /* A yes/no question is answered by choosing, not by describing the
             choice. What the next step reads — the port to reply on and the
             `decision` field the branches test — is in the question already,
             so a person was being asked to retype two facts the panel had.

             The box beside the buttons is the one thing the panel cannot
             supply: why. A workflow that sends rejected work back to its Agent
             carries this across the back edge as the next attempt's brief, and
             without it the Agent is asked to try again knowing only that the
             last try was refused. Optional, and it rides with either decision
             — silently dropping something a person typed would be worse than
             sending a note nobody reads. */
          <>
            <input
              value={answer}
              placeholder={t('reason')}
              onChange={event => setAnswer(event.currentTarget.value)}
            />
            {(['approve', 'reject'] as const).map(decision => (
              <Button
                key={decision}
                size="sm"
                variant={decision === 'approve' ? 'primary' : 'outline'}
                disabled={busy}
                onClick={() => act(
                  'langgraph_run.resume', resumeAt,
                  approvalValue(approval, decision, answer), approval.id,
                )}
              >
                {busy ? t('working') : t(decision === 'approve' ? 'approve' : 'reject')}
              </Button>
            ))}
          </>
        ) : (
          /* Anything else still takes a typed answer: this panel knows the
             shape of an approval and nothing about the rest. */
          <>
            <input value={answer} placeholder={t('answer')} onChange={event => setAnswer(event.currentTarget.value)} />
            <Button size="sm" variant="primary" disabled={busy} onClick={() => act('langgraph_run.resume', resumeAt, answer)}>
              {busy ? t('working') : t('resume')}
            </Button>
          </>
        )}
      </div>
      <PanelErrorText t={t} error={error} />
    </>
  )
}

/**
 * What the Run produced, under the steps that produced it.
 *
 * The reason somebody started a Goal is its answer, and until now the panel
 * was the one surface that never showed one — it could say a Run succeeded and
 * not what it succeeded at. Drawn only once there is something to draw: a
 * running Run has no answer yet, and an empty box promising one is worse than
 * no box.
 */
/**
 * One Artifact: a quick look, and a copy of it on the filesystem.
 *
 * Two different needs. The link opens the bytes in a tab, which answers "what
 * does it say" for anything a browser renders. The export answers "give me the
 * file" — because the path Orbit already has for it is a content-addressed
 * blob named by its own sha256, shared with every Artifact holding the same
 * bytes and collected when nothing references it. Editing that in place would
 * corrupt the store, so what a person gets is a copy that belongs to them.
 */
function ArtifactRow(
  { t, call, sessionId, artifactId }: {
    t: Translate; call: HostCall; sessionId: string; artifactId: string
  },
) {
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<PanelError | null>(null)
  /** Its text, when the Host judged its text to be the answer. */
  const [inline, setInline] = useState<string | null>(null)
  const href = artifactHref(sessionId, artifactId)
  useEffect(() => {
    const controller = new AbortController()
    call<{ text: string | null }>('readArtifactText', [sessionId, artifactId], controller.signal)
      .then(held => { if (!controller.signal.aborted) setInline(held.text) })
      // A preview that could not be read is not a failure worth a sentence:
      // the Artifact is still there, and its link and its export still work.
      .catch(() => {})
    return () => controller.abort()
  }, [call, sessionId, artifactId])
  return (
    <div className={styles.artifactRow}>
      {/* Read here when it is text and small: a workflow that wrote its reply
          as markdown wrote the reply, and a click to reach it is a click
          charged for the thing that was asked for. */}
      {inline === null ? null : <pre className={styles.artifactText}>{inline}</pre>}
      <div className={styles.artifacts}>
        {/* Without a Session there is nothing to ask on behalf of, and a link
            that goes nowhere is worse than the name on its own. Nor is there a
            reason to offer one for something already on the page. */}
        {inline !== null ? null : href ? (
          <a className={styles.artifact} href={href} target="_blank" rel="noopener" title={artifactId}>
            {t('artifactOpen', { name: artifactLabel(artifactId) })}
          </a>
        ) : (
          <span className={styles.artifact} title={artifactId}>{artifactLabel(artifactId)}</span>
        )}
        <button
          type="button" className={styles.artifact} disabled={busy}
          onClick={() => {
            setBusy(true); setFailed(null)
            void call<{ path: string }>('exportArtifact', [sessionId, artifactId], new AbortController().signal)
              .then(saved => setPath(saved.path))
              .catch(reason => setFailed(panelError(reason)))
              .finally(() => setBusy(false))
          }}
        >
          {busy ? t('working') : t('artifactExport')}
        </button>
      </div>
      {/* Selectable, because the point of a path is to be taken somewhere else. */}
      {path ? <code className={styles.artifactPath} title={path}>{path}</code> : null}
      <PanelErrorText t={t} error={failed} />
    </div>
  )
}

function RunResult(
  { t, run, sessionId, call }: {
    t: Translate; run: RunRowData; sessionId: string; call: HostCall
  },
) {
  // Nothing to say until it has stopped. A Run still going has no outcome, and
  // an empty block promising one is worse than no block.
  if (run.live) return null
  const failure = run.error ?? ''
  const { text: answer, artifacts } = failure
    ? { text: '', artifacts: [] as readonly string[] }
    : resultOutcome(run.result)
  return (
    <div className={styles.resultBlock}>
      <span className={styles.resultLabel}>{t(failure ? 'resultFailed' : 'result')}</span>
      {/* How it ended, first and in words. A reader asking "did it work" was
          being answered with whatever the terminal step happened to emit —
          which for a Run that wrote a file was a 64-character hash. */}
      <span className={`${styles.outcome} ${styles[`outcome_${dotState(run.status)}`]}`}>
        {t(`outcome_${run.status}` as OrbitLocaleKey, { status: run.status })}
      </span>
      {failure ? <pre className={`${styles.result} ${styles.resultError}`}>{failure}</pre> : null}
      {artifacts.map(id => (
        <ArtifactRow key={id} t={t} call={call} sessionId={sessionId} artifactId={id} />
      ))}
      {answer ? <pre className={styles.result}>{answer}</pre> : null}
    </div>
  )
}

export interface OrbitRunRowProps {
  call: HostCall; t: Translate; sessionId: string; run: RunRowData
  onBack: () => void
}

export function OrbitRunDetail({ call, t, sessionId, run, onBack }: OrbitRunRowProps) {
  const open = true
  const [steps, setSteps] = useState<StepSummary[] | null>(null)
  const [error, setError] = useState<PanelError | null>(null)
  const load = useCallback((signal: AbortSignal) => {
    call<{ steps: StepSummary[] }>('getRunDetail', [sessionId, run.runId], signal)
      .then(detail => { if (!signal.aborted) { setSteps(detail.steps); setError(null) } })
      .catch(reason => { if (!signal.aborted) setError(panelError(reason)) })
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
      <BackButton t={t} onBack={onBack} />
      <section className={styles.goalCard}>
        <div className={styles.goalHead}>
          <StateDot state={dotState(run.status)} size={9} className={styles.listDot} />
          <span className={styles.listMain}>
            <span className={styles.goalTitle}>{run.workflowName}</span>
            <FoldedText t={t} text={run.prompt} lines={PROMPT_LINES} />
          </span>
        </div>
        <RunControls call={call} t={t} sessionId={sessionId} run={run} />
        <PanelErrorText t={t} error={error} />
        {!error && steps === null ? <p className={styles.empty}>{t('loading')}</p> : null}
        {steps?.length ? (
          <div className={styles.goalSteps}>
            <OrbitStepList
              call={call} t={t} sessionId={sessionId} runId={run.runId}
              steps={steps} live={run.live} onSettled={setSteps}
            />
          </div>
        ) : null}
        <RunResult t={t} run={run} sessionId={sessionId} call={call} />
      </section>
    </div>
  )
}
