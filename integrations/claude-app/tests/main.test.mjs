import assert from 'node:assert/strict'
import test from 'node:test'

import { isEntryPoint, orbitCommandFrom, projectRootFrom } from '../lib/main.js'

/** A client that starts one server per project passes the directory; one that
 *  does not, launches it in the directory. Both have to work. */
test('the project comes from the flag, or from where it was launched', () => {
  assert.equal(projectRootFrom(['--project-root', '/a/b'], '/cwd'), '/a/b')
  assert.equal(projectRootFrom([], '/cwd'), '/cwd')
  // A flag with nothing after it is not an instruction to serve "".
  assert.equal(projectRootFrom(['--project-root'], '/cwd'), '/cwd')
  assert.equal(projectRootFrom(['--project-root', ''], '/cwd'), '/cwd')
})

test('a checkout can point at its own orbit', () => {
  assert.equal(orbitCommandFrom(['--orbit-command', '/x/orbit'], {}), '/x/orbit')
  assert.equal(orbitCommandFrom([], { ORBIT_COMMAND: '/y/orbit' }), '/y/orbit')
  // The flag beats the environment: it was typed for this launch.
  assert.equal(orbitCommandFrom(['--orbit-command', '/x/orbit'], { ORBIT_COMMAND: '/y/orbit' }), '/x/orbit')
  assert.equal(orbitCommandFrom([], {}), 'orbit')
})

/**
 * Importing this module must not start a server.
 *
 * Compared as resolved paths rather than by suffix: `main.js` ends the same
 * way as any other `main.js`, and a test that imports this one would have
 * started reading stdin.
 */
test('it runs only when it is the program', () => {
  const url = new URL('../lib/main.js', import.meta.url).href
  assert.equal(isEntryPoint(url, new URL(url).pathname), true)
  assert.equal(isEntryPoint(url, undefined), false)
  assert.equal(isEntryPoint(url, ''), false)
  assert.equal(isEntryPoint(url, '/somewhere/else/main.js'), false)
})
