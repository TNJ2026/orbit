import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const clientDir = join(here, '..', 'src', 'client')
/* The host-agnostic half of this integration lives outside it. These tests
   still read it because several of the rules they hold are about the two
   halves agreeing — the panel and the Host reaching for the same `goalRuns`,
   the close button and `stopRuntime` being one gesture. */
const coreDir = join(here, '..', '..', '..', 'integration-core', 'src')
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
    // Not the bare 'generateWorkflow': `/orbit-generate` starts a job from this
    // Session and the Workflows page follows its console, which is news about
    // this Workspace. Changing a published Workflow is the authoring surface,
    // and Orbit draws all of it.
    'modifyWorkflow', 'getAuthoringJob',
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
    'exportArtifact', 'generateWorkflowForSession', 'getAuthoringOutput', 'getPanelState',
    'getRunDetail', 'getStepOutput', 'getWorkflowDefinition', 'readArtifactText',
    'reconcileStep', 'runCommand', 'stopRuntime',
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

test('a listing navigates and never starts anything', () => {
  // Opening a row is navigation; starting a Run from one would answer a
  // different question wrongly, because the Agent would not know it exists.
  const listing = code.slice(code.indexOf("tab === 'workflows'"), code.indexOf('styles.resize'))
  assert.match(listing, /setSelectedFlow\(item\.workflow_id\)/, 'a Workflow row does not open')
  assert.equal(/runCommand|start_run/.test(listing), false, 'a listing grew a launcher')
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
  assert.match(select, /input\.setDraft\(/)
  assert.equal(/start_run|runCommand|window\.open/.test(select), false, 'the popup grew a launcher')
})

/**
 * One write, so there is no half-written sentence to fall back from.
 *
 * `insertReference` was span-CAS'd and refused silently, which is why picking
 * a Workflow used to write the sentence, then splice the name into the gap,
 * then check whether the splice had happened. The name is part of the sentence
 * now: it goes in with the one `setDraft` or not at all.
 */
test('the sentence is written whole, in a single draft write', () => {
  const select = code.slice(code.indexOf('onSelect: (option, session)'), code.indexOf("}, 'orbit: workflow popup'"))
  assert.equal((select.match(/input\.setDraft\(/g) ?? []).length, 1)
  assert.doesNotMatch(select, /if \(!inserted\)/)
  assert.doesNotMatch(select, /draftRev/)
})

test('the source that owns the reference can project and serialise it', () => {
  // Required of any source producing insert outcomes, and unforgiving: a throw
  // blocks the send rather than degrading to the clipboard text, so both
  // projections must answer for an id they have never seen.
  assert.match(code, /codec: \{/)
  assert.match(code, /clipboardText: \(ref: string\)/)
  assert.match(code, /serialize: async \(ref: string\)/)
  // `}), ` — the registration closes a call, not just an object. The anchor
  // used to read `}, `, never matched, and sliced to the end of the file; the
  // count it asserted happened to hold across the whole module.
  const end = code.indexOf("}), 'orbit: slash command")
  assert.notEqual(end, -1, 'the codec slice no longer ends where it thinks')
  const codec = code.slice(code.indexOf('codec: {'), end)
  // Answered from the id itself. There is no table of names to miss in now,
  // which is the only way an unknown id can be guaranteed an answer.
  assert.doesNotMatch(codec, /namesById|\?\?|undefined/,
    'a projection that can come up empty can throw, and a throw blocks the send')
  assert.match(codec, /clipboardText: \(ref: string\) => `\$\{MARK_OPEN\}\$\{ref\}\$\{MARK_CLOSE\}`/)
  assert.match(codec, /serialize: async \(ref: string\) => ref/)
})

test('/orbit still only folds the panel', () => {
  const source = code.slice(code.indexOf('registerOrbitSlashSource'), code.indexOf('interface SelectOption'))
  assert.match(source, /orbit:toggle-panel/)
  // Comments stripped first: this is about what the source does, and a comment
  // explaining why it no longer carries Workflows names them to say so.
  const behaviour = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
  assert.equal(/workflow/i.test(behaviour), false, 'the trigger source is carrying Workflows again')
})

test('the sentence leaves a gap for the Workflow rather than naming it', async () => {
  // The chip is the name now: a reference replaces a span, so the copy is the
  // two halves around it and never interpolates one.
  const copy = await readFile(join(clientDir, 'locales.ts'), 'utf8')
  assert.equal((copy.match(/runHead:/g) ?? []).length, 2, 'both dictionaries must carry it')
  assert.equal((copy.match(/runTail:/g) ?? []).length, 2)
  assert.equal(/runHead: '[^']*\{name\}/.test(copy), false, 'the head interpolates a name again')
})

test('refresh asks again rather than redrawing what is already held', () => {
  // The obvious wrong version calls the poll function, which leaves the loop
  // asleep on its own timer and races the answer it is already waiting for.
  // Bumping a dependency tears that loop down and starts a fresh one.
  assert.match(code, /setAsked\(count => count \+ 1\)/)
  // `asked` among the dependencies is the whole mechanism; the rest of the
  // list grows as the panel learns new reasons to restart its loop, and
  // freezing it made adding one look like breaking this.
  assert.match(code, /\}, \[sessionId,[^\]]*\basked\]\)/)
  // And the press has to say it is one. Two of the three lists the panel draws
  // are held on purpose — the catalog behind a TTL, the Agents for the life of
  // the Runtime — so an unmarked poll re-reads neither, and pressing refresh
  // redraws the runs beside a workflow list up to five minutes old.
  assert.match(code, /'getPanelState', \[sessionId, forced,/)
  assert.match(code, /forceNext\.current = true/)
})

