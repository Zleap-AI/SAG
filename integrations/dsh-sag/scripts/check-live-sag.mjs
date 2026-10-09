#!/usr/bin/env node
/** Mutating acceptance against an explicitly selected isolated, real SAG backend. */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir, mkdtemp, readFile, stat, writeFile } from 'node:fs/promises'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'
import { promisify } from 'node:util'
import { execFile } from 'node:child_process'

const { values } = parseArgs({ options: {
  connection: { type: 'string' }, upload: { type: 'string' }, evidence: { type: 'string' },
  'profile-dir': { type: 'string' }, 'dsh-home': { type: 'string' }, 'install-anchor': { type: 'string' }, 'packed-package': { type: 'string' },
  'bootstrap-auth': { type: 'string' }, 'model-boundary': { type: 'string', default: 'unspecified' },
  'read-only': { type: 'boolean', default: false }, help: { type: 'boolean', short: 'h' },
} })
if (values.help || !values.connection || !values.evidence || (!values['read-only'] && !values.upload)) {
  console.log('Usage: node scripts/check-live-sag.mjs --connection <isolated SAG descriptor> --upload <test.md> --evidence <directory> [--bootstrap-auth <isolated login JSON>] [--model-boundary deterministic-local-fixture]')
  console.log('Use --read-only to repeat profile restart, CLI doctor, search, and read checks from an existing evidence directory.')
  process.exit(values.help ? 0 : 2)
}
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../packages/dsh-sag')
const actualProfile = Boolean(values['profile-dir'] && values['dsh-home'] && values['install-anchor'] && values['packed-package'])
const require = createRequire(actualProfile ? resolve(values['install-anchor']) : join(packageRoot, 'package.json'))
const load = name => import(pathToFileURL(require.resolve(name)).href)
const [{ boot, readProfilePatches, loadProfileDirectory, createRuntimeResolution, PluginPackages },
  { default: ConfigEditor }, { LocalCredentialProvider }, { default: SettingsForms },
  { default: SystemPrompt }, { default: ToolRuntime }, { ToolCallId }] = await Promise.all([
  load('@deepseek-ai/dsh-app-boot'), load('@deepseek-ai/dsh-config-editor'), load('@deepseek-ai/dsh-credentials-local'),
  load('@deepseek-ai/dsh-settings'), load('@deepseek-ai/dsh-system-prompt'), load('@deepseek-ai/dsh-tools'),
  load('@deepseek-ai/dsh-llm'),
])
const DshSag = actualProfile ? undefined : await import(pathToFileURL(join(packageRoot, 'lib/index.js')).href)
const { createCliRuntime } = actualProfile ? {} : await import(pathToFileURL(join(packageRoot, 'lib/cli/runtime.js')).href)
const { runCli } = actualProfile ? {} : await import(pathToFileURL(join(packageRoot, 'lib/cli.js')).href)
const evidence = resolve(values.evidence)
await mkdir(evidence, { recursive: true })
let descriptor = JSON.parse(await readFile(resolve(values.connection), 'utf8'))
assert.equal(new URL(descriptor.apiUrl).hostname, '127.0.0.1', 'Acceptance must target an explicitly isolated loopback API')
const token = descriptor.accessToken
const bootstrapAuth = values['bootstrap-auth'] ? JSON.parse(await readFile(resolve(values['bootstrap-auth']), 'utf8')) : undefined
process.env.SAG_DSH_CONNECTION_FILE = resolve(values.connection)
const hostVersions = {}
for (const name of ['dsh-app-boot', 'dsh-settings', 'dsh-credentials-local', 'dsh-tools']) {
  hostVersions[name] = JSON.parse(await readFile(require.resolve(`@deepseek-ai/${name}/package.json`), 'utf8')).version
  assert.equal(hostVersions[name], '0.2.0-rc.2')
}
const resultPath = join(evidence, 'registered-tools.json')
const previous = values['read-only'] ? JSON.parse(await readFile(resultPath, 'utf8')) : undefined
const home = values['dsh-home'] ? resolve(values['dsh-home']) : previous?.dshHome ?? await mkdtemp(join(evidence, 'dsh-home-'))
const dir = values['profile-dir'] ? resolve(values['profile-dir']) : join(home, 'profiles', 'live-acceptance')
process.env.DSH_HOME = home
process.env.PATH = [dirname(process.execPath), process.env.PATH].filter(Boolean).join(delimiter)
if (!previous && !actualProfile) {
  const bundle = join(dir, 'node_modules/sag-live-bundle')
  await mkdir(bundle, { recursive: true })
  await writeFile(join(dir, 'package.json'), JSON.stringify({ name: 'dsh-sag-live-acceptance-profile', dsh: { profile: { bundles: ['sag-live-bundle'] } } }))
  const manifest = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
  await writeFile(join(bundle, 'package.json'), JSON.stringify({ name: 'sag-live-bundle', version: '1.0.0', peerDependencies: manifest.peerDependencies, dsh: { bundle: { patch: 'cordis.patch.yml' } } }))
  await writeFile(join(bundle, 'cordis.patch.yml'), '- insert:\n    - id: config-editor\n      name: cordis:config-editor\n    - id: settings\n      name: cordis:settings\n    - id: dsh-sag\n      name: cordis:dsh-sag\n      config:\n        requestTimeoutMs: 30000\n        connectionCacheTtlMs: 1\n        discoveryUrls: [http://127.0.0.1:1]\n')
  await writeFile(join(dir, 'cordis.patch.yml'), '# Isolated real SAG acceptance profile\n- id: dsh-sag\n  config:\n    maxReadChars: 12000\n')
  await writeFile(join(dir, 'cordis.yml'), '[]\n')
}
const report = { realSagApi: descriptor.apiUrl, modelBoundary: values['model-boundary'], hostVersions,
  packageInstallation: actualProfile ? 'actual tarball installed by dsh plugin add' : 'workspace with declared package peers',
  dshHome: home, profileDir: dir, checks: [], toolCalls: [], sourceId: previous?.sourceId,
  uploadedDocumentId: previous?.uploadedDocumentId, ingestedDocumentId: previous?.ingestedDocumentId }
