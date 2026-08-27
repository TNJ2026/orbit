import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ORBIT_IDLE_MS, ORBIT_POLL_MS, commandRevision, dotState, goalRuns, isLive, mergeChunks,
  approvalValue, artifactHref, artifactLabel, nextInterval, orderRows, outputText,
  progressOf, promptText, resultOutcome, resultText, stepDotState, summarise, toInterrupts,
  toRow, toStepRow,
} from '../lib/orbit-model.js'

const run = (over = {}) => ({
  run_id: 'r', goal: 'g', workflow_id: 'wf', workflow_version: 1,
  status: 'running', revision: 1, artifact_count: 0,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  interrupts: [], allowed_commands: [], ...over,
})

test('only a settled outcome stops a Run counting as live', () => {
  for (const status of ['completed', 'failed', 'cancelled', 'unknown']) {
    assert.equal(isLive(status), false, status)
  }
  for (const status of ['running', 'queued', 'interrupted']) {
    assert.equal(isLive(status), true, status)
  }
})

test('the cadence follows the work, not the clock', () => {
  assert.equal(nextInterval([toRow(run({ status: 'running' }))]), ORBIT_POLL_MS)
  assert.equal(nextInterval([toRow(run({ status: 'completed' }))]), ORBIT_IDLE_MS)
  assert.equal(nextInterval([]), ORBIT_IDLE_MS, 'an empty Workspace is idle, not urgent')
})

test('running Runs come first, then the most recently touched', () => {
  const rows = orderRows([
    toRow(run({ run_id: 'old-done', status: 'completed', updated_at: '2026-01-01T00:00:00Z' })),
    toRow(run({ run_id: 'new-done', status: 'completed', updated_at: '2026-01-03T00:00:00Z' })),
    toRow(run({ run_id: 'live', status: 'running', updated_at: '2026-01-02T00:00:00Z' })),
  ])
  assert.deepEqual(rows.map(r => r.runId), ['live', 'new-done', 'old-done'])
})

test('the collapsed badge counts what is moving', () => {
  assert.deepEqual(summarise([
    toRow(run({ status: 'running' })), toRow(run({ status: 'completed' })),
  ]), { live: 1, total: 2 })
})

test('a Run with no goal is still identifiable', () => {
  assert.equal(toRow(run({ goal: '', run_id: 'langgraph_run:abc' })).goal, 'langgraph_run:abc')
})

test('a Run uses the catalog name and falls back to its workflow id', () => {
  assert.equal(toRow(run({ workflow_id: 'review' }), 'Code review').workflowName, 'Code review')
  assert.equal(toRow(run({ workflow_id: 'review' })).workflowName, 'review')
})

test('an unresolved outcome is amber, not red', () => {
  // The Runtime deliberately left it open; drawing it as a failure would answer
  // a question nobody has ruled on.
  assert.equal(dotState('unknown'), 'warning')
  assert.equal(dotState('failed'), 'error')
  assert.equal(dotState('cancelled'), 'error')
  assert.equal(dotState('completed'), 'done')
  assert.equal(dotState('running'), 'ongoing')
})

test('step history uses still outcome colours', () => {
  assert.equal(stepDotState('succeeded'), 'success')
  assert.equal(stepDotState('failed'), 'error')
  assert.equal(stepDotState('cancelled'), 'error')
  assert.equal(stepDotState('not_reached'), 'skipped')
  assert.equal(stepDotState('unknown'), 'warning')
  assert.equal(stepDotState('waiting'), 'warning')
  assert.equal(stepDotState('running'), 'ongoing', 'an active step is not one that was skipped')
})

test('a step waiting on a person is distinguished from one merely running', () => {
  const pending = toStepRow({
    node_id: 'n', status: 'unknown',
    resolution: { kind: 'reconciliation_required', delegation_id: 'd' },
  })
  assert.equal(pending.needsPerson, true)
  const settled = toStepRow({
    node_id: 'n', status: 'unknown',
    resolution: { kind: 'reconciliation_required', delegation_id: 'd' },
    reconciliation: { outcome: 'confirmed_succeeded', note: '', created_at: 'x' },
  })
  assert.equal(settled.needsPerson, false, 'an answered question is not still asking')
  assert.equal(toStepRow({ node_id: 'n', status: 'running' }).needsPerson, false)
})