test('only the tick the press starts is forced', async () => {
  // Forcing the timer ticks that follow would re-read the whole catalog every
  // two seconds for the rest of the session.
  assert.match(code, /forceNext\.current = false/)
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const state = host.slice(host.indexOf("@Remote('getPanelState')"), host.indexOf("@Remote('getRunDetail')"))
  // Also re-read when a job this Host was watching has just published: the
  // page a person is looking at is the page it lands on.
  assert.match(state, /if \(force \|\| authoring\.published\) \{[\s\S]{0,240}?refreshCatalog\(scope\)/)
})

test('a page with nothing on it still says something', () => {
  // The pages carried a heading and a subtitle each, which is how a panel this
  // size spends a fifth of its height explaining four tabs that already name
  // themselves. What that copy was really covering is a page with no rows yet,
  // and the empty line covers it without costing anything when there are rows.
  for (const page of ['Goal', 'Workflows', 'History', 'Agents']) {
    assert.ok(code.includes(`t('empty${page}')`), `${page} goes blank when empty`)
  }
  assert.ok(code.includes("t('loading')"), 'nothing is said while the first poll runs')
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

test('what the panel lists, it can open', async () => {
  // Widening the list without the reads made every History row a trap: the
  // panel showed a Run and then answered "not found" when it was expanded.
  // The panel used to hold that line itself, by asking each read for
  // `owner: 'workspace'`. The Runtime holds it now — one Runtime serves one
  // Workspace, and that is the whole of what a read may see — so the argument
  // is gone from both transports rather than accepted and ignored.
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  for (const call of ['list_runs', 'get_run_steps', 'read_run_output']) {
    assert.ok(host.includes(`'${call}'`), `${call} is not called`)
  }
  // Used, not merely mentioned: the comment beside the poll names the argument
  // to explain what replaced it, and a test that cannot tell those apart
  // forbids writing down the reason.
  const written = host.split('\n').filter(line => !/^\s*(\/\/|\*|\/\*)/.test(line)).join('\n')
  assert.doesNotMatch(written, /owner: 'workspace'/)
})

test('a Run opens into the panel rather than inside the list', () => {
  // A 400px panel cannot show a Run's steps beside its siblings: expanding one
  // inline pushed the rest of the list out, which is the same as losing it.
  assert.match(code, /OrbitRunListRow/)
  assert.match(code, /OrbitRunDetail/)
  const detail = code.slice(code.indexOf('export function OrbitRunDetail'))
  assert.match(detail, /onBack/, 'detail with no way back')
  assert.equal(/<DisclosureRow[\s\S]{0,400}OrbitRunDetail/.test(code), false)
})

test('changing page clears every detail it was showing', () => {
  // A detail left behind a tab is a place a reader returns to without meaning
  // to — and there are two kinds now, so forgetting one is the likely mistake.
  const handler = code.slice(code.indexOf('onClick={() => {'), code.indexOf('setTab(key) }}') + 14)
  assert.match(handler, /setSelected\(null\)/)
  assert.match(handler, /setSelectedFlow\(null\)/)
})

test('a Workflow lists its steps and links out for the graph', async () => {
  // The steps are listed, not drawn: a list answers "what happens and who does
  // it" in the width a panel has, and the picture stays with Orbit, which has
  // a frame built for it.
  const detail = await readFile(join(clientDir, 'OrbitWorkflowDetail.tsx'), 'utf8')
  assert.match(detail, /openThisInOrbit/)
  assert.match(detail, /#\/workflows\//)
  assert.match(detail, /getWorkflowDefinition/)
  const body = detail.replace(/^\s*\*.*$/gm, '')
  for (const drawn of ['getGraph', 'getEdges', 'edges', 'layout']) {
    assert.equal(new RegExp(`\\b${drawn}\\b`).test(body), false,
      `${drawn} is being drawn here`)
  }
})

test('the listing draws a workflow from the tally it was sent', async () => {
  // A shape per row must not become a request per row: the listing already
  // carries node_count and node_kinds, and reaching for the definition here
  // would turn one poll into fifteen calls that the detail page makes anyway.
  // The last occurrence of each: the first is the page heading, which comes
  // above all four list bodies.
  const list = code.slice(
    code.lastIndexOf("tab === 'workflows'"), code.lastIndexOf("tab === 'agents'"),
  )
  assert.ok(list.length > 200, 'the workflows list body was not found')
  assert.match(list, /node_kinds/)
  assert.equal(/getWorkflowDefinition|getGraph/.test(list), false)
})

test('a definition is read once, not polled', async () => {
  // It changes only when somebody republishes it. A poll would re-ask a
  // settled question at the cadence of one that is not.
  const detail = await readFile(join(clientDir, 'OrbitWorkflowDetail.tsx'), 'utf8')
  assert.equal(/setInterval|setTimeout/.test(detail), false)
})

/**
 * The list still says which Workflows a goal cannot be started from.
 *
 * The detail page used to carry the readiness verdict and its reason as well.
 * Both are gone from it, so the "why" now lives only in Orbit — but the list
 * has to keep the verdict: it carries the whole catalog, and without it the
 * ones needing work read as ready.
 */
test('the catalog marks what a goal cannot be started from', () => {
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  assert.match(panel, /item\.goal_readiness === 'ready' \? null : \(/)
  assert.match(panel, /needsMigration' : 'needsUpgrade'/)
  // And the detail page no longer repeats the verdict beside it.
  const detail = sources[names.indexOf('OrbitWorkflowDetail.tsx')]
  assert.doesNotMatch(detail, /goal_readiness|readiness_reason/)
  assert.doesNotMatch(detail, /styles\.facts/)
})

/**
 * The Goal page is the whole Run, not a summary of it.
 *
 * It exists to answer what is happening now, and the answer is which step the
 * Run is on and what that step is saying. A progress line said the first half
 * and made the reader leave the page for the second.
 */
test('a running Goal draws its steps on the Goal page', () => {
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const from = panel.indexOf("tab === 'goal' ? (")
  const until = panel.indexOf("tab === 'history' ? (")
  assert.ok(from > 0 && until > from, 'the Goal and History blocks were not found')
  const goal = panel.slice(from, until)
  assert.ok(goal.includes('OrbitRunGoalCard'), 'the Goal page draws the Run as a card')
  // Drawn from the polled steps, not from nothing: the card renders its
  // heading either way, so a card handed no steps loses the ladder in silence.
  assert.match(goal, /steps=\{steps\[/, 'the Goal card is fed the polled steps')
  assert.equal(goal.includes('OrbitRunListRow'), false,
    'the Goal page is past the summary row it replaced')
  // The History page keeps the row: a finished Run's steps are read by opening
  // it, and a page of settled ladders is not a list any more.
  assert.ok(panel.slice(until).includes('OrbitRunListRow'))
  assert.ok(rows.includes('export function OrbitStepList'))
  // Both readers of a Run go through it, so neither can drift from the other.
  assert.equal((rows.match(/<OrbitStepList/g) ?? []).length, 2)
  assert.equal((rows.match(/<StepDisclosure/g) ?? []).length, 1,
    'the step row is instantiated in one place, inside OrbitStepList')
})

/** The Goal page needs `has_output` and the reconciliation fields, or its steps
 *  cannot offer output or the decision a person owes them. */
test('the Host sends the Goal page what its steps are drawn from', async () => {
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const projection = host.slice(host.indexOf('private async liveSteps'))
  const body = projection.slice(0, projection.indexOf('\n  }'))
  for (const field of ['node_id', 'label', 'status', 'has_output', 'resolution', 'reconciliation']) {
    assert.ok(body.includes(field), `liveSteps must carry ${field}`)
  }
  // Still a projection, not the whole StepSummary: this rides a two-second poll.
  for (const heavy of ['prompt', 'handler', 'first_at']) {
    assert.equal(body.includes(heavy), false, `liveSteps must not carry ${heavy}`)
  }
  // Read for the Runs the Goal page draws, by that page's own rule. Reading
  // only the live ones leaves a Goal that has just finished showing the last
  // step it was seen running, because the read stops when the Run does.
  assert.ok(body.includes('goalRuns('), 'the Host reads steps by the Goal page rule')
})

/** One rule for which Runs the Goal page is about, or the Host serves steps for
 *  a set the page does not draw — and the page draws Runs with no steps. */
test('the Goal page and the Host agree on which Runs it is about', async () => {
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const shared = await readFile(join(coreDir, 'run-progress.ts'), 'utf8')
  assert.ok(shared.includes('export function goalRuns'))
  // Reached through the shared package now, but still the same function: two
  // copies of "which Runs is this Goal page about" is the disagreement this
  // test exists to prevent.
  assert.match(host, /goalRuns[^\n]*|[^\n]*goalRuns/)
  assert.match(host, /from '@orbit-runtime\/integration-core'/)
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  const from = panel.indexOf("tab === 'goal' ? (")
  const until = panel.indexOf("tab === 'history' ? (")
  assert.match(panel.slice(from, until), /goal\.map\(/, 'the Goal page draws goalRuns')
  assert.match(panel, /const goal = goalRuns\(/)
  // The steps the panel holds are pruned by the same rule, so the Run on the
  // page keeps its ladder and every other Run stops being remembered.
  assert.match(panel, /for \(const row of goalRuns\(next\)\)/)
})

/**
 * Reading a Goal takes no clicks: the steps, what each printed, and the answer.
 *
 * A step's output used to be behind a chevron, which made "what is it doing" a
 * four-click question, and the Run's result was on no page at all — the panel
 * could say a Run succeeded and never say at what.
 */
test('a Goal shows its output and its answer without being asked', () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  // Open because the step has something to say. Seeded from `hasOutput` on
  // every render rather than into useState, because that arrives a poll later
  // than the step does.
  assert.match(rows, /const open = override \?\? expandable/)
  // Followed only while the step is the one working: an open ladder is one
  // poll, not one per step per poll.
  assert.match(rows, /const working = live && step\.status === 'running'/)
  assert.ok(rows.includes('function RunResult'))
  // On both pages a Run is read on, and drawn from one place.
  assert.equal((rows.match(/<RunResult/g) ?? []).length, 2)
  assert.ok(rows.includes('resultOutcome'))
})

/**
 * The Goal page is the whole Run, so its heading goes nowhere.
 *
 * And the controls came with it. Cancel and resume lived on the detail page,
 * which the Goal page was the only way into for a Run still running — History
 * lists Runs that are over, and Orbit advertises neither command for those.
 */
test('the Goal heading is not a link, and the Run is still actionable', () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const card = rows.slice(rows.indexOf('export function OrbitRunGoalCard'))
  const body = card.slice(0, card.indexOf('\n}'))
  assert.equal(/onOpen/.test(body), false, 'the Goal card no longer navigates')
  assert.match(body, /<div className=\{styles\.goalHead\}/, 'the heading is a heading')
  assert.ok(body.includes('<RunControls'), 'a running Goal can still be stopped')
  // One definition of what may be done to a Run, on both pages that read one.
  assert.ok(rows.includes('function RunControls'))
  assert.equal((rows.match(/<RunControls/g) ?? []).length, 2)
  assert.equal((rows.match(/commandRevision\(/g) ?? []).length, 2,
    'the advertised revision is read in one place, inside RunControls')
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  const from = panel.indexOf("tab === 'goal' ? (")
  assert.equal(/onOpen/.test(panel.slice(from, panel.indexOf("tab === 'history' ? ("))), false)
  // History still opens a Run: its steps are worth reading, and reading them
  // is the whole reason that page has rows rather than cards.
  assert.match(panel.slice(panel.indexOf("tab === 'history' ? (")), /onOpen=\{/)
})

/**
 * The Goal heading names the work and shows what was asked for.
 *
 * It used to be the goal, the Workflow's id and the status. The id names a
 * definition the reader never chose by name, and the status is already on
 * every step below it — while the request itself, which is the one thing on
 * that card nothing else carries, was on no page at all.
 */
test('the Goal heading is the goal and the request, not the id and the status', () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const card = rows.slice(rows.indexOf('export function OrbitRunGoalCard'))
  const head = card.slice(card.indexOf('goalHead'), card.indexOf('</div>'))
  assert.match(head, /run\.goal/)
  assert.match(head, /<FoldedText t=\{t\} text=\{run\.prompt\}/)
  assert.equal(/run\.workflow/.test(head), false, 'the Workflow id left the heading')
  assert.equal(/styles\.status/.test(head), false, 'the status is on the steps, not twice')
  // Folded by layout, not by counting characters: the same paste is two lines
  // wide and six lines narrow, and only the box knows which.
  assert.match(rows, /const PROMPT_LINES = 5/)
  // The depth is the caller's; the fold is one widget shared with the console.
  assert.match(rows, /lines=\{PROMPT_LINES\}/)
  assert.match(rows, /WebkitLineClamp: lines/)
  assert.match(rows, /scrollHeight > node\.clientHeight/)
  assert.ok(rows.includes('ResizeObserver'), 'the toggle is re-measured when the panel resizes')
  // Only when something is actually folded, and it must survive being opened.
  assert.match(rows, /folded \|\| open \?/)
})

/**
 * A Workflow being written says where it is, the way a Goal does.
 *
 * The Runtime has written a marker at each authoring stage since the feature
 * existed; the panel matched them only to drop them, so a job that spent a
 * minute on its second attempt showed one unchanging line. Reading them means
 * reading the console whether or not anyone opened it — the markers are in
 * that stream, so a ladder gated on the disclosure would only move while
 * somebody was already watching the thing it was there to replace.
 */
test('an authoring job draws its stages, not one unchanging line', () => {
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  const row = panel.slice(panel.indexOf('function AuthoringRow'), panel.indexOf('export interface OrbitPanelProps'))
  assert.match(row, /authoringProgress\(markers, job\.status\)/)
  assert.match(row, /progress\.stages\.map\(/)
  // Read unconditionally: the previous guard was `!open || !outputHref`.
  assert.equal(/if \(!open/.test(row), false, 'the console is read whether or not it is open')
  assert.match(row, /if \(!outputHref\) return/)
  // Markers are control data: kept out of the console text, read as the ladder.
  assert.match(row, /filter\(chunk => !isProgressMarker\(chunk\)\)/)
  assert.match(row, /setMarkers\(/)
  assert.equal(row.includes("'\\x1eorbit-progress:'"), false,
    'the sentinel is matched in one place, beside what parses it')
})

/**
 * A claim loop that can only complain to a terminal cannot be debugged.
 *
 * The Harness logs to whichever terminal started it, which is nowhere a person
 * looking at the panel — or at this repository — can read. When the loop
 * claimed work and then failed to put it to the model, the only visible
 * symptom was a job that ended `unknown_external_result` minutes later, with
 * the reason in a scrollback nobody had.
 */
test('the authoring loop reports where it can be read', async () => {
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const diagnostics = host.slice(host.indexOf("@Remote('getDiagnostics')"))
  const body = diagnostics.slice(0, diagnostics.indexOf('\n  }'))
  for (const field of ['waiting', 'driving', 'agentRegistry', 'lastError']) {
    assert.ok(body.includes(field), `diagnostics must report ${field}`)
  }
  assert.match(host, /this\.authoringTrouble\.set\(/, 'a failure is kept, not only logged')
  // Services are captured when the Host is composed, so one it cannot reach is
  // discovered at startup rather than at the instant a Workflow needs writing.
  assert.match(host, /this\.agents = ctx\.get\('agents'\)/)
  assert.equal(/this\.ctx\.get\(/.test(host), false,
    'services are read from the constructor context, as every other one here is')
})

/**
 * The turn is queued the way the platform queues one.
 *
 * `followup` is on the Agent, not on its inbox — the inbox is the durable
 * projection of pending work, the Agent is what runs. Reading the inbox's
 * interface as the Agent's cost a claimed request and ten minutes of a job
 * sitting at `generating` with `agent.inbox.followup is not a function` in a
 * terminal nobody was watching.
 */
test('an authoring turn is queued on the Agent, framed as the platform frames one', async () => {
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const ask = host.slice(host.indexOf('private async askTheSession'))
  const body = ask.slice(0, ask.indexOf('\n  }'))
  assert.match(body, /agent\.followup\(/)
  assert.equal(/agent\.inbox\.followup/.test(body), false, 'followup is not on the inbox')
  // Identity and freezing belong to the platform's constructor, not to us.
  assert.match(body, /createUserMessage\(/)
  assert.equal(/role: 'user'/.test(body), false, 'a hand-rolled message skips that contract')
  // Idle is whole-agent quiescence, which a Session busy with the person's own
  // turn reaches before the queued one has run.
  assert.match(body, /for \(;;\)/)
  assert.match(body, /if \(Date\.now\(\) >= deadline\) return ''/)
  const manifest = JSON.parse(await readFile(join(here, '..', 'package.json'), 'utf8'))
  assert.ok(manifest.peerDependencies['@deepseek-ai/dsh-llm'], 'the framing package is declared')
})

/**
 * Both detail pages are left the same way, by the same control.
 *
 * A Run's detail and a Workflow's are reached alike and left alike; a back
 * control spelled once in each file is two affordances for one idea, and they
 * drift the moment one of them is restyled. It carries the accent colour
 * because it is the one control on a detail page a reader is looking for — a
 * body-coloured word is something they have to find.
 */
test('detail pages share a bright, readable back control', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const flow = sources[names.indexOf('OrbitWorkflowDetail.tsx')]
  assert.ok(rows.includes('export function BackButton'))
  for (const page of [rows, flow]) assert.match(page, /<BackButton t=\{t\} onBack=\{onBack\} \/>/)
  // Spelled once, inside the shared component and nowhere else. Counted rather
  // than forbidden outright, because the definition itself is one of them.
  assert.equal((rows.match(/className=\{styles\.back\}/g) ?? []).length, 1)
  assert.equal((flow.match(/className=\{styles\.back\}/g) ?? []).length, 0)

  // Read rather than taken from `sources`, which holds only .ts and .tsx.
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const back = css.slice(css.indexOf('.back {'), css.indexOf('.backArrow'))
  // A token the shell actually defines. `--dsw-alias-label-accent`, which the
  // hover used to reach for, is not one — so that colour only ever existed as
  // the `LinkText` fallback beside it.
  assert.match(back, /color: var\(--dsw-alias-state-business-primary, Highlight\)/)
  assert.match(back, /font-size: 14px/)
  // Used, not merely mentioned: the comment above `.back` names the dead token
  // to explain why it is gone, and a test that cannot tell those apart forbids
  // writing down the reason.
  assert.equal(/var\(--dsw-alias-label-accent/.test(css), false)
  // The arrow is drawn, not glued into the copy, so it can carry its own size.
  assert.match(css, /\.backArrow \{ font-size: 16px/)
  // Hover underlines the label, not the button. `text-decoration` on a flex
  // container is not passed down to its items, so the rule on `.back` drew
  // nothing at all — and the label is a bare text node without an element.
  assert.match(css, /\.back:hover \.backLabel \{ text-decoration: underline/)
  assert.match(rows, /className=\{styles\.backLabel\}>\{t\('back'\)\}/)
  assert.equal(/back: '←/.test(sources[names.indexOf('locales.ts')]), false)
})

/**
 * The close button stops a service, so it asks before it does.
 *
 * Every other control in that bar is reversible — fold the panel, open a tab,
 * poll again. This one stops a Runtime that Orbit's own UI, other Sessions and
 * any Run in flight are using, and a press in that row cannot be undone. The
 * Gateway's own note said a panel must never stop a Runtime; that changed
 * deliberately, and the question is what makes it safe.
 */
test('stopping Orbit is asked for, not merely clicked', async () => {
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  // The press opens the question; only the confirm reaches the Host.
  assert.match(panel, /onClick=\{\(\) => setConfirmingStop\(true\)\}/)
  const asked = panel.slice(panel.indexOf('confirmingStop ? ('))
  const dialog = asked.slice(0, asked.indexOf('\n      ) : null}'))
  assert.match(dialog, /'stopRuntime', \[sessionId\]/)
  // It is a close button, so it closes — everything. Not folded to the mark:
  // a badge still sitting there offers to reopen a page about a service the
  // same press stopped, and the next poll would fill it with the error of
  // finding that out.
  assert.match(dialog, /dismissed: true/)
  assert.doesNotMatch(dialog, /collapsed: true/)
  assert.match(panel, /if \(layout\.dismissed\) return null/)
  // And there is a way back, or the button would be one-way.
  assert.match(panel, /dismissed: false, collapsed: false/)
  // Nothing to poll for a panel that is not there, against a Runtime that has
  // just been stopped.
  assert.match(panel, /if \(!sessionId \|\| layout\.dismissed\)/)
  const locales = sources[names.indexOf('locales.ts')]
  // The question says what is lost, not just that something will happen.
  for (const key of ['stopRuntimeAsk', 'stopCancel', 'stopConfirm']) {
    assert.ok(locales.includes(`${key}:`), `the confirmation needs ${key}`)
  }

  // Session-scoped like every other call: the Workspace is derived from the
  // Session, so this can only stop the Runtime the person is looking at.
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const stop = host.slice(host.indexOf("@Remote('stopRuntime')"))
  const body = stop.slice(0, stop.indexOf('\n  }'))
  assert.match(body, /const scope = await this\.sessionWorkspace\(sessionId\)/)
  // The authoring waiter is parked on that Runtime; leaving it would have it
  // report the shutdown as a failure of something that was asked for.
  assert.match(body, /this\.authoringWaiters\.get\(scope\.canonicalPath\)\?\.abort\(\)/)

  // The Runtime's own command, not a signal: it unwinds rather than is cut.
  const gateway = await readFile(join(coreDir, 'gateway.ts'), 'utf8')
  assert.match(gateway, /\/api\/v1\/runtime\/shutdown/)
  assert.match(gateway, /'idempotency-key': crypto\.randomUUID\(\)/)
  assert.equal(/process\.kill|SIGTERM/.test(gateway), false,
    'a Runtime is asked to stop, never signalled')
  // The connection goes whatever the answer was.
  assert.match(gateway, /\} finally \{\n      this\.runtimes\.delete\(key\)/)
})

/**
 * The folded mark sits against the right edge, halfway down.
 *
 * Placed in the stylesheet rather than inline, so where it sits is said once.
 * And centred by margin: the hover lift owns `transform`, so centring with one
 * would be cancelled the moment a pointer arrived — the mark would drop half
 * its height on hover and climb back on leaving.
 */
test('the collapsed mark is centred against the right edge', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const badge = css.slice(css.indexOf('.badge {'), css.indexOf('.badge:hover'))
  assert.match(badge, /top: 50%/)
  assert.match(badge, /right: 18px/)
  assert.doesNotMatch(badge, /bottom:/)
  assert.doesNotMatch(badge, /transform:/)
  // The offset is half the height, and the two are a pair.
  const height = /height: (\d+)px/.exec(badge)
  const offset = /margin-top: -(\d+)px/.exec(badge)
  assert.ok(height && offset, 'the mark must state both its height and its offset')
  assert.equal(Number(offset[1]) * 2, Number(height[1]),
    'centring by margin means exactly half the height')
  // The lift keeps the transform it needs.
  assert.match(css, /\.badge:hover \{ transform: translateY\(-1px\)/)
  // Nothing places it from the component any more.
  const panel = sources[names.indexOf('OrbitPanel.tsx')]
  assert.doesNotMatch(panel, /style=\{\{ right: 18, bottom: 24 \}\}/)
})

/**
 * The request is a quoted block, and it cannot widen the panel.
 *
 * It is somebody else's text sitting inside the panel's own, so it wears the
 * same card a step's output does. And it is bounded: a `pre` sizes to its
 * longest line unless told otherwise, and a pasted URL or a line of JSON is
 * one long line — `overflow-wrap` cannot break a box that has already grown to
 * fit it, so the panel scrolls sideways instead.
 */
test('the request and a step console share one card, which never widens the panel', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  // The card is the wrapper, so the control that unfolds the request is inside
  // it rather than a separate remark sitting under a tinted block.
  const wrapper = css.slice(css.indexOf('.goalPromptCard {'), css.indexOf('.goalPrompt {'))
  const prompt = css.slice(css.indexOf('.goalPrompt {'), css.indexOf('.goalPrompt[data-open]'))
  // Literally the same surface now: the request and a step's console are one
  // widget, so there is nothing left for them to drift apart from.
  const card = /background: (var\(--dsw-alias-bg-module-platform[^;]*\));/
  assert.match(wrapper, card)
  assert.equal((css.match(/\.goalPromptCard \{/g) ?? []).length, 1)
  assert.match(wrapper, /border-radius: 6px/)
  assert.match(wrapper, /padding: /)
  // Width containment, and the padding counted inside it.
  assert.match(wrapper, /max-width: 100%/)
  assert.match(wrapper, /min-width: 0/)
  // The text keeps none of it: two backgrounds would draw one inside the other.
  assert.doesNotMatch(prompt, card)
  // And the column it sits in is bounded: a grid item's default `min-width` is
  // `auto`, so an `auto` track grows to whatever its content refuses to shrink
  // below and the child's `max-width: 100%` then measures against a track that
  // has already grown.
  const main = css.slice(css.indexOf('.listMain {'), css.indexOf('\n}', css.indexOf('.listMain {')))
  assert.match(main, /grid-template-columns: minmax\(0, 1fr\)/)
  assert.match(main, /min-width: 0/)
  // Neither of which mattered while the row around them was `width: 100%` plus
  // 24px of padding under `content-box`: three rows are written that way, and
  // each was its container plus 24px before its contents were laid out at all.
  assert.match(css, /\.panel,\n\.panel \*,[\s\S]{0,80}box-sizing: border-box/)
  // Unfolded it is simply a block: a box that scrolls inside a panel that
  // scrolls is two places to be lost in, and the reader asked for the whole
  // thing by pressing the control that says so.
  const open = css.slice(css.indexOf('.goalPrompt[data-open]'), css.indexOf('.goalPromptToggle'))
  assert.doesNotMatch(open, /overflow/)
  assert.doesNotMatch(open, /max-height/)
})

/**
 * A failure is read to the person, not recited at them.
 *
 * Every panel call travels Host → Gateway → MCP → Runtime and each layer wraps
 * the last, so what used to reach a reader was `Error: {"error":"workflow
 * version not found: workflow:wf_7ba…@2"}` — four layers of packaging around a
 * fact that was not the one they needed. The original is kept on the element,
 * because a reading is a guess and a message nobody can quote is a message
 * nobody can get help with.
 */
test('no panel surface prints a raw failure', () => {
  for (const [index, name] of names.entries()) {
    if (!/\.tsx$/.test(name)) continue
    assert.equal(/String\(reason\)|String\(error\)/.test(sources[index]), false,
      `${name} still shows a failure as it arrived`)
  }
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  assert.ok(rows.includes('export function PanelErrorText'))
  // The original rides along on the element, where it can be hovered and copied.
  assert.match(rows, /title=\{error\.detail\}/)
  // One renderer for every page, so a failure looks the same wherever it lands.
  const shown = sources
    .filter((_, i) => /\.tsx$/.test(names[i]))
    .flatMap(source => [...source.matchAll(/<PanelErrorText/g)])
  assert.ok(shown.length >= 6, `expected every display site, found ${String(shown.length)}`)
  assert.equal(
    sources.some((s, i) => /\.tsx$/.test(names[i]) && /className=\{styles\.error\}>\{/.test(s)),
    false,
    'a failure is never interpolated straight into the page',
  )
})

/**
 * A result says how it ended and hands over what it made.
 *
 * A workflow that writes a file answers `{"artifact_id":
 * "langgraph_artifact:4c1e5281…"}`, and printing that put a 64-character hash
 * on the page where the answer should have been — the one part of the result
 * that means nothing to a reader. It is a door, so it is drawn as one, and
 * the question it was hiding — did this work — is answered in a word first.
 */
test('a result is an outcome and a door, not an artifact id', () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const result = rows.slice(rows.indexOf('function RunResult'))
  const body = result.slice(0, result.indexOf('\n}'))
  // How it ended, before anything the terminal step happened to emit.
  assert.match(body, /outcome_\$\{run\.status\}/)
  assert.match(body, /styles\.outcome/)
  // Artifacts as a row of their own, drawn once per Artifact.
  assert.match(body, /<ArtifactRow/)
  // And nothing at all while it is still going.
  assert.match(body, /if \(run\.live\) return null/)
  // The panel still draws nothing of an Artifact: it hands over a link, and
  // the browser opens what the Host sends back.
  assert.equal(/getArtifactContent|listArtifacts/.test(rows), false)
  const locales = sources[names.indexOf('locales.ts')]
  for (const key of ['outcome_completed', 'outcome_failed', 'outcome_unknown', 'artifactOpen']) {
    assert.ok(locales.includes(`${key}:`), `the outcome needs ${key}`)
  }
})

/**
 * The Host serves an Artifact's bytes, because only it can read them.
 *
 * Artifacts are owned by the actor that produced them; a browser reaching
 * `/api/v1` on loopback is `local`; a Run this panel starts belongs to
 * `harness:session:<id>`. Orbit's own link is therefore a 404 for every
 * Artifact this Harness ever made — and so is Orbit's own UI. The Host is the
 * identity that can read one, so the link points at the Host.
 *
 * It passes bytes through and draws nothing: no gallery, no viewer, no second
 * drawing of anything Orbit draws.
 */
test('an Artifact is served by the Host, sandboxed and typed as Orbit recorded it', async () => {
  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  const route = host.slice(host.indexOf("path: '/plugins/dsh-orbit/artifact'"))
  const body = route.slice(0, route.indexOf("path: '/plugins/dsh-orbit/api'"))
  // Read as the Session, which is the whole reason this route exists.
  assert.match(body, /this\.sessionWorkspace\(sessionId\)/)
  assert.match(body, /'read_artifact_content', \{ artifact_id: artifactId \}/)
  // A GET, because a link is what a person clicks.
  assert.match(body, /req\.method !== 'GET'/)
  // Typed as Orbit recorded it, never guessed: guessing is how a text file
  // becomes a download and a script becomes a script.
  assert.match(body, /held\.artifact\.content_type \|\| 'application\/octet-stream'/)
  assert.match(body, /nosniff/)
  // Never inlined into this origin: the bytes are whatever a workflow wrote,
  // and this origin is the Harness the person is signed in to.
  assert.match(body, /content-security-policy/)
  assert.match(body, /sandbox/)
})

/**
 * An Artifact is offered two ways, because they answer different questions.
 *
 * The link opens the bytes in a tab — "what does it say". The export writes an
 * ordinary file and says where — "give me the file". The path Orbit already
 * has is neither: it is a content-addressed blob named by its own sha256,
 * shared with every Artifact holding the same bytes and collected when nothing
 * references it, so a person told "that is your file" would corrupt the store
 * by saving in it.
 */
test('an Artifact can be looked at and can be taken away', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const row = rows.slice(rows.indexOf('function ArtifactRow'))
  const body = row.slice(0, row.indexOf('\nfunction RunResult'))
  // Read here when its text is the answer, and only then: a link is not
  // offered for something already on the page.
  assert.match(body, /'readArtifactText', \[sessionId, artifactId\]/)
  assert.match(body, /styles\.artifactText/)
  assert.match(body, /inline !== null \? null :/)
  // A quick look, through the Host, which holds the identity that owns it.
  assert.match(body, /artifactHref\(sessionId, artifactId\)/)
  assert.match(body, /target="_blank"/)
  // A link that goes nowhere is worse than the name on its own.
  assert.match(body, /href \? \(/)
  // And a copy that belongs to the person, with the path it went to.
  assert.match(body, /'exportArtifact', \[sessionId, artifactId\]/)
  assert.match(body, /styles\.artifactPath/)

  const host = await readFile(join(here, '..', 'src', 'index.ts'), 'utf8')
  // Metadata before bytes: asking for a 2 MiB PDF to discover it is a 2 MiB
  // PDF is the round trip the ordering exists to avoid.
  const read = host.slice(host.indexOf("@Remote('readArtifactText')"))
  const reading = read.slice(0, read.indexOf('\n  }'))
  assert.ok(
    reading.indexOf("'read_artifact'") < reading.indexOf('readableAsText'),
    'the recorded type and size are read before the content is asked for',
  )
  assert.match(reading, /if \(!readableAsText\(contentType, sizeBytes\)\) return/)
  const remote = host.slice(host.indexOf("@Remote('exportArtifact')"))
  const impl = remote.slice(0, remote.indexOf('\n  }'))
  // Session-scoped twice over: which Workspace, and the only identity allowed
  // to read an Artifact somebody else's Session produced.
  assert.match(impl, /this\.sessionWorkspace\(sessionId\)/)
  assert.match(impl, /'read_artifact_content'/)
  // Named by the helper, never by anything a workflow wrote into the id.
  assert.match(impl, /artifactFilename\(artifactId/)
  assert.equal(/held\.artifact\.filename\s*\)\s*$/m.test(impl), false)
  // The CAS path is never handed out: that is the whole reason this exists.
  assert.equal(/blob_key/.test(host), false)
})

/**
 * A list separator is lighter than a section edge, and stops short of one.
 *
 * These rows used to carry a full-width `border-bottom` in the panel's own
 * divider colour — but `--dsw-alias-line-normal` is not a token the shell
 * defines, so the rule only ever drew the flat 28% grey written beside it:
 * heavier than anything the shell draws, and blind to the theme. A line that
 * also runs edge to edge reads as the end of a section, and these are items
 * inside one.
 */
test('list rows are separated, not bounded', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const rule = css.slice(css.indexOf('.listRow::after'), css.indexOf('/* Nothing to separate it from'))
  // Inset to the padding the rows already use, so the line starts where their
  // text does rather than at the panel's edge.
  assert.match(rule, /left: 12px/)
  // …except the step list, which overrides it below; see the stripe test.
  assert.match(css, /\.defnRow::after \{ left: 0; \}/)
  assert.match(rule, /right: 12px/)
  assert.match(rule, /height: 1px/)
  // A token the shell actually defines, and a lighter one.
  assert.match(rule, /--dsw-alias-border-l2/)
  // Drawn as a rule inside the row, which is the only way it can be inset.
  assert.match(css, /\.listRow,\n\.flowRow,\n\.defnRow \{ position: relative; \}/)
  assert.match(css, /:last-child::after,[\s\S]{0,60}\.defnRow:last-child::after \{ display: none/)
  // Every list in the panel separates its rows the same way — a published
  // step included, which used to carry a full-width border in the heavier
  // colour and so read as the end of a section rather than an item in one.
  assert.match(css, /\.listRow::after,\n\.flowRow::after,\n\.defnRow::after \{/)
  // And the border it replaced is gone, or both would draw.
  const row = css.slice(css.indexOf('.listRow {'), css.indexOf('.listRow:hover'))
  assert.doesNotMatch(row, /border-bottom/)
  assert.doesNotMatch(css.slice(css.indexOf('.flowRow {'), css.indexOf('.flowName')), /border-bottom/)
  // The step keeps its kind stripe and nothing else of its own.
  // Anchored on the rule itself: `.defnRow {` now also opens the shared
  // `position: relative` group further up, and slicing from there sweeps in
  // every rule in between.
  const step = css.slice(css.indexOf('.defnRow {\n  padding'), css.indexOf('.kind_action'))
  assert.doesNotMatch(step, /border-bottom/)
  assert.match(step, /border-left: 2px solid/)
  // A row that is a <button> must zero all four sides, not the three that were
  // not carrying the divider: the browser's own is 2px outset black, and it
  // reappears the moment the fourth side stops being overridden.
  const button = css.slice(css.indexOf('.flowButton {'), css.indexOf('.flowButton:hover'))
  assert.match(button, /border: 0;/)
  assert.doesNotMatch(button, /border-left: 0/)
})

/**
 * An Orbit command reports into the panel, so it opens the panel.
 *
 * The panel is where the work an Orbit command starts becomes visible — a
 * Run's steps, a Workflow being written. Started behind a folded panel or a
 * dismissed one, a command has done something and said nothing.
 *
 * `orbit:show-panel` and not `orbit:toggle-panel`: a toggle run twice hides
 * the thing it was meant to reveal, and hides it for someone who already had
 * it open. `/orbit` is the one command that may toggle, because toggling is
 * what it is for.
 */
test('every Orbit command that does work opens the panel', async () => {
  const client = await readFile(join(clientDir, 'index.tsx'), 'utf8')

  // One helper, and it only ever shows.
  assert.match(client, /function showOrbitPanel\(tab\?: 'workflows'\): void/)
  const helper = client.slice(client.indexOf('function showOrbitPanel'),
    client.indexOf('const MARK_OPEN'))
  assert.match(helper, /'orbit:show-panel'/)
  assert.doesNotMatch(helper, /toggle/)

  // Only `/orbit` toggles. Counted over code alone: the helper's own comment
  // names the toggle event in order to say it is not that.
  const code = client.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
  const toggles = [...code.matchAll(/orbit:toggle-panel/g)]
  assert.equal(toggles.length, 1, 'something other than /orbit is toggling the panel')
  const panelCommand = client.slice(client.indexOf('function registerOrbitSlashSource'),
    client.indexOf('interface SelectOption'))
  assert.match(panelCommand, /orbit:toggle-panel/)

  // Both working commands call it, and neither dispatches the event itself.
  // Ends at its own registration, not at whatever declaration follows it: the
  // helper is defined further down the file and would otherwise be read as
  // part of this command's body.
  const generate = client.slice(client.indexOf('function registerGenerateSlashSource'),
    client.indexOf("}), 'orbit: slash command generating a workflow')"))
  assert.ok(generate.length > 0 && generate.length < 4000, 'the generate slice ran past its command')
  const popup = client.slice(client.indexOf('function registerWorkflowPopup'),
    client.indexOf("}), 'orbit: workflow popup')"))
  for (const [where, body] of [['generate', generate], ['popup', popup]]) {
    assert.match(body, /showOrbitPanel\(/, `${where} does not open the panel`)
    assert.doesNotMatch(body, /dispatchEvent/, `${where} should go through the helper`)
  }

  // Before the work, not after it: a failure has to be met by an open panel
  // too, and writing a Workflow takes long enough that the panel is the only
  // thing that can say it started.
  const started = generate.indexOf('showOrbitPanel')
  const called = generate.indexOf('hostCall')
  assert.ok(started > 0 && started < called, 'the panel opens only if the work succeeds')

  // The generate command lands on the tab its job will appear on; the popup
  // takes no view over, having just shown the list itself.
  assert.match(generate, /showOrbitPanel\('workflows'\)/)
  assert.match(popup, /showOrbitPanel\(\)/)
})

/**
 * Being put away is not being unmounted.
 *
 * A dismissed panel renders nothing, so the only way "show it again" can reach
 * it is if the listener is registered before that return — a hook after a
 * conditional return would not run at all, and the command would be shouting
 * at something that had stopped listening.
 */
test('a hidden panel is still listening for the command that reveals it', async () => {
  const panel = await readFile(join(clientDir, 'OrbitPanel.tsx'), 'utf8')
  const listener = panel.indexOf("addEventListener('orbit:show-panel'")
  const bail = panel.indexOf('if (layout.dismissed) return null')
  assert.ok(listener > 0 && bail > 0)
  assert.ok(listener < bail, 'the show listener is registered after the panel bails out')

  // Showing clears both ways of being out of sight, not just the fold.
  const show = panel.slice(panel.indexOf('const show = (event: Event)'), listener)
  assert.match(show, /dismissed: false/)
  assert.match(show, /collapsed: false/)
  // And a tab is only taken over when one was asked for.
  assert.match(show, /detail\?\.tab === 'workflows'/)
})

/**
 * The Workflow goes into the sentence as text, not as a chip.
 *
 * The composer draws in two layers: a `.backdrop` holds the visible text and
 * the `<textarea>` over it is transparent except for its caret. They agree
 * only while both lay out the same string — and a reference is one `￼` cell
 * in the backdrop however long the name is. That is why a chipped name is
 * squeezed to about six characters, and why giving the chip a real width moved
 * the visible text 86px right while the caret did not move at all: the
 * textarea still had one `￼` to lay out. Both measured in a browser.
 *
 * Bracketed text has no `￼`, so the layers cannot drift, the name is shown
 * whole, and the caret lands where the sentence ends. The Workflow's identity
 * is not lost with the chip: the model is already handed the catalog, ids and
 * all, in its system prompt.
 */
test('a picked Workflow is written into the draft as its whole name', async () => {
  const client = await readFile(join(clientDir, 'index.tsx'), 'utf8')
  const pick = client.slice(client.indexOf('onSelect:'), client.indexOf("}), 'orbit: workflow popup')"))

  // One write, of the whole sentence, with the name entire.
  assert.match(pick, /input\.setDraft\(`\$\{head\}\$\{MARK_OPEN\}\$\{option\.label\}\$\{MARK_CLOSE\}\$\{t\('runTail'\)\}`\)/)
  assert.match(client, /const MARK_OPEN = '「'/)
  assert.match(client, /const MARK_CLOSE = '」'/)
  // Not shortened: the reason to shorten was a chip that no longer exists.
  assert.doesNotMatch(client, /referenceLabel/)

  // No chip, and nothing left over from having had one: a reference would
  // reintroduce the `￼` the two layers disagree about, and a width override
  // is what made them disagree by 86px.
  assert.doesNotMatch(client, /insertReference/)
  assert.doesNotMatch(client, /data-decoration="chip"/)
  assert.doesNotMatch(client, /syncWorkflowChipWidths|HARNESS_CHIP_LABEL_SCALE/)
  // The name table fed a codec that answered for minted references; with none
  // minted it would be filled and never read.
  assert.doesNotMatch(client, /namesById/)

  // And the caret is put where the sentence ends, which is the point of
  // writing the goal's colon last.
  assert.match(pick, /caretToEnd\(input\.state\.getSnapshot\(\)\.draft\)/)
})

/**
 * The brackets are the separation, so the sentence adds none of its own —
 * `用「名字」执行：` rather than `用 「名字」 执行：`.
 */
test('the run sentence leans on the brackets, not on spaces', async () => {
  const locale = await readFile(join(clientDir, 'locales.ts'), 'utf8')
  assert.match(locale, /runHead: '用'/)
  assert.match(locale, /runTail: '执行：'/)
  assert.doesNotMatch(locale, /runHead: '用 '/)
  assert.doesNotMatch(locale, /runTail: ' 执行：'/)
})

/**
 * Typing the command is the asking.
 *
 * `getPanelState` takes `startIfMissing`, and the entry points disagreed about
 * it: the panel passes it when it is expanded, `/orbit-generate` passes it to
 * have something to write into, and `/orbit-workflows` passed only a Session
 * id — so both trailing parameters defaulted to false. A person who typed the
 * command to find out what could run was told that nothing was running, on a
 * machine where the fix was to start the thing they had just asked about.
 *
 * Asserted on the call rather than on the popup's behaviour because that is
 * where it went wrong: the argument was not passed, not mishandled.
 */
test('every deliberate entry point starts a Runtime rather than reporting its absence', async () => {
  const client = await readFile(join(clientDir, 'index.tsx'), 'utf8')
  const panel = await readFile(join(clientDir, 'OrbitPanel.tsx'), 'utf8')

  // The popup behind `/orbit-workflows`.
  assert.match(client, /'getPanelState', \[session\.sessionId, false, true\]/)

  // Nowhere passes the Session id alone: the two flags after it are the whole
  // difference between starting Orbit and complaining that it is not up.
  for (const [source, where] of [[client, 'index.tsx'], [panel, 'OrbitPanel.tsx']]) {
    for (const [call] of source.matchAll(/'getPanelState',\s*\[[^\]]*\]/g)) {
      const args = call.slice(call.indexOf('[') + 1, -1).split(',')
      assert.equal(args.length, 3, `${where}: getPanelState needs all three arguments: ${call}`)
    }
  }
  // The panel starts one only when it is showing something; a collapsed badge
  // must not hold a Runtime up on its own.
  assert.match(panel, /'getPanelState', \[sessionId, forced, !layout\.collapsed\]/)
})

/**
 * "Something went wrong" was the answer to four failures in five.
 *
 * `panelError` classifies a raw failure into a sentence a reader can act on,
 * and falls back to `errUnknown` when nothing matches. The table was written
 * from the failures that had been seen by hand, so it covered eleven of the
 * fifty-one strings this integration can throw — and the forty it missed
 * included every error on the startup path, which is the one a person meets
 * first. A classifier that abstains four times out of five is worse than no
 * classifier: it replaces a quotable message with a sentence that says nothing.
 *
 * So the source is the test's input. Every `new Error(...)` in `src/` has to
 * be classified, which means a new throw either matches an existing reading or
 * arrives with one — it cannot quietly land on the fallback.
 */
test('every failure this integration can throw is classified', async () => {
  const table = await readFile(join(coreDir, 'error-text.ts'), 'utf8')
  const readings = [...table.matchAll(/\[\/(.+?)\/i,\s*'(\w+)'\]/gs)]
    .map(([, pattern, key]) => [new RegExp(pattern, 'i'), key])
  assert.ok(readings.length >= 20, `expected the readings table, saw ${readings.length}`)

  // Both halves. The table is the shared package's, and it has to answer for
  // what this host throws as well as for what the package does.
  const roots = [join(clientDir, '..'), join(clientDir, '..', 'client'), coreDir]
  const thrown = new Set()
  for (const here of roots) {
    for (const name of await readdir(here)) {
      if (!name.endsWith('.ts') || name.endsWith('.d.ts')) continue
      const text = await readFile(join(here, name), 'utf8')
      for (const [, message] of text.matchAll(
        /new (?:[A-Za-z]*Error)\(\s*\n?\s*[`'"]([^`'"]{10,120})/g)) {
        thrown.add(message.trim())
      }
    }
  }
  assert.ok(thrown.size >= 40, `expected the thrown messages, saw ${thrown.size}`)

  const unclassified = [...thrown].filter(
    (message) => !readings.some(([pattern]) => pattern.test(message)))
  assert.deepEqual(unclassified, [],
    'these would show the reader "Something went wrong" and nothing else')

  // Order is load-bearing where messages overlap, so the overlaps are pinned.
  const readingOf = (message) =>
    readings.find(([pattern]) => pattern.test(message))?.[1] ?? 'errUnknown'
  // A start that ran out of time is a failed start, not a generic timeout —
  // and it now carries the Runtime's own last words, which are arbitrary text
  // that must not be matched against anything further down.
  assert.equal(readingOf('Orbit Runtime auto-start timed out for Workspace /x'), 'errStartFailed')
  assert.equal(readingOf(
    'Orbit Runtime auto-start failed with code 3 for Workspace /x: run not found'), 'errStartFailed')
  // A refusal that carries a 5xx of its own is still a refusal.
  assert.equal(readingOf('Orbit refused to stop: HTTP 503'), 'errStopRefused')
  // Names both a missing Session and a missing folder; the folder is the half
  // a reader can do something about.
  assert.equal(readingOf(
    'Orbit requires the Harness Session to have a Workspace cwd'), 'errNoWorkspace')
  assert.equal(readingOf('Orbit requires a live Harness Session'), 'errNoSession')
  // The thrown text says "advertised"; the reading used to say "advertises".
  assert.equal(readingOf(
    'Orbit command is no longer advertised at this revision'), 'errRunMoved')

  // Every reading has a sentence in both languages, or it shows a key.
  const locales = await readFile(join(clientDir, 'locales.ts'), 'utf8')
  const en = locales.slice(0, locales.indexOf('zh'))
  const zh = locales.slice(locales.indexOf('zh'))
  for (const [, key] of readings) {
    assert.match(en, new RegExp(`\\b${key}:`), `${key} has no English sentence`)
    assert.match(zh, new RegExp(`\\b${key}:`), `${key} has no Chinese sentence`)
  }
})

/**
 * One flat grey drew every line in the panel, at a weight the shell never uses.
 *
 * `--dsw-alias-line-normal` is not a token; nineteen rules named it and drew
 * the `rgb(128 128 128 / 28%)` written beside them — heavier than
 * `border-l4`, the strongest line the shell has, and the same grey in both
 * themes, so it never inverted the way a border on a dark surface must.
 *
 * Replaced by weight rather than one-for-one, because the nineteen were not
 * doing one job: the panel's own edge sits on somebody else's page, a card
 * has an edge of its own, and a band separator should read like the list
 * dividers it sits among. Three tiers, and every rule is assigned to one —
 * the substitution asserts each selector is classified rather than defaulting,
 * so a new bordered element has to be placed rather than silently inheriting.
 */
test('lines are weighted by what they separate', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, '')
  assert.doesNotMatch(rules, /--dsw-alias-line-normal[,)]/,
    '--dsw-alias-line-normal is not a token the shell defines')
  assert.doesNotMatch(rules, /--dsw-alias-label-inverse[,)]/,
    '--dsw-alias-label-inverse is not a token the shell defines')

  // Every border names a rung of the shell's ramp and carries a translucent
  // fallback — translucent because one value has to darken a light surface
  // and lighten a dark one, which a flat grey cannot do.
  const borders = rules.match(/border[a-z-]*: [^;]*var\(--dsw-alias-[^;]*;/g) ?? []
  assert.ok(borders.length >= 19, `expected the panel's borders, saw ${borders.length}`)
  for (const decl of borders) {
    if (!/--dsw-alias-border-l/.test(decl)) continue
    assert.match(decl, /--dsw-alias-border-l[1-4]/, `not a ramp rung: ${decl}`)
    assert.match(decl, /rgb\(128 128 128 \/ \d+%\)/, `no translucent fallback: ${decl}`)
    assert.doesNotMatch(decl, /Canvas|GrayText|ButtonBorder|Highlight/, `system colour: ${decl}`)
  }

  // The three tiers, spot-checked where the difference is the point.
  const at = (sel, prop) => {
    const i = rules.indexOf(sel)
    assert.notEqual(i, -1, `${sel} not found`)
    const block = rules.slice(i, rules.indexOf('}', i))
    const hit = new RegExp(`${prop}: [^;]*--dsw-alias-(border-l[1-4])`).exec(block)
    assert.ok(hit, `${sel} has no ${prop}`)
    return hit[1]
  }
  // Sits on the host's page, so it needs a real edge.
  assert.equal(at('.panel {', 'border'), 'border-l4')
  assert.equal(at('.badge {', 'border'), 'border-l4')
  // Separates bands inside the panel: the same weight as the list dividers,
  // so everything that separates reads alike.
  assert.equal(at('.bar {', 'border-bottom'), 'border-l2')
  assert.equal(at('.tabs {', 'border-bottom'), 'border-l2')
  // A box with an edge of its own sits between the two.
  assert.equal(at('.agentCard {', 'border'), 'border-l3')
  assert.equal(at('.artifact {', 'border'), 'border-l3')

  // The divider rule the bands are matched to, unchanged.
  assert.match(rules, /\.defnRow::after \{[\s\S]{0,40}left: 0/)
  assert.match(rules, /\.listRow::after,\n\.flowRow::after,\n\.defnRow::after \{[^}]*--dsw-alias-border-l2/)
})

/**
 * A hover that paints the colour underneath it is not a hover.
 *
 * `--dsw-alias-bg-module` is not a token the shell defines — its module
 * surface is `bg-module-platform` — so thirteen rules fell through to the
 * `Canvas` beside them. `Canvas` is the browser's own surface colour, and the
 * shell sets `color-scheme`, so it tracked the theme and looked plausible
 * while being the one colour that could not work: measured against the page,
 * `.listRow:hover` differed by 0 channels on light and 5 on dark — and on
 * dark it went *darker* than the page it sat on. Pointing at a row did
 * nothing a person could see.
 *
 * The fallbacks are translucent grey rather than either theme's literal: one
 * value that darkens a light surface and lightens a dark one, which is what a
 * fallback has to do when it cannot know which theme it is standing in.
 */
test('module surfaces name a token that exists, and lift off the page', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, '')
  // `bg-module-platform` starts with `bg-module`, so the bare name is only
  // matched where a `,` or `)` closes it.
  assert.doesNotMatch(rules, /--dsw-alias-bg-module[,)]/,
    '--dsw-alias-bg-module is not a token the shell defines')
  assert.match(rules, /--dsw-alias-bg-module-platform[,)]/)
  // No module surface falls back to a system colour: `Canvas` is what made
  // these invisible, and it is the plausible-looking wrong answer.
  for (const decl of rules.match(/background: var\(--dsw-alias-bg-module-platform[^;]*;/g) ?? []) {
    assert.doesNotMatch(decl, /Canvas|Window|ButtonFace/, `system colour in: ${decl}`)
    assert.match(decl, /,\s*rgb\(/, `no fallback colour in: ${decl}`)
  }
  // The hovers in particular, since they are the ones with nothing else to
  // say they happened.
  for (const hover of ['listRow', 'flowButton', 'agentCard']) {
    const rule = new RegExp(`\\.${hover}:hover[^}]*background: var\\(--dsw-alias-bg-module-platform`)
    assert.match(rules, rule, `.${hover}:hover does not lift off the page`)
  }
})

/**
 * The three state names the shell has never defined.
 *
 * `state-success`, `state-danger` and `state-warning` look like tokens and are
 * not: the shell's are `state-success-primary`, `state-error-primary` and
 * `state-warn-primary`. A `var()` on a name nobody defines takes the fallback
 * beside it, so these failed in two different ways — a rule with a literal
 * fallback drew that literal in both themes, and a rule with no fallback drew
 * nothing at all, which is how a finished Run and a failed Run came to share
 * one blank 8px square.
 *
 * Banned across the sheet rather than checked rule by rule, because the ones
 * that were caught were spread over status dots, step dots, outcome text, a
 * shape chip, a callout border and a Workflow badge — six places that had
 * nothing in common except the mistake.
 */
test('no rule names a state token the shell does not define', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  // Prose mentions these names to explain them; only declarations count.
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, '')
  for (const name of ['state-success', 'state-danger', 'state-warning']) {
    assert.doesNotMatch(rules, new RegExp(`--dsw-alias-${name}[,)]`),
      `--dsw-alias-${name} is not a token the shell defines`)
  }
  // And the names that replaced them are in use, or the ban above would also
  // pass on a sheet that had simply dropped the colours.
  for (const name of ['state-success-primary', 'state-error-primary', 'state-warn-primary']) {
    assert.match(rules, new RegExp(`--dsw-alias-${name}[,)]`), `${name} unused`)
  }
})

/**
 * A dot has to be visible before it can mean anything.
 *
 * `--dsw-alias-state-success` and `--dsw-alias-state-danger` are not tokens
 * the shell defines; its own are `state-success-primary` and
 * `state-error-primary`. `.done` and `.failed` named the missing ones with
 * nothing beside them, and an invalid `background` computes to the initial
 * value — so in a browser with the shell's real variables loaded, a Run that
 * finished and a Run that failed both drew a blank 8px square. Measured, not
 * inferred: transparent for both, while `.unknown` on the same list drew grey
 * because its fallback happened to name a token that exists.
 *
 * So every dot is checked twice — that it names a real token, and that it
 * carries that token's value beside it, which is what makes the rule valid
 * even when the shell is not there to answer.
 */
test('every dot names a token that exists, and falls back to a colour', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const runDots = css.slice(css.indexOf('.live {'), css.indexOf('.goal {'))
  const stepDots = css.slice(css.indexOf('.stepDot_success'), css.indexOf('.attention {'))
  const outcomes = css.slice(css.indexOf('.outcome_done'), css.indexOf('.artifactRow'))

  // Not one of the three names the shell has no answer for.
  for (const [where, block] of [['run', runDots], ['step', stepDots], ['outcome', outcomes]]) {
    assert.doesNotMatch(block, /--dsw-alias-state-(success|danger|warning)[,)]/,
      `${where} dots still name a token the shell does not define`)
    // Every declaration carries a literal after the comma. Without one, a
    // missing token is not a wrong colour but no colour at all.
    for (const decl of block.match(/(background|color): var\([^;]*\);/g) ?? []) {
      assert.match(decl, /,\s*(rgb|#)/, `no fallback colour in: ${decl}`)
    }
  }
  // Not a system colour either. `LinkText` resolves, so the rule was valid and
  // drew — it just drew the browser's link blue for a Run that had finished.
  assert.doesNotMatch(outcomes, /LinkText|Highlight|GrayText|ActiveText/)

  // A Run and a step in the same state are the same colour: two reds for one
  // failure is the panel disagreeing with itself.
  // Three surfaces named the same four states three different ways, so the
  // mapping is spelled out rather than assumed: a Run is `.failed`, its step
  // is `.stepDot_error`, and the word under them is `.outcome_error`.
  const pairs = [['done', 'success', 'done', 'state-success-primary'],
                 ['failed', 'error', 'error', 'state-error-primary'],
                 ['unknown', 'warning', 'warning', 'state-warn-primary'],
                 ['live', 'ongoing', 'ongoing', 'state-business-primary']]
  for (const [run, step, word, token] of pairs) {
    const a = new RegExp(`\\.${run} \\{ background: var\\(--dsw-alias-${token}, (rgb\\([^)]*\\))\\)`)
    const b = new RegExp(`\\.stepDot_${step} \\{ background: var\\(--dsw-alias-${token}, (rgb\\([^)]*\\))\\)`)
    const hitA = a.exec(runDots)
    const hitB = b.exec(stepDots)
    assert.ok(hitA, `.${run} does not use ${token}`)
    assert.ok(hitB, `.stepDot_${step} does not use ${token}`)
    assert.equal(hitA[1], hitB[1], `.${run} and .stepDot_${step} fall back differently`)
    // …and the word beside the dot says it in the same colour.
    const c = new RegExp(`\\.outcome_${word} \\{ color: var\\(--dsw-alias-${token}, (rgb\\([^)]*\\))\\)`)
    const hitC = c.exec(outcomes)
    assert.ok(hitC, `.outcome_${word} does not use ${token}`)
    assert.equal(hitA[1], hitC[1], `.${run} and .outcome_${word} disagree`)
  }
})

/**
 * A step is a kind before it is anything else.
 *
 * The list drew a stripe for three kinds and nothing for the other two, so
 * `terminal` and `join` rows began at a transparent 2px gap: the eye read
 * them as steps whose colour had failed to load rather than as steps of a
 * different sort. And the two colours it did draw named
 * `--dsw-alias-state-warning` and `--dsw-alias-state-success`, neither of
 * which the shell defines — the shell's are `state-warn-primary` and
 * `state-success-primary`, so those rules quietly painted the literal beside
 * them and stayed the same colour in both themes.
 *
 * Kept honest by naming the five kinds a definition can actually hold. A
 * sixth would arrive with no rule of its own and inherit the neutral, which
 * is the intended reading and not a hole.
 */
test('every step is striped, and the stripe names its kind', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const stripes = css.slice(css.indexOf('.kind_action'), css.indexOf('.defnHead {'))
  // Work, a person, an ending: the three that carry a hue.
  assert.match(stripes, /\.kind_action \{[^}]*--dsw-alias-state-business-primary/)
  assert.match(stripes, /\.kind_human \{[^}]*--dsw-alias-state-warn-primary/)
  assert.match(stripes, /\.kind_terminal \{[^}]*--dsw-alias-state-success-primary/)
  // Routing, in neutrals — brighter for the branch than for the merge.
  assert.match(stripes, /\.kind_decision \{[^}]*--dsw-alias-label-secondary/)
  assert.match(stripes, /\.kind_join \{[^}]*--dsw-alias-label-tertiary/)
  // Every kind a definition holds has one, so no row starts on a blank 2px.
  for (const kind of ['action', 'human', 'terminal', 'decision', 'join']) {
    assert.match(stripes, new RegExp(`\\.kind_${kind} \\{`), `no stripe for ${kind}`)
  }
  assert.doesNotMatch(stripes, /transparent/)
  // Including the base rule an unnamed kind falls through to. A transparent
  // border still reserves its 2px, so "no colour" is not a missing stripe but
  // a stripe-shaped hole — the exact thing `terminal` and `join` used to show.
  const base = css.slice(css.indexOf('.defnRow {\n  padding'), css.indexOf('.kind_action'))
  assert.doesNotMatch(base, /transparent/)
  assert.match(base, /border-left: 2px solid var\(--dsw-alias-label-tertiary/)
  // Not the names the shell lacks; a sheet-wide ban has its own test below.
  assert.doesNotMatch(stripes, /--dsw-alias-state-(warning|success)[,)]/)
  const chips = css.slice(css.indexOf('.node_human'), css.indexOf('.node_more'))
  assert.match(chips, /\.node_human \{[^}]*--dsw-alias-state-warn-primary/)
  assert.match(chips, /\.node_terminal \{[^}]*--dsw-alias-state-success-primary/)
  // The stripe sits at the row's leading edge, so the divider meets it there
  // rather than floating 12px in and leaving the stripe hanging past the row.
  assert.match(css, /\.defnRow::after \{ left: 0; \}/)
  // The colours only reach a row if the kind reaches the class name. An
  // allow-list here was what dropped `terminal` and `join`: the stylesheet
  // could name every kind and they would still render unstyled.
  const tsx = await readFile(join(clientDir, 'OrbitWorkflowDetail.tsx'), 'utf8')
  assert.match(tsx, /styles\[`kind_\$\{step\.kind\}`\]/)
  assert.doesNotMatch(tsx, /ACCENTED/)
})

/**
 * A History row says what was asked for, not which definition answered it.
 *
 * It used to carry `workflow:wf_7ba1a9d2-…@2` under the goal — the same string
 * on every Run of one Workflow, which is the case a reader is trying to tell
 * apart. The request is what differs, and the Run's own page still carries the
 * id for whoever wants it.
 */
test('a History row shows the request, folded at two lines', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const row = rows.slice(rows.indexOf('export function OrbitRunListRow'))
  const body = row.slice(0, row.indexOf('\n}'))
  assert.match(body, /styles\.listPrompt/)
  assert.match(body, /run\.prompt \? /, 'a row is better short than padded with an empty line')
  assert.doesNotMatch(body, /run\.workflow/)

  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const rule = css.slice(css.indexOf('.listPrompt {'), css.indexOf('\n}', css.indexOf('.listPrompt {')))
  // Two lines, because a request is often a paragraph and a row that grew with
  // it would push the rest of the list off the page.
  assert.match(rule, /-webkit-line-clamp: 2/)
  assert.match(rule, /overflow: hidden/)
  // And it can never widen the row: a pasted URL is one unbreakable line.
  assert.match(rule, /overflow-wrap: anywhere/)
  // Newlines fold into spaces here and only here: two lines are a glance, and
  // a request that opens with a blank line would spend one saying nothing.
  assert.match(rule, /white-space: normal/)
  // The Goal page still shows the request exactly as it was written.
  const goal = css.slice(css.indexOf('.goalPrompt {'), css.indexOf('.goalPrompt[data-open]'))
  assert.match(goal, /white-space: pre-wrap/)
})

/**
 * The control that unfolds a request belongs to the block it unfolds.
 *
 * Outside the tinted card it read as a separate remark that happened to sit
 * under one. And it carries the same chevron the step disclosures do, turned
 * the same way, so one arrow means one thing across the panel.
 */
test('the request unfolds from inside its own card, by an arrow', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const fn = rows.slice(rows.indexOf('function FoldedText'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  // The toggle is nested in the card, not a sibling of it.
  const card = body.indexOf('styles.goalPromptCard')
  assert.ok(card > 0 && card < body.indexOf('styles.goalPromptToggle'))
  assert.doesNotMatch(body, /<>/, 'the card replaced the fragment that held them apart')
  assert.match(body, /<IconChevronDownOutline14/)
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  // Points down to open, up to close — the same turn a step disclosure makes.
  assert.match(css, /\.goalPromptChevronOpen \{ transform: rotate\(180deg\)/)
  assert.match(css, /\.stepChevronOpen \{ transform: rotate\(180deg\)/)
  // The accent, like the way out of a detail page: a control the colour of the
  // text it sits under is something a reader finds rather than sees. And a
  // token the shell defines, not the fallback beside an undefined one.
  const toggle = css.slice(css.indexOf('.goalPromptToggle {'), css.indexOf('.goalPromptToggle:hover'))
  assert.match(toggle, /color: var\(--dsw-alias-state-business-primary, Highlight\)/)
})

/**
 * A step's console folds like everything else, rather than scrolling.
 *
 * It was the one block in the panel that answered "show me the rest" with a
 * scrollbar while its neighbour a few pixels away answered with a control —
 * the same gesture meaning two things on one page. They are one widget now.
 */
test('a step console and a request are the same folded block', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  assert.match(rows, /<FoldedText t=\{t\} text=\{text\} lines=\{OUTPUT_LINES\}/)
  assert.match(rows, /<FoldedText t=\{t\} text=\{run\.prompt\} lines=\{PROMPT_LINES\}/)
  // One implementation, or the two drift the moment either is restyled.
  assert.equal((rows.match(/function FoldedText/g) ?? []).length, 1)
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  // And the scroll box it replaced is gone, not merely unused.
  assert.equal(css.includes('.output {'), false)
})

/**
 * What can be done to a Run sits under what the Run was asked to do, centred.
 *
 * On the left edge of the card it read as a footnote to the line above it,
 * and in the body colour it was something to find rather than see. `primary`
 * is the shell's own highlight — backed by the `--dsw-alias-button-primary-*`
 * family — so it follows the theme, which a colour mixed here would not.
 */
test('a Run\'s controls are highlighted and centred under its request', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const fn = rows.slice(rows.indexOf('function RunControls'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  assert.match(body, /styles\.runActions/)
  // Bounded by the branch it belongs to, not by a character count: a comment
  // added between the two would slide the button out of a fixed window.
  const cancel = body.slice(body.indexOf('cancelAt !== undefined ?'), body.indexOf("act('langgraph_run.cancel'"))
  assert.match(cancel, /variant="primary"/)
  assert.doesNotMatch(cancel, /variant="outline"/)

  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  assert.match(css, /\.runActions \{[^}]*justify-content: center/)
  // The step's own reconciliation buttons keep the left edge they share with
  // that step's text: they answer a question inside a step, not about the Run.
  const shared = css.slice(css.indexOf('.actions {'), css.indexOf('.runActions'))
  assert.doesNotMatch(shared, /justify-content/)
})

/**
 * A yes/no question is answered by choosing, not by describing the choice.
 *
 * A human node with `task_kind: approval` was answered through a text box: the
 * person had to know that a reply must name the output port, that the branches
 * test a `decision` field, and to spell both without a typo minutes after the
 * question was asked. All of that is in the interrupt already.
 */
test('an approval is two buttons, and anything else still takes an answer', async () => {
  const rows = sources[names.indexOf('OrbitRunRow.tsx')]
  const fn = rows.slice(rows.indexOf('function RunControls'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  // Chosen from what the Run is actually stopped on, not assumed.
  assert.match(body, /run\.interrupts\.find\(item => item\.taskKind === 'approval'\)/)
  assert.match(body, /approvalValue\(approval, decision\)/)
  // Answered against that interrupt, not just against the Run.
  assert.match(body, /approvalValue\(approval, decision\), approval\.id/)
  // The typed answer survives for the questions this panel cannot shape.
  assert.match(body, /placeholder=\{t\('answer'\)\}/)
  assert.match(body, /approval !== undefined \? \(/)
  const locales = sources[names.indexOf('locales.ts')]
  for (const key of ['approve:', 'reject:']) assert.ok(locales.includes(key), key)
})

/**
 * Every block of writing the panel did not produce wears the same card.
 *
 * The request, a step's console, an inlined Artifact, and a Run's result are
 * one kind of thing to a reader. The result reached for `--dsw-alias-bg-module`
 * — not a token the shell defines — so it fell through to `Canvas`, the page's
 * own colour, which is to say no card at all.
 */
test('a result sits on the same card as every other quoted block', async () => {
  const css = await readFile(join(clientDir, 'OrbitPanel.module.css'), 'utf8')
  const card = /background: var\(--dsw-alias-bg-module-platform, [^)]*\)[^;]*;/
  for (const rule of ['.result {', '.goalPromptCard {', '.artifactText {']) {
    const block = css.slice(css.indexOf(rule), css.indexOf('\n}', css.indexOf(rule)))
    assert.match(block, card, `${rule} must use the shade the shell actually defines`)
    assert.match(block, /border-radius: 6px/, `${rule} must round like the others`)
  }
  // Narrowed to the cards on purpose: the bare `--dsw-alias-bg-module` is still
  // reached for by the bar, the tabs and every hover in the panel, each of
  // which therefore paints the page's own colour. That is a separate finding,
  // not something this test should quietly hold the line on.
  for (const rule of ['.result {', '.goalPromptCard {', '.artifactText {']) {
    const block = css.slice(css.indexOf(rule), css.indexOf('\n}', css.indexOf(rule)))
    assert.equal(/background: var\(--dsw-alias-bg-module,/.test(block), false, rule)
  }
})
