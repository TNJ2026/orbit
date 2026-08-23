import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const source = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')

/**
 * The browser sends a Workspace with every Host API call, and a browser is not
 * an authority on which directory a Session belongs to. Every Gateway call must
 * therefore travel on a Workspace this Host derived and checked itself.
 *
 * Asserted against the source rather than through the service, because what
 * needs guarding is not one call's behaviour but the absence of an exception:
 * a method added later that forgets. A behavioural test can only cover the
 * methods somebody remembered to list.
 */
test('every Gateway call travels on a verified Workspace', () => {
  const calls = [...source.matchAll(/this\.gateway\.(call|run|acquire)\(\s*([A-Za-z_.]+)/g)]
  assert.ok(calls.length >= 20, `expected the Gateway call sites, found ${String(calls.length)}`)
  const unverified = calls
    .map(([, method, argument]) => ({ method, argument }))
    .filter(entry => entry.argument !== 'scope')
  assert.deepEqual(unverified, [], `these Gateway calls do not use a verified Workspace: ${
    unverified.map(entry => `gateway.${entry.method}(${entry.argument})`).join(', ')
  }`)
})

test('a Workspace reaches the Gateway only through verified or registered', () => {
  // Both helpers end at the Session store or the Workspace registry; neither
  // returns the caller's object. If a third path appears, it needs the same
  // scrutiny before this list grows.
  const producers = [...source.matchAll(/const scope = await this\.(verified|registered)\(/g)]
    .map(([, name]) => name)
  assert.ok(producers.length > 0)
  assert.deepEqual([...new Set(producers)].sort(), ['registered', 'verified'])
})