test('a step label falls back to its node id', () => {
  assert.equal(toStepRow({ node_id: 'compute', status: 'running' }).label, 'compute')
  assert.equal(toStepRow({ node_id: 'compute', status: 'running', label: 'Compute totals' }).label, 'Compute totals')
  assert.equal(toStepRow({ node_id: 'compute', status: 'succeeded', has_output: true }).hasOutput, true)
  assert.equal(toStepRow({ node_id: 'compute', status: 'succeeded' }).hasOutput, false)
})

test('output pages merge without duplicating or reordering a chunk', () => {
  const page = (ids) => ids.map(id => ({ chunk_id: id, text: `${String(id)};`, node_id: 'n', attempt_id: 'a', stream: 'stdout', created_at: 'x' }))
  const merged = mergeChunks(page([1, 2]), page([2, 3]))
  assert.deepEqual(merged.map(c => c.chunk_id), [1, 2, 3])
  assert.equal(outputText(merged), '1;2;3;')
  assert.equal(outputText(page([3, 1, 2])), '1;2;3;', 'a page out of order still reads in order')
})

test('a command is offered only at the revision Orbit advertises it for', () => {
  const row = toRow(run({
    revision: 7,
    allowed_commands: [{ command: 'langgraph_run.cancel', expected_version: 7 }],
  }))
  assert.equal(commandRevision(row, 'langgraph_run.cancel'), 7)
  assert.equal(commandRevision(row, 'langgraph_run.resume'), undefined,
    'a command Orbit did not advertise has no revision to act at')
  assert.equal(commandRevision(toRow(run()), 'langgraph_run.cancel'), undefined)
})

test('a step carries the delegation a person would be ruling on', () => {
  assert.equal(toStepRow({
    node_id: 'n', status: 'unknown',
    resolution: { kind: 'reconciliation_required', delegation_id: 'deleg-1' },
  }).delegationId, 'deleg-1')
  assert.equal(toStepRow({ node_id: 'n', status: 'running' }).delegationId, undefined)
})

test('progress counts what finished, not what was reached', () => {
  const steps = [
    { node_id: 'a', label: 'Read', status: 'succeeded' },
    { node_id: 'b', label: 'Translate', status: 'running' },
    { node_id: 'c', label: 'Write', status: 'not_reached' },
    { node_id: 'd', label: 'Done', status: 'not_reached' },
  ]
  assert.deepEqual(progressOf(steps), {
    done: 1, total: 4, current: 'Translate', blocked: false,
  })
})

test('a failed step is not counted as done', () => {
  const steps = [
    { node_id: 'a', status: 'succeeded' },
    { node_id: 'b', status: 'succeeded' },
    { node_id: 'c', status: 'failed' },
  ]
  assert.deepEqual(progressOf(steps), { done: 2, total: 3, blocked: false },
    'two steps produced something; the row\'s red status says the third did not')
})

test('a person answering a step counts as finishing it', () => {
  assert.equal(progressOf([{ node_id: 'a', status: 'answered' }]).done, 1)
})

test('progress names what it is stuck on, and says that it is stuck', () => {
  const waiting = progressOf([
    { node_id: 'a', status: 'succeeded' },
    { node_id: 'b', label: 'Confirm', status: 'waiting' },
  ])
  assert.equal(waiting.current, 'Confirm')
  assert.equal(waiting.blocked, true)
  const unruled = progressOf([{ node_id: 'b', label: 'Subagent', status: 'unknown' }])
  assert.equal(unruled.blocked, true, 'an outcome nobody ruled on is not ordinary progress')
})

