import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, mkdir } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { OrbitGateway } from '../lib/gateway.js'

const here = dirname(fileURLToPath(import.meta.url))
const orbit = process.env.ORBIT_BIN || resolve(
  here,
  process.platform === 'win32' ? '../../../.venv/Scripts/orbit.exe' : '../../../.venv/bin/orbit',
)

async function freePort() {
  const server = createServer()
  await new Promise(resolveReady => server.listen(0, '127.0.0.1', resolveReady))
  const port = server.address().port
  await new Promise((resolveClosed, reject) => server.close(error => error ? reject(error) : resolveClosed()))
  return port
}

async function waitReady(url, child) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (child.exitCode !== null) throw new Error(`Orbit Hub exited with ${String(child.exitCode)}`)
    try { if ((await fetch(url)).ok) return } catch {}
    await new Promise(resolveWait => setTimeout(resolveWait, 50))
  }
  throw new Error('Orbit Hub did not become ready')
}

async function stop(child) {
  if (child.exitCode !== null) return
  child.kill('SIGTERM')
  await new Promise(resolveExit => child.once('exit', resolveExit))
}

test('Harness reaches its workspace Runtime through the fixed Hub', { timeout: 30_000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'orbit-independent-e2e-'))
  // Both roots, not just the discovery one. Acquiring a Workspace runs
  // `orbit hub register`, which writes the machine-wide workspace registry —
  // so without this every run of this test left its throwaway directory in the
  // developer's real one, and nothing ever took it out. Redirecting beats
  // deregistering in teardown: a run that dies half way leaves nothing behind
  // either.
  const overrides = { ORBIT_RUNTIME_ROOT: root, ORBIT_HUB_ROOT: join(root, 'hub') }
  const previous = Object.fromEntries(
    Object.keys(overrides).map(name => [name, process.env[name]]),
  )
  Object.assign(process.env, overrides)
  t.after(() => {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name]
      else process.env[name] = value
    }
  })
  const workspacePath = join(root, 'workspace')
  await mkdir(workspacePath)
  const port = await freePort()
  const child = spawn(orbit, [
    'hub', 'serve', '--host', '127.0.0.1', '--port', String(port),
  ], { cwd: workspacePath, stdio: ['ignore', 'ignore', 'pipe'] })
  let stderr = ''
  child.stderr.setEncoding('utf8'); child.stderr.on('data', chunk => { stderr += chunk })
  t.after(async () => { await stop(child) })
  const base = `http://127.0.0.1:${String(port)}`
  try { await waitReady(`${base}/health/ready`, child) }
  catch (error) { throw new Error(`${String(error)}\n${stderr}`) }

  const gateway = new OrbitGateway(orbit, [], globalThis.fetch, root, base)
  const workspace = { id: 'workspace:e2e', canonicalPath: workspacePath }
  const release = await gateway.acquire(workspace, true)
  const first = await gateway.call(workspace, 'first', 'list_runs', {})
  const second = await gateway.call(workspace, 'second', 'list_runs', {})
  assert.deepEqual(first.runs, [])
  assert.deepEqual(second.runs, [])
  await release()

  // What `/orbit` opens stays on the stable Hub namespace.
  const ui = await gateway.uiUrl(workspace)
  assert.match(ui, new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/workspaces/[a-f0-9]{12}/ui/$`))
  assert.equal((await fetch(ui)).ok, true)
  assert.equal((await fetch(`${base}/health/ready`)).ok, true)
  assert.equal(child.exitCode, null)
})
