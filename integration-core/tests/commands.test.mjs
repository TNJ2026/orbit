import assert from 'node:assert/strict'
import test from 'node:test'

import { advertisedAt, commandTool } from '../lib/commands.js'

const run = (commands) => ({
  run_id: 'r', goal: 'g', workflow_id: 'wf', workflow_version: 1, status: 'interrupted',
  revision: 9, artifact_count: 0, created_at: 'c', updated_at: 'u',
  interrupts: [], allowed_commands: commands,
})

test('a command is accepted only at the revision it was advertised for', () => {
  const interrupted = run([{ command: 'langgraph_run.resume', expected_version: 9 }])
  assert.ok(advertisedAt(interrupted, 'langgraph_run.resume', 9))
})

test('a revision the reader was not looking at is refused, not corrected', () => {
  // The dangerous case: the Run moved between the panel drawing it and the
  // person pressing the button. Acting at the *current* revision would succeed
  // against a state they never saw.
  const moved = run([{ command: 'langgraph_run.resume', expected_version: 11 }])
  assert.equal(advertisedAt(moved, 'langgraph_run.resume', 9), undefined)
})

test('a command Orbit never offered has nothing to act at', () => {
  const running = run([{ command: 'langgraph_run.cancel', expected_version: 9 }])
  assert.equal(advertisedAt(running, 'langgraph_run.resume', 9), undefined)
  assert.equal(advertisedAt(run([]), 'langgraph_run.cancel', 9), undefined)
})

test('each command names the tool that carries it', () => {
  assert.equal(commandTool('langgraph_run.cancel'), 'cancel_run')
  assert.equal(commandTool('langgraph_run.resume'), 'resume_run')
})
