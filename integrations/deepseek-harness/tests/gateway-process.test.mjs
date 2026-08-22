import assert from 'node:assert/strict'
import { mkdtemp, readFile, realpath, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { OrbitGateway } from '../lib/gateway.js'

const fixture = fileURLToPath(new URL('./fixtures/orbit-mcp-fixture.mjs', import.meta.url))

async function workspace(mode) {
  const path = await realpath(await mkdtemp(join(tmpdir(), `orbit-gateway-${mode}-`)))
  await writeFile(join(path, 'fixture-mode'), mode)
  return { id: `workspace:${mode}`, canonicalPath: path }
}

test('concurrent sessions reuse one Workspace runtime and release it', async () => {
  const target = await workspace('compatible')
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const [releaseOne, releaseTwo] = await Promise.all([
    gateway.acquire(target), gateway.acquire(target),
  ])
  const result = await gateway.call(target, 'one', 'inspect_run', {})
  assert.equal(result.run_id, 'run:1')
  assert.equal((await readFile(join(target.canonicalPath, 'starts.log'), 'utf8')).trim().split('\n').length, 1)
  await releaseOne()
  await releaseTwo()
})

test('an incompatible integration protocol fails readiness', async () => {
  const target = await workspace('incompatible')
  const gateway = new OrbitGateway(process.execPath, [fixture])
  await assert.rejects(gateway.acquire(target), /incompatible Orbit integration protocol/)
})

for (const mode of ['malformed', 'exit']) {
  test(`${mode} child response rejects pending RPC instead of hanging`, async () => {
    const target = await workspace(mode)
    const gateway = new OrbitGateway(process.execPath, [fixture])
    const release = await gateway.acquire(target)
    try {
      await assert.rejects(gateway.call(target, 'one', 'inspect_run', {}), error => {
        assert.match(error.message, /Orbit Runtime exited/)
        if (mode === 'exit') assert.match(error.message, /code 23/)
        else assert.match(error.message, /SIGTERM|signal/)
        return true
      })
    } finally { await release() }
  })
}
