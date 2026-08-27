import assert from 'node:assert/strict'
import test from 'node:test'

import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  CLAIM_CLIENT, CLAIM_RETRY_MS, CLAIM_WAIT_SECONDS, answerFrom,
  authoringClientForSession, claimOnce, isUnknownToolError,
} from '../src/authoring-claim.ts'

const here = dirname(fileURLToPath(import.meta.url))
const gateway = await readFile(join(here, '..', 'src', 'gateway.ts'), 'utf8')

const request = { request_id: 'req-1', prompt: 'write me a workflow', job_id: 'job-1' }

function deps(over = {}) {
  const seen = { waited: [], asked: [], submitted: [], reported: [] }
  return [{
    wait: async seconds => { seen.waited.push(seconds); return request },
    ask: async prompt => { seen.asked.push(prompt); return '{"dsl_version":"1.3"}' },
    submit: async (id, dsl) => { seen.submitted.push([id, dsl]) },
    report: (stage, error) => { seen.reported.push([stage, String(error)]) },
    ...over,
  }, seen]
}

test('the name and the wait are the ones Orbit offers this Host under', () => {
  assert.equal(CLAIM_CLIENT, 'harness')
  assert.ok(CLAIM_WAIT_SECONDS >= 30, 'waiting is the point; a short poll is just asking again')
})

test('each Harness Session has a stable private Orbit writer address', () => {
  const first = authoringClientForSession('session:first/unsafe')
  assert.equal(first, authoringClientForSession('session:first/unsafe'))
  assert.notEqual(first, authoringClientForSession('session:second/unsafe'))
  assert.match(first, /^route\.harness\.[a-f0-9]{24}$/)
  assert.ok(first.length <= 64)
})

test('only the named missing compatibility tool is ignored', () => {
  assert.equal(isUnknownToolError(
    new Error('Error: unknown tool register_authoring_client'),
    'register_authoring_client',
  ), true)
  assert.equal(isUnknownToolError(
    new Error('not authorized to call register_authoring_client'),
    'register_authoring_client',
  ), false)
  assert.equal(isUnknownToolError(
    new Error('unknown tool generate_workflow'),
    'register_authoring_client',
  ), false)
})

/**
 * The wait has to fit inside the call that carries it.
 *
 * `wait_authoring_request` parks on the Runtime, but the park rides one MCP
 * request and the transport abandons any request at its ceiling. A wait longer
 * than that is not a longer wait: it is an aborted request, which cancels the
 * park, takes this Host off the queue, and reports a timeout the Host asked
 * for. The first version asked for 120s against a 60s ceiling and was on the
 * queue for a third of the time, losing every race it should have won.
 */
test('a wait never outlasts the transport that carries it', () => {
  // Read out of the Gateway rather than imported: this module is loaded as
  // source, and a value import would have to resolve out of it. The number is
  // what matters, and reading it is what stops the two from drifting apart.
  const found = /export const ORBIT_RPC_TIMEOUT_MS = ([0-9_]+)/.exec(gateway)
  assert.ok(found, 'the Gateway no longer names its RPC ceiling')
  const ceiling = Number(found[1].replaceAll('_', ''))
  assert.ok(CLAIM_WAIT_SECONDS * 1_000 < ceiling,
    `a ${String(CLAIM_WAIT_SECONDS)}s wait cannot ride a ${String(ceiling / 1_000)}s call`)
  // With room either side for the round trip, not scraping the ceiling.
  assert.ok(CLAIM_WAIT_SECONDS * 1_000 <= ceiling - 10_000)
  // And the Gateway must still be the thing that enforces it.
  assert.match(gateway, /controller\.abort\(\), ORBIT_RPC_TIMEOUT_MS/)
})

test('backing off from a broken Runtime is not the same clock as waiting', () => {
  // Silence on the queue is the queue working; an unanswering Runtime is not.
  assert.ok(CLAIM_RETRY_MS > 0)
  assert.notEqual(CLAIM_RETRY_MS, CLAIM_WAIT_SECONDS * 1_000)
})

