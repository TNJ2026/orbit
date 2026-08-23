import assert from 'node:assert/strict'
import test from 'node:test'

import { CATALOG_LIMIT, CATALOG_TTL_MS, WorkflowCatalog } from '../lib/workflow-catalog.js'

const workflow = (over = {}) => ({
  workflow_id: 'workflow:clean', name: '清洗 CSV', description: '',
  latest_version: 2, goal_readiness: 'ready', inputs: [{ id: 'prompt' }], ...over,
})

test('nothing known contributes nothing', () => {
  // Not a sentence about the emptiness: that costs the same tokens every turn
  // and says less than the absence does.
  assert.equal(new WorkflowCatalog().render(), '')
})

test('a ready Workflow is named with the input it needs', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [workflow()])
  const text = catalog.render()
  assert.match(text, /workflow:clean@2 — 清洗 CSV \(input: prompt\)/)
  assert.match(text, /orbit_start_run/)
})

test('a Workflow that cannot start a goal is not offered as one', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [workflow({ goal_readiness: 'needs_upgrade' })])
  assert.equal(catalog.render(), '')
})

test('each Workspace is named, so a model with two can tell them apart', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/b', [workflow({ workflow_id: 'workflow:b' })])
  catalog.remember('/a', [workflow({ workflow_id: 'workflow:a' })])
  const text = catalog.render()
  assert.ok(text.indexOf('/a') < text.indexOf('/b'), 'stable order, not insertion order')
  assert.match(text, /Orbit Workflows ready in \/a:/)
})

test('a long catalog stops and says how to see the rest', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', Array.from({ length: CATALOG_LIMIT + 3 }, (_, i) =>
    workflow({ workflow_id: `workflow:w${String(i)}` })))
  const text = catalog.render()
  assert.equal(text.match(/^- workflow:w/gm).length, CATALOG_LIMIT)
  assert.match(text, /and 3 more; call orbit_list_workflows/)
})

test('an entry goes stale rather than being told forever', () => {
  let now = 1_000
  const catalog = new WorkflowCatalog(() => now)
  assert.equal(catalog.stale('/w'), true, 'never read is stale')
  catalog.remember('/w', [workflow()])
  assert.equal(catalog.stale('/w'), false)
  now += CATALOG_TTL_MS + 1
  assert.equal(catalog.stale('/w'), true)
})

test('forgetting a Workspace stops describing it', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [workflow()])
  catalog.forget('/w')
  assert.equal(catalog.render(), '')
})
