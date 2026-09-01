import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, readFile, realpath, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { OrbitGateway } from '../lib/gateway.js'

const fixture = fileURLToPath(new URL('./fixtures/orbit-mcp-fixture.mjs', import.meta.url))

async function target(mode, handler) {
  const path = await realpath(await mkdtemp(join(tmpdir(), `orbit-gateway-${mode}-`)))
  let calls = 0, lastActor, lastMessage
  const server = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8'); request.on('data', chunk => { body += chunk })
    request.on('end', () => {
      calls++
      lastActor = request.headers['x-orbit-actor']
      const message = JSON.parse(body)
      lastMessage = message
      const result = handler(message)
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
    })
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  await writeFile(join(path, 'runtime.json'), JSON.stringify([{
    project_root: path, transport: 'http', mcp_url: `http://127.0.0.1:${String(address.port)}/mcp`,
    base_url: `http://127.0.0.1:${String(address.port)}`,
  }]))
  return { workspace: { id: `workspace:${mode}`, canonicalPath: path }, server,
    hubUrl: `http://127.0.0.1:${String(address.port)}`,
    calls: () => calls, lastActor: () => lastActor, lastMessage: () => lastMessage }
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

test('concurrent sessions reuse one Hub workspace connection', async t => {
  const runtime = await target('compatible', mcp())
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture], globalThis.fetch, undefined, runtime.hubUrl)
  const [releaseOne, releaseTwo] = await Promise.all([
    gateway.acquire(runtime.workspace), gateway.acquire(runtime.workspace),
  ])
  assert.equal((await gateway.call(runtime.workspace, 'one', 'inspect_run', {})).run_id, 'run:1')
  assert.equal(runtime.calls(), 3)
  assert.equal(runtime.lastActor(), 'harness:session:one')
  assert.deepEqual(runtime.lastMessage().params._meta['orbit/workspace'], {
    id: 'workspace:compatible', canonicalPath: runtime.workspace.canonicalPath,
  })
  assert.equal(gateway.diagnostics().discoveryAttempts, 2)
  assert.equal(gateway.diagnostics().rpcCalls, 3)
  assert.equal(gateway.diagnostics().transportFailures, 0)
  await releaseOne(); await releaseTwo()
  assert.equal(runtime.server.listening, true)
})

test('generateAndRunGoal drives authoring and execution over MCP without a UI', async t => {
  let inspected = 0
  const run = status => ({
    run_id: 'run:generated', goal: 'do the work', workflow_id: 'workflow:generated',
    workflow_version: 1, status, revision: inspected, artifact_count: 1,
    created_at: 'now', updated_at: 'now', interrupts: [], allowed_commands: [],
  })
  const runtime = await target('generate-and-run', message => {
    if (message.method === 'initialize') return { protocolVersion: '2025-06-18', capabilities: {} }
    const name = message.params.name
    if (name === 'get_capabilities') return { structuredContent: { integration_protocol: 'orbit-harness/1' } }
    if (name === 'generate_workflow') return { structuredContent: {
      job_id: 'authoring_job:1', type: 'generate', workflow_id: 'workflow:generated',
      prompt: 'make a workflow', status: 'queued', created_at: 'now', updated_at: 'now',
    } }
    if (name === 'get_authoring_job') return { structuredContent: {
      job_id: 'authoring_job:1', type: 'generate', workflow_id: 'workflow:generated',
      prompt: 'make a workflow', status: 'done', created_at: 'now', updated_at: 'now',
    } }
    if (name === 'start_run') return { structuredContent: run('running') }
    if (name === 'inspect_run') return { structuredContent: run(++inspected > 1 ? 'completed' : 'running') }
    throw new Error(`unexpected tool ${String(name)}`)
  })
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture], globalThis.fetch, undefined, runtime.hubUrl)
  const result = await gateway.generateAndRunGoal(
    runtime.workspace, 'session', 'make a workflow', 'do the work', { text: 'input' },
    { agent: 'antigravity', pollMs: 1 },
  )
  assert.equal(result.workflow.workflow_id, 'workflow:generated')
  assert.equal(result.run.status, 'completed')
  assert.equal(runtime.lastMessage().params.name, 'inspect_run')
})

test('an incompatible Runtime behind the Hub fails readiness', async t => {
  const runtime = await target('incompatible', mcp('other/9'))
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture], globalThis.fetch, undefined, runtime.hubUrl)
  await assert.rejects(gateway.acquire(runtime.workspace), /incompatible Orbit integration protocol/)
})