test('something working outranks something waiting as the step to name', () => {
  const both = progressOf([
    { node_id: 'a', label: 'Ask', status: 'waiting' },
    { node_id: 'b', label: 'Fetch', status: 'running' },
  ])
  assert.equal(both.current, 'Fetch')
  assert.equal(both.blocked, false)
})

test('a step without a label is named by its node id, as elsewhere', () => {
  assert.equal(progressOf([{ node_id: 'compute', status: 'running' }]).current, 'compute')
})

test('a finished Run is on nothing, and a definitionless Run counts nothing', () => {
  assert.deepEqual(progressOf([{ node_id: 'a', status: 'succeeded' }]),
    { done: 1, total: 1, blocked: false })
  assert.deepEqual(progressOf([]), { done: 0, total: 0, blocked: false })
})


test('the Goal page shows everything that is running', () => {
  const rows = [
    toRow(run({ run_id: 'a', status: 'running', updated_at: '2026-01-02T00:00:00Z' })),
    toRow(run({ run_id: 'done', status: 'completed', updated_at: '2026-01-03T00:00:00Z' })),
    toRow(run({ run_id: 'b', status: 'waiting', updated_at: '2026-01-01T00:00:00Z' })),
  ]
  assert.deepEqual(goalRuns(rows).map(r => r.runId), ['a', 'b'],
    'a finished Run does not join the page while anything is still moving')
})

test('a finished Goal holds the page until the next one starts', () => {
  const finished = [
    toRow(run({ run_id: 'older', status: 'completed', updated_at: '2026-01-01T00:00:00Z' })),
    toRow(run({ run_id: 'last', status: 'failed', updated_at: '2026-01-03T00:00:00Z' })),
    toRow(run({ run_id: 'middle', status: 'cancelled', updated_at: '2026-01-02T00:00:00Z' })),
  ]
  assert.deepEqual(goalRuns(finished).map(r => r.runId), ['last'],
    'the Goal that ended last is the one still worth reading')
  const started = [...finished, toRow(run({ run_id: 'new', status: 'running', updated_at: '2026-01-02T12:00:00Z' }))]
  assert.deepEqual(goalRuns(started).map(r => r.runId), ['new'],
    'the next Goal takes the page from the one that ended')
})

test('a Workspace that has never run anything shows no Goal', () => {
  assert.deepEqual(goalRuns([]), [])
})

test('an unknown outcome still holds the page rather than emptying it', () => {
  // `unknown` is not live — nobody can say whether it is still working — but it
  // is exactly the outcome a reader must be shown rather than have cleared.
  const rows = [toRow(run({ run_id: 'u', status: 'unknown', updated_at: '2026-01-01T00:00:00Z' }))]
  assert.deepEqual(goalRuns(rows).map(r => r.runId), ['u'])
})

test('a Run carries what it produced, and why it failed', () => {
  const done = toRow(run({ status: 'completed', result: 'the answer' }))
  assert.equal(done.result, 'the answer')
  assert.equal(done.error, undefined)
  assert.equal(toRow(run({ status: 'failed', error: 'agent exited 1' })).error, 'agent exited 1')
  assert.equal(toRow(run({ status: 'failed', error: null })).error, undefined,
    'no error is absent, not the string "null"')
  assert.equal(toRow(run({ status: 'failed', error: '' })).error, undefined)
})

test('a result reads as what it is, not as JSON around it', () => {
  assert.equal(resultText('plain text'), 'plain text',
    'a string answer is not shown wrapped in quotes')
  assert.equal(resultText({ translation: 'Hello' }), 'Hello',
    'a one-field container is not the answer; what it holds is')
  assert.equal(resultText(42), '42')
  assert.equal(resultText(true), 'true')
})

test('a result with more than one field is printed rather than guessed at', () => {
  assert.equal(resultText({ a: 'one', b: 'two' }), '{\n  "a": "one",\n  "b": "two"\n}',
    'unwrapping here would hide a field the reader asked for')
  assert.equal(resultText({ count: 3 }), '{\n  "count": 3\n}',
    'only a string is unwrapped: a number alone loses which field it was')
  assert.equal(resultText(['a', 'b']), '[\n  "a",\n  "b"\n]')
})

