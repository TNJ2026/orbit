import { appendFileSync, readFileSync } from 'node:fs'
import { createInterface } from 'node:readline'

appendFileSync('starts.log', 'start\n')
const mode = readFileSync('fixture-mode', 'utf8').trim()
const output = message => process.stdout.write(`${JSON.stringify(message)}\n`)

createInterface({ input: process.stdin }).on('line', line => {
  const request = JSON.parse(line)
  if (request.method === 'initialize') {
    output({ jsonrpc: '2.0', id: request.id, result: { protocolVersion: '2025-06-18' } })
    return
  }
  const name = request.params?.name
  if (name === 'get_capabilities') {
    output({ jsonrpc: '2.0', id: request.id, result: {
      structuredContent: { integration_protocol: mode === 'incompatible' ? 'other/9' : 'orbit-harness/1' },
    } })
    return
  }
  if (mode === 'malformed') { process.stdout.write('not-json\n'); return }
  if (mode === 'exit') { process.exit(23) }
  if (name === 'inspect_run') {
    output({ jsonrpc: '2.0', id: request.id, result: { structuredContent: {
      run_id: 'run:1', goal: 'fixture', workflow_id: 'workflow:1',
      workflow_version: 1, status: 'succeeded', revision: 1,
      artifact_count: 0, created_at: 'now', updated_at: 'now',
      interrupts: [], allowed_commands: [],
    } } })
    return
  }
  output({ jsonrpc: '2.0', id: request.id, result: {
    structuredContent: { name, ok: true },
  } })
})
