import assert from 'node:assert/strict'
import test from 'node:test'

import {
  INTERNAL_ERROR, LineReader, PARSE_ERROR, errorReply, expectsReply, frame, parse,
} from '../lib/stdio.js'

/**
 * A chunk boundary is not a message boundary.
 *
 * stdin delivers bytes when it feels like it: a request can arrive in three
 * pieces, and three requests can arrive in one. Treating every `data` event as
 * a message cuts requests in half and works for as long as the messages stay
 * small enough to fit one chunk.
 */
test('messages are read by their newlines, not by their chunks', () => {
  const reader = new LineReader()
  assert.deepEqual(reader.push('{"a":1}\n{"b":2}\n'), ['{"a":1}', '{"b":2}'])
  // Split down the middle of one message.
  assert.deepEqual(reader.push('{"c":'), [])
  assert.deepEqual(reader.push('3}\n'), ['{"c":3}'])
  // Blank lines are framing, not messages.
  assert.deepEqual(reader.push('\n\n{"d":4}\n'), ['{"d":4}'])
  assert.equal(reader.rest(), '')
})

test('a message still arriving when the stream ends is kept, not invented', () => {
  const reader = new LineReader()
  assert.deepEqual(reader.push('{"half":'), [])
  assert.equal(reader.rest(), '{"half":')
})

test('a frame is one line', () => {
  const line = frame({ jsonrpc: '2.0', id: 1, result: { ok: true } })
  assert.ok(line.endsWith('\n'))
  assert.equal(line.split('\n').length, 2)
  assert.deepEqual(JSON.parse(line), { jsonrpc: '2.0', id: 1, result: { ok: true } })
})

/**
 * A malformed line is answered, not thrown on: this server has one pipe and
 * one peer, and dying takes the session with it.
 */
test('an unreadable line becomes a reply rather than a crash', () => {
  const bad = parse('{not json')
  assert.ok('parseError' in bad)
  const arrays = parse('[1,2]')
  assert.ok('parseError' in arrays, 'a batch is not a message this server handles')
  const fine = parse('{"jsonrpc":"2.0","id":7,"method":"tools/list"}')
  assert.equal(fine.method, 'tools/list')
})

test('a failure comes back in the shape the caller is waiting for', () => {
  const reply = errorReply(9, INTERNAL_ERROR, 'nope')
  assert.deepEqual(reply, { jsonrpc: '2.0', id: 9, error: { code: INTERNAL_ERROR, message: 'nope' } })
  // A parse failure has no id to answer to, and null is JSON-RPC's word for it.
  assert.equal(errorReply(undefined, PARSE_ERROR, 'bad').id, null)
})

/** Answering a notification is a message the peer never asked for. */
test('only requests are replied to', () => {
  assert.equal(expectsReply({ jsonrpc: '2.0', id: 1, method: 'x' }), true)
  assert.equal(expectsReply({ jsonrpc: '2.0', id: 0, method: 'x' }), true)
  assert.equal(expectsReply({ jsonrpc: '2.0', method: 'notifications/initialized' }), false)
  assert.equal(expectsReply({ jsonrpc: '2.0', id: null, method: 'x' }), false)
})
