/**
 * Browser plugin: a resident Orbit panel in the shell overlay.
 *
 * The panel says what is running in this Workspace and links into Orbit for
 * anything deeper. It deliberately stops at that boundary — Orbit renders its
 * own graphs, Artifacts and Workflow authoring, and a second drawing of those
 * here would be a second answer to the same question.
 *
 * @module @orbit-runtime/dsh-orbit/client
 */

import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls the browser locale service into ClientContext.
import type {} from '@deepseek-ai/dsh-client-locale/client'
// Type-only: `shell.overlay` is declared by ui-layout; ctx.slots.inject owns
// the runtime wait for that declaration.
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import { OrbitPanel } from './OrbitPanel.tsx'
import { ORBIT_LOCALE_NAMESPACE, en, zh, type OrbitLocaleKey } from './locales.ts'
import { panelError } from '@orbit-runtime/integration-core'
import { caretToEnd } from './composer-caret.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** Orbit resident panel copy. */
    orbit: OrbitLocaleKey
  }
}

const PANEL_COMMAND = 'orbit'
const LIST_COMMAND = 'orbit-workflows'
const GENERATE_COMMAND = 'orbit-generate'

interface InputTriggerRegistry { registerSource(source: Record<string, unknown>): () => void }
type SubmitResult = { kind: 'success'; text?: string } | { kind: 'error'; text: string }
type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string

/** `/orbit` folds the resident panel; it never opens a second one. */
function registerOrbitSlashSource(ctx: ClientContext, t: Translate): void {
  const inputTriggers = ctx.get('inputTriggers') as unknown as InputTriggerRegistry | undefined
  if (!inputTriggers) throw new Error('Orbit /orbit requires the Harness inputTriggers service')
  const claim = () => ({
    token: `/${PANEL_COMMAND}`,
    submit: async (args: string): Promise<SubmitResult> => {
      if (args.trim()) return { kind: 'error', text: '/orbit takes no argument; it shows or hides the Orbit panel' }
      window.dispatchEvent(new Event('orbit:toggle-panel'))
      return { kind: 'success' }
    },
  })
  ctx.effect(() => inputTriggers.registerSource({
    trigger: '/', name: 'orbit', order: -10, showGroupTitle: false,
    candidates: async (_session: unknown, request: { query: string }) =>
      PANEL_COMMAND.includes(request.query.toLowerCase())
        ? [{ name: PANEL_COMMAND, description: t('togglePanel') }] : [],
    onPick: () => ({ claim: claim() }),
    matchSpace: (_session: unknown, token: string) =>
      token === `/${PANEL_COMMAND}` ? { claim: claim() } : undefined,
    matchEnter: async (_session: unknown, line: string) =>
      new RegExp(`^/${PANEL_COMMAND}(?:\\s|$)`, 'u').test(line.trim()) ? { claim: claim() } : undefined,
    // Required of any source that produces insert outcomes. A throw here
    // blocks the send rather than degrading to the clipboard text, so both
    // projections must answer for any id at all.
    //
    // Nothing mints an Orbit reference now that the Workflow goes into the
    // draft as text, so in practice neither of these is called. They stay
    // because the contract is about what a source must be able to answer, not
    // about what it happens to produce today — and answering from the id is
    // the honest answer once there is no table of names to consult.
    codec: {
      clipboardText: (ref: string) => `${MARK_OPEN}${ref}${MARK_CLOSE}`,
      serialize: async (ref: string) => ref,
    },
  }), 'orbit: slash command folding the panel')
}

interface SelectOption { readonly id: string; readonly label: string; readonly detail?: string }
interface SessionContext { readonly sessionId: string }
interface TriggerPick { readonly session: SessionContext }