test('work claimed is put to the model and its answer handed back', async () => {
  const [d, seen] = deps()
  assert.equal(await claimOnce(d), 'answered')
  assert.deepEqual(seen.waited, [CLAIM_WAIT_SECONDS])
  assert.deepEqual(seen.asked, ['write me a workflow'])
  assert.deepEqual(seen.submitted, [['req-1', '{"dsl_version":"1.3"}']])
  assert.deepEqual(seen.reported, [])
})

test('an expired wait is the queue working, not a failure', async () => {
  const [d, seen] = deps({ wait: async () => null })
  assert.equal(await claimOnce(d), 'idle')
  assert.deepEqual(seen.asked, [], 'nothing was claimed, so nothing is asked')
  assert.deepEqual(seen.reported, [])
})

test('whatever the model says is submitted; Orbit owns the judging', async () => {
  // A chatty answer costs a round — Orbit compiles it and re-issues the
  // request with the compiler's findings. Judging it here would be a second,
  // worse copy of that validator.
  const [d, seen] = deps({ ask: async () => 'Sure! Here you go:\n```json\n{}\n```' })
  assert.equal(await claimOnce(d), 'answered')
  assert.equal(seen.submitted[0][1], 'Sure! Here you go:\n```json\n{}\n```')
})

test('a turn that said nothing is not answered with emptiness', async () => {
  const [d, seen] = deps({ ask: async () => '   \n  ' })
  assert.equal(await claimOnce(d), 'failed')
  assert.deepEqual(seen.submitted, [],
    'the lease lapses and the request goes back on the queue instead')
  assert.equal(seen.reported[0][0], 'ask')
})

test('a model that threw is never answered for', async () => {
  const [d, seen] = deps({ ask: async () => { throw new Error('model exploded') } })
  assert.equal(await claimOnce(d), 'failed')
  assert.deepEqual(seen.submitted, [],
    'submitting something the model never said would publish a Workflow nobody wrote')
  assert.deepEqual(seen.reported, [['ask', 'Error: model exploded']])
})

test('an unreachable queue is reported and nothing else happens', async () => {
  const [d, seen] = deps({ wait: async () => { throw new Error('no runtime') } })
  assert.equal(await claimOnce(d), 'failed')
  assert.deepEqual(seen.asked, [])
  assert.deepEqual(seen.reported, [['wait', 'Error: no runtime']])
})

test('a submission that failed is reported rather than swallowed', async () => {
  const [d, seen] = deps({ submit: async () => { throw new Error('lease expired') } })
  assert.equal(await claimOnce(d), 'failed')
  assert.deepEqual(seen.reported, [['submit', 'Error: lease expired']])
})

const assistant = (...blocks) => ({ type: 'assistant/message', data: { message: { content: blocks } } })
const text = value => ({ type: 'text', text: value })

test('the answer is the model text a turn appended, in order', () => {
  const events = [
    assistant(text('before the mark')),
    { type: 'turn/start' },
    assistant(text('first half')),
    { type: 'tool/call' },
    assistant(text('second half')),
  ]
  assert.equal(answerFrom(events, 1), 'first half\nsecond half',
    'a turn that used a tool answers across more than one message')
  assert.equal(answerFrom(events, 0), 'before the mark\nfirst half\nsecond half')
})

test('thinking and tool calls are not the answer', () => {
  const events = [assistant(
    { type: 'reasoning', text: 'let me think about the ports' },
    { type: 'tool-call', id: 'c1', name: 'bash' },
    text('{"dsl_version":"1.3"}'),
  )]
  assert.equal(answerFrom(events, 0), '{"dsl_version":"1.3"}',
    'handing Orbit the working-out wrapped around the document would fail to compile')
})

test('a turn that produced no text reads as no answer', () => {
  assert.equal(answerFrom([{ type: 'turn/end' }], 0), '')
  assert.equal(answerFrom([assistant()], 0), '')
  assert.equal(answerFrom([{ type: 'assistant/message' }], 0), '', 'a message with no data')
  assert.equal(answerFrom([{ type: 'assistant/message', data: { message: { content: 'x' } } }], 0), '')
  assert.equal(answerFrom([], 5), '', 'a mark past the end reads nothing, not a crash')
})
