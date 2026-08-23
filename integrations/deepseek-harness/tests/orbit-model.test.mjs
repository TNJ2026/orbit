import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ORBIT_IDLE_MS, ORBIT_POLL_MS, commandRevision, dotState, isLive, mergeChunks, nextInterval,
  orderRows, outputText, renderRunnable, summarise, toRow, toStepRow,
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

test('a command is offered only at the revision Orbit advertises it for', () => {
  const row = toRow(run({
    revision: 7,
    allowed_commands: [{ command: 'langgraph_run.cancel', expected_version: 7 }],
  }))
  assert.equal(commandRevision(row, 'langgraph_run.cancel'), 7)
  assert.equal(commandRevision(row, 'langgraph_run.resume'), undefined,
    'a command Orbit did not advertise has no revision to act at')
  assert.equal(commandRevision(toRow(run()), 'langgraph_run.cancel'), undefined)
})

test('a step carries the delegation a person would be ruling on', () => {
  assert.equal(toStepRow({
    node_id: 'n', status: 'unknown',
    resolution: { kind: 'reconciliation_required', delegation_id: 'deleg-1' },
  }).delegationId, 'deleg-1')
  assert.equal(toStepRow({ node_id: 'n', status: 'running' }).delegationId, undefined)
})

test('an empty catalog says so instead of showing an empty block', () => {
  assert.equal(renderRunnable([], '还没有'), '还没有')
})

test('a listing leads with the name and follows with what a call needs', () => {
  const text = renderRunnable([{
    workflow_id: 'workflow:sales', name: 'Sales CSV report', latest_version: 3,
    goal_readiness: 'ready', description: '', inputs: [{ id: 'prompt' }],
  }], 'empty')
  const [first, second] = text.split('\n')
  assert.equal(first, 'Sales CSV report', 'the name is what someone recognises')
  assert.match(second, /workflow:sales@3/)
  assert.match(second, /input: prompt/)
})

test('a workflow with no declared inputs says nothing about them', () => {
  const text = renderRunnable([{
    workflow_id: 'workflow:x', name: 'X', latest_version: 1,
    goal_readiness: 'ready', description: '',
  }], 'empty')
  assert.equal(/input:/.test(text), false)
})
