import assert from 'node:assert/strict'
import test from 'node:test'

import { ORBIT_ERROR_KEYS } from '@orbit-runtime/integration-core'
import { COVERED, MESSAGES, sentenceFor } from '../lib/messages.js'

/**
 * The core says what can go wrong; each host says it in its own words.
 *
 * This is the seam the split was made at, so it is the one worth holding: a
 * failure the core can classify and this host has no sentence for would reach
 * a reader as an identifier. The check is against the exported vocabulary
 * rather than a copy of it, so adding a key to the core fails here until
 * somebody writes the sentence.
 */
test('every failure the core can name has a sentence here', () => {
  assert.deepEqual([...COVERED].sort(), [...ORBIT_ERROR_KEYS].sort())
  for (const key of ORBIT_ERROR_KEYS) {
    const sentence = MESSAGES[key]
    assert.equal(typeof sentence, 'string', `${key} has no sentence`)
    assert.ok(sentence.length > 12, `${key}'s sentence says nothing: ${sentence}`)
    // Not the key wearing a disguise.
    assert.doesNotMatch(sentence, /^err[A-Z]/, `${key} is showing its own name`)
  }
})

/**
 * The panel's wording does not fit here and was not copied.
 *
 * "Reopen the panel to start it" is advice for somebody who has a panel. This
 * host is a background process with no surface of its own, so its sentences
 * name what a person would type instead.
 */
test('the sentences are written for a host with no panel', () => {
  for (const [key, sentence] of Object.entries(MESSAGES)) {
    assert.doesNotMatch(sentence, /panel|hover|click|button/i,
      `${key} tells the reader to use a panel this host does not have`)
  }
  assert.match(MESSAGES.errNoRuntime, /orbit serve/)
})

test('an unlisted key still yields a sentence rather than a key', () => {
  assert.equal(sentenceFor('errNoRuntime'), MESSAGES.errNoRuntime)
  assert.equal(sentenceFor('errNotAKey'), MESSAGES.errUnknown)
})
