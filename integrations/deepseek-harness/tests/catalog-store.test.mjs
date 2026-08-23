import assert from 'node:assert/strict'
import test from 'node:test'

import { MENU_LIMIT, matchWorkflows, rememberWorkflows } from '../src/client/catalog-store.ts'

const workflow = (over = {}) => ({
  workflow_id: 'workflow:sales', name: 'Sales CSV report', description: '',
  latest_version: 3, goal_readiness: 'ready', ...over,
})

test('nothing remembered offers nothing', () => {
  rememberWorkflows([])
  assert.deepEqual(matchWorkflows('anything'), [])
})

test('an empty search offers the shortlist rather than nothing', () => {
  // The picker opens before a name has been typed; that is exactly when the
  // person is least likely to know one.
  rememberWorkflows([workflow(), workflow({ workflow_id: 'workflow:b', name: 'B' })])
  assert.equal(matchWorkflows('').length, 2)
})

test('the search is the name, and only the name', () => {
  // The id rides along for the Agent to act on, but matching it would surface
  // rows whose visible text has nothing to do with what was typed.
  rememberWorkflows([workflow()])
  assert.equal(matchWorkflows('sales csv').length, 1)
  assert.equal(matchWorkflows('SALES').length, 1, 'case is not a spelling')
  assert.equal(matchWorkflows('workflow:sales').length, 0, 'the id is not searchable')
  assert.equal(matchWorkflows('invoices').length, 0)
})

test('a workflow with no name is still findable by what it shows', () => {
  rememberWorkflows([workflow({ name: '' })])
  assert.equal(matchWorkflows('').length, 1)
  assert.equal(matchWorkflows('sales').length, 0, 'nothing visible matched')
})

test('the picker stays a shortlist', () => {
  rememberWorkflows(Array.from({ length: MENU_LIMIT + 5 }, (_, i) =>
    workflow({ workflow_id: `workflow:w${String(i)}`, name: `W${String(i)}` })))
  assert.equal(matchWorkflows('').length, MENU_LIMIT)
})
