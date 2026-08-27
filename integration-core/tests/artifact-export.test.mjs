import assert from 'node:assert/strict'
import test from 'node:test'

import {
  READABLE_MAX_BYTES, artifactExtension, artifactFilename, readableAsText,
} from '../lib/artifact-export.js'

const ART = 'langgraph_artifact:4c1e5281cb5f49d9b60afbbac0508a7c4c2c37022312a116ebd54d703adfecd6'

test('the type Orbit recorded decides the extension', () => {
  // Every Artifact in a real store here is text/markdown, and a `.md` is the
  // difference between a file that opens and one that has to be told what it is.
  assert.equal(artifactExtension('text/markdown'), '.md')
  assert.equal(artifactExtension('text/html'), '.html')
  assert.equal(artifactExtension('application/json'), '.json')
  assert.equal(artifactExtension('image/png'), '.png')
  // Parameters ride along on a recorded type and are not part of the name.
  assert.equal(artifactExtension('text/markdown; charset=utf-8'), '.md')
  assert.equal(artifactExtension('TEXT/MARKDOWN'), '.md')
})

test('a name the workflow chose beats the table', () => {
  // A workflow that said what it was writing knows better than a lookup does.
  assert.equal(artifactExtension('application/octet-stream', 'report.pdf'), '.pdf')
  assert.equal(artifactExtension('text/markdown', 'notes.TXT'), '.txt')
  // A name with no extension says nothing, so the type still answers.
  assert.equal(artifactExtension('text/markdown', 'notes'), '.md')
})

test('nobody saying anything is `.bin`, which is a statement rather than a guess', () => {
  assert.equal(artifactExtension('application/octet-stream'), '.bin')
  assert.equal(artifactExtension(''), '.bin')
  assert.equal(artifactExtension(null), '.bin')
  assert.equal(artifactExtension(undefined, null), '.bin')
  assert.equal(artifactExtension('application/x-nobody-has-heard-of'), '.bin')
})

test('the copy is named for the Artifact, so exporting twice writes one file', () => {
  // The name an Artifact came from *is* the hash of its bytes, so the same id
  // is the same content — a second export is the same file, not a `(1)` copy.
  assert.equal(artifactFilename(ART, 'text/markdown'), 'orbit-4c1e5281cb5f.md')
  assert.equal(artifactFilename(ART, 'text/markdown'), artifactFilename(ART, 'text/markdown'))
})

test('a filename can never escape the directory it is written into', () => {
  // The id reaches this from a Run's result, which a workflow wrote. Anything
  // that is not a digest character is not part of the name.
  for (const hostile of [
    'langgraph_artifact:../../etc/passwd',
    'langgraph_artifact:/absolute',
    'langgraph_artifact:..',
    '../../../evil',
  ]) {
    const name = artifactFilename(hostile, 'text/plain')
    assert.equal(/[/\\]|\.\./.test(name.slice(0, -4)), false, `${hostile} -> ${name}`)
    assert.match(name, /^orbit-[A-Za-z0-9]*\.[a-z0-9]+$/)
  }
  // And a name a workflow supplied cannot bring a path with it either.
  assert.equal(artifactFilename(ART, '', '../../evil.sh'), 'orbit-4c1e5281cb5f.sh')
})


test('text small enough to be the answer is read here, not handed over', () => {
  // The real shape in this workspace: every Artifact is text/markdown, a few
  // hundred bytes, and it *is* the translation somebody asked for.
  assert.equal(readableAsText('text/markdown', 435), true)
  assert.equal(readableAsText('text/plain', 0), true)
  assert.equal(readableAsText('text/markdown; charset=utf-8', 2934 % 2048), true)
  assert.equal(readableAsText('TEXT/PLAIN', 10), true)
})

test('past the size it stops being something to read on a panel', () => {
  assert.equal(readableAsText('text/markdown', READABLE_MAX_BYTES), false, 'the bound is exclusive')
  assert.equal(readableAsText('text/markdown', READABLE_MAX_BYTES - 1), true)
  assert.equal(readableAsText('text/markdown', 5_000_000), false)
})

test('anything not plainly text is a file, including what a browser could render', () => {
  // Rendering someone else's HTML inside the panel's own page is not reading,
  // it is hosting — and the bytes are whatever a workflow chose to write.
  assert.equal(readableAsText('text/html', 100), false)
  assert.equal(readableAsText('image/png', 100), false)
  assert.equal(readableAsText('application/json', 100), false)
  assert.equal(readableAsText('application/octet-stream', 10), false)
})

test('a size nobody recorded is a file, not a gamble', () => {
  assert.equal(readableAsText('text/markdown', null), false)
  assert.equal(readableAsText('text/markdown', undefined), false)
  assert.equal(readableAsText('text/markdown', NaN), false)
  assert.equal(readableAsText(null, 10), false)
})
