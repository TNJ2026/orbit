import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const bundle = resolve(here, '..')
const dsh = process.env.DSH_BIN || (process.platform === 'win32' ? 'dsh.cmd' : 'dsh')
const home = await mkdtemp(resolve(tmpdir(), 'orbit-dsh-profile-'))
const environment = { ...process.env, DSH_HOME: home }
let web

function run(args) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(dsh, args, { cwd: bundle, env: environment, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = '', stderr = ''
    child.stdout.setEncoding('utf8'); child.stdout.on('data', chunk => { stdout += chunk })
    child.stderr.setEncoding('utf8'); child.stderr.on('data', chunk => { stderr += chunk })
    child.once('error', reject)
    child.once('exit', code => code === 0
      ? resolveRun({ stdout, stderr })
      : reject(new Error(`dsh ${args.join(' ')} exited ${String(code)}\n${stdout}${stderr}`)))
  })
}

async function startWeb() {
  return await new Promise((resolveWeb, reject) => {
    const child = spawn(dsh, ['web', '--port', '0'], {
      cwd: bundle, env: environment, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let output = ''
    const timer = setTimeout(() => reject(new Error(`Harness Web startup timed out\n${output}`)), 20_000)
    const inspect = chunk => {
      output += chunk
      const match = output.match(/dsh web:\s+(http:\/\/[^\s]+)/)
      if (match) { clearTimeout(timer); resolveWeb({ child, url: match[1] }) }
    }
    child.stdout.setEncoding('utf8'); child.stdout.on('data', inspect)
    child.stderr.setEncoding('utf8'); child.stderr.on('data', inspect)
    child.once('error', error => { clearTimeout(timer); reject(error) })
    child.once('exit', code => { clearTimeout(timer); reject(new Error(`Harness Web exited during startup: ${String(code)}\n${output}`)) })
  })
}

async function stop(child) {
  if (!child || child.exitCode !== null) return
  child.kill('SIGTERM')
  await Promise.race([
    new Promise(resolveExit => child.once('exit', resolveExit)),
    new Promise((_, reject) => setTimeout(() => reject(new Error('Harness Web did not stop')), 10_000)),
  ])
}

try {
  await run(['plugin', '--profile', 'web', 'add', bundle])
  const installed = await run(['--profile', 'web', '--dump-config'])
  assert.match(installed.stdout, /name: '@orbit-runtime\/dsh-orbit'/)

  web = await startWeb()
  const response = await fetch(`${web.url}/`)
  assert.equal(response.status, 200)
  await stop(web.child); web = undefined

  await run(['plugin', '--profile', 'web', 'remove', '@orbit-runtime/dsh-orbit'])
  const removed = await run(['--profile', 'web', '--dump-config'])
  assert.doesNotMatch(removed.stdout, /name: '@orbit-runtime\/dsh-orbit'/)
  process.stdout.write('Harness Profile install/start/remove smoke passed\n')
} finally {
  await stop(web?.child)
  await rm(home, { recursive: true, force: true })
}
