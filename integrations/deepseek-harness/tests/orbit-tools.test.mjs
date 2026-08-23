import assert from 'node:assert/strict'
import test from 'node:test'

import { OrbitToolBridge } from '../lib/orbit-tools.js'

function fixture() {
  const definitions = new Map(), calls = []
  const tools = { register(definition) { definitions.set(definition.name, definition); return () => definitions.delete(definition.name) } }
  const workspaceRegistry = { async resolveByPath(path) { return { id: 'workspace:1', path } } }
  const ctx = { get(name) { return name === 'tools' ? tools : workspaceRegistry } }
  const gateway = {
    async call(workspace, sessionId, name, args) {
      calls.push({ workspace, sessionId, name, args })
      return name === 'start_run' ? { run_id: 'run:1' } : { ok: true }
    },
    async run(workspace, sessionId, runId) {
      calls.push({ workspace, sessionId, name: 'inspect_run', args: { run_id: runId } })
      return { run_id: runId, allowed_commands: [{ command: 'langgraph_run.cancel', expected_version: 7 }] }
    },
  }
  new OrbitToolBridge(ctx, gateway).register()
  const exec = { signal: new AbortController().signal, agent: { session: { id: 'session:abc', header: { cwd: '/workspace' } } } }
  return { definitions, calls, exec }
}

test('registers the bounded Orbit MCP tool surface', () => {
  const { definitions } = fixture()
  assert.deepEqual([...definitions.keys()], [
    'orbit_list_workflows', 'orbit_list_runs', 'orbit_inspect_run',
    'orbit_start_run', 'orbit_cancel_run', 'orbit_resume_run',
  ])
})

test('start routes through Session Workspace and owns idempotency', async () => {
  const { definitions, calls, exec } = fixture()
  const result = await definitions.get('orbit_start_run').execute({ workflow_id: 'wf', goal: 'ship it' }, exec)
  assert.equal(result.run_id, 'run:1')
  assert.equal(calls[0].workspace.id, 'workspace:1')
  assert.equal(calls[0].sessionId, 'session:abc')
  assert.equal(calls[0].name, 'start_run')
  assert.equal(calls[0].args.wait, false)
  assert.match(calls[0].args.idempotency_key, /^[0-9a-f-]{36}$/)
})

test('cancel re-reads advertised command and revision before mutation', async () => {
  const { definitions, calls, exec } = fixture()
  await definitions.get('orbit_cancel_run').execute({ run_id: 'run:1' }, exec)
  assert.deepEqual(calls.map(call => call.name), ['inspect_run', 'cancel_run'])
  assert.equal(calls[1].args.expected_version, 7)
  assert.match(calls[1].args.idempotency_key, /^[0-9a-f-]{36}$/)
})

test('tool execution refuses calls without a live Agent Session cwd', async () => {
  const { definitions, exec } = fixture()
  await assert.rejects(definitions.get('orbit_list_runs').execute({}, { ...exec, agent: undefined }), /live Harness Agent Session/)
  await assert.rejects(definitions.get('orbit_list_runs').execute({}, { ...exec, agent: { session: { id: 'x', header: {} } } }), /Workspace cwd/)
})
