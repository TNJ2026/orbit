import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const source = await readFile(join(here, '..', 'src', 'client.ts'), 'utf8')
const code = source.split('\n').filter(line => !line.trimStart().startsWith('*')).join('\n')

/**
 * The panel shows Orbit's own page. It must never grow a second account of
 * what that page already renders — which is what every earlier version of this
 * module was, and what a Run list or a step view appearing here would make it
 * again. Reading data is the tell: a client that only needs an address cannot
 * drift into drawing runs.
 */
test('the client asks the Host for an address and nothing else', () => {
  const actions = [...code.matchAll(/orbitHostCall<[^>]*>\(\s*'([A-Za-z]+)'/g)].map(([, name]) => name)
  assert.deepEqual([...new Set(actions)], ['getRuntimeUi'])
})

test('no Orbit domain data is fetched or drawn here', () => {
  for (const forbidden of [
    'listRuns', 'listWorkflows', 'getSteps', 'getGraph', 'getEdges',
    'readOutput', 'listArtifacts', 'executeCommand', 'reconcileDelegation',
    'orbit/run-started', 'allowed_commands',
  ]) {
    assert.equal(code.includes(forbidden), false, `${forbidden} belongs to Orbit's own UI`)
  }
})

test('/orbit takes no argument and opens the panel in place', () => {
  assert.equal(/hint:\s*'<goal>'/.test(code), false, 'the goal argument is back')
  assert.match(code, /takes no argument/)
  assert.match(code, /createElement\('iframe'/)
  assert.equal(code.includes('window.open('), false, 'the panel replaced the new tab')
})

test('a frame the Host refuses still offers the address', () => {
  // A blocked frame neither loads nor errors, so nothing but a timer can tell
  // the difference between refused and slow.
  assert.match(code, /EMBED_TIMEOUT_MS/)
  assert.match(code, /target: '_blank'/)
})
