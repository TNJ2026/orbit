import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const clientDir = join(here, '..', 'src', 'client')
const names = (await readdir(clientDir)).filter(name => /\.(ts|tsx)$/.test(name))
const sources = await Promise.all(names.map(name => readFile(join(clientDir, name), 'utf8')))
const code = sources.join('\n').split('\n')
  .filter(line => !line.trimStart().startsWith('*')).join('\n')

/**
 * The panel says what is running and links into Orbit for the rest.
 *
 * That boundary is the whole reason this module is 500 lines instead of the
 * 685-line duplicate it replaced: Orbit already draws graphs, Artifacts and
 * Workflow authoring, and a second drawing of them is a second answer to the
 * same question. Reading their data here is how a panel becomes that duplicate,
 * so reading their data is what this forbids.
 */
test('the deep surfaces stay in Orbit', () => {
  for (const elsewhere of [
    'getGraph', 'getEdges', 'listArtifacts', 'getArtifactContent', 'importArtifact',
    'generateWorkflow', 'modifyWorkflow', 'getAuthoringJob',
  ]) {
    assert.equal(code.includes(elsewhere), false, `${elsewhere} belongs to Orbit's own UI`)
  }
})

test('every Host call is one the panel can name a reason for', async () => {
  // Matched against the Host's own dispatch table rather than against a call
  // spelling. The first version of this looked for `hostCall(` and passed
  // vacuously the moment a row component received that function under another
  // name — a guard that reads the caller can always be renamed out of.
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const dispatchable = [...host.matchAll(/case '([A-Za-z]+)':/g)].map(([, name]) => name)
  assert.ok(dispatchable.length > 10, 'the Host dispatch table was not found')
  // A copy key that happens to share a Host action's name would read as a call
  // here; they are kept distinct rather than excused, so this stays a plain
  // membership test.
  const used = dispatchable.filter(action => new RegExp(`'${action}'`).test(code))
  assert.deepEqual(used.sort(), [
    'getPanelState', 'getRunDetail', 'getStepOutput', 'reconcileStep', 'runCommand',
  ])
})

test('the panel is a Harness surface, not one with its own palette', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const hardcoded = [...css.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map(([hit]) => hit)
  assert.deepEqual(hardcoded, [], 'colours belong to the --dsw-alias-* tokens')
  assert.match(css, /--dsw-alias-/)
})

test('the panel reads its Session from the store the slot actually hands over', () => {
  // `shell.overlay` is root-scoped and passes the session *store*, never an id.
  // A `sessionId` prop typechecks, arrives undefined forever, and leaves the
  // panel permanently empty — which is exactly how it shipped once.
  assert.match(code, /useSessions\(state => state\.current\)/)
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  assert.equal(/sessionId\?: string\s*\}/.test(panel), false, 'a prop the slot never sends is back')
})

test('the title area drags, and only its controls do not', () => {
  // setPointerCapture redirects every following pointer event to the captor, so
  // a press that started on a control never becomes a click. Excluding controls
  // is enough; excluding everything but the bar's own background — the first
  // attempt — left the title text, the obvious handle, inert.
  assert.match(code, /closest\('button, a, input'\)/)
  assert.equal(/event\.target !== event\.currentTarget/.test(code), false)
})

test('every border names a colour that survives an unfamiliar theme', async () => {
  // An undefined custom property invalidates the whole declaration, not just
  // its colour: a divider written without a fallback is simply not drawn.
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const bare = [...css.matchAll(/var\(--dsw-alias-[a-z0-9-]+\)/g)]
    .map(([hit]) => hit)
    .filter(hit => /line|bg-/.test(hit))
  assert.deepEqual(bare, [], 'a surface or border token needs a fallback')
})

test('bar controls are glyphs, not buttons with a surface of their own', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const rule = css.slice(css.indexOf('.iconButton {'), css.indexOf('}', css.indexOf('.iconButton {')))
  assert.match(rule, /background:\s*none/)
  assert.match(rule, /border:\s*0/)
})

test('the deep surfaces are reachable, in Orbit rather than redrawn here', () => {
  // An anchor, not a scripted open: the browser's own new-tab behaviour is the
  // behaviour a person expects from something that leaves the page, and a
  // pop-up blocker never gets to decide whether the press counted.
  assert.match(code, /href=\{uiUrl\}/)
  assert.match(code, /target="_blank"/)
  assert.match(code, /rel="noopener"/)
})

