/**
 * The only thing this bundle puts in front of a person: a way to leave.
 *
 * `/orbit` takes no argument and renders nothing. Orbit's own Runtime UI is
 * where Runs are read and driven, so the command's whole job is to open it —
 * anything more would be a second interface competing with the first.
 *
 * @module @orbit-runtime/dsh-orbit/client
 */

import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'

async function orbitHostCall<T>(action: string, args: unknown[], signal: AbortSignal): Promise<T> {
  const response = await fetch('/plugins/dsh-orbit/api', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action, args }), signal,
  })
  const payload = await response.json() as { result?: T; error?: string }
  if (!response.ok || payload.error) throw new Error(payload.error || `Orbit Host API failed with HTTP ${String(response.status)}`)
  return payload.result as T
}

interface InputTriggerRegistry { registerSource(source: Record<string, unknown>): () => void }

type SubmitResult = { kind: 'success' } | { kind: 'error'; text: string }

export function registerOrbitSlashSource(ctx: ClientContext): void {
  const inputTriggers = ctx.get('inputTriggers') as unknown as InputTriggerRegistry | undefined
  if (!inputTriggers) throw new Error('Orbit /orbit requires the Harness inputTriggers service')
  const claim = (sessionId: string) => ({
    token: '/orbit',
    submit: async (args: string): Promise<SubmitResult> => {
      // The command carries nothing, so anything typed after it was meant for
      // somewhere else. Saying so beats silently opening a window.
      if (args.trim()) return { kind: 'error', text: '/orbit takes no argument; it opens the Orbit Runtime UI' }
      const controller = new AbortController()
      try {
        const url = await orbitHostCall<string>('getRuntimeUi', [sessionId], controller.signal)
        const opened = window.open(url, '_blank', 'noopener')
        if (!opened) return { kind: 'error', text: `Allow pop-ups for this site, or open ${url}` }
        return { kind: 'success' }
      } catch (reason) { return { kind: 'error', text: String(reason) } }
    },
  })
  const candidate = { name: 'orbit', description: 'open the Orbit Runtime UI for this Workspace' }
  ctx.effect(() => inputTriggers.registerSource({
    trigger: '/', name: 'orbit', order: -10, showGroupTitle: false,
    candidates: async (_session: unknown, request: { query: string }) =>
      'orbit'.includes(request.query.toLowerCase()) ? [candidate] : [],
    onPick: (pick: { session: { sessionId: string } }) => ({ claim: claim(String(pick.session.sessionId)) }),
    matchSpace: (session: { sessionId: string }, token: string) =>
      token === '/orbit' ? { claim: claim(String(session.sessionId)) } : undefined,
    matchEnter: async (session: { sessionId: string }, line: string) =>
      /^\/orbit(?:\s|$)/u.test(line.trim()) ? { claim: claim(String(session.sessionId)) } : undefined,
  }), 'orbit: slash command opening the Runtime UI')
}

export const inject = ['inputTriggers']
export function apply(ctx: ClientContext): void { registerOrbitSlashSource(ctx) }
