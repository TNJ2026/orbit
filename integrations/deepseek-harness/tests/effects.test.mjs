import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { promisify } from 'node:util'
import test from 'node:test'

import { effectManifest, snapshotWorkspace } from '../lib/effects.js'

const exec = promisify(execFile)

test('effect manifest observes changed, created, and deleted files', async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'orbit-effects-'))
  t.after(() => rm(cwd, { recursive: true, force: true }))
  await exec('git', ['init', cwd])
  await exec('git', ['-C', cwd, 'config', 'user.email', 'orbit@example.invalid'])
  await exec('git', ['-C', cwd, 'config', 'user.name', 'Orbit Test'])
  await writeFile(join(cwd, 'changed.txt'), 'before')
  await writeFile(join(cwd, 'deleted.txt'), 'delete me')
  await exec('git', ['-C', cwd, 'add', '.'])
  await exec('git', ['-C', cwd, 'commit', '-m', 'base'])
  const before = await snapshotWorkspace(cwd)
  await writeFile(join(cwd, 'changed.txt'), 'after')
  await writeFile(join(cwd, 'created.txt'), 'new')
  await exec('git', ['-C', cwd, 'rm', 'deleted.txt'])
  const effects = effectManifest(before, await snapshotWorkspace(cwd))
  assert.deepEqual(effects.changedFiles, ['changed.txt'])
  assert.deepEqual(effects.createdFiles, ['created.txt'])
  assert.deepEqual(effects.deletedFiles, ['deleted.txt'])
  assert.equal(effects.observation, 'git-status')
})

test('effect manifest detects a second change to an already dirty file', async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'orbit-effects-dirty-'))
  t.after(() => rm(cwd, { recursive: true, force: true }))
  await exec('git', ['init', cwd])
  await exec('git', ['-C', cwd, 'config', 'user.email', 'orbit@example.invalid'])
  await exec('git', ['-C', cwd, 'config', 'user.name', 'Orbit Test'])
  await writeFile(join(cwd, 'dirty.txt'), 'base')
  await exec('git', ['-C', cwd, 'add', '.'])
  await exec('git', ['-C', cwd, 'commit', '-m', 'base'])
  await writeFile(join(cwd, 'dirty.txt'), 'first change')
  const before = await snapshotWorkspace(cwd)
  await writeFile(join(cwd, 'dirty.txt'), 'second change')
  const effects = effectManifest(before, await snapshotWorkspace(cwd))
  assert.deepEqual(effects.changedFiles, ['dirty.txt'])
})