test('the panel carries the Runtime\'s own four pages, in its order', () => {
  // Goal, Workflows, History, Agents — what is running, what could, what did,
  // and who by. Reading them off Orbit rather than inventing a fifth keeps one
  // vocabulary between the two surfaces.
  const strip = code.slice(code.indexOf('styles.tabs'), code.indexOf('styles.body'))
  assert.deepEqual(
    [...strip.matchAll(/'(goal|workflows|history|agents)'/g)].map(([, name]) => name),
    ['goal', 'workflows', 'history', 'agents'],
  )
})

test('the Workflows and Agents pages name things and offer no action', () => {
  // Naming them answers "what can I ask for". Starting one here would answer a
  // different question wrongly: the Agent would not know the Run exists.
  const listing = code.slice(code.indexOf("tab === 'workflows'"), code.indexOf('styles.resize'))
  assert.equal(/onClick/.test(listing), false, 'a listing grew an action')
  assert.equal(/runCommand|start_run/.test(listing), false)
})


test('the Workflow list is the shell\'s own popup, not one built here', () => {
  // `/model` registers a `popupSelect` contribution and the shell owns the
  // list, its search box and its empty state. Building a second one over the
  // trigger menu meant reproducing all three — and the trigger pipeline will
  // not reopen a menu from a pick, so it could never behave like this one.
  assert.match(code, /commandUi\.register/)
  assert.match(code, /kind: 'popupSelect'/)
  assert.match(code, /options: async \(session, signal\)/)
})

test('selecting a Workflow writes the request, it does not start one', () => {
  // The Run has to be the Agent's or it cannot report on it afterwards — and a
  // popupSelect has nowhere to put the goal these Workflows declare an input
  // for, so the sentence is left for the person to finish.
  const select = code.slice(code.indexOf('onSelect: (option, session)'), code.indexOf("}, 'orbit: workflow popup'"))
  assert.match(select, /insertReference/)
  assert.equal(/start_run|runCommand|window\.open/.test(select), false, 'the popup grew a launcher')
})

test('a reference that cannot be inserted falls back to a readable sentence', () => {
  // insertReference is span-CAS'd and refuses silently; a sentence with a hole
  // where the Workflow should be is worse than a plain one.
  const select = code.slice(code.indexOf('onSelect: (option, session)'), code.indexOf("}, 'orbit: workflow popup'"))
  assert.match(select, /if \(!inserted\)/)
  assert.match(select, /「\$\{option\.label\}」/)
})

test('the source that owns the reference can project and serialise it', () => {
  // Required of any source producing insert outcomes, and unforgiving: a throw
  // blocks the send rather than degrading to the clipboard text, so both
  // projections must answer for an id they have never seen.
  assert.match(code, /codec: \{/)
  assert.match(code, /clipboardText: \(ref: string\)/)
  assert.match(code, /serialize: async \(ref: string\)/)
  const codec = code.slice(code.indexOf('codec: {'), code.indexOf("}, 'orbit: slash command"))
  assert.equal((codec.match(/\?\? ref|=== undefined \? ref/g) ?? []).length, 2, 'an unknown id must still resolve')
})

test('/orbit still only folds the panel', () => {
  const source = code.slice(code.indexOf('registerOrbitSlashSource'), code.indexOf('interface SelectOption'))
  assert.match(source, /orbit:toggle-panel/)
  assert.equal(/workflow/i.test(source), false, 'the trigger source is carrying Workflows again')
})

test('the sentence leaves a gap for the Workflow rather than naming it', async () => {
  // The chip is the name now: a reference replaces a span, so the copy is the
  // two halves around it and never interpolates one.
  const copy = await readFile(join(clientDir, 'locales.ts'), 'utf8')
  assert.equal((copy.match(/runHead:/g) ?? []).length, 2, 'both dictionaries must carry it')
  assert.equal((copy.match(/runTail:/g) ?? []).length, 2)
  assert.equal(/runHead: '[^']*\{name\}/.test(copy), false, 'the head interpolates a name again')
})

test('each page opens with a heading and a line saying what it lists', () => {
  // Orbit's pages do, and a page is legible before its first row loads only if
  // this one does too.
  for (const page of ['Goal', 'Workflows', 'History', 'Agents']) {
    assert.ok(code.includes(`t('head${page}')`), `${page} has no heading`)
    assert.ok(code.includes(`t('sub${page}')`), `${page} says nothing about itself`)
  }
})

test('the Agent mark is derived, not a palette that would drift from Orbit\'s', async () => {
  // The same Agent must look the same on every open; shipping colours of our
  // own would be a second palette to keep in step with the Runtime's.
  assert.match(code, /function agentMark/)
  assert.match(code, /codePointAt/)
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  assert.equal(/\.avatar\s*\{[^}]*background:\s*(?!none)[^v}]/.test(css), false,
    'the mark carries a fixed colour')
})
