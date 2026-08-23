import assert from 'node:assert/strict'
import test from 'node:test'
import { ORBIT_COMMAND, ORBIT_SELECTION_PENDING_TEXT, orbitGoal } from '../lib/orbit-command.js'

test('normalizes /orbit goal text', () => {
  assert.equal(orbitGoal('  review the release  '), 'review the release')
})

test('exports stable slash-command selection markers', () => {
  assert.equal(ORBIT_COMMAND, 'orbit')
  assert.equal(ORBIT_SELECTION_PENDING_TEXT, 'Choose an Orbit Workflow to continue.')
})
