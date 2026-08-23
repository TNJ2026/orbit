import assert from 'node:assert/strict'
import test from 'node:test'

import {
  MENU_LIMIT, knownWorkflows, matchWorkflows, rememberWorkflows,
} from '../src/client/catalog-store.ts'

const workflow = (over = {}) => ({
  workflow_id: 'workflow:sales', name: 'Sales CSV report', description: '',
  latest_version: 3, goal_readiness: 'ready', ...over,
})

test('nothing remembered offers nothing', () => {
  rememberWorkflows([])
  assert.deepEqual(matchWorkflows('anything'), [])
  assert.deepEqual(knownWorkflows(), [])
})

test('an empty query offers the shortlist rather than nothing', () => {
  // The menu opens before a person has typed a name; that is when they are
  // most likely not to know one.
  rememberWorkflows([workflow(), workflow({ workflow_id: 'workflow:b', name: 'B' })])
  assert.equal(matchWorkflows('').length, 2)
})

test('a name or an id matches, because either is how it was last seen', () => {
  rememberWorkflows([workflow()])
  assert.equal(matchWorkflows('sales csv').length, 1)
  assert.equal(matchWorkflows('workflow:sales').length, 1)
  assert.equal(matchWorkflows('SALES').length, 1, 'case is not a spelling')
  assert.equal(matchWorkflows('invoices').length, 0)
})

test('the menu stays a shortlist', () => {
  rememberWorkflows(Array.from({ length: MENU_LIMIT + 5 }, (_, i) =>
    workflow({ workflow_id: `workflow:w${String(i)}`, name: `W${String(i)}` })))
  assert.equal(matchWorkflows('').length, MENU_LIMIT)
})
