import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AUTHORING_STAGES, authoringProgress, isProgressMarker,
} from '../src/authoring-progress.ts'

const mark = (stage, attempt = 1, max = 3) => ({
  text: `\x1eorbit-progress:${JSON.stringify({ stage, attempt, max_attempts: max })}`,
})
const said = text => ({ text })
const names = p => p.stages.map(s => `${s.stage}:${s.status}`)

test('a job nobody has heard from yet has reached no stage', () => {
  assert.deepEqual(names(authoringProgress([], 'queued')),
    AUTHORING_STAGES.map(s => `${s}:not_reached`))
})

test('the ladder follows the job through its stages', () => {
  assert.deepEqual(names(authoringProgress([mark('generating')], 'running')),
    ['generating:running', 'validating:not_reached', 'publishing:not_reached'])
  assert.deepEqual(names(authoringProgress([mark('generating'), mark('validating')], 'running')),
    ['generating:succeeded', 'validating:running', 'publishing:not_reached'])
  assert.deepEqual(
    names(authoringProgress([mark('generating'), mark('validating'), mark('publishing')], 'running')),
    ['generating:succeeded', 'validating:succeeded', 'publishing:running'])
})

test('a compiler refusal sends the ladder back to drafting', () => {
  const chunks = [mark('generating', 1), mark('validating', 1), mark('repairing', 1), mark('generating', 2)]
  const progress = authoringProgress(chunks, 'running')
  assert.deepEqual(names(progress),
    ['generating:running', 'validating:not_reached', 'publishing:not_reached'],
    'it is drafting again, not still validating')
  assert.equal(progress.attempt, 2, 'which try it is on is the news')
  assert.equal(progress.maxAttempts, 3)
})

test('validation passing moves on without waiting for the publish marker', () => {
  assert.deepEqual(names(authoringProgress([mark('generating'), mark('validating'), mark('validated')], 'running')),
    ['generating:succeeded', 'validating:succeeded', 'publishing:running'])
})

test('a job that is done reached every stage, whatever it last printed', () => {
  assert.deepEqual(names(authoringProgress([mark('generating')], 'done')),
    AUTHORING_STAGES.map(s => `${s}:succeeded`))
})

test('a job that failed failed at the rung it had reached', () => {
  assert.deepEqual(names(authoringProgress([mark('generating'), mark('validating')], 'failed')),
    ['generating:succeeded', 'validating:failed', 'publishing:not_reached'],
    'a killed job must not show a stage running for as long as anyone looks')
  assert.deepEqual(names(authoringProgress([mark('generating')], 'cancelled')),
    ['generating:failed', 'validating:not_reached', 'publishing:not_reached'])
})

test('only a whole marker chunk moves the ladder', () => {
  assert.equal(isProgressMarker(said('writing the workflow…')), false)
  // An Agent printing the sentinel inside its own output is printing, not
  // reporting: the marker is the whole chunk or it is nothing.
  assert.equal(isProgressMarker(said('note: \x1eorbit-progress:{"stage":"publishing"}')), false)
  assert.deepEqual(
    names(authoringProgress([mark('generating'), said('note: \x1eorbit-progress:{"stage":"publishing"}')], 'running')),
    ['generating:running', 'validating:not_reached', 'publishing:not_reached'])
})

test('a marker this cannot read loses itself, not the ladder', () => {
  const chunks = [mark('generating'), said('\x1eorbit-progress:{not json'), mark('validating')]
  assert.deepEqual(names(authoringProgress(chunks, 'running')),
    ['generating:succeeded', 'validating:running', 'publishing:not_reached'])
  assert.deepEqual(
    names(authoringProgress([mark('generating'), said('\x1eorbit-progress:{"stage":"inventing"}')], 'running')),
    ['generating:running', 'validating:not_reached', 'publishing:not_reached'],
    'a stage this build does not know must not move the ladder somewhere odd')
})

test('the attempt count is absent rather than invented', () => {
  const progress = authoringProgress([said('\x1eorbit-progress:{"stage":"publishing"}')], 'running')
  assert.equal(progress.attempt, 0)
  assert.equal(progress.maxAttempts, 0)
})
