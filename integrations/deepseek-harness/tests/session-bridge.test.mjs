import assert from 'node:assert/strict'
import test from 'node:test'

import { OrbitSessionBridge, restoredBridgeState, sessionCanBridge } from '../lib/session-bridge.js'

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
