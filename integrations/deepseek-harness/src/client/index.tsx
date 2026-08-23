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

type Translate = (key: OrbitLocaleKey, values?: Record<string, string | number>) => string
type SubmitResult = { kind: 'success' } | { kind: 'error'; text: string }

const LIST_COMMAND = 'orbit-workflows'

/**
 * The `/` menu shows two commands; `/orbit-workflows ` turns it into a Workflow
 * picker.
 *
 * Two levels, because the first attempt had one: listing the Workflows beside
 * the commands buried the native ones under however many a Workspace happened
 * to have, and made picking from `/` mean "paste a name" rather than "do the
 * thing". Behind the command they are a list someone asked for, filtered by
 * what they type after it.
 *
 * Picking one writes a request into the draft rather than starting anything.
 * The Run has to be the Agent's — a Run begun here would be one it knows
 * nothing about, unable to report on it or take the next step from it — so the
 * menu's job ends at sparing a person the name.
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
      const typed = request.query
      if (typed.startsWith(`${LIST_COMMAND} `) || typed === LIST_COMMAND) {
        const search = typed.slice(LIST_COMMAND.length)
        const hits = matchWorkflows(search)
        if (!hits.length) return [{ name: t('noMatch'), value: 'none' }]
        return hits.map(item => ({
          name: item.name || item.workflow_id,
          description: `${item.workflow_id}@${String(item.latest_version)}`,
          // `value` is the source's own opaque payload: it is how onPick tells
          // a Workflow from a command without re-matching on the label.
          value: `run:${item.workflow_id}`,
        }))
      }
      return [
        { name: 'orbit', description: t('togglePanel') },
        { name: LIST_COMMAND, description: t('askWhatRuns'), hint: t('askWhatRunsHint') },
      ].filter(item => item.name.includes(typed.toLowerCase()))
    },
    onPick: (pick: { candidate: { name: string; value?: string; description?: string } }) => {
      const value = pick.candidate.value
      if (value === 'none') return 'handled'
      if (value?.startsWith('run:')) {
        return { text: t('runPrefix', { name: pick.candidate.name, id: value.slice(4) }) }
      }
      return { claim: claim() }
    },
    // Longest token first: `/orbit-workflows` starts with `/orbit`, and the
    // shorter claim would swallow it and then refuse its own name as an
    // argument. Typed in full it resolves to the same instruction the menu
    // writes, so the two ways of reaching it agree.
    // `/orbit-workflows` is a menu opener, not a command that settles: space
    // and enter leave it alone so the picker stays up while a name is typed.
    // `/orbit` still claims, and is checked first only because the longer name
    // starts with it.
    matchSpace: (_session: unknown, token: string) =>
      token === '/orbit' ? { claim: claim() } : undefined,
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
