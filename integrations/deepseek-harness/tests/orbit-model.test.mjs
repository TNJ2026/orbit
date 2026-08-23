import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ORBIT_IDLE_MS, ORBIT_POLL_MS, isLive, nextInterval, orderRows, summarise, toRow,
} from '../src/client/orbit-model.ts'

const run = (over = {}) => ({
  run_id: 'r', goal: 'g', workflow_id: 'wf', workflow_version: 1,
  status: 'running', revision: 1, artifact_count: 0,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  interrupts: [], allowed_commands: [], ...over,
})

test('only a settled outcome stops a Run counting as live', () => {
  for (const status of ['completed', 'failed', 'cancelled', 'unknown']) {
    assert.equal(isLive(status), false, status)
  }
  for (const status of ['running', 'queued', 'interrupted']) {
    assert.equal(isLive(status), true, status)
  }
})

test('the cadence follows the work, not the clock', () => {
  assert.equal(nextInterval([toRow(run({ status: 'running' }))]), ORBIT_POLL_MS)
  assert.equal(nextInterval([toRow(run({ status: 'completed' }))]), ORBIT_IDLE_MS)
  assert.equal(nextInterval([]), ORBIT_IDLE_MS, 'an empty Workspace is idle, not urgent')
})

test('running Runs come first, then the most recently touched', () => {
  const rows = orderRows([
    toRow(run({ run_id: 'old-done', status: 'completed', updated_at: '2026-01-01T00:00:00Z' })),
    toRow(run({ run_id: 'new-done', status: 'completed', updated_at: '2026-01-03T00:00:00Z' })),
    toRow(run({ run_id: 'live', status: 'running', updated_at: '2026-01-02T00:00:00Z' })),
  ])
  assert.deepEqual(rows.map(r => r.runId), ['live', 'new-done', 'old-done'])
})

test('the collapsed badge counts what is moving', () => {
  assert.deepEqual(summarise([
    toRow(run({ status: 'running' })), toRow(run({ status: 'completed' })),
  ]), { live: 1, total: 2 })
})

test('a Run with no goal is still identifiable', () => {
  assert.equal(toRow(run({ goal: '', run_id: 'langgraph_run:abc' })).goal, 'langgraph_run:abc')
})
