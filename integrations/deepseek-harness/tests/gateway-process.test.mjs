import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, realpath, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { OrbitGateway } from '../lib/gateway.js'

const fixture = fileURLToPath(new URL('./fixtures/orbit-mcp-fixture.mjs', import.meta.url))

async function target(mode, handler) {
  const path = await realpath(await mkdtemp(join(tmpdir(), `orbit-gateway-${mode}-`)))
  let calls = 0, lastActor
  const server = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8'); request.on('data', chunk => { body += chunk })
    request.on('end', () => {
      calls++
      lastActor = request.headers['x-orbit-actor']
      const message = JSON.parse(body)
      const result = handler(message)
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
    })
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  await writeFile(join(path, 'runtime.json'), JSON.stringify([{
    project_root: path, transport: 'http', mcp_url: `http://127.0.0.1:${String(address.port)}/mcp`,
  }]))
  return { workspace: { id: `workspace:${mode}`, canonicalPath: path }, server, calls: () => calls, lastActor: () => lastActor }
}

function mcp(protocol = 'orbit-harness/1') {
  return message => {
    if (message.method === 'initialize') return { protocolVersion: '2025-06-18', capabilities: {} }
    const name = message.params.name
    if (name === 'get_capabilities') return { structuredContent: { integration_protocol: protocol } }
    if (name === 'inspect_run') return { structuredContent: {
      run_id: 'run:1', goal: 'test', workflow_id: 'wf', workflow_version: 1,
      status: 'running', revision: 1, artifact_count: 0, created_at: 'now', updated_at: 'now',
      interrupts: [], allowed_commands: [],
    } }
    throw new Error(`unexpected tool ${String(name)}`)
  }
}

test('concurrent sessions reuse one discovered Runtime without owning its lifecycle', async t => {
  const runtime = await target('compatible', mcp())
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const [releaseOne, releaseTwo] = await Promise.all([
    gateway.acquire(runtime.workspace), gateway.acquire(runtime.workspace),
  ])
  assert.equal((await gateway.call(runtime.workspace, 'one', 'inspect_run', {})).run_id, 'run:1')
  assert.equal(runtime.calls(), 3)
  assert.equal(runtime.lastActor(), 'harness:session:one')
  await releaseOne(); await releaseTwo()
  assert.equal(runtime.server.listening, true)
})

test('an incompatible independent Runtime fails readiness', async t => {
  const runtime = await target('incompatible', mcp('other/9'))
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture])
  await assert.rejects(gateway.acquire(runtime.workspace), /incompatible Orbit integration protocol/)
})

test('a missing Workspace Runtime gives an actionable start command', async () => {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'orbit-gateway-missing-')))
  await writeFile(join(path, 'runtime.json'), '[]')
  const gateway = new OrbitGateway(process.execPath, [fixture])
  await assert.rejects(gateway.acquire({ id: 'missing', canonicalPath: path }), /No independent Orbit Runtime.*orbit serve --project-root/)
})

test('an empty or malformed Session id never becomes an actor header', async t => {
  const runtime = await target('invalid-session', mcp())
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture])
  await assert.rejects(gateway.call(runtime.workspace, '', 'inspect_run', {}), /invalid Harness session id/)
  await assert.rejects(gateway.call(runtime.workspace, 'bad/session', 'inspect_run', {}), /invalid Harness session id/)
  assert.equal(runtime.lastActor(), undefined)
})

test('a transport loss invalidates the cached endpoint for rediscovery', async t => {
  const original = await target('restart', mcp())
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const release = await gateway.acquire(original.workspace)
  await release()
  await new Promise(resolve => original.server.close(resolve))
  await assert.rejects(gateway.call(original.workspace, 'one', 'list_runs', {}), /transport failed/)

  const replacement = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8'); request.on('data', chunk => { body += chunk })
    request.on('end', () => {
      const message = JSON.parse(body), result = mcp()(message)
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
    })
  })
  await new Promise(resolve => replacement.listen(0, '127.0.0.1', resolve))
  t.after(() => replacement.close())
  await writeFile(join(original.workspace.canonicalPath, 'runtime.json'), JSON.stringify([{
    project_root: original.workspace.canonicalPath, transport: 'http',
    mcp_url: `http://127.0.0.1:${String(replacement.address().port)}/mcp`,
  }]))
  const result = await gateway.call(original.workspace, 'one', 'inspect_run', {})
  assert.equal(result.run_id, 'run:1')
})
