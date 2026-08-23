/**
 * `/orbit` opens Orbit's own Runtime UI inside the Harness window.
 *
 * The panel holds an iframe and nothing else. Every earlier version of this
 * module drew Orbit's data itself — a Run drawer, a Workflow picker, a
 * settings page — and each was a second account of something Orbit already
 * renders. Showing Orbit's own page instead means there is one interface, and
 * this module's whole job is deciding when it is visible.
 *
 * @module @orbit-runtime/dsh-orbit/client
 */

import { createElement, useEffect, useRef, useState } from 'react'
import type { ClientContext, SessionListState } from '@deepseek-ai/dsh-client-runtime/client'

/** How long a frame may stay blank before we assume the Host refused to embed it. */
const EMBED_TIMEOUT_MS = 6_000

async function orbitHostCall<T>(action: string, args: unknown[], signal: AbortSignal): Promise<T> {
  const response = await fetch('/plugins/dsh-orbit/api', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action, args }), signal,
  })
  const payload = await response.json() as { result?: T; error?: string }
  if (!response.ok || payload.error) throw new Error(payload.error || `Orbit Host API failed with HTTP ${String(response.status)}`)
  return payload.result as T
}

const openEvent = 'orbit:panel'
const openKey = (sessionId: string) => `orbit.panel.${sessionId}`
function readOpen(sessionId: string): boolean {
  try { return sessionStorage.getItem(openKey(sessionId)) === '1' } catch { return false }
}
function writeOpen(sessionId: string, open: boolean): void {
  try {
    if (open) sessionStorage.setItem(openKey(sessionId), '1')
    else sessionStorage.removeItem(openKey(sessionId))
  } catch { /* a Session that cannot remember still opens */ }
  window.dispatchEvent(new CustomEvent(openEvent, { detail: { sessionId } }))
}

const overlayStyle = {
  position: 'fixed' as const, inset: 0, zIndex: 1200, display: 'flex',
  flexDirection: 'column' as const, background: 'var(--background, #fff)',
}
const barStyle = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
  gap: 12, padding: '10px 16px', borderBottom: '1px solid #8884', flex: '0 0 auto',
}
const frameStyle = { flex: '1 1 auto', width: '100%', border: 0 }

interface PanelProps { sessionId: string; url: string; onClose: () => void }

function OrbitPanel({ sessionId, url, onClose }: PanelProps) {
  const [loaded, setLoaded] = useState(false)
  const [blocked, setBlocked] = useState(false)
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    closeRef.current?.focus()
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])
  useEffect(() => {
    if (loaded) return
    // A frame the Host's policy refuses never loads and never errors; it just
    // stays blank. Offering the address after a wait beats a white rectangle.
    const timer = setTimeout(() => setBlocked(true), EMBED_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [loaded, url])
  return createElement('section', {
    role: 'dialog', 'aria-modal': true, 'aria-label': 'Orbit Runtime', style: overlayStyle,
  },
    createElement('div', { style: barStyle },
      createElement('strong', null, 'Orbit Runtime'),
      createElement('span', { style: { flex: 1, opacity: 0.6, fontSize: 12 } }, url),
      blocked && !loaded
        ? createElement('a', { href: url, target: '_blank', rel: 'noopener' }, '在新标签页打开')
        : null,
      createElement('button', { ref: closeRef, type: 'button', onClick: onClose }, '关闭'),
    ),
    createElement('iframe', {
      key: sessionId, src: url, style: frameStyle, title: 'Orbit Runtime',
      onLoad: () => { setLoaded(true); setBlocked(false) },
    }),
  )
}

interface OverlayProps { sessionId: string; useSessions: <T>(selector: (state: SessionListState) => T) => T }

function OrbitOverlay({ sessionId }: OverlayProps) {
  const id = String(sessionId)
  const [open, setOpen] = useState(() => readOpen(id))
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    const refresh = (event: Event) => {
      const detail = (event as CustomEvent<{ sessionId?: string }>).detail
      if (!detail?.sessionId || detail.sessionId === id) setOpen(readOpen(id))
    }
    window.addEventListener(openEvent, refresh)
    window.addEventListener('storage', refresh)
    return () => {
      window.removeEventListener(openEvent, refresh)
      window.removeEventListener('storage', refresh)
    }
  }, [id])
  useEffect(() => {
    if (!open || url) return
    const controller = new AbortController()
    orbitHostCall<string>('getRuntimeUi', [id], controller.signal)
      .then(setUrl)
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [open, url, id])
  const close = () => { writeOpen(id, false); setError('') }
  if (!open) return null
  if (error) {
    return createElement('section', { role: 'alert', style: overlayStyle },
      createElement('div', { style: barStyle },
        createElement('strong', null, 'Orbit Runtime'),
        createElement('button', { type: 'button', onClick: close }, '关闭'),
      ),
      createElement('p', { style: { padding: 16 } }, error),
    )
  }
  if (!url) return null
  return createElement(OrbitPanel, { sessionId: id, url, onClose: close })
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
      // somewhere else. Saying so beats silently opening a panel.
      if (args.trim()) return { kind: 'error', text: '/orbit takes no argument; it opens the Orbit Runtime UI' }
      writeOpen(String(sessionId), true)
      return { kind: 'success' }
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

export const inject = ['inputTriggers', 'slots']
export function apply(ctx: ClientContext): void {
  registerOrbitSlashSource(ctx)
  ctx.slots.inject('conversation.input.overlay', () => ctx.slots.register(
    { name: 'conversation.input.overlay', id: 'orbit-runtime-panel', order: 100 },
    (props: OverlayProps) => createElement(OrbitOverlay, props),
  ))
}
