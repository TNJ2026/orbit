import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const source = await readFile(join(here, '..', 'src', 'client.ts'), 'utf8')
const code = source.split('\n').filter(line => !line.trimStart().startsWith('*')).join('\n')

/**
 * Orbit's own Runtime UI is the only interface a person uses. The browser
 * module exists to open it, so anything here that renders, overlays or holds
 * view state is a second interface starting to grow.
 */
test('the client renders nothing', () => {
  for (const forbidden of ['react', 'createElement', 'useState', 'useEffect', 'aria-modal', 'position: \'fixed\'']) {
    assert.equal(code.includes(forbidden), false, `${forbidden} belongs to a rendered surface`)
  }
})

test('/orbit takes no argument', () => {
  assert.equal(/hint:\s*'<goal>'/.test(code), false, 'the goal argument is back')
  assert.match(code, /takes no argument/)
  assert.match(code, /window\.open\(/)
})