/** `/orbit-generate` starts the existing authoring flow and reveals its row. */
function registerGenerateSlashSource(ctx: ClientContext, t: Translate): void {
  const inputTriggers = ctx.get('inputTriggers') as unknown as InputTriggerRegistry | undefined
  if (!inputTriggers) throw new Error('Orbit /orbit-generate requires the Harness inputTriggers service')
  const claim = (session: SessionContext) => ({
    // The claim token is also what a menu pick inserts into the composer.
    // Keep the argument separator in it so the person can type the Workflow
    // description immediately without first adding a space.
    token: `/${GENERATE_COMMAND} `,
    submit: async (args: string): Promise<SubmitResult> => {
      const prompt = args.trim()
      if (!prompt) return { kind: 'error', text: t('generateUsage') }
      // The Workflows tab, because that is where the job appears and where the
      // Workflow it publishes will land. Before the call: writing one takes a
      // while, and the panel is the only place that says it started.
      showOrbitPanel('workflows')
      try {
        await hostCall<unknown>(
          'generateWorkflowForSession', [session.sessionId, prompt], new AbortController().signal,
        )
        return { kind: 'success' }
      } catch (reason) {
        // The same reading the panel gives, because it is the same failure
        // arriving by the same route — only the place it is shown differs.
        const failure = panelError(reason)
        return { kind: 'error', text: t(failure.key, failure.values) }
      }
    },
  })
  ctx.effect(() => inputTriggers.registerSource({
    trigger: '/', name: GENERATE_COMMAND, order: -9, showGroupTitle: false,
    candidates: async (session: SessionContext, request: { query: string }) =>
      GENERATE_COMMAND.includes(request.query.toLowerCase())
        ? [{ name: GENERATE_COMMAND, description: t('generateCommandDescription') }] : [],
    // Menu picks wrap the Session in an InputTriggerPick; space and enter pass
    // the ClientSessionContext directly. Treating the wrapper itself as the
    // Session sent `undefined` to the Host and produced "requires a live
    // Harness Session" only when the command was chosen from the menu.
    onPick: (pick: TriggerPick) => ({ claim: claim(pick.session) }),
    matchSpace: (session: SessionContext, token: string) =>
      token === `/${GENERATE_COMMAND}` ? { claim: claim(session) } : undefined,
    matchEnter: async (session: SessionContext, line: string) =>
      new RegExp(`^/${GENERATE_COMMAND}(?:\\s|$)`, 'u').test(line.trim())
        ? { claim: claim(session) } : undefined,
  }), 'orbit: slash command generating a workflow')
}
interface SessionInput {
  setDraft(text: string): void
  readonly state: { getSnapshot(): { draft: string; draftRev: number } }
}
interface Conversation {
  readonly input: { for(actx: unknown): SessionInput }
  send(text: string): Promise<void>
}
interface Sessions { scope(id: string): ClientContext | undefined }

function conversationFor(
  ctx: ClientContext, sessionId: string,
): { conversation: Conversation; actx: ClientContext } | undefined {
  const sessions = ctx.get('sessions') as unknown as Sessions | undefined
  const actx = sessions?.scope(sessionId)
  if (actx === undefined) return undefined
  const conversation = actx.get('conversation') as unknown as Conversation | undefined
  return conversation ? { conversation, actx } : undefined
}

function writeDraft(ctx: ClientContext, sessionId: string, text: string): void {
  const scoped = conversationFor(ctx, sessionId)
  if (!scoped) return
  const input = scoped.conversation.input.for(scoped.actx)
  input.setDraft(text)
  caretToEnd(input.state.getSnapshot().draft)
}

/** Write the workflow invocation prefix into the active conversation draft.
 *
 * Workflow rows live in the resident panel, but the goal belongs in the
 * conversation composer. Keeping this bridge in the host-facing module means
 * the panel stays a presentational component and the draft is written through
 * the same session-scoped input API used by `/orbit-workflows`.
 */
function writeWorkflowDraft(
  ctx: ClientContext,
  t: Translate,
  workflow: { workflow_id: string; name?: string },
  sessionId: string,
): void {
  const label = workflow.name || workflow.workflow_id
  writeDraft(
    ctx,
    sessionId,
    `${t('runHead')}${MARK_OPEN}${label}${MARK_CLOSE}（${workflow.workflow_id}）${t('runTail')}`,
  )
}

/**
 * Bring the panel out, wherever it was put.
 *
 * Distinct from `orbit:toggle-panel`, which flips: a command that toggles is a
 * command that hides the panel for anyone who already had it open. This one
 * only ever shows, so running an Orbit command twice is not a way to lose
 * sight of what it did.
 *
 * The panel is where an Orbit command's result actually appears — a Run's
 * steps, a Workflow being written — so a command that starts work behind a
 * hidden panel has reported nothing. Called before the work rather than after
 * it, so a failure is met by an open panel too.
 */
function showOrbitPanel(tab?: 'workflows'): void {
  window.dispatchEvent(new CustomEvent('orbit:show-panel', {
    detail: tab === undefined ? {} : { tab },
  }))
}

/* The Workflow's name is written into the sentence rather than chipped, so
   something has to show where it starts and ends. Corner brackets, because the
   sentence around them is Chinese and a name may contain spaces or a comma. */
