import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { lstat, readFile, readlink } from 'node:fs/promises'
import { resolve } from 'node:path'
import { promisify } from 'node:util'

const run = promisify(execFile)

export interface WorkspaceEffects {
  baseRevision?: string
  finalRevision?: string
  changedFiles: string[]
  createdFiles: string[]
  deletedFiles: string[]
  generatedArtifacts: string[]
  commandCategories: string[]
  observation: 'git-status' | 'unavailable'
}

interface Entry { code: string; digest?: string }
interface Snapshot { revision?: string; entries: Map<string, Entry> }

async function git(cwd: string, args: string[]): Promise<string> {
  const result = await run('git', ['-C', cwd, ...args], {
    timeout: 10_000, maxBuffer: 2 * 1024 * 1024, encoding: 'utf8',
  })
  return result.stdout
}

export async function snapshotWorkspace(cwd: string): Promise<Snapshot | null> {
  try {
    const [revision, status] = await Promise.all([
      git(cwd, ['rev-parse', '--verify', 'HEAD']),
      git(cwd, ['status', '--porcelain=v1', '-z', '--untracked-files=all']),
    ])
    const entries = new Map<string, Entry>()
    for (const record of status.split('\0')) {
      if (!record) continue
      const code = record.slice(0, 2)
      const path = record.slice(3)
      if (path) {
        let digest: string | undefined
        if (!code.includes('D')) {
          try {
            const target = resolve(cwd, path)
            const metadata = await lstat(target)
            const content = metadata.isSymbolicLink() ? Buffer.from(await readlink(target)) : await readFile(target)
            digest = createHash('sha256').update(content).digest('hex')
          }
          catch { /* a disappearing path is represented by its status alone */ }
        }
        entries.set(path, { code, digest })
      }
    }
    return { revision: revision.trim(), entries }
  } catch { return null }
}

export function effectManifest(before: Snapshot | null, after: Snapshot | null): WorkspaceEffects {
  if (!before || !after) return {
    changedFiles: [], createdFiles: [], deletedFiles: [], generatedArtifacts: [],
    commandCategories: [], observation: 'unavailable',
  }
  const changedFiles: string[] = [], createdFiles: string[] = [], deletedFiles: string[] = []
  const paths = new Set([...before.entries.keys(), ...after.entries.keys()])
  for (const path of [...paths].sort()) {
    const prior = before.entries.get(path), current = after.entries.get(path)
    if (JSON.stringify(prior) === JSON.stringify(current)) continue
    if (current === undefined || current.code.includes('D')) deletedFiles.push(path)
    else if (prior === undefined && (current.code === '??' || current.code.includes('A'))) createdFiles.push(path)
    else changedFiles.push(path)
  }
  return {
    baseRevision: before.revision, finalRevision: after.revision,
    changedFiles, createdFiles, deletedFiles, generatedArtifacts: [],
    commandCategories: [], observation: 'git-status',
  }
}
