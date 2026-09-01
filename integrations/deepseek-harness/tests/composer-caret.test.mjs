import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CARET_ATTEMPTS, COMPOSER_SELECTOR, caretToEnd, draftHasLanded, pickComposer,
} from '../src/client/composer-caret.ts'

/**
 * Picking a Workflow leaves a sentence with its goal missing, and the caret
 * was not going to the end of it. The input machine has no caret to set:
 * `setDraft`, `insertReference`, `insertText` and `track` all take text or
 * spans, and the composer's own `restoreCaret` is scoped to the cut and paste
 * handlers that already hold the element. So this reaches into the shell's
 * DOM, and the rules about when it refuses to are the whole safety of it.
 */
test('the composer is identified by the input machine\'s own attribute', () => {
  // Not `textarea`: a page may hold others, and only a composer carries the
  // phase the input machine writes on it.
  assert.equal(COMPOSER_SELECTOR, 'textarea[data-phase], [contenteditable="true"][data-phase]')
})

test('a focused composer wins, a lone one is taken, and a tie is refused', () => {
  const one = { id: 1 }, two = { id: 2 }
  // With several Sessions rendered, the focused one is the one being read.
  assert.equal(pickComposer([one, two], two), two)
  assert.equal(pickComposer([one, two], one), one)
  // Only one candidate: no ambiguity to resolve.
  assert.equal(pickComposer([one], null), one)
  assert.equal(pickComposer([one], { id: 99 }), one)
  // Two, and neither focused. Moving the caret in the wrong conversation is
  // worse than leaving it alone, so this must not guess.
  assert.equal(pickComposer([one, two], null), null)
  assert.equal(pickComposer([one, two], { id: 99 }), null)
  assert.equal(pickComposer([], one), null)
})

/**
 * Two races, one guard: a render that has not happened yet shows the old
 * draft, and a person who has started typing shows a longer one. Both mean
 * the caret position we computed is about somebody else's text.
 */
test('the caret only moves onto the draft it was asked about', () => {
  assert.equal(draftHasLanded('用￼ 执行：', '用￼ 执行：'), true)
  // Not yet rendered.
  assert.equal(draftHasLanded('', '用￼ 执行：'), false)
  assert.equal(draftHasLanded('用执行：', '用￼ 执行：'), false)
  // Already typed into.
  assert.equal(draftHasLanded('用￼ 执行：查一下', '用￼ 执行：'), false)
})

test('it waits across frames rather than assuming one is enough', () => {
  assert.ok(CARET_ATTEMPTS >= 2, 'a single frame is an assumption about someone else\'s render')
  assert.ok(CARET_ATTEMPTS <= 12, 'long enough to outlast a person starting to type')
})

/** Runs on a server too, where there is no document to reach into. */
test('it does nothing outside a browser', () => {
  assert.equal(typeof document, 'undefined')
  assert.doesNotThrow(() => { caretToEnd('用￼ 执行：') })
})