const MARK_OPEN = '「'
const MARK_CLOSE = '」'
interface CommandUi {
  register(contribution: {
    name: string
    description: string
    available(session: SessionContext): boolean
    ui: {
      kind: 'popupSelect'
      options(session: SessionContext, signal: AbortSignal): Promise<readonly SelectOption[]>
      onSelect(option: SelectOption, session: SessionContext): void | Promise<void>
    }
  }): () => void
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

/**
 * `/orbit-workflows` opens the shell's own popup — the one `/model` uses.
 *
 * Selecting writes the request into the draft for the person to finish. A
 * popupSelect is one list and one pick, with nowhere to put the goal these
 * Workflows declare an input for — and the Run has to be the Agent's, or it
 * cannot report on it afterwards.
 */
function registerWorkflowPopup(ctx: ClientContext, t: Translate): void {
  const commandUi = ctx.get('commandUi') as unknown as CommandUi | undefined
  if (!commandUi) return
  ctx.effect(() => commandUi.register({
    name: LIST_COMMAND,
    description: t('askWhatRuns'),
    available: () => true,
    ui: {
      kind: 'popupSelect',
      // Loaded once per open; the shell filters these as the reader types, so
      // the search is its own and matches on the label it is showing.
      options: async (session, signal) => {
        // No tab: the popup is already showing the list, and taking over the
        // panel's tab as well would move something the person did not ask to
        // have moved. What they need is for the panel to be on screen when the
        // Run they are about to describe starts reporting.
        showOrbitPanel()
        // `startIfMissing`, because typing the command is the asking. The
        // panel starts a Runtime when it is expanded and `/orbit-generate`
        // starts one to write into; this list was the one entry point that
        // required a Runtime to already be there, and answered a person who
        // asked what could run with an error about nothing running.
        //
        // Not `force`: the catalog refreshes itself when stale, and making
        // every open re-ask would charge each one for a freshness that only
        // matters after somebody publishes. A cold start is waited on here,
        // so the popup can take a few seconds the first time.
        const state = await hostCall<{ workflows: readonly {
          workflow_id: string; name: string; latest_version: number
        }[] }>('getPanelState', [session.sessionId, false, true], signal)
        const options = (state.workflows ?? []).map(item => {
          const label = item.name || item.workflow_id
          return {
            id: item.workflow_id,
            label,
            detail: `${item.workflow_id}@${String(item.latest_version)}`,
          }
        })
        return options
      },
      // Writes the request and stops. Starting here would be a Run the Agent
      // knows nothing about — unable to report on it or take the next step from
      // it — and a popupSelect has nowhere to put the goal anyway.
      onSelect: (option, session) => {
        const conversation = ctx.get('conversation') as unknown as Conversation | undefined
        const sessions = ctx.get('sessions') as unknown as Sessions | undefined
        const actx = sessions?.scope(session.sessionId)
        if (!conversation || actx === undefined) return
        const input = conversation.input.for(actx)
        const head = t('runHead')
        // Plain text, not a reference chip.
        //
        // The composer draws in two layers: the visible text is a `.backdrop`
        // element, and the `<textarea>` over it has transparent text and a
        // visible caret. They line up only while both lay out the same string,
        // and a chip is one `￼` cell in the backdrop no matter how long the
        // name is — which is why a chip squeezes the name to about six
        // characters, and why widening the chip moved the visible text 86px
        // right while the caret, laid out from the textarea's own single `￼`,
        // did not follow. Measured, both ways.
        //
        // Written as bracketed text there is no `￼`: both layers lay out the
        // same characters, the caret lands where the text ends, and the name
        // is shown whole at full size. What it costs is the chip's atomicity —
        // this can be edited character by character — and the codec's
        // `serialize`, which only fires for minted references. The Workflow's
        // identity survives because the model is already told the catalog:
        // `- workflow:wf_… — name (input: …)` sits in its system prompt.
        input.setDraft(`${head}${MARK_OPEN}${option.label}${MARK_CLOSE}${t('runTail')}`)
        caretToEnd(input.state.getSnapshot().draft)
      },
    },
  }), 'orbit: workflow popup')
}

export const inject = ['inputTriggers', 'slots', 'locale', 'commandUi', 'conversation', 'sessions']

export function apply(ctx: ClientContext): void {
  ctx.effect(
    () => ctx.locale.register(ORBIT_LOCALE_NAMESPACE, { zh, en }),
    'orbit: dictionaries',
  )
  // Bound once: the reference is stable per namespace and reads the active
  // locale at call time, so a menu built outside any component still speaks
  // the language the shell is in.
  const t = ctx.locale.bind(ORBIT_LOCALE_NAMESPACE)
  registerOrbitSlashSource(ctx, t)
  registerGenerateSlashSource(ctx, t)
  registerWorkflowPopup(ctx, t)
  const Panel = ({ t, useSessions }: PropsLocale<'orbit'> & {
    useSessions: <T>(selector: (state: { current?: string }) => T) => T
  }) => <OrbitPanel
    t={t}
    useSessions={useSessions}
    onSelectWorkflow={(workflow, sessionId) => writeWorkflowDraft(ctx, t, workflow, sessionId)}
    onEditWorkflow={(workflow, sessionId) => writeDraft(ctx, sessionId, t('editWorkflowPrompt', {
      name: workflow.name || workflow.workflow_id, id: workflow.workflow_id,
    }))}
    onDeleteWorkflow={async (workflow, sessionId) => {
      const scoped = conversationFor(ctx, sessionId)
      if (!scoped) return
      await scoped.conversation.send(t('deleteWorkflowPrompt', {
        name: workflow.name || workflow.workflow_id, id: workflow.workflow_id,
      }))
    }}
  />
  ctx.slots.inject('shell.overlay', () => ctx.slots.register({
    name: 'shell.overlay',
    id: 'orbit-runs',
    order: 80,
    label: 'Orbit runs',
    locale: ORBIT_LOCALE_NAMESPACE,
  }, Panel))
}
