import { createServer } from 'node:http'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const args = process.argv.slice(2)
if (args.slice(-2).join(' ') === 'runtimes --json') {
  process.stdout.write(await readFile(join(process.cwd(), 'runtime.json'), 'utf8'))
} else if (args[0] === 'serve') {
  const projectAt = args.indexOf('--project-root')
  const projectRoot = projectAt < 0 ? process.cwd() : args[projectAt + 1]
  // A Runtime that dies on the way up, for the tests that are about what it
  // said while dying. Keyed on a file so the same fixture serves both.
  if (await readFile(join(projectRoot, 'refuse-to-start'), 'utf8').catch(() => '') !== '') {
    process.stderr.write('Traceback (most recent call last):\n')
    // Names its own Workspace, so a test can tell whose reason it read.
    process.stderr.write(`RuntimeError: the database is from a newer Orbit at ${projectRoot}\n`)
    process.exit(3)
  }
  const server = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8'); request.on('data', chunk => { body += chunk })
    request.on('end', () => {
      const message = JSON.parse(body)
      const result = message.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: {} }
        : { structuredContent: { integration_protocol: 'orbit-harness/1' } }
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
    })
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${String(server.address().port)}`
  await writeFile(join(projectRoot, 'runtime.json'), JSON.stringify([{
    project_root: projectRoot, transport: 'http', base_url: base, mcp_url: `${base}/mcp`,
  }]))
  await writeFile(join(projectRoot, 'spawned.pid'), String(process.pid))
  const stop = () => server.close(() => process.exit(0))
  process.on('SIGTERM', stop); process.on('SIGINT', stop)
} else {
  process.exit(64)
}
