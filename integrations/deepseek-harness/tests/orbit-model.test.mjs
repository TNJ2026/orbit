import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ORBIT_IDLE_MS, ORBIT_POLL_MS, dotState, isLive, mergeChunks, nextInterval,
  orderRows, outputText, summarise, toRow, toStepRow,
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

test('an unresolved outcome is amber, not red', () => {
  // The Runtime deliberately left it open; drawing it as a failure would answer
  // a question nobody has ruled on.
  assert.equal(dotState('unknown'), 'warning')
  assert.equal(dotState('failed'), 'error')
  assert.equal(dotState('cancelled'), 'error')
  assert.equal(dotState('completed'), 'done')
  assert.equal(dotState('running'), 'ongoing')
})

test('a step waiting on a person is distinguished from one merely running', () => {
  const pending = toStepRow({
    node_id: 'n', status: 'unknown',
    resolution: { kind: 'reconciliation_required', delegation_id: 'd' },
  })
  assert.equal(pending.needsPerson, true)
  const settled = toStepRow({
    node_id: 'n', status: 'unknown',
    resolution: { kind: 'reconciliation_required', delegation_id: 'd' },
    reconciliation: { outcome: 'confirmed_succeeded', note: '', created_at: 'x' },
  })
  assert.equal(settled.needsPerson, false, 'an answered question is not still asking')
  assert.equal(toStepRow({ node_id: 'n', status: 'running' }).needsPerson, false)
})

test('a step label falls back to its node id', () => {
  assert.equal(toStepRow({ node_id: 'compute', status: 'running' }).label, 'compute')
  assert.equal(toStepRow({ node_id: 'compute', status: 'running', label: 'Compute totals' }).label, 'Compute totals')
})

test('output pages merge without duplicating or reordering a chunk', () => {
  const page = (ids) => ids.map(id => ({ chunk_id: id, text: `${String(id)};`, node_id: 'n', attempt_id: 'a', stream: 'stdout', created_at: 'x' }))
  const merged = mergeChunks(page([1, 2]), page([2, 3]))
  assert.deepEqual(merged.map(c => c.chunk_id), [1, 2, 3])
  assert.equal(outputText(merged), '1;2;3;')
  assert.equal(outputText(page([3, 1, 2])), '1;2;3;', 'a page out of order still reads in order')
})
