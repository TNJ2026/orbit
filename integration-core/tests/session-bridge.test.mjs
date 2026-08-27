import assert from 'node:assert/strict'
import test from 'node:test'

import { OrbitSessionBridge, bridgeWithRetry, restoredBridgeState, sessionCanBridge } from '../lib/session-bridge.js'

const run = {
  run_id: 'run:1', goal: 'goal', workflow_id: 'wf', workflow_version: 1,
  revision: 2, status: 'running', artifact_count: 0, created_at: 'created', updated_at: 'updated',
}

test('restores cursor and known Runs only from durable Orbit events', () => {
  const state = restoredBridgeState([
    { type: 'user/message', data: { sourcePosition: 999, runId: 'foreign' } },
    { type: 'orbit/run-started', data: { sourcePosition: 3, runId: 'run:1' } },
    { type: 'orbit/run-checkpoint', data: { sourcePosition: 8, runId: 'run:1' } },
    { type: 'orbit/run-ended', data: { sourcePosition: 6, runId: 'run:2' } },
    { type: 'orbit/run-ended', data: null },
    { type: 'orbit/run-ended', data: { sourcePosition: -1, runId: 'corrupt' } },
    { type: 'orbit/run-ended', data: { sourcePosition: 12 } },
  ])
  assert.equal(state.position, 8)
  assert.deepEqual([...state.knownRuns], ['run:1', 'run:2'])
})

test('bridge releases its Runtime reference when cursor recovery fails', async () => {
  let released = false
  const bridge = new OrbitSessionBridge({
    async acquire() { return async () => { released = true } },
  }, { load: () => { throw new Error('corrupt cursor') }, save: () => {} }, 1)
  await assert.rejects(
    bridge.run({ id: 'w', canonicalPath: '/workspace' }, 's', { append: () => {} }, new AbortController().signal),
    /corrupt cursor/,
  )
  assert.equal(released, true)
})

test('only root Sessions with cwd receive an automatic Bridge', () => {
  assert.equal(sessionCanBridge({ cwd: '/workspace' }), true)
  assert.equal(sessionCanBridge({}), false)
  assert.equal(sessionCanBridge({ cwd: '/workspace', delegationDepth: 1 }), false)
})

test('bridge emits one start plus checkpoint, saves cursor and releases on abort', async () => {
  const controller = new AbortController(), appended = [], saved = []
  let released = false
  const gateway = {
    async acquire() { return async () => { released = true } },
    async call(_workspace, _session, name) {
      if (name === 'list_runtime_events') return { events: [{ run_id: 'run:1', position: 4 }], next_position: 4 }
      if (name === 'get_run_steps') return { steps: [{ node_id: 'node', status: 'running' }] }
      throw new Error(`unexpected ${name}`)
    },
    async run() { return run },
  }
  const bridge = new OrbitSessionBridge(gateway, {
    load: () => undefined,
    save: (_workspace, _session, position) => { saved.push(position); controller.abort() },
  }, 1)
  await bridge.run({ id: 'w', canonicalPath: '/workspace' }, 's', { append: event => { appended.push(event) } }, controller.signal)
  assert.deepEqual(appended.map(event => event.type), ['orbit/run-started', 'orbit/run-checkpoint'])
  assert.deepEqual(saved, [4])
  assert.equal(released, true)
})

test('known Run suppresses duplicate start and terminal events publish immediately', async () => {
  const controller = new AbortController(), appended = []
  const gateway = {
    async acquire() { return async () => {} },
    async call(_workspace, _session, name) {
      if (name === 'list_runtime_events') return { events: [{ run_id: 'run:1', position: 9 }], next_position: 9 }
      if (name === 'get_run_steps') return { steps: [] }
      throw new Error(`unexpected ${name}`)
    },
    async run() { return { ...run, status: 'completed' } },
  }
  const bridge = new OrbitSessionBridge(gateway, { load: () => 4, save: () => { controller.abort() } }, 1)
  await bridge.run({ id: 'w', canonicalPath: '/workspace' }, 's', { append: event => { appended.push(event) } }, controller.signal, ['run:1'])
  assert.deepEqual(appended.map(event => event.type), ['orbit/run-ended'])
})

test('a retry re-reads the Session, so an announced Run is not announced twice', async () => {
  // The failure this pins: knownRuns captured once, before the retry loop. The
  // first attempt announces run:1 and then the transport dies; the second
  // attempt must learn about run:1 from the events the first one durably
  // appended, not start over from the state the Bridge booted with.
  const events = []
  const seen = []
  let attempts = 0
  await bridgeWithRetry({
    events: () => events,
    attempt: async knownRuns => {
      attempts++
      seen.push([...knownRuns])
      if (attempts === 1) {
        events.push({ type: 'orbit/run-started', data: { sourcePosition: 4, runId: 'run:1' } })
        throw new Error('transport lost')
      }
    },
    onWaiting: () => {},
    signal: new AbortController().signal,
    retryDelayMs: 1,
  })
  assert.equal(attempts, 2)
  assert.deepEqual(seen, [[], ['run:1']])
})

test('a repeated failure is reported once, a changed one again', async () => {
  const reported = []
  const controller = new AbortController()
  let attempts = 0
  await bridgeWithRetry({
    events: () => [],
    attempt: async () => {
      attempts++
      if (attempts <= 2) throw new Error('same')
      if (attempts === 3) throw new Error('different')
      controller.abort()
      throw new Error('same')
    },
    onWaiting: message => { reported.push(message) },
    signal: controller.signal,
    retryDelayMs: 1,
  })
  assert.deepEqual(reported, ['Error: same', 'Error: different'])
})

test('an abort during the wait ends the retry loop without another attempt', async () => {
  const controller = new AbortController()
  let attempts = 0
  await bridgeWithRetry({
    events: () => [],
    attempt: async () => { attempts++; controller.abort(); throw new Error('lost') },
    onWaiting: () => { throw new Error('an aborted Bridge should report nothing') },
    signal: controller.signal,
    retryDelayMs: 1,
  })
  assert.equal(attempts, 1)
})
