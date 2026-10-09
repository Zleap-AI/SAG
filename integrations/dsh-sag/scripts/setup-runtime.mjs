import { access, mkdir } from 'node:fs/promises'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const args = process.argv.slice(2)
function option(name) {
  const index = args.indexOf(name)
  if (index < 0 || !args[index + 1]) throw new Error(`${name} is required`)
  return args[index + 1]
}

function run(command, commandArgs, options = {}) {
  const result = spawnSync(command, commandArgs, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...options })
  if (result.status !== 0) throw new Error(result.stderr.trim() || `${command} exited ${result.status}`)
  return result.stdout.trim()
}

const python = option('--python')
const target = resolve(option('--target'))
const version = run(python, ['-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'])
const [major, minor] = version.split('.').map(Number)
if (major < 3 || (major === 3 && minor < 11)) throw new Error(`Python 3.11+ is required; got ${version}`)

const scriptDir = dirname(fileURLToPath(import.meta.url))
const candidates = [resolve(scriptDir, '../python/runtime'), resolve(scriptDir, '../runtime')]
let project
for (const candidate of candidates) {
  try {
    await access(join(candidate, 'uv.lock'))
    project = candidate
    break
  } catch (_candidateDoesNotContainRuntimeLock) {
    // The script supports both the source workspace and the packed Bundle layout.
  }
}
if (!project) throw new Error('bundled dsh-sag runtime project is missing')

const environment = join(target, 'dsh-sag-runtime-0.14.0')
await mkdir(target, { recursive: true })
run('uv', ['venv', '--python', python, environment])
run('uv', ['sync', '--frozen', '--no-dev', '--project', project], {
  env: { ...process.env, UV_PROJECT_ENVIRONMENT: environment, PATH: `${process.env.PATH ?? ''}${delimiter}${dirname(python)}` },
})
const executable = process.platform === 'win32' ? join(environment, 'Scripts', 'python.exe') : join(environment, 'bin', 'python')
run(executable, ['-c', "import importlib.metadata as m; assert m.version('zleap-sag') == '0.14.0'"])
process.stdout.write(`${executable}\n`)
