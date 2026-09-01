import { spawn } from 'node:child_process'

const baselines = [
  ['rc', process.env.DSH_RC_VERSION || '0.1.1-rc.2', process.env.DSH_RC_BIN],
  ['alpha', process.env.DSH_ALPHA_VERSION || '0.1.2-alpha.3', process.env.DSH_ALPHA_BIN],
]

for (const [name, version, installedBin] of baselines) {
  process.stdout.write(`\n=== Harness ${name} (${version}) Profile smoke ===\n`)
  await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['tests/profile-smoke.mjs'], {
      cwd: new URL('..', import.meta.url),
      env: {
        ...process.env,
        DSH_BIN: installedBin || (process.platform === 'win32' ? 'npx.cmd' : 'npx'),
        DSH_BIN_ARGS: installedBin
          ? JSON.stringify([])
          : JSON.stringify(['--yes', '--package', `@deepseek-ai/dsh@${version}`, 'dsh']),
      },
      stdio: 'inherit',
    })
    child.once('error', reject)
    child.once('exit', code => code === 0 ? resolve() : reject(
      new Error(`Harness ${name} Profile smoke exited ${String(code)}`),
    ))
  })
}

process.stdout.write('\nHarness RC/alpha Profile smoke matrix passed\n')
