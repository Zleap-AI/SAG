import { execFileSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { withFileLock, type FileLockOptions } from '@deepseek-ai/dsh-atomic-write'

function parents(): Map<number, number> {
  const result = new Map<number, number>()
  if (process.platform === 'win32') {
    const text = execFileSync('powershell.exe', [
      '-NoProfile', '-NonInteractive', '-Command',
      'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress',
    ], { encoding: 'utf8', timeout: 2500, maxBuffer: 2 * 1024 * 1024 })
    const rows = JSON.parse(text)
    for (const row of Array.isArray(rows) ? rows : [rows]) result.set(row.ProcessId, row.ParentProcessId)
  } else {
    const text = execFileSync('ps', ['-axo', 'pid=,ppid='], { encoding: 'utf8', timeout: 2500, maxBuffer: 2 * 1024 * 1024 })
    for (const line of text.trim().split('\n')) {
      const [pid, parent] = line.trim().split(/\s+/).map(Number)
      if (pid !== undefined && parent !== undefined) result.set(pid, parent)
    }
  }
  return result
}

function descendsFrom(pid: number, ancestor: number, ancestry: ReadonlyMap<number, number>): boolean {
  const seen = new Set<number>()
  while (pid > 0 && !seen.has(pid)) {
    if (pid === ancestor) return true
    seen.add(pid)
    pid = ancestry.get(pid) ?? 0
  }
  return false
}

async function optional(path: string): Promise<string | undefined> {
  try { return await readFile(path, 'utf8') } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined
    throw error
  }
}

async function ownsInheritedRun(dir: string): Promise<boolean> {
  const lockPath = join(dir, 'package.json.lock')
  const runPath = join(dir, '.plugin-manager', 'run.json')
  // The manager writes run.json just after spawning pnpm. Allow that short
  // publication race before falling back to ordinary lock contention.
  for (let attempt = 0; attempt < 5; attempt++) {
    const [lock, record] = await Promise.all([optional(lockPath), optional(runPath)])
    if (lock === undefined || !/^\d+\s*$/.test(lock)) return false
    if (record === undefined) {
      if (attempt < 4) await new Promise(resolve => setTimeout(resolve, 50))
      continue
    }
    const owner = Number(lock.trim())
    const run = JSON.parse(record)
    if (!Number.isSafeInteger(owner) || owner <= 0 || owner === process.pid
      || !Number.isSafeInteger(run?.pid) || run.pid <= 0 || typeof run.grouped !== 'boolean') return false
    const ancestry = parents()
    // This profile's manager owns the lock and is waiting for the recorded
    // pnpm child; our CLI must belong to that same child's process tree.
    if (ancestry.get(run.pid) !== owner || !descendsFrom(process.pid, run.pid, ancestry)) return false
    const [currentLock, currentRecord] = await Promise.all([optional(lockPath), optional(runPath)])
    if (currentLock !== lock || currentRecord !== record) return false
    process.kill(owner, 0)
    return true
  }
  return false
}

/** Borrow only a proven enclosing dsh package operation; never release its lock. */
export async function withCliProfileLock<T>(dir: string, operation: () => Promise<T>, options?: FileLockOptions): Promise<T> {
  let inherited = false
  try { inherited = await ownsInheritedRun(dir) } catch { /* Missing OS ancestry or malformed records fail closed. */ }
  if (inherited) return operation()
  return withFileLock(join(dir, 'package.json'), operation, options)
}
