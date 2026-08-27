import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { ORBIT_ERROR_KEYS, panelError } from '../lib/error-text.js'

const here = dirname(fileURLToPath(import.meta.url))

/* The shapes that actually reach the panel: the Host stringifies whatever it
   caught, and the layers below wrap each other, so a reader is handed four
   layers of packaging around one fact. */
const reading = value => panelError(value).key

test('a Runtime that is not there is named as that, before anything else', () => {
  assert.equal(
    reading('Error: No independent Orbit Runtime is serving Workspace /Users/x/p'),
    'errNoRuntime',
    'a stopped Runtime also fails as transport; the specific reading wins',
  )
  assert.equal(reading('Orbit Runtime auto-start failed with code 1 for Workspace /x'), 'errStartFailed')
})

test('a Runtime that is there but silent reads differently from one that is gone', () => {
  assert.equal(reading('Error: Orbit MCP tools/call timed out'), 'errTimeout')
  assert.equal(reading('Orbit MCP transport failed: fetch failed'), 'errUnreachable')
  assert.equal(reading('Orbit MCP HTTP 502'), 'errUnreachable')
})

test('the failure that started all this reads as a stale list, not as a missing version', () => {
  // `Error: {"error":"workflow version not found: wf_7ba1a9d2-…@2"}` — four
  // layers of wrapping around "your list is out of date".
  assert.equal(
    reading('Error: {"error":"workflow version not found: workflow:wf_7ba1a9d2@2"}'),
    'errWorkflowGone',
  )
  assert.equal(reading('{"error":"workflow not found: workflow:wf_x"}'), 'errWorkflowGone')
  // Deleted is its own answer: refreshing will not bring it back.
  assert.equal(reading('{"error":"workflow was deleted: workflow:wf_x"}'), 'errWorkflowDeleted')
})

test('a slot somebody else holds says who to wait for', () => {
  assert.equal(reading('{"error":"ActiveGoalExists","job":{}}'), 'errGoalActive')
  assert.equal(reading('{"error":"workflow_generation_already_active","job":{}}'), 'errAuthoringActive')
})

test('a run that moved under the reader is not a run that vanished', () => {
  assert.equal(reading('Orbit no longer offers langgraph_run.cancel at revision 7'), 'errRunMoved')
  assert.equal(reading('Orbit no longer advertises langgraph_run.resume for Run r'), 'errRunMoved')
  assert.equal(reading('LangGraph run not found: langgraph_run:abc'), 'errRunGone')
})

test('a refusal is not a failure', () => {
  assert.equal(reading('only a Runtime operator may stop Orbit'), 'errNotAllowed')
  assert.equal(reading('valid actor credentials are required'), 'errNotAllowed')
  assert.equal(reading('Orbit refused to stop: HTTP 403'), 'errNotAllowed')
})

test('a Harness with no writer says so rather than blaming Orbit', () => {
  assert.equal(reading('no live Agent for Session session-abc'), 'errNoAgent')
  assert.equal(reading('this Harness exposes no Agent registry'), 'errNoAgent')
})

test('an unrecognised failure is not dressed up as a known one', () => {
  // A wrong diagnosis sends somebody to fix the wrong thing, which is worse
  // than saying plainly that this one is not understood.
  assert.equal(reading('Error: something nobody has seen before'), 'errUnknown')
  assert.equal(reading(''), 'errUnknown')
})

test('the original text is always kept, whatever shape it arrived in', () => {
  const raw = 'Error: {"error":"workflow was deleted: workflow:wf_x"}'
  assert.equal(panelError(raw).detail, raw, 'a message nobody can quote is one nobody can get help with')
  assert.equal(panelError(new Error('boom')).detail, 'boom')
  assert.equal(panelError({ error: 'from a payload' }).detail, 'from a payload')
  assert.equal(panelError({ message: 'from an object' }).detail, 'from an object')
  assert.equal(panelError(undefined).detail, 'undefined')
  assert.equal(panelError(null).detail, 'null')
})

/**
 * The vocabulary is the core's, the wording is the host's.
 *
 * `ORBIT_ERROR_KEYS` is what a host must be able to say. It used to be derived
 * from one host's dictionary — `keyof typeof en` — which made the set of
 * things that can go wrong a property of the panel's copy. It is not: it is
 * decided by the table below, and a second host has to answer for the same
 * failures in its own words.
 *
 * So the two are checked against each other here, and each host checks its own
 * dictionary covers them.
 */
test('the vocabulary and the readings describe the same failures', async () => {
  const source = await readFile(join(here, '..', 'src', 'error-text.ts'), 'utf8')
  const readings = new Set([...source.matchAll(/\[\/.+?\/i,\s*'(err[A-Za-z]+)'\]/gs)]
    .map(([, key]) => key))
  assert.ok(readings.size >= 20, `expected the readings, found ${String(readings.size)}`)

  const declared = new Set(ORBIT_ERROR_KEYS)
  // Everything the table can produce is declared, or a host cannot know to
  // write a sentence for it and the reader gets a key.
  for (const key of readings) assert.ok(declared.has(key), `${key} is not declared`)
  // And nothing is declared that the table can never produce, or hosts are
  // asked to translate failures that cannot happen. `errUnknown` is the one
  // exception: it is the fallback, reached by matching nothing.
  for (const key of declared) {
    if (key === 'errUnknown') continue
    assert.ok(readings.has(key), `${key} is declared but nothing classifies to it`)
  }
  assert.ok(declared.has('errUnknown'))
})

