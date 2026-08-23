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
import { matchWorkflows } from './catalog-store.ts'
import { ORBIT_LOCALE_NAMESPACE, en, zh, type OrbitLocaleKey } from './locales.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** Orbit resident panel copy. */
    orbit: OrbitLocaleKey
  }
}

interface InputTriggerRegistry { registerSource(source: Record<string, unknown>): () => void }
type SubmitResult = { kind: 'success' } | { kind: 'error'; text: string }

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string

/**
 * `/orbit` folds the panel, and the same menu offers the Workflows that can be
 * run here.
 *
 * Picking a Workflow writes a sentence into the draft rather than starting
 * anything. That is the whole point: the Run has to be the Agent's, or it
 * cannot report on it afterwards — so the menu's job is to spare a person
 * remembering a name, not to become a second way to launch work.
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
      const own = 'orbit'.includes(query)
        ? [{ name: 'orbit', description: t('togglePanel') }]
        : []
      // `value` is the source's own opaque payload — it is how onPick tells a
      // Workflow apart from the command without re-matching on the label.
      const flows = matchWorkflows(request.query).map(item => ({
        name: item.name || item.workflow_id,
        description: `${item.workflow_id}@${String(item.latest_version)}`,
        hint: t('menuHint'),
        section: t('menuSection'),
        value: item.workflow_id,
      }))
      return [...own, ...flows]
    },
    onPick: (pick: { candidate: { name: string; value?: string } }) =>
      pick.candidate.value === undefined
        ? { claim: claim() }
        : { text: t('runPrefix', { name: pick.candidate.name }) },
    matchSpace: (_session: unknown, token: string) => token === '/orbit' ? { claim: claim() } : undefined,
    matchEnter: async (_session: unknown, line: string) =>
      /^\/orbit(?:\s|$)/u.test(line.trim()) ? { claim: claim() } : undefined,
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
