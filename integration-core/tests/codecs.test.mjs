import assert from 'node:assert/strict'
import test from 'node:test'
import { decodeRun, decodeToolResult } from '../lib/codecs.js'

const run = { run_id: 'run:1', goal: 'g', workflow_id: 'workflow:1', workflow_version: 1, status: 'running', revision: 1, artifact_count: 0, created_at: 'now', updated_at: 'now', interrupts: [], allowed_commands: [] }

test('run codec accepts the contract and rejects missing identities', () => {
  assert.equal(decodeRun(run).run_id, 'run:1')
  assert.throws(() => decodeRun({ ...run, run_id: 1 }), /run.run_id/)
})

test('step codec validates reconciliation markers', () => {
  const result = decodeToolResult('get_run_steps', { steps: [{ node_id: 'agent', status: 'unknown', resolution: { kind: 'reconciliation_required', delegation_id: 'delegation:1' } }] })
  assert.equal(result.steps[0].resolution.delegation_id, 'delegation:1')
  assert.throws(() => decodeToolResult('get_run_steps', { steps: [{ node_id: 'agent', status: 'unknown', resolution: { kind: 'retry' } }] }), /resolution.kind/)
  assert.throws(() => decodeToolResult('get_run_steps', { steps: [{ node_id: 'agent', status: 'unknown', reconciliation: { outcome: 'maybe', note: '', created_at: 'now' } }] }), /reconciliation.outcome/)
})

test('output codec rejects malformed payloads', () => {
  assert.throws(() => decodeToolResult('read_run_output', { chunks: [], after: '0', has_more: false }), /output.after/)
})

test('P4 catalog and authoring codecs reject malformed product data', () => {
  const workflows = decodeToolResult('list_workflows', { workflows: [{
    workflow_id: 'workflow:1', name: 'One', description: '', latest_version: 2, goal_readiness: 'ready',
  }] })
  assert.equal(workflows.workflows[0].latest_version, 2)
  assert.throws(() => decodeToolResult('list_workflows', { workflows: [{ workflow_id: 'x' }] }), /workflows\[0\]\.name/)
  const job = decodeToolResult('generate_workflow', {
    job_id: 'job:1', type: 'generate', prompt: 'make it', status: 'queued', created_at: 'now', updated_at: 'now',
  })
  assert.equal(job.status, 'queued')
  assert.throws(() => decodeToolResult('get_authoring_job', { ...job, status: 1 }), /authoring_job.status/)
})
