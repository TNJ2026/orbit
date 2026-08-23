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
const orbit = resolve(here, '../../../.venv/bin/orbit')

async function freePort() {
  const server = createServer()
  await new Promise(resolveReady => server.listen(0, '127.0.0.1', resolveReady))
  const port = server.address().port
  await new Promise((resolveClosed, reject) => server.close(error => error ? reject(error) : resolveClosed()))
  return port
}

async function waitReady(url, child) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (child.exitCode !== null) throw new Error(`independent Orbit exited with ${String(child.exitCode)}`)
    try { if ((await fetch(url)).ok) return } catch {}
    await new Promise(resolveWait => setTimeout(resolveWait, 50))
  }
  throw new Error('independent Orbit did not become ready')
}

async function stop(child) {
  if (child.exitCode !== null) return
  child.kill('SIGTERM')
  await new Promise(resolveExit => child.once('exit', resolveExit))
}

test('Harness discovers and talks to an independent Runtime without owning it', { timeout: 20_000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'orbit-independent-e2e-'))
  const previousRuntimeRoot = process.env.ORBIT_RUNTIME_ROOT
  process.env.ORBIT_RUNTIME_ROOT = root
  t.after(() => {
    if (previousRuntimeRoot === undefined) delete process.env.ORBIT_RUNTIME_ROOT
    else process.env.ORBIT_RUNTIME_ROOT = previousRuntimeRoot
  })
  const workspacePath = join(root, 'workspace')
  await mkdir(workspacePath)
  const port = await freePort()
  const child = spawn(orbit, [
    'serve', '--host', '127.0.0.1', '--port', String(port),
    '--project-root', workspacePath, '--db', join(root, 'runtime.db'),
    '--mcp-tool-profile', 'harness', '--no-agent-discovery',
  ], { cwd: workspacePath, stdio: ['ignore', 'ignore', 'pipe'] })
  let stderr = ''
  child.stderr.setEncoding('utf8'); child.stderr.on('data', chunk => { stderr += chunk })
  t.after(async () => { await stop(child) })
  const base = `http://127.0.0.1:${String(port)}`
  try { await waitReady(`${base}/health/ready`, child) }
  catch (error) { throw new Error(`${String(error)}\n${stderr}`) }

  const gateway = new OrbitGateway(orbit)
  const workspace = { id: 'workspace:e2e', canonicalPath: workspacePath }
  const release = await gateway.acquire(workspace)
  const first = await gateway.call(workspace, 'first', 'list_runs', {})
  const second = await gateway.call(workspace, 'second', 'list_runs', {})
  assert.deepEqual(first.runs, [])
  assert.deepEqual(second.runs, [])
  await release()

  const sessions = await (await fetch(`${base}/api/v1/mcp/sessions`)).json()
  assert.deepEqual(
    sessions.data.sessions.map(item => item.actor).filter(Boolean).sort(),
    ['harness:session:first', 'harness:session:second', 'local'],
  )
  assert.equal((await fetch(`${base}/health/ready`)).ok, true)
  assert.equal(child.exitCode, null)
})
