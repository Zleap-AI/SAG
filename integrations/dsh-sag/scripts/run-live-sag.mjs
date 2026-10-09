#!/usr/bin/env node
/** Reproducible real SAG acceptance; only the external model provider is a fixture. */
import assert from 'node:assert/strict'
import { spawn, execFile } from 'node:child_process'
import { createRequire } from 'node:module'
import { openSync, closeSync } from 'node:fs'
import { mkdir, mkdtemp, readdir, readFile, realpath, stat, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { delimiter, dirname, isAbsolute, join, resolve } from 'node:path'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { parseArgs, promisify } from 'node:util'

const { values } = parseArgs({ options: { evidence: { type: 'string' }, help: { type: 'boolean', short: 'h' } } })
if (values.help) {
  console.log('Usage: node scripts/run-live-sag.mjs [--evidence <new disposable directory>]')
  console.log('Prerequisites: uv sync --project ../../apps/api --frozen --extra dev; pnpm run build')
  console.log('Uses the published zleap-sag 0.14.0 and real SAG HTTP/MCP/SQLite/LanceDB, with deterministic local extraction/embedding responses. External model quality is not validated.')
  process.exit(0)
}
const integrationRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const apiRoot = resolve(integrationRoot, '../../apps/api')
const python = join(apiRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const engine = await promisify(execFile)(python, ['-c', 'import importlib.metadata; print(importlib.metadata.version("zleap-sag"))'])
assert.equal(engine.stdout.trim(), '0.14.0', 'Install the pinned published API dependencies first')
const evidence = values.evidence ? resolve(values.evidence) : await mkdtemp(join(tmpdir(), 'dsh-sag-real-'))
await mkdir(evidence, { recursive: true })
assert.deepEqual(await readdir(evidence), [], 'Use a new, empty evidence directory to keep acceptance storage isolated')
const summaryPath = join(evidence, 'live-summary.json')
console.log(`Real SAG acceptance evidence: ${evidence}`)
const port = async () => {
  const reservation = createServer()
  await new Promise((resolve, reject) => { reservation.once('error', reject); reservation.listen(0, '127.0.0.1', resolve) })
  const selected = reservation.address().port
  await new Promise(resolve => reservation.close(resolve))
  return selected
}
const apiPort = await port()
let modelPort
do { modelPort = await port() } while (modelPort === apiPort)
const origin = `http://127.0.0.1:${apiPort}`
const connectionFile = join(evidence, 'connection.json')
const authFile = join(evidence, 'bootstrap-auth.json')
const uploadFile = join(evidence, 'uploaded.md')
await writeFile(uploadFile, '# SAG acceptance\n\nAcceptance marker ORBIT-7246. The integration document says the maintenance window is Tuesday at 09:30 Asia/Shanghai. Upload, processing, vector search, and evidence read must preserve this exact sentence.\n')
const cleanEnvironment = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('SAG_')))
const env = { ...cleanEnvironment,
  PATH: [dirname(process.execPath), process.env.PATH].filter(Boolean).join(delimiter),
  DSH_TELEMETRY_DISABLED: '1',
  NO_PROXY: [process.env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(','),
  no_proxy: [process.env.no_proxy, '127.0.0.1', 'localhost'].filter(Boolean).join(','),
  SAG_DATABASE_URL: `sqlite+aiosqlite:///${join(evidence, 'sag.db')}`, SAG_DATA_DIR: join(evidence, 'engine'), SAG_UPLOAD_DIR: join(evidence, 'uploads'),
  SAG_DSH_CONNECTION_FILE: connectionFile, SAG_DSH_PUBLIC_URL: origin, SAG_DSH_LOCAL_DISCOVERY: 'true',
  SAG_AUTH_MODE: 'local', SAG_SECRET_KEY: randomBytes(32).toString('hex'), SAG_DEBUG: 'false',
  SAG_LLM_PROVIDER: 'openai', SAG_LLM_API_KEY: 'deterministic-fixture-only', SAG_LLM_MODEL: 'fixture-extraction', SAG_LLM_BASE_URL: `http://127.0.0.1:${modelPort}/v1`,
  SAG_EMBEDDING_API_KEY: 'deterministic-fixture-only', SAG_EMBEDDING_MODEL: 'fixture-embedding', SAG_EMBEDDING_BASE_URL: `http://127.0.0.1:${modelPort}/v1`,
  SAG_EMBEDDING_SCHEMA_DIMENSIONS: '16', SAG_EMBEDDING_REQUEST_DIMENSIONS: '16',
  SAG_DOCUMENT_EXTRACT_CONCURRENCY: '1', SAG_JOB_CONCURRENCY: '1', SAG_JOB_MAX_ATTEMPTS: '1', SAG_LLM_MAX_RETRIES: '0',
  SAG_LLM_TIMEOUT_MS: '10000', SAG_EMBEDDING_TIMEOUT: '10', SAG_DOCUMENT_PARSER: 'markitdown', SAG_SEARCH_STRATEGY: 'vector', SAG_ENGINE_WARMUP_COUNT: '0',
}
const dshBin = process.env.DSH_BIN ?? 'dsh'
const dshVersion = await promisify(execFile)(dshBin, ['--version'], { env, timeout: 30000 })
assert.equal(dshVersion.stdout.trim(), '0.2.0-rc.2', 'DSH_BIN must select the exact supported published host')
let dshBinaryPath = dshBin
if (!isAbsolute(dshBinaryPath)) {
  for (const directory of env.PATH.split(delimiter)) {
    const candidate = join(directory, dshBin)
    try { if ((await stat(candidate)).isFile()) { dshBinaryPath = candidate; break } } catch { /* next PATH candidate */ }
  }
}
const installAnchor = createRequire(await realpath(dshBinaryPath)).resolve('@deepseek-ai/dsh/package.json')
const hostManifest = JSON.parse(await readFile(installAnchor, 'utf8'))
assert.equal(hostManifest.name, '@deepseek-ai/dsh')
assert.equal(hostManifest.version, '0.2.0-rc.2')
env.DSH_BIN = dshBinaryPath
env.DSH_HOME = join(evidence, 'dsh-home')
const profileDir = join(env.DSH_HOME, 'profiles/live-acceptance')
const archiveDirectory = join(evidence, 'package')
await mkdir(archiveDirectory)
const packed = await promisify(execFile)('pnpm', ['--filter', '@zleap-ai/dsh-sag', 'pack', '--pack-destination', archiveDirectory], { cwd: integrationRoot, env, timeout: 120000 })
await writeFile(join(evidence, 'package.log'), packed.stdout + packed.stderr)
const archive = join(archiveDirectory, (await readdir(archiveDirectory)).find(name => name.endsWith('.tgz')))
const installed = await promisify(execFile)(dshBinaryPath, ['plugin', '--profile', 'live-acceptance', 'add', archive], { cwd: evidence, env, timeout: 180000 })
await writeFile(join(evidence, 'installation.log'), installed.stdout + installed.stderr)
await writeFile(join(profileDir, 'cordis.patch.yml'), '# Isolated real SAG acceptance profile\n- id: dsh-sag\n  config:\n    mode: local\n    requestTimeoutMs: 30000\n    connectionCacheTtlMs: 1\n    maxReadChars: 12000\n    discoveryUrls: [http://127.0.0.1:1]\n- id: fs-sandbox\n  config:\n    cwd: ' + JSON.stringify(evidence) + '\n')
// The normal dsh profile launcher creates this empty Include root before boot.
await writeFile(join(profileDir, 'cordis.yml'), '[]\n')
const installedPackage = join(profileDir, 'node_modules/@zleap-ai/dsh-sag')
const owned = []
function start(command, args, options) {
  const fd = openSync(options.log, 'a')
  let child
  try { child = spawn(command, args, { cwd: options.cwd, env: options.env, stdio: ['ignore', fd, fd] }) } finally { closeSync(fd) }
  const processState = { child, stopped: false, exit: undefined }
  processState.exit = new Promise(resolve => {
    child.once('exit', (code, signal) => { processState.stopped = true; resolve({ code, signal }) })
    child.once('error', error => { processState.stopped = true; resolve({ error: String(error) }) })
  })
  owned.push(processState)
  return processState
}
async function stop(processState) {
  if (!processState || processState.stopped) return
  processState.child.kill('SIGTERM')
  let timer
  try {
    await Promise.race([processState.exit, new Promise(resolve => {
      timer = setTimeout(() => { processState.child.kill('SIGKILL'); resolve() }, 10000)
    })])
    await processState.exit
  } finally { clearTimeout(timer) }
}
async function ready(processState) {
  const deadline = Date.now() + 90000
  do {
    assert.equal(processState.stopped, false, 'SAG API exited during startup; inspect api.log')
    try {
      const response = await fetch(`${origin}/api/v1/system/ready`, { signal: AbortSignal.timeout(2000) })
      if (response.ok && (await response.json()).status === 'ready') return
    } catch { /* startup still importing and initializing */ }
    await new Promise(resolve => setTimeout(resolve, 250))
  } while (Date.now() < deadline)
  throw new Error('Real SAG startup timed out; inspect api.log')
}
const startApi = () => start(python, ['-m', 'uvicorn', 'sag_api.main:app', '--host', '127.0.0.1', '--port', String(apiPort)], { cwd: apiRoot, env, log: join(evidence, 'api.log') })
async function check(readOnly = false) {
  const args = [join(integrationRoot, 'scripts/check-live-sag.mjs'), '--connection', connectionFile, '--upload', uploadFile,
    '--evidence', evidence, '--bootstrap-auth', authFile, '--model-boundary', 'deterministic-local-fixture',
    '--dsh-home', env.DSH_HOME, '--profile-dir', profileDir, '--install-anchor', installAnchor, '--packed-package', installedPackage,
    ...(readOnly ? ['--read-only'] : [])]
  const child = start(process.execPath, args, { cwd: integrationRoot, env, log: join(evidence, readOnly ? 'after-restart.log' : 'registered-tools.log') })
  let timer
  try {
    const outcome = await Promise.race([child.exit, new Promise(resolve => {
      timer = setTimeout(() => { child.child.kill('SIGTERM'); resolve({ code: 124 }) }, 240000)
    })])
    assert.equal(outcome.code, 0, `Registered tools acceptance failed (${readOnly ? 'after API restart' : 'main path'}); inspect acceptance logs`)
  } finally { clearTimeout(timer) }
}
const summary = { backend: 'real SAG API', engine: 'published zleap-sag 0.14.0', modelBoundary: 'deterministic-local-fixture',
  dshVersion: '0.2.0-rc.2', packageArchive: archive, profileDir, installedPackage,
  externalModelsValidated: false, apiOrigin: origin, modelOrigin: `http://127.0.0.1:${modelPort}/v1`, evidenceDirectory: evidence, completed: false, ownedProcessesStopped: false }
let api
try {
  start(process.execPath, [join(integrationRoot, 'scripts/fixtures/local-model-provider.mjs')], { cwd: integrationRoot,
    env: { ...env, SAG_FIXTURE_PORT: String(modelPort), SAG_FIXTURE_EVIDENCE_FILE: join(evidence, 'model-requests.jsonl') }, log: join(evidence, 'model.log') })
  api = startApi()
  await ready(api)
  const login = await fetch(`${origin}/api/v1/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'Isolated integration acceptance' }) })
  assert.equal(login.status, 200)
  await writeFile(authFile, JSON.stringify(await login.json()), { mode: 0o600 })
  await check()
  const main = JSON.parse(await readFile(join(evidence, 'registered-tools.json'), 'utf8'))
  assert.equal(main.completed, true)
  assert.equal(new Set(main.toolCalls.map(call => call.name)).size, 11)
  summary.mainChecks = main.checks.length
  summary.registeredTools = 11
  console.log(`PASS real SAG registered tools: ${summary.mainChecks} checks, all 11 tools`)
  const connectionBefore = JSON.parse(await readFile(connectionFile, 'utf8'))
  await stop(api)
  api = startApi()
  await ready(api)
  const connectionAfter = JSON.parse(await readFile(connectionFile, 'utf8'))
  assert.equal(connectionAfter.accessToken, connectionBefore.accessToken)
  assert.equal(connectionAfter.defaultSourceId, connectionBefore.defaultSourceId)
  await check(true)
  const restarted = JSON.parse(await readFile(join(evidence, 'after-api-restart.json'), 'utf8'))
  assert.equal(restarted.completed, true)
  summary.afterApiRestartChecks = restarted.checks.length
  summary.completed = true
  console.log(`PASS real SAG API restart and saved profile: ${summary.afterApiRestartChecks} checks`)
} catch (error) {
  summary.error = String(error)
  console.error(summary.error)
  process.exitCode = 1
} finally {
  for (const processState of [...owned].reverse()) await stop(processState)
  summary.ownedProcessesStopped = owned.every(processState => processState.stopped)
  await writeFile(summaryPath, JSON.stringify(summary, null, 2) + '\n')
  console.log(`Owned acceptance processes stopped: ${summary.ownedProcessesStopped}`)
}
