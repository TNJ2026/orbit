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

import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls the browser locale service into ClientContext.
import type {} from '@deepseek-ai/dsh-client-locale/client'
// Type-only: `shell.overlay` is declared by ui-layout; ctx.slots.inject owns
// the runtime wait for that declaration.
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import { OrbitPanel } from './OrbitPanel.tsx'
import { ORBIT_LOCALE_NAMESPACE, en, zh, type OrbitLocaleKey } from './locales.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** Orbit resident panel copy. */
    orbit: OrbitLocaleKey
  }
}

const PANEL_COMMAND = 'orbit'
const LIST_COMMAND = 'orbit-workflows'

interface InputTriggerRegistry { registerSource(source: Record<string, unknown>): () => void }
type SubmitResult = { kind: 'success' } | { kind: 'error'; text: string }
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
  }), 'orbit: slash command folding the panel')
}

interface SelectOption { readonly id: string; readonly label: string; readonly detail?: string }
interface SessionContext { readonly sessionId: string }
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
 * Selecting opens the Workflow in Orbit rather than starting it. A popupSelect
 * is one list and one pick: it has nowhere to put the goal these Workflows
 * declare an input for, and starting without one is refused by the Runtime
 * (`missing workflow input 'prompt'`). Orbit's own page is where that sentence
 * gets written.
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
        const state = await hostCall<{ workflows: readonly {
          workflow_id: string; name: string; latest_version: number
        }[] }>('getPanelState', [session.sessionId], signal)
        return (state.workflows ?? []).map(item => ({
          id: item.workflow_id,
          label: item.name || item.workflow_id,
          detail: `${item.workflow_id}@${String(item.latest_version)}`,
        }))
      },
      onSelect: async (option, session) => {
        const base = await hostCall<string>('getRuntimeUi', [session.sessionId], new AbortController().signal)
        window.open(`${base}#/workflows/${encodeURIComponent(option.id)}`, '_blank', 'noopener')
      },
    },
  }), 'orbit: workflow popup')
}

export const inject = ['inputTriggers', 'slots', 'locale', 'commandUi']

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
  registerWorkflowPopup(ctx, t)
  const Panel = ({ t, useSessions }: PropsLocale<'orbit'> & {
    useSessions: <T>(selector: (state: { current?: string }) => T) => T
  }) => <OrbitPanel t={t} useSessions={useSessions} />
  ctx.slots.inject('shell.overlay', () => ctx.slots.register({
    name: 'shell.overlay',
    id: 'orbit-runs',
    order: 80,
    label: 'Orbit runs',
    locale: ORBIT_LOCALE_NAMESPACE,
  }, Panel))
}
