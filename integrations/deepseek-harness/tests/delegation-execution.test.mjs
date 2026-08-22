import assert from 'node:assert/strict'
import test from 'node:test'

import { executeDelegation } from '../lib/delegation-execution.js'

const workspace = { id: 'workspace:1', canonicalPath: '/workspace', isolationMode: 'shared' }
const job = {
  delegation_id: 'delegation:1', status: 'leased', cancel_requested: false,
  request: { input: { task: 'fix it' }, config: { provider: 'codex', max_wall_seconds: 2 } },
}
const snapshot = async () => ({ revision: 'abc', entries: new Map() })
const parent = {}

function ports(overrides = {}) {
  const settlements = []
  return {
    settlements,
    value: {
      snapshot, renewalMilliseconds: 5,
      renew: async () => ({ ...job, cancel_requested: false }),
      settle: async (result, error) => { settlements.push({ result, error }) },
      ...overrides,
    },
  }
}

test('provider start failure is a known single settlement', async () => {
  let starts = 0
  const target = ports()
  const subagents = {
    list: () => ['codex'],
    start: async () => { starts++; throw new Error('spawn refused') },
  }
  await executeDelegation(workspace, job, parent, subagents, new AbortController().signal, target.value)
  assert.equal(starts, 1)
  assert.equal(target.settlements.length, 1)
  assert.match(target.settlements[0].error, /spawn refused/)
})

test('published infrastructure loss remains unsettled and disposes once', async () => {
  let starts = 0, disposals = 0
  const target = ports()
  const subagents = {
    list: () => ['codex'],
    start: async () => {
      starts++
      return { result: Promise.reject(new Error('transport lost')), dispose: async () => { disposals++ } }
    },
  }
  await executeDelegation(workspace, job, parent, subagents, new AbortController().signal, target.value)
  assert.equal(starts, 1)
  assert.equal(disposals, 1)
  assert.deepEqual(target.settlements, [])
})

test('renewal observes cancellation, aborts provider, settles, and disposes', async () => {
  let starts = 0, disposals = 0, observedAbort = false
  const target = ports({ renew: async () => ({ ...job, cancel_requested: true }) })
  const subagents = {
    list: () => ['codex'],
    start: async (_provider, request) => {
      starts++
      return {
        result: new Promise(resolve => request.signal.addEventListener('abort', () => {
          observedAbort = true
          resolve({ output: [], stopReason: 'aborted', diagnostic: 'cancelled' })
        }, { once: true })),
        dispose: async () => { disposals++ },
      }
    },
  }
  await executeDelegation(workspace, job, parent, subagents, new AbortController().signal, target.value)
  assert.equal(starts, 1)
  assert.equal(disposals, 1)
  assert.equal(observedAbort, true)
  assert.deepEqual(target.settlements, [{ result: undefined, error: 'cancelled' }])
})

test('renewal transport loss aborts but never settles an uncertain result', async () => {
  let disposals = 0, observedAbort = false
  const target = ports({ renew: async () => { throw new Error('Orbit disconnected') } })
  const subagents = {
    list: () => ['codex'],
    start: async (_provider, request) => ({
      result: new Promise(resolve => request.signal.addEventListener('abort', () => {
        observedAbort = true
        resolve({ output: [], stopReason: 'aborted', diagnostic: 'cancelled locally' })
      }, { once: true })),
      dispose: async () => { disposals++ },
    }),
  }
  await executeDelegation(workspace, job, parent, subagents, new AbortController().signal, target.value)
  assert.equal(observedAbort, true)
  assert.equal(disposals, 1)
  assert.deepEqual(target.settlements, [])
})

test('completed provider settles exactly once with effects and disposes', async () => {
  let disposals = 0
  const target = ports()
  const subagents = {
    list: () => ['codex'],
    start: async () => ({
      result: Promise.resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' }),
      dispose: async () => { disposals++ },
    }),
  }
  await executeDelegation(workspace, job, parent, subagents, new AbortController().signal, target.value)
  assert.equal(disposals, 1)
  assert.equal(target.settlements.length, 1)
  assert.deepEqual(target.settlements[0].result.answer, { output: [{ type: 'text', text: 'done' }] })
  assert.equal(target.settlements[0].result.effects.observation, 'git-status')
})
