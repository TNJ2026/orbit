import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

if (process.argv.slice(-2).join(' ') !== 'runtimes --json') process.exit(64)
process.stdout.write(await readFile(join(process.cwd(), 'runtime.json'), 'utf8'))