const redact = value => JSON.parse([token, bootstrapAuth?.access_token].filter(Boolean).reduce((text, secret) => text.split(secret).join('<redacted>'), JSON.stringify(value)))
const record = (name, detail) => { report.checks.push({ name, ...redact(detail) }); console.log(`PASS ${name}`) }
let callSequence = 0

async function cli(args) {
  if (actualProfile) {
    const { stdout, stderr } = await promisify(execFile)(process.env.DSH_BIN ?? 'dsh', ['plugin', '--profile', 'live-acceptance', 'exec', 'dsh-sag', ...args], {
      cwd: evidence, env: process.env, timeout: 30000,
    })
    assert.ok(stdout.includes('SAG 已连接。'), stdout + stderr)
    record(`installed profile CLI ${args[0]}`, { output: stdout + stderr })
    return
  }
  const output = []
  const exitCode = await runCli(args, { stdout: text => output.push(text), stderr: text => output.push(text) },
    { createRuntime: () => createCliRuntime({ dshHome: home, profileDir: dir }) })
  assert.equal(exitCode, 0, output.join(''))
  record(`CLI ${args[0]}`, { exitCode, output: output.join('') })
}

async function standaloneDoctor() {
  const target = actualProfile ? resolve(values['packed-package']) : packageRoot
  const { stdout, stderr } = await promisify(execFile)(process.execPath, [join(target, 'lib/dsh-sag-cli.js'), 'doctor'], {
    cwd: dir, env: { ...process.env, DSH_HOME: home }, timeout: 30000,
  })
  assert.ok(stdout.includes('SAG 已连接。'), stdout + stderr)
  record('standalone CLI doctor from the selected profile directory', { output: stdout + stderr })
}