test('a Run with no result yet shows nothing rather than a promise of one', () => {
  assert.equal(resultText(null), '')
  assert.equal(resultText(undefined), '')
  assert.equal(resultText(''), '')
})

test('a result that cannot be printed still says something', () => {
  const circular = {}
  circular.self = circular
  assert.notEqual(resultText(circular), '', 'a cycle must not blank the answer')
})

test('a Run carries the request behind its goal, not just the label', () => {
  const row = toRow(run({
    goal: 'Translate the given text',
    inputs: { prompt: 'line one\nline two' },
  }))
  assert.equal(row.goal, 'Translate the given text')
  assert.equal(row.prompt, 'line one\nline two',
    'the label is a summary of the request; the reader needs the request')
})

test('a lone string input is the request, shown as it was written', () => {
  assert.equal(promptText({ prompt: 'do the thing' }), 'do the thing')
  assert.equal(promptText({ text: '  keeps  spacing  ' }), '  keeps  spacing  ')
})

test('a workflow taking several inputs has no one of them to single out', () => {
  assert.equal(promptText({ a: 'one', b: 'two' }), '{\n  "a": "one",\n  "b": "two"\n}',
    'singling one out would hide the other')
  assert.equal(promptText({ value: 0 }), '{\n  "value": 0\n}',
    'only a string is unwrapped: a bare 0 loses which input it was')
})

test('a Run started with no inputs shows no request', () => {
  assert.equal(promptText({}), '')
  assert.equal(promptText(undefined), '')
  assert.equal(promptText(null), '')
  assert.equal(promptText('not an input map'), '')
  assert.equal(promptText(['a']), '', 'inputs are named; a list is not an input map')
  assert.equal(toRow(run()).prompt, '', 'a Run whose payload predates inputs still reads')
})

const ART = 'langgraph_artifact:4c1e5281cb5f49d9b60afbbac0508a7c4c2c37022312a116ebd54d703adfecd6'
const ART2 = 'langgraph_artifact:5c0c54117b2f2890789bebd4790991a58c14974f10c7418c4e090ccbab6fe61c'

test('a result that is only an artifact reads as a door, not as a hash', () => {
  // The real shape a file-writing workflow answers with. Printing it put a
  // 64-character hash where the answer should have been.
  const outcome = resultOutcome({ artifact_id: ART })
  assert.deepEqual(outcome.artifacts, [ART])
  assert.equal(outcome.text, '', 'the wrapper around a door is not an answer with a door in it')
})

test('an answer that also produced a file keeps both', () => {
  const outcome = resultOutcome({ summary: 'Translated 8 segments', artifact_id: ART })
  assert.deepEqual(outcome.artifacts, [ART])
  assert.equal(outcome.text, 'Translated 8 segments',
    'dropping either half answers a different question than the one asked')
})

test('artifacts are found wherever the result happens to put them', () => {
  assert.deepEqual(resultOutcome({ files: [ART, ART2] }).artifacts, [ART, ART2])
  assert.deepEqual(resultOutcome([{ out: { artifact_id: ART } }]).artifacts, [ART])
  assert.deepEqual(resultOutcome(ART).artifacts, [ART], 'a bare reference is still one')
  assert.equal(resultOutcome(ART).text, '')
})

test('a result with no artifacts is untouched', () => {
  assert.deepEqual(resultOutcome('plain text'), { artifacts: [], text: 'plain text' })
  assert.deepEqual(resultOutcome({ a: 'one', b: 'two' }).artifacts, [])
  assert.equal(resultOutcome({ a: 'one', b: 'two' }).text, '{\n  "a": "one",\n  "b": "two"\n}')
  assert.deepEqual(resultOutcome(null), { artifacts: [], text: '' })
})

test('something merely shaped like an id is not one', () => {
  for (const near of ['langgraph_artifact', 'langgraph_artifact:', 'artifact:abc',
                      'langgraph_run:abc', 'see langgraph_artifact:abc for the file']) {
    assert.deepEqual(resultOutcome(near).artifacts, [], near)
    assert.equal(resultOutcome(near).text, near, near)
  }
})

