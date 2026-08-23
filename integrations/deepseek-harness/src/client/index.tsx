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
import type { WorkflowSummary } from '../types.js'
import { OrbitPanel } from './OrbitPanel.tsx'
import { renderRunnable } from './orbit-model.ts'
import { ORBIT_LOCALE_NAMESPACE, en, zh, type OrbitLocaleKey } from './locales.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** Orbit resident panel copy. */
    orbit: OrbitLocaleKey
  }
}

interface InputTriggerRegistry { registerSource(source: Record<string, unknown>): () => void }

async function hostCall<T>(action: string, args: unknown[], signal: AbortSignal): Promise<T> {
  const response = await fetch('/plugins/dsh-orbit/api', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action, args }), signal,
  })
  const payload = await response.json() as { result?: T; error?: string }
  if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${String(response.status)}`)
  return payload.result as T
}

/** The Session a command was typed in, however the host spells it. */
function currentSessionId(actx: { sessionId?: string }): string | undefined {
  return typeof actx.sessionId === 'string' ? actx.sessionId : undefined
}
type SubmitResult = { kind: 'success'; text?: string } | { kind: 'error'; text: string }

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string

/**
 * Two commands: fold the panel, and list what can be run.
 *
 * Both answer in place, which is what picking from `/` should do. The listing
 * is the Host's, not the Agent's — instant, and free of the model — with the
 * cost stated where it lands: the Agent does not see this output, so a
 * follow-up like "run the second one" needs a name rather than an ordinal.
 *
 * An earlier version listed the Workflows themselves as menu entries. The `/`
 * menu is for commands, where picking one makes something happen; filling it
 * with data made picking mean "paste a name", and buried the native commands
 * under however many Workflows a Workspace happened to have.
 */
function registerOrbitSlashSource(ctx: ClientContext, t: Translate): void {
  const inputTriggers = ctx.get('inputTriggers') as unknown as InputTriggerRegistry | undefined
  if (!inputTriggers) throw new Error('Orbit /orbit requires the Harness inputTriggers service')
  const claim = () => ({
    token: '/orbit',
    submit: async (args: string): Promise<SubmitResult> => {
      if (args.trim()) return { kind: 'error', text: '/orbit takes no argument; it shows or hides the Orbit panel' }
      window.dispatchEvent(new Event('orbit:toggle-panel'))
      return { kind: 'success' }
    },
  })
  const listClaim = (sessionId: string) => ({
    token: '/orbit-workflows',
    submit: async (args: string): Promise<SubmitResult> => {
      if (args.trim()) return { kind: 'error', text: '/orbit-workflows takes no argument' }
      try {
        const workflows = await hostCall<readonly WorkflowSummary[]>(
          'listRunnable', [sessionId], new AbortController().signal,
        )
        return { kind: 'success', text: renderRunnable(workflows, t('noRunnable')) }
      } catch (reason) { return { kind: 'error', text: String(reason) } }
    },
  })
  ctx.effect(() => inputTriggers.registerSource({
    trigger: '/', name: 'orbit', order: -10, showGroupTitle: false,
    candidates: async (_session: unknown, request: { query: string }) => {
      const query = request.query.toLowerCase()
      return [
        { name: 'orbit', description: t('togglePanel') },
        { name: 'orbit-workflows', description: t('askWhatRuns'), value: 'list' },
      ].filter(item => item.name.includes(query))
    },
    onPick: (pick: { candidate: { value?: string }; session: { sessionId: string } }) =>
      pick.candidate.value === 'list'
        ? { claim: listClaim(String(pick.session.sessionId)) }
        : { claim: claim() },
    // Longest token first: `/orbit-workflows` starts with `/orbit`, and the
    // shorter claim would swallow it and then refuse its own argument.
    matchSpace: (session: { sessionId: string }, token: string) =>
      token === '/orbit-workflows' ? { claim: listClaim(String(session.sessionId)) }
        : token === '/orbit' ? { claim: claim() } : undefined,
    matchEnter: async (session: { sessionId: string }, line: string) => {
      const text = line.trim()
      if (/^\/orbit-workflows(?:\s|$)/u.test(text)) {
        return { claim: listClaim(String(session.sessionId)) }
      }
      return /^\/orbit(?:\s|$)/u.test(text) ? { claim: claim() } : undefined
    },
  }), 'orbit: slash command folding the panel')
}

export const inject = ['inputTriggers', 'slots', 'locale']

export function apply(ctx: ClientContext): void {
  ctx.effect(
    () => ctx.locale.register(ORBIT_LOCALE_NAMESPACE, { zh, en }),
    'orbit: dictionaries',
  )
  // Bound once: the reference is stable per namespace and reads the active
  // locale at call time, so a menu built outside any component still speaks
  // the language the shell is in.
  registerOrbitSlashSource(ctx, ctx.locale.bind(ORBIT_LOCALE_NAMESPACE))
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
