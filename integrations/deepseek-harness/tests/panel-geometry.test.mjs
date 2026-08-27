import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_PANEL_LAYOUT, PANEL_COMPACT_BREAKPOINT, PANEL_FLOAT_MARGIN,
  PANEL_MAX_WIDTH, PANEL_MIN_HEIGHT, PANEL_MIN_WIDTH,
  dragPanel, placePanel, readLayout, resizePanel,
} from '../src/client/panel-geometry.ts'

const wide = { width: 1440, height: 900 }

test('an unreadable or absent layout is the default, not a crash', () => {
  assert.deepEqual(readLayout(null), DEFAULT_PANEL_LAYOUT)
  assert.deepEqual(readLayout('not json'), DEFAULT_PANEL_LAYOUT)
  assert.deepEqual(readLayout('[]'), DEFAULT_PANEL_LAYOUT)
  assert.deepEqual(readLayout('"floating"'), DEFAULT_PANEL_LAYOUT)
})

test('a stored layout is honoured only where it still makes sense', () => {
  // Written on a wider monitor, by a version that spelled the mode differently.
  const restored = readLayout(JSON.stringify({
    mode: 'sideways', collapsed: false, x: 10, y: 20, width: 4000, height: 12,
  }))
  assert.equal(restored.mode, 'docked', 'an unknown mode falls back rather than persisting')
  assert.equal(restored.collapsed, false)
  assert.equal(restored.width, PANEL_MAX_WIDTH)
  assert.equal(restored.height, PANEL_MIN_HEIGHT)
})

test('the panel starts folded away', () => {
  assert.equal(DEFAULT_PANEL_LAYOUT.collapsed, true)
})

test('a docked panel hangs from the right edge at the height it was given', () => {
  const layout = { ...DEFAULT_PANEL_LAYOUT, collapsed: false }
  const box = placePanel(layout, wide)
  assert.equal(box.left + box.width, wide.width - 18)
  assert.equal(box.height, layout.height, 'docked is a side, not a full-height column')
})

test('a short window caps the docked height rather than overflowing it', () => {
  const short = { width: 1440, height: 380 }
  const box = placePanel({ ...DEFAULT_PANEL_LAYOUT, collapsed: false }, short)
  assert.ok(box.top + box.height <= short.height, 'the panel fits the window it is in')
  assert.ok(box.height >= PANEL_MIN_HEIGHT)
})

test('a narrow window docks a floating panel rather than floating it off-screen', () => {
  const narrow = { width: PANEL_COMPACT_BREAKPOINT - 1, height: 700 }
  const floating = { ...DEFAULT_PANEL_LAYOUT, mode: 'floating', collapsed: false, x: 5000, y: 5000 }
  const box = placePanel(floating, narrow)
  assert.ok(box.left >= PANEL_FLOAT_MARGIN)
  assert.ok(box.left + box.width <= narrow.width)
})

test('dragging cannot push the panel out of reach', () => {
  const floating = { ...DEFAULT_PANEL_LAYOUT, mode: 'floating', collapsed: false }
  const far = dragPanel(floating, 99_999, 99_999, wide)
  const box = placePanel(far, wide)
  assert.ok(box.left + box.width <= wide.width)
  assert.ok(box.top + box.height <= wide.height)
  const negative = dragPanel(floating, -99_999, -99_999, wide)
  assert.ok(placePanel(negative, wide).left >= PANEL_FLOAT_MARGIN)
})

test('resizing stays between the bounds a person can still use', () => {
  const floating = { ...DEFAULT_PANEL_LAYOUT, mode: 'floating', collapsed: false }
  assert.equal(resizePanel(floating, -99_999, -99_999, wide).width, PANEL_MIN_WIDTH)
  assert.equal(resizePanel(floating, -99_999, -99_999, wide).height, PANEL_MIN_HEIGHT)
  assert.equal(resizePanel(floating, 99_999, 0, wide).width, PANEL_MAX_WIDTH)
})

/**
 * Put away is a third state, not a deeper fold.
 *
 * `collapsed` is the panel folded to its mark and still watching; `dismissed`
 * is a person done with Orbit here. The close button reaches the second after
 * stopping the Runtime, because a mark still sitting there would be an offer
 * to reopen a page about a service that is no longer running.
 */
test('a layout written before dismissal existed still shows the panel', () => {
  assert.equal(readLayout(JSON.stringify({ collapsed: false })).dismissed, false)
  assert.equal(DEFAULT_PANEL_LAYOUT.dismissed, false)
  assert.equal(readLayout(null).dismissed, false)
  assert.equal(readLayout('not json').dismissed, false)
})

test('only an explicit dismissal hides it', () => {
  assert.equal(readLayout(JSON.stringify({ dismissed: true })).dismissed, true)
  // Anything else is a layout this panel cannot read as "hidden", and the
  // safe reading of that is to be visible: a panel nobody can reach is worse
  // than one they have to fold again.
  for (const value of ['true', 1, null, undefined, {}]) {
    assert.equal(readLayout(JSON.stringify({ dismissed: value })).dismissed, false, String(value))
  }
})

test('dismissal is independent of the fold and of the geometry', () => {
  const stored = readLayout(JSON.stringify({
    dismissed: true, collapsed: false, mode: 'floating', width: 500, height: 400,
  }))
  assert.equal(stored.dismissed, true)
  assert.equal(stored.collapsed, false, 'it remembers how it was folded when it comes back')
  assert.equal(stored.mode, 'floating')
  assert.equal(stored.width, 500)
})