test('an artifact is opened through the Host, as the Session that owns it', () => {
  // Not Orbit's own address. Artifacts belong to the actor that produced them,
  // a browser on loopback is `local`, and a Run this panel started belongs to
  // `harness:session:<id>` — so Orbit's URL 404s for every Artifact this
  // Harness ever made, and so does Orbit's own UI.
  const href = artifactHref('session-81b8c40d', ART)
  assert.equal(href.startsWith('/plugins/dsh-orbit/artifact?'), true)
  const query = new URLSearchParams(href.slice(href.indexOf('?') + 1))
  assert.equal(query.get('session'), 'session-81b8c40d')
  assert.equal(query.get('id'), ART, 'the id survives being put through a query string')
  // Nothing to ask on behalf of means no link, rather than one that goes nowhere.
  assert.equal(artifactHref('', ART), '')
  assert.equal(artifactHref('session-81b8c40d', ''), '')
})

test('an artifact is offered under a name short enough to read', () => {
  assert.equal(artifactLabel(ART), '4c1e5281cb5f…')
  assert.equal(artifactLabel('langgraph_artifact:abc'), 'abc')
})

/* The real payload a `task_kind: approval` node parks, from a Run in this
   workspace: the port to answer on and the kind of question are both in it. */
const APPROVAL = {
  id: '15696412042d5df4828d1c173cfac9e5',
  value: {
    node_id: 'review',
    config: { participants: ['local'], quorum: 'any', task_kind: 'approval' },
    output_ports: [{ id: 'result', schema_id: 'schema://object/1.0' }],
    input: { result: { text: '…' } },
  },
}

test('an interrupt carries where to answer and what kind of answer it wants', () => {
  assert.deepEqual(toInterrupts([APPROVAL]), [{
    id: '15696412042d5df4828d1c173cfac9e5',
    nodeId: 'review',
    taskKind: 'approval',
    outputPort: 'result',
  }])
  assert.deepEqual(toRow(run({ interrupts: [APPROVAL] })).interrupts[0].outputPort, 'result')
  assert.deepEqual(toRow(run()).interrupts, [])
})

test('a question with nowhere to put the answer is not offered', () => {
  // Replying without naming a port produces a node that "returned undeclared
  // outputs" minutes later, which is worse than not offering the control.
  const noPort = { id: 'i', value: { node_id: 'n', config: {}, output_ports: [] } }
  assert.deepEqual(toInterrupts([noPort]), [])
  assert.deepEqual(toInterrupts([{ value: APPROVAL.value }]), [], 'nor one with no id')
  assert.deepEqual(toInterrupts([{ id: 'i' }, null, 'nonsense', { id: 'i', value: 3 }]), [])
  assert.deepEqual(toInterrupts(undefined), [])
})

test('a node that wants something other than a yes or no keeps its own kind', () => {
  const other = { ...APPROVAL, value: { ...APPROVAL.value, config: { task_kind: 'form' } } }
  assert.equal(toInterrupts([other])[0].taskKind, 'form')
  const none = { ...APPROVAL, value: { ...APPROVAL.value, config: {} } }
  assert.equal(toInterrupts([none])[0].taskKind, '')
})

test('an answer names the port, because a mapping *is* the outputs', () => {
  // `{decision: …}` on its own would look for a port called `decision`.
  const [asked] = toInterrupts([APPROVAL])
  assert.deepEqual(approvalValue(asked, 'approve'), { result: { decision: 'approve' } })
  assert.deepEqual(approvalValue(asked, 'reject'), { result: { decision: 'reject' } })
  // And `decision` is the field the branches test: every approval workflow
  // here routes on `source.<port>.decision == "approve"`.
  assert.deepEqual(
    approvalValue({ id: 'x', nodeId: 'n', taskKind: 'approval', outputPort: 'confirmation' }, 'approve'),
    { confirmation: { decision: 'approve' } },
  )
})
