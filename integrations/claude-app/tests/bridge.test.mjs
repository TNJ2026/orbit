import assert from 'node:assert/strict'
import test from 'node:test'

import { OrbitBridge } from '../lib/bridge.js'
import { MESSAGES } from '../lib/messages.js'
import { INTERNAL_ERROR } from '../lib/stdio.js'

const workspace = { id: 'w', canonicalPath: '/tmp/project' }
const ok = (body) => ({
  ok: true, status: 200, json: async () => body,
})

/** A gateway stub: the bridge's whole job is what it does around one. */
const gatewayThat = (endpoint) => ({
  calls: 0,
  async endpoint(ws, startIfMissing) {
    this.calls += 1
    this.lastStart = startIfMissing
    this.lastWorkspace = ws
    return endpoint()
  },
})

test('a message is forwarded to the Runtime the gateway resolved', async () => {
  const gateway = gatewayThat(() => ({ mcpUrl: 'http://127.0.0.1:9/mcp', baseUrl: 'http://127.0.0.1:9' }))
  let seen
  const bridge = new OrbitBridge({
    workspace, gateway,
    fetchImpl: async (url, init) => { seen = { url, body: JSON.parse(init.body) }
      return ok({ jsonrpc: '2.0', id: 1, result: { tools: [] } }) },
  })
  const reply = await bridge.forward({ jsonrpc: '2.0', id: 1, method: 'tools/list' })
  assert.equal(seen.url, 'http://127.0.0.1:9/mcp')
  assert.equal(seen.body.method, 'tools/list')
  assert.deepEqual(reply, { jsonrpc: '2.0', id: 1, result: { tools: [] } })
  // Pointed at a project means Orbit should be there: starting one is the
  // point of launching this server for that directory.
  assert.equal(gateway.lastStart, true)
  assert.equal(gateway.lastWorkspace, workspace)
})

/** Resolving costs a subprocess and possibly a Runtime start; doing it per
 *  message would pay that on every tool call. */
test('the endpoint is resolved once and then reused', async () => {
  const gateway = gatewayThat(() => ({ mcpUrl: 'http://127.0.0.1:9/mcp', baseUrl: 'x' }))
  const bridge = new OrbitBridge({
    workspace, gateway, fetchImpl: async () => ok({ jsonrpc: '2.0', id: 1, result: {} }),
  })
  for (const id of [1, 2, 3]) await bridge.forward({ jsonrpc: '2.0', id, method: 'x' })
  assert.equal(gateway.calls, 1)
})

/**
 * Nothing is thrown at the caller. A stdio server has one peer waiting on one
 * pipe: a throw ends the session, while a failure it is told about is one it
 * can report or retry.
 */
test('a Runtime that will not start becomes a sentence, not a crash', async () => {
  const gateway = gatewayThat(() => {
    throw new Error('Orbit Runtime auto-start failed with code 3 for Workspace /x: boom')
  })
  const bridge = new OrbitBridge({ workspace, gateway, fetchImpl: async () => ok({}) })
  const reply = await bridge.forward({ jsonrpc: '2.0', id: 4, method: 'tools/list' })
  assert.equal(reply.id, 4)
  assert.equal(reply.error.code, INTERNAL_ERROR)
  assert.equal(reply.error.message, MESSAGES.errStartFailed)
  // The classification is a guess, so the text it was made from travels too.
  assert.match(reply.error.data.detail, /auto-start failed with code 3/)
})

test('an HTTP failure reads as unreachable rather than as a status code', async () => {
  const gateway = gatewayThat(() => ({ mcpUrl: 'http://127.0.0.1:9/mcp', baseUrl: 'x' }))
  const bridge = new OrbitBridge({
    workspace, gateway, fetchImpl: async () => ({ ok: false, status: 502, json: async () => ({}) }),
  })
  const reply = await bridge.forward({ jsonrpc: '2.0', id: 5, method: 'tools/list' })
  assert.equal(reply.error.message, MESSAGES.errUnreachable)
})

/** A Runtime can stop between two calls, and the address it answered on is
 *  then the wrong thing to retry. */
test('a failed call forgets the address it failed on', async () => {
  let up = false
  const gateway = gatewayThat(() => ({ mcpUrl: 'http://127.0.0.1:9/mcp', baseUrl: 'x' }))
  const bridge = new OrbitBridge({
    workspace, gateway,
    fetchImpl: async () => up
      ? ok({ jsonrpc: '2.0', id: 2, result: { back: true } })
      : { ok: false, status: 503, json: async () => ({}) },
  })
  await bridge.forward({ jsonrpc: '2.0', id: 1, method: 'x' })
  assert.equal(gateway.calls, 1)
  up = true
  const reply = await bridge.forward({ jsonrpc: '2.0', id: 2, method: 'x' })
  assert.equal(gateway.calls, 2, 'it retried a stale address instead of resolving again')
  assert.deepEqual(reply.result, { back: true })
})

/**
 * A Runtime that stops answering must not stop this server answering.
 *
 * Without a deadline the client waits on a promise nothing will settle, and a
 * stdio peer has no other way to notice — no status code, no closed socket,
 * just silence. And the deadline has to be told apart from a cancellation: an
 * abort says only "this operation was aborted", while "it timed out" means
 * Orbit may still be working and "it was cancelled" means nobody is waiting.
 */
test('a Runtime that never answers becomes a timeout, not a hang', async () => {
  const gateway = gatewayThat(() => ({ mcpUrl: 'http://127.0.0.1:9/mcp', baseUrl: 'x' }))
  const bridge = new OrbitBridge({
    workspace, gateway, timeoutMs: 25,
    // Never settles on its own; only the signal can end it.
    fetchImpl: (url, init) => new Promise((_, reject) => {
      init.signal.addEventListener('abort', () => {
        reject(new DOMException('This operation was aborted', 'AbortError'))
      })
    }),
  })
  const started = Date.now()
  const reply = await bridge.forward({ jsonrpc: '2.0', id: 8, method: 'tools/list' })
  assert.ok(Date.now() - started < 5000, 'it waited for the real ceiling')
  assert.equal(reply.error.message, MESSAGES.errTimeout)
  assert.notEqual(reply.error.message, MESSAGES.errAborted)
  assert.match(reply.error.data.detail, /timed out after 25ms/)
})

/** An abort nobody labelled is a cancellation, and reads as one. */
test('an abort this server did not schedule reads as a cancellation', async () => {
  const gateway = gatewayThat(() => ({ mcpUrl: 'http://127.0.0.1:9/mcp', baseUrl: 'x' }))
  const bridge = new OrbitBridge({
    workspace, gateway,
    fetchImpl: async () => { throw new DOMException('This operation was aborted', 'AbortError') },
  })
  const reply = await bridge.forward({ jsonrpc: '2.0', id: 9, method: 'tools/list' })
  assert.equal(reply.error.message, MESSAGES.errAborted)
})
