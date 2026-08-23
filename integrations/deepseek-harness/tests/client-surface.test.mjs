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

test('/orbit folds the resident panel rather than opening another', () => {
  assert.equal(/hint:\s*'<goal>'/.test(code), false, 'the goal argument is back')
  assert.match(code, /takes no argument/)
  assert.match(code, /orbit:toggle-panel/)
  assert.equal(code.includes('window.open('), false, 'the panel is resident, not launched')
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

test('the catalog names Workflows and does not offer to start one', () => {
  // Naming them answers "what can I ask for". Starting one here would answer a
  // different question wrongly: the Agent would not know the Run exists.
  assert.match(code, /styles\.catalog/)
  const section = code.slice(code.indexOf('styles.catalog'), code.indexOf('</details>'))
  assert.equal(/onClick/.test(section), false, 'the catalog grew an action')
  assert.equal(/runCommand|start_run|startRun/.test(section), false)
})

test('the top level of the menu is commands, the Workflows sit behind one', () => {
  // Listing them beside the commands buried the native ones under however many
  // a Workspace happened to have, and made picking from `/` mean "paste a name".
  const candidates = code.slice(code.indexOf('candidates:'), code.indexOf('onPick:'))
  const gate = candidates.indexOf('startsWith(LIST_COMMAND)')
  assert.ok(gate > 0, 'the picker is not gated behind the command')
  assert.ok(gate < candidates.indexOf("{ name: 'orbit',"), 'the gate comes first')
})

test('picking a Workflow writes a request, it does not start one', () => {
  // The Run has to be the Agent's or it cannot report on it afterwards, so the
  // menu spares a person the name and stops there.
  assert.match(code, /text: t\('runPrefix'/)
  const pick = code.slice(code.indexOf('onPick:'), code.indexOf('matchSpace:'))
  assert.equal(/start_run|runCommand|getPanelState/.test(pick), false, 'the menu grew a launcher')
})

test('every menu candidate is told apart by its own payload, not by falling through', () => {
  // The opener shipped without a `value`, so picking it fell to the last branch
  // and claimed `/orbit` — the command appeared to do nothing while quietly
  // folding the panel. Each candidate carries what onPick dispatches on.
  const candidates = code.slice(code.indexOf('candidates:'), code.indexOf('onPick:'))
  const named = [...candidates.matchAll(/\{ name: (?:'[a-z]+'|LIST_COMMAND)[^}]*\}/g)].map(([hit]) => hit)
  assert.ok(named.length >= 2, 'the command candidates were not found')
  for (const entry of named) assert.match(entry, /value:/, `${entry} has no payload to dispatch on`)
})

test('picking the opener reopens the menu, and leaves no space to close it', () => {
  // A space is the pipeline's adjudication moment and ends the trigger token,
  // so a trailing one closed the menu the pick had just opened.
  assert.match(code, /text: `\/\$\{LIST_COMMAND\}`, continue: true/)
})

test('the search is whatever follows the command name', () => {
  // Gating on `name + ' '` meant the picker appeared only while the space was
  // absent and filtered on nothing when it was present.
  const candidates = code.slice(code.indexOf('candidates:'), code.indexOf('onPick:'))
  assert.match(candidates, /typed\.startsWith\(LIST_COMMAND\)/)
  assert.equal(/startsWith\(`\$\{LIST_COMMAND\} `\)/.test(candidates), false)
  assert.match(candidates, /replace\(\/\^\\s\+\/u, ''\)/)
})