async function host() {
  if (actualProfile) {
    const installAnchor = resolve(values['install-anchor'])
    const profile = loadProfileDirectory('dsh-sag-live', dir, installAnchor)
    assert.deepEqual(profile.skippedBundles, [], 'The packed SAG bundle must pass real host peer checks')
    assert.ok(profile.layers.some(layer => layer.packageName === '@zleap-ai/dsh-sag'))
    const profileContext = { name: 'live-acceptance', home, dir, patchPath: join(dir, 'cordis.patch.yml'),
      cwd: evidence, installAnchor, startedBundles: profile.layers.map(layer => layer.packageName), overlays: [], telemetryDisabledEnv: process.env.DSH_TELEMETRY_DISABLED }
    const resolution = await createRuntimeResolution({ installAnchor, profile, home })
    const { provideCmdline } = await load('@deepseek-ai/dsh-cmdline')
    let applicationReady = false
    const readyListeners = new Set()
    const ready = { onReady(listener) {
      if (applicationReady) { listener(); return () => {} }
      readyListeners.add(listener)
      return () => readyListeners.delete(listener)
    } }
    const ctx = await boot('dsh-sag-live', join(dir, 'cordis.yml'), readProfilePatches('dsh', profileContext, profile), async child => {
      child.provide('profileContext', profileContext)
      await child.plugin(PluginPackages, { resolution })
      provideCmdline(child, { args: [], exit(code) { throw new Error(`Acceptance host requested exit ${code}`) }, ready })
    })
    // Match the published launcher's readiness commit after successful boot.
    applicationReady = true
    for (const listener of readyListeners) listener()
    readyListeners.clear()
    const row = [...ctx.loader.entries()].find(entry => entry.options.id === 'dsh-sag')
    assert.equal(row?.fiber?.state, 2, 'The installed package must be active in the actual host composition')
    assert.equal(row.options.name, '@zleap-ai/dsh-sag')
    return { ctx, dispose: () => ctx.fiber.dispose() }
  }
  const profileContext = { name: 'live-acceptance', home, dir, patchPath: join(dir, 'cordis.patch.yml'),
    cwd: dir, installAnchor: join(dir, 'package.json'), startedBundles: ['sag-live-bundle'], overlays: [], telemetryDisabledEnv: undefined }
  const ctx = await boot('dsh-sag-live', join(dir, 'cordis.yml'), readProfilePatches('dsh', profileContext), async child => {
    child.provide('profileContext', profileContext)
    await child.plugin(SystemPrompt)
    await child.plugin(ToolRuntime)
    await child.plugin(LocalCredentialProvider, { dshHome: home, watch: false })
    Object.assign(child.loader.builtins, { 'config-editor': ConfigEditor, settings: SettingsForms, 'dsh-sag': DshSag })
    child.provide('fs', {
      async resolve(path) { const filename = resolve(path); return { targetKey: filename, displayPath: filename } },
      async readText(target) { return readFile(target.displayPath, 'utf8') },
      async stat(target) { const info = await stat(target.displayPath); return { type: info.isFile() ? 'file' : 'directory', size: info.size } },
      async readBytes(target) { return readFile(target.displayPath) },
    })
  })
  assert.equal([...ctx.loader.entries()].find(entry => entry.options.id === 'dsh-sag').fiber?.state, 2)
  return { ctx, dispose: () => ctx.fiber.dispose() }
}

async function call(ctx, name, args, expectError = false) {
  const response = await ctx.tools.execute({ callId: ToolCallId(`live-${++callSequence}`), name, arguments: args, signal: new AbortController().signal })
  report.toolCalls.push(redact({ name, arguments: args, isError: response.isError ?? false, value: response.value, content: response.content }))
  assert.equal(response.isError ?? false, expectError, JSON.stringify(redact(response)))
  return expectError ? response : response.value
}