test.skip('legacy direct Runtime auto-start is replaced by Hub auto-start', async t => {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'orbit-gateway-missing-')))
  await writeFile(join(path, 'runtime.json'), '[]')
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const workspace = { id: 'missing', canonicalPath: path }
  await assert.rejects(gateway.acquire(workspace), /No independent Orbit Runtime/)
  const release = await gateway.acquire(workspace, true)
  const pid = Number(await readFile(join(path, 'spawned.pid'), 'utf8'))
  t.after(() => { try { process.kill(pid, 'SIGTERM') } catch {} })
  assert.ok(pid > 0)
  assert.ok(gateway.diagnostics().discoveryAttempts >= 2)
  await release()
  process.kill(pid, 0)
})

/**
 * A Runtime that dies on the way up has said why, and it used to be discarded.
 *
 * Auto-start spawned with `stdio: 'ignore'`, so a failed start reached the
 * panel as an exit code and nothing else — and the panel, having no reading
 * for it, said "something went wrong". The reason existed the whole time; it
 * was written to a file descriptor pointed at nowhere.
 *
 * stderr goes to a file rather than a pipe on purpose: this child is detached
 * and outlives the Host, so nothing would be draining a pipe afterwards.
 */
test.skip('legacy direct Runtime startup diagnostics are replaced by Hub diagnostics', async t => {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'orbit-gateway-refuses-')))
  await writeFile(join(path, 'runtime.json'), '[]')
  await writeFile(join(path, 'refuse-to-start'), 'yes')
  const gateway = new OrbitGateway(process.execPath, [fixture])
  await assert.rejects(
    gateway.acquire({ id: 'refuses', canonicalPath: path }, true),
    (error) => {
      // The exit code, which was all there used to be…
      assert.match(error.message, /auto-start failed with code 3/)
      // …and the sentence that says what to do about it.
      assert.match(error.message, /RuntimeError: the database is from a newer Orbit/)
      return true
    })
})

/**
 * Two Workspaces failing at once must not read each other's reason.
 *
 * The log is named for the Workspace and truncated per attempt. Sharing one
 * path would be worse than discarding the output: a reader would be handed a
 * confident explanation belonging to a different project.
 */
test.skip('legacy per-Runtime startup logs are replaced by Hub logs', async () => {
  const make = async (tag) => {
    const path = await realpath(await mkdtemp(join(tmpdir(), `orbit-gateway-${tag}-`)))
    await writeFile(join(path, 'runtime.json'), '[]')
    await writeFile(join(path, 'refuse-to-start'), 'yes')
    return path
  }
  const [one, two] = await Promise.all([make('one'), make('two')])
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const reasons = await Promise.all([one, two].map(async (path) => {
    try {
      await gateway.acquire({ id: path, canonicalPath: path }, true)
      throw new Error('expected the start to fail')
    } catch (error) { return String(error.message) }
  }))
  for (const [index, path] of [one, two].entries()) {
    assert.match(reasons[index], new RegExp(`newer Orbit at ${path}`),
      'read a reason belonging to another Workspace')
  }
})

test.skip('legacy direct MCP readiness is replaced by Hub readiness', async t => {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'orbit-gateway-starting-')))
  const reservation = createServer()
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise(resolve => reservation.close(resolve))
  await writeFile(join(path, 'runtime.json'), JSON.stringify([{
    project_root: path, transport: 'http',
    mcp_url: `http://127.0.0.1:${String(port)}/mcp`,
  }]))
  const server = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8'); request.on('data', chunk => { body += chunk })
    request.on('end', () => {
      const message = JSON.parse(body), result = mcp()(message)
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
    })
  })
  t.after(() => server.close())
  setTimeout(() => server.listen(port, '127.0.0.1'), 200)
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const release = await gateway.acquire({ id: 'starting', canonicalPath: path }, true)
  assert.ok(gateway.diagnostics().discoveryAttempts >= 2)
  await release()
})

test('an empty or malformed Session id never becomes an actor header', async t => {
  const runtime = await target('invalid-session', mcp())
  t.after(() => runtime.server.close())
  const gateway = new OrbitGateway(process.execPath, [fixture])
  await assert.rejects(gateway.call(runtime.workspace, '', 'inspect_run', {}), /invalid Harness session id/)
  await assert.rejects(gateway.call(runtime.workspace, 'bad/session', 'inspect_run', {}), /invalid Harness session id/)
  assert.equal(runtime.lastActor(), undefined)
})

test.skip('Hub transport recovery is covered by the Hub end-to-end test', async t => {
  const original = await target('restart', mcp())
  const gateway = new OrbitGateway(process.execPath, [fixture])
  const release = await gateway.acquire(original.workspace)
  await release()
  await new Promise(resolve => original.server.close(resolve))
  await assert.rejects(gateway.call(original.workspace, 'one', 'list_runs', {}), /transport failed/)
  assert.equal(gateway.diagnostics().transportFailures, 1)
  assert.match(gateway.diagnostics().lastTransportError, /transport failed/)

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
  assert.equal(gateway.diagnostics().discoveryAttempts, 2)
  assert.equal(gateway.diagnostics().lastTransportError, undefined)
})
