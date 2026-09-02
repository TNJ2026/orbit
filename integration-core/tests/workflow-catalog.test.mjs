import assert from 'node:assert/strict'
import test from 'node:test'

import { CATALOG_LIMIT, CATALOG_TTL_MS, WorkflowCatalog } from '../lib/workflow-catalog.js'

const workflow = (over = {}) => ({
  workflow_id: 'workflow:clean', name: '清洗 CSV', description: '',
  latest_version: 2, goal_readiness: 'ready', inputs: [{ id: 'prompt' }], ...over,
})

test('nothing known in this Workspace contributes nothing', () => {
  // Not a sentence about the emptiness: that costs the same tokens every turn
  // and says less than the absence does.
  assert.equal(new WorkflowCatalog().render('/w'), '')
})

test('a ready Workflow is named with the input it needs', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [workflow()])
  const text = catalog.render('/w')
  assert.match(text, /workflow:clean@2 — 清洗 CSV \(input: prompt\)/)
  assert.match(text, /orbit_start_run/)
})

test('a Workflow that cannot start a goal is not offered as one', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [workflow({ goal_readiness: 'needs_upgrade' })])
  assert.equal(catalog.render('/w'), '')
})

test('the panel is still shown the Workflow the model is not offered', () => {
  // The two readers want different halves of one read. Naming an unrunnable
  // Workflow to the model is an offer it cannot take; hiding it from the panel
  // hides the one entry a person has to go and fix, and leaves them reading a
  // catalog that quietly disagrees with Orbit's own.
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [
    workflow({ goal_readiness: 'needs_upgrade' }),
    workflow({ workflow_id: 'workflow:fine' }),
  ])
  assert.deepEqual(
    catalog.list('/w').map(item => item.goal_readiness), ['needs_upgrade', 'ready'],
  )
  assert.equal(catalog.render('/w').includes('workflow:clean'), false)
  assert.match(catalog.render('/w'), /workflow:fine/)
})

test('a model is offered only the Workflows routed by its Workspace', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/b', [workflow({ workflow_id: 'workflow:b' })])
  catalog.remember('/a', [workflow({ workflow_id: 'workflow:a' })])
  const text = catalog.render('/a')
  assert.match(text, /Orbit Workflows ready in \/a:/)
  assert.doesNotMatch(text, /workflow:b/)
  assert.doesNotMatch(text, /\/b/)
})

test('a long catalog stops and says how to see the rest', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', Array.from({ length: CATALOG_LIMIT + 3 }, (_, i) =>
    workflow({ workflow_id: `workflow:w${String(i)}` })))
  const text = catalog.render('/w')
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
  assert.equal(catalog.render('/w'), '',
    'an expired offer stays hidden until a successful refresh remembers it again')
})

test('forgetting a Workspace stops describing it', () => {
  const catalog = new WorkflowCatalog()
  catalog.remember('/w', [workflow()])
  catalog.forget('/w')
  assert.equal(catalog.render('/w'), '')
})
