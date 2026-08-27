import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { chmod, mkdtemp, readFile, realpath, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const server = join(here, '..', 'lib', 'main.js')

/** A Runtime that answers MCP and records what it was asked. */
async function fakeOrbit() {
  const seen = []
  const http = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8')
    request.on('data', chunk => { body += chunk })
    request.on('end', () => {
      const message = JSON.parse(body)
      seen.push({ message, actor: request.headers['x-orbit-actor'] })
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({
        jsonrpc: '2.0',
        id: message.id,
        result: message.method === 'initialize'
          ? { protocolVersion: '2025-06-18', capabilities: {} }
          : { structuredContent: { integration_protocol: 'orbit-harness/1', echoed: message.method } },
      }))
    })
  })
  await new Promise(resolve => http.listen(0, '127.0.0.1', resolve))
  return { http, seen, port: http.address().port }
}

/** A project whose `orbit runtimes --json` already names that Runtime, so the
 *  server discovers rather than starts one. */
async function project(port) {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'claude-orbit-e2e-')))
  const base = `http://127.0.0.1:${String(port)}`
  await writeFile(join(path, 'runtime.json'), JSON.stringify([{
    project_root: path, transport: 'http', base_url: base, mcp_url: `${base}/mcp`,
  }]))
  const fake = join(path, 'orbit')
  await writeFile(fake, `#!/usr/bin/env node
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
if (process.argv.slice(2).slice(-2).join(' ') === 'runtimes --json') {
  process.stdout.write(await readFile(join(process.cwd(), 'runtime.json'), 'utf8'))
} else { process.exit(64) }
`)
  await chmod(fake, 0o755)
  return { path, fake }
}

/** Drive the built server the way a client does: a child process, one JSON
 *  message per line on stdin, replies on stdout. */
async function converse(args, lines, cwd) {
  const child = spawn(process.execPath, [server, ...args], { cwd, stdio: ['pipe', 'pipe', 'pipe'] })
  let out = '', err = ''
  child.stdout.setEncoding('utf8'); child.stdout.on('data', c => { out += c })
  child.stderr.setEncoding('utf8'); child.stderr.on('data', c => { err += c })
  child.stdin.end(lines.map(line => `${JSON.stringify(line)}\n`).join(''))
  const code = await new Promise(resolve => child.once('exit', resolve))
  return {
    code, err,
    replies: out.split('\n').filter(Boolean).map(line => JSON.parse(line)),
  }
}

test('a client talks to Orbit through it, over real stdio', async t => {
  const orbit = await fakeOrbit()
  t.after(() => orbit.http.close())
  const { path, fake } = await project(orbit.port)

  const { replies, err, code } = await converse(
    ['--project-root', path, '--orbit-command', fake, '--actor', 'claude-test'],
    [
      // Ids well clear of the Gateway's own: it speaks to the same Runtime to
      // decide whether it is usable, and its handshake is `initialize` too.
      { jsonrpc: '2.0', id: 1001, method: 'initialize', params: {} },
      { jsonrpc: '2.0', method: 'notifications/initialized' },
      { jsonrpc: '2.0', id: 1002, method: 'tools/list', params: {} },
    ],
    path,
  )
  assert.equal(code, 0)
  assert.match(err, /serving/)

  // Two requests, two replies — and nothing for the notification, which asked
  // for nothing.
  assert.equal(replies.length, 2, `expected two replies, got ${JSON.stringify(replies)}`)
  assert.deepEqual(replies.map(r => r.id), [1001, 1002])
  assert.equal(replies[1].result.structuredContent.echoed, 'tools/list')

  // Everything the client sent is attributed. Orbit scopes writes by actor,
  // and an unattributed caller is indistinguishable from a person working in
  // the same project at a terminal.
  const mine = (m) => m.id === 1001 || m.id === 1002
    || m.method === 'notifications/initialized'
  const forwarded = orbit.seen.filter(s => mine(s.message))
  assert.equal(forwarded.length, 3, 'a message the client sent did not arrive')
  assert.deepEqual([...new Set(forwarded.map(s => s.actor))], ['claude-test'])
  // The notification reached Orbit even though nothing was sent back.
  assert.ok(forwarded.some(s => s.message.method === 'notifications/initialized'))

  // The Gateway's own handshake is not attributed, and does not need to be: it
  // reads capabilities to decide whether this Runtime is usable at all, before
  // there is a caller to credit. Asserted so the difference is deliberate
  // rather than discovered again later.
  const handshake = orbit.seen.filter(s => !mine(s.message))
  assert.ok(handshake.length > 0)
  assert.deepEqual([...new Set(handshake.map(s => s.actor))], [undefined])
})

