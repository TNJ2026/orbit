import assert from 'node:assert/strict'
import test from 'node:test'

import { delegationRefusal } from '../lib/delegation-policy.js'

const workspace = (isolationMode = 'shared') => ({
  id: 'workspace:1', canonicalPath: '/tmp/workspace', isolationMode,
})
const delegation = (provider, config = {}) => ({
  delegation_id: `delegation:${provider}`,
  status: 'leased', cancel_requested: false,
  request: { input: { task: 'test' }, config: { provider, ...config } },
})

test('Codex, Claude Code, and ACP use the same registered-provider contract', () => {
  const providers = ['codex', 'claude-code', 'acp']
  for (const provider of providers) {
    assert.equal(delegationRefusal(
      workspace(), delegation(provider), providers,
    ), undefined)
  }
  assert.match(
    delegationRefusal(workspace(), delegation('missing'), providers),
    /not registered/,
  )
})

test('write, isolation, and concurrency policies fail before provider start', () => {
  const providers = ['codex']
  assert.match(delegationRefusal(
    workspace('shared'), delegation('codex', { effects: 'write' }), providers,
  ), /write delegation refused/)
  assert.match(delegationRefusal(
    workspace('exclusive'), delegation('codex'), providers,
  ), /isolation mismatch/)
  assert.match(delegationRefusal(
    workspace(), delegation('codex', { max_concurrency: 2 }), providers,
  ), /max_concurrency=1/)
  assert.equal(delegationRefusal(
    workspace('worktree'), delegation('codex', {
      effects: 'write', isolation_mode: 'worktree', max_concurrency: 1,
    }), providers,
  ), undefined)
})