async function waitReady(ctx, id, reprocess = false) {
  const deadline = Date.now() + 90000
  let document
  do {
    document = await call(ctx, 'sag_get_document', { document_id: id, source_id: report.sourceId })
    if (document.status === 'failed') throw new Error(`Document failed: ${JSON.stringify(document)}`)
    if (document.status === 'ready') return document
    await new Promise(resolve => setTimeout(resolve, 250))
  } while (Date.now() < deadline)
  throw new Error(`Processing timeout (${reprocess ? 'reprocess' : 'ingest'}): ${JSON.stringify(document)}`)
}

async function searchRead(ctx) {
  const found = await call(ctx, 'sag_search', { query: 'ORBIT-7246', source_ids: [report.sourceId], strategy: 'vector', limit: 5 })
  assert.ok(found.evidences.length > 0)
  const reference = found.evidences.find(item => item.excerpt.includes('Tuesday'))?.evidenceRef ?? found.evidences[0].evidenceRef
  const page = await call(ctx, 'sag_read', { evidence_ref: reference, max_chars: 32 })
  assert.equal(Array.from(page.content).length, 32)
  assert.equal(page.nextOffset, 32)
  const remainder = await call(ctx, 'sag_read', { evidence_ref: reference, offset: page.nextOffset, max_chars: 12000 })
  assert.ok((page.content + remainder.content).includes('ORBIT-7246'))
  assert.ok((page.content + remainder.content).includes('Tuesday at 09:30 Asia/Shanghai'))
  record('registered search and paged evidence read', { evidences: found.evidences.length, totalChars: page.totalChars, content: page.content + remainder.content })
}

