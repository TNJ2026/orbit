import assert from 'node:assert/strict'
import test from 'node:test'

import { artifactImageInput } from '../lib/artifact-import.js'

test('converts a bounded Orbit image Artifact into a Harness Attachment input', () => {
  const result = artifactImageInput({
    artifact: { artifact_id: 'artifact:1', run_id: 'run:1', content_type: 'image/png', name: 'chart.png' },
    encoding: 'base64', content: Buffer.from('png-bytes').toString('base64'),
  })
  assert.equal(result.mediaType, 'image/png')
  assert.equal(result.name, 'chart.png')
  assert.equal(Buffer.from(result.data).toString(), 'png-bytes')
})

test('refuses unsupported media and malformed base64 before Attachment storage', () => {
  const base = { artifact: { artifact_id: 'artifact:1', run_id: 'run:1' }, encoding: 'base64' }
  assert.throws(() => artifactImageInput({ ...base, artifact: { ...base.artifact, content_type: 'text/plain' }, content: 'dGV4dA==' }), /images only/)
  assert.throws(() => artifactImageInput({ ...base, artifact: { ...base.artifact, content_type: 'image/png' }, content: '***' }), /canonical base64/)
})
