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

interface InputTriggerRegistry { registerSource(source: Record<string, unknown>): () => void }

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type SubmitResult = { kind: 'success' } | { kind: 'error'; text: string }

/**
 * Two commands: fold the panel, and ask for the Workflows.
 *
 * The second writes an instruction into the draft — call the tool, lay out the
 * answer — rather than listing anything itself. Going through the Agent costs a
 * turn and buys the only thing a command result cannot have: the listing is in
 * the conversation, so "run the second one" has something to count, and the
 * numbers come from the tool rather than from a catalog that may be minutes old.
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
  ctx.effect(() => inputTriggers.registerSource({
    trigger: '/', name: 'orbit', order: -10, showGroupTitle: false,
    candidates: async (_session: unknown, request: { query: string }) => {
      const query = request.query.toLowerCase()
      return [
        { name: 'orbit', description: t('togglePanel') },
        { name: 'orbit-workflows', description: t('askWhatRuns'), value: 'list' },
      ].filter(item => item.name.includes(query))
    },
    onPick: (pick: { candidate: { value?: string } }) =>
      pick.candidate.value === 'list' ? { text: t('listRequest') } : { claim: claim() },
    // Longest token first: `/orbit-workflows` starts with `/orbit`, and the
    // shorter claim would swallow it and then refuse its own name as an
    // argument. Typed in full it resolves to the same instruction the menu
    // writes, so the two ways of reaching it agree.
    matchSpace: (_session: unknown, token: string) =>
      token === '/orbit-workflows' ? { text: t('listRequest') }
        : token === '/orbit' ? { claim: claim() } : undefined,
    matchEnter: async (_session: unknown, line: string) => {
      const text = line.trim()
      if (/^\/orbit-workflows(?:\s|$)/u.test(text)) return { text: t('listRequest') }
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