let active
try {
  if (!previous) await cli(['setup', resolve(values.connection)])
  await cli(['doctor'])
  await standaloneDoctor()
  active = await host()
  const status = await call(active.ctx, 'sag_status', {})
  assert.equal(status.status, 'ready')
  await call(active.ctx, 'sag_list_sources', {})
  record('registered host connection and MCP compatibility', { status })
  if (!previous) {
    const source = await call(active.ctx, 'sag_create_source', { name: `DSH real acceptance ${Date.now()}`, description: 'Disposable integration test source' })
    report.sourceId = source.id
    record('registered source creation', { sourceId: source.id })
    if (values['bootstrap-auth']) {
      const response = await fetch(`${descriptor.apiUrl}/system/dsh/settings`, { method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${bootstrapAuth.access_token}` }, body: JSON.stringify({ default_source_id: source.id }) })
      assert.equal(response.status, 200, await response.text())
      descriptor = JSON.parse(await readFile(resolve(values.connection), 'utf8'))
      assert.equal(descriptor.defaultSourceId, source.id)
      await active.dispose(); active = undefined
      await cli(['setup', resolve(values.connection)])
      active = await host()
      record('backend default selection and selected-profile setup', { defaultSourceId: descriptor.defaultSourceId })
    }
    const sourceArgs = values['bootstrap-auth'] ? {} : { source_id: source.id }
    const upload = await call(active.ctx, 'sag_upload_file', { path: resolve(values.upload), ...sourceArgs })
    report.uploadedDocumentId = upload.documentId
    assert.equal(upload.sourceId, source.id)
    const readyUpload = await waitReady(active.ctx, upload.documentId)
    assert.ok(readyUpload.chunkCount >= 1)
    record('registered upload processed by real SAG', { document: readyUpload })
    const ingest = await call(active.ctx, 'sag_ingest_text', { text: 'ORBIT-7246 ingestion: the review code is COMET-5812.', title: 'Direct ingestion acceptance', ...sourceArgs })
    report.ingestedDocumentId = ingest.documentId
    record('registered text ingestion processed by real SAG', { document: await waitReady(active.ctx, ingest.documentId) })
    const listing = await call(active.ctx, 'sag_list_documents', sourceArgs)
    assert.ok(listing.documents.some(item => item.id === upload.documentId))
    assert.ok(listing.documents.some(item => item.id === ingest.documentId))
    record('registered document listing', { documentCount: listing.documents.length })
    await searchRead(active.ctx)
    const reprocess = await call(active.ctx, 'sag_reprocess_document', { document_id: upload.documentId, ...sourceArgs })
    assert.equal(reprocess.accepted, true)
    if (bootstrapAuth) {
    const deadline = Date.now() + 90000
    let job
    do {
      const response = await fetch(`${descriptor.apiUrl}/jobs/${reprocess.jobId}`, { headers: { Authorization: `Bearer ${bootstrapAuth.access_token}` } })
      assert.equal(response.status, 200)
      job = await response.json()
      if (job.status === 'succeeded') break
      assert.notEqual(job.status, 'failed', JSON.stringify(job))
      await new Promise(resolve => setTimeout(resolve, 250))
    } while (Date.now() < deadline)
    assert.equal(job.status, 'succeeded')
    record('registered reprocess completed', { jobId: reprocess.jobId, status: job.status, document: await waitReady(active.ctx, upload.documentId, true) })
    } else record('registered reprocess accepted', { jobId: reprocess.jobId, completionCheck: 'bootstrap authentication required to inspect the private job status' })
    const denied = await call(active.ctx, 'sag_delete_document', { document_id: ingest.documentId, ...sourceArgs }, true)
    const denial = denied.content.map(part => part.type === 'text' ? part.text : '').join('\n')
    assert.ok(actualProfile
      ? denial.includes('tool "sag_delete_document" requires approval, but the call has no agent to route it through')
      : denial.includes('删除 SAG 文档后无法恢复'), 'Deletion must fail at the real host approval gate')
    assert.equal((await call(active.ctx, 'sag_get_document', { document_id: ingest.documentId, ...sourceArgs })).status, 'ready')
    record('registered deletion requires approval and preserves document', { denied: true })
  } else await searchRead(active.ctx)
  await active.dispose(); active = undefined
  const patch = await readFile(join(dir, 'cordis.patch.yml'), 'utf8')
  assert.ok(patch.includes('# Isolated real SAG acceptance profile'))
  assert.ok(!patch.includes(token))
  if (!actualProfile) {
    const persisted = await createCliRuntime({ dshHome: home, profileDir: dir })
    try {
      const saved = await persisted.store.load()
      assert.equal(saved.apiUrl, descriptor.apiUrl)
      assert.equal(saved.accessToken, token)
      assert.equal(saved.defaultSourceId, descriptor.defaultSourceId)
    } finally { await persisted.dispose() }
  }
  await cli(['doctor'])
  await standaloneDoctor()
  active = await host()
  if (actualProfile) {
    const saved = active.ctx.settings.describe().find(form => form.ns === 'dsh-sag')?.value.connection
    assert.equal(saved.apiUrl, descriptor.apiUrl)
    assert.equal(saved.defaultSourceId, descriptor.defaultSourceId)
    assert.equal((await active.ctx.credentials.readRecord('dsh-sag/local')).payload.accessToken, token)
  }
  assert.equal((await call(active.ctx, 'sag_status', {})).status, 'ready')
  await searchRead(active.ctx)
  record('host restart and CLI share saved profile endpoints credential and default source', { defaultSourceId: descriptor.defaultSourceId, tokenOutsideProfile: true })
  report.completed = true
} catch (error) {
  report.completed = false
  report.error = String(error).split(token).join('<redacted>')
  console.error(report.error)
  process.exitCode = 1
} finally {
  try { await active?.dispose() } finally {
    await writeFile(previous ? join(evidence, 'after-api-restart.json') : resultPath, JSON.stringify(redact(report), null, 2) + '\n')
  }
}