/**
 * The failure a person meets first, and the one that used to be silent: no
 * Runtime, and no way to start one.
 */
test('a Runtime it cannot reach becomes a sentence the client can read', async () => {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'claude-orbit-down-')))
  await writeFile(join(path, 'runtime.json'), '[]')
  const fake = join(path, 'orbit')
  await writeFile(fake, `#!/usr/bin/env node
if (process.argv.slice(2).slice(-2).join(' ') === 'runtimes --json') { process.stdout.write('[]') }
else { process.stderr.write('RuntimeError: no database here\\n'); process.exit(3) }
`)
  await chmod(fake, 0o755)
  const { replies, code } = await converse(
    ['--project-root', path, '--orbit-command', fake],
    [{ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }],
    path,
  )
  assert.equal(code, 0, 'the server died instead of answering')
  assert.equal(replies.length, 1)
  assert.equal(replies[0].id, 1)
  // A sentence, not a stack trace…
  assert.match(replies[0].error.message, /would not start|not serving/i)
  assert.doesNotMatch(replies[0].error.message, /^err[A-Z]/)
  // …with the Runtime's own last words kept for whoever has to fix it.
  assert.match(replies[0].error.data.detail, /RuntimeError: no database here/)
})

test('a line that is not a message is answered, not fatal', async () => {
  const orbit = await fakeOrbit()
  const { path, fake } = await project(orbit.port)
  const child = spawn(process.execPath,
    [server, '--project-root', path, '--orbit-command', fake],
    { cwd: path, stdio: ['pipe', 'pipe', 'pipe'] })
  let out = ''
  child.stdout.setEncoding('utf8'); child.stdout.on('data', c => { out += c })
  child.stdin.write('{not json\n')
  child.stdin.end(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' })}\n`)
  const code = await new Promise(resolve => child.once('exit', resolve))
  orbit.http.close()
  assert.equal(code, 0)
  const replies = out.split('\n').filter(Boolean).map(line => JSON.parse(line))
  assert.equal(replies[0].error.code, -32700)
  assert.equal(replies[0].id, null, 'a parse failure has no id to answer to')
  // And the session carried on: the next message was still served.
  assert.equal(replies[1].id, 2)
})

/**
 * A Runtime this server starts offers the full tool set.
 *
 * The profile belongs to the Runtime, and whoever starts it decides for
 * everyone who connects later. The Harness starts `harness` Runtimes because
 * that is the surface its own model needs; a general-purpose client has no use
 * for the authoring claim loop and every use for the tools that subset leaves
 * out. Checked on the spawn line rather than on the constructor call, because
 * the flag is what the Runtime actually reads.
 */
test('a Runtime it starts is asked for the full tool set', async t => {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'claude-orbit-profile-')))
  await writeFile(join(path, 'runtime.json'), '[]')
  const fake = join(path, 'orbit')
  await writeFile(fake, `#!/usr/bin/env node
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
const args = process.argv.slice(2)
if (args.slice(-2).join(' ') === 'runtimes --json') { process.stdout.write('[]') }
else {
  await writeFile(join(process.cwd(), 'spawned.argv'), args.join('\\n'))
  process.stderr.write('RuntimeError: stopping here\\n')
  process.exit(3)
}
`)
  await chmod(fake, 0o755)

  await converse(['--project-root', path, '--orbit-command', fake],
    [{ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }], path)

  const spawned = (await readFile(join(path, 'spawned.argv'), 'utf8')).split('\n')
  assert.ok(spawned.includes('serve'), `it never tried to start one: ${spawned.join(' ')}`)
  assert.ok(spawned.includes('--mcp-tool-profile'))
  assert.equal(spawned[spawned.indexOf('--mcp-tool-profile') + 1], 'full')
  assert.ok(!spawned.includes('harness'))
})
