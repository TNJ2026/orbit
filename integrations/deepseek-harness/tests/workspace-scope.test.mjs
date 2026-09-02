import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const source = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')

/**
 * A caller sends a Workspace with every Host API call, and a caller is not an
 * authority on which directory a Session belongs to. Every Gateway call must
 * therefore travel on a Workspace this Host derived and checked itself.
 *
 * Asserted against the source rather than through the service, because what
 * needs guarding is not one call's behaviour but the absence of an exception:
 * a method added later that forgets. A behavioural test can only cover the
 * methods somebody remembered to list.
 */
test('every Gateway call travels on a verified Workspace', () => {
  const calls = [...source.matchAll(/this\.gateway\.([A-Za-z]+)\(\s*([A-Za-z_.]+)/g)]
  assert.ok(calls.length >= 20, `expected the Gateway call sites, found ${String(calls.length)}`)
  const unverified = calls
    .map(([, method, argument]) => `gateway.${method}(${argument})`)
    .filter(call => !call.endsWith('(scope)'))
  assert.deepEqual(unverified, [], `these Gateway calls do not use a verified Workspace: ${unverified.join(', ')}`)
})

test('a Workspace becomes a scope only through a derivation this Host owns', () => {
  // Every producer must end at the Session store or the Workspace registry,
  // never at the caller's object. Matched openly rather than against a list of
  // known names, so a new producer has to be added here deliberately.
  // Destructured producers count too: `const { scope, live } = await this.x()`
  // is the same claim, and matching only the plain form would let a new one in
  // without this test noticing.
  const producers = [
    ...source.matchAll(/const (?:scope|\{ scope[^}]*\}) = await this\.([A-Za-z]+)\(/g),
  ].map(([, name]) => name)
  assert.ok(producers.length > 0)
  assert.deepEqual(
    [...new Set(producers)].sort(),
    ['registered', 'sessionScope', 'sessionWorkspace', 'verified'],
  )
})

test('read-only panel state can resolve a persisted Harness Session', () => {
  const resolver = source.slice(
    source.indexOf('private async sessionWorkspace('),
    source.indexOf('private liveSession('),
  )
  assert.match(resolver, /allowPersisted = false/)
  assert.match(resolver, /this\.workspaceRegistry\.list\(\)/)
  assert.match(resolver, /workspace\.sessionIds\.some/)

  const panel = source.slice(
    source.indexOf("@Remote('getPanelState')"),
    source.indexOf("@Remote('getRunDetail')"),
  )
  assert.match(panel, /sessionScope\(sessionId, true\)/)
  // Reading from the durable registry is a projection; starting a process is
  // not. `startIfMissing` says the panel was opened deliberately, and only a
  // live Session is evidence that anyone did so this turn.
  assert.match(panel, /acquire\(scope, startIfMissing && live\)/,
    'a Workspace no live Session vouched for must not launch a Runtime')

  // The reads the panel drives survive the same window it does. Otherwise the
  // list paints and every row in it throws when opened.
  const bodyOf = (remote) => {
    const start = source.indexOf(`@Remote('${remote}')`)
    assert.notEqual(start, -1, `${remote} is gone; this rule needs revisiting`)
    const next = source.indexOf("@Remote('", start + 10)
    return source.slice(start, next === -1 ? source.length : next)
  }
  for (const remote of [
    'getRunDetail', 'getWorkflowDefinition', 'getStepOutput',
    'getAuthoringOutput', 'readArtifactText',
  ]) {
    assert.match(
      bodyOf(remote), /sessionWorkspace\(sessionId, true\)/,
      `${remote} is a read the panel makes; it must resolve a persisted Session`,
    )
  }
  // `exportArtifact` is on this side of the line: it writes a file into the
  // person's Downloads folder, which is an act on the machine rather than a
  // projection of what Orbit holds.
  for (const remote of [
    'runCommand', 'reconcileStep', 'stopRuntime', 'exportArtifact',
  ]) {
    assert.match(
      bodyOf(remote), /sessionWorkspace\(sessionId\)[^,]/,
      `${remote} mutates; it must still require a live Session`,
    )
  }

  const verified = source.slice(
    source.indexOf('private async verified('),
    source.indexOf('private async registered('),
  )
  assert.match(verified, /liveSession\(sessionId\)/,
    'caller-supplied Workspace mutations must still require a live Session')
})
