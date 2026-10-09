import { spawn, spawnSync } from 'node:child_process'
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { boot, readProfilePatches, type ProfileContext } from '@deepseek-ai/dsh-app-boot'
import ConfigEditor from '@deepseek-ai/dsh-config-editor'
import { LocalCredentialProvider } from '@deepseek-ai/dsh-credentials-local'
import SettingsForms from '@deepseek-ai/dsh-settings'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import { afterEach, describe, expect, it } from 'vitest'
import { parse } from 'yaml'
import * as DshSag from '../src/index.ts'
import { withCliProfileLock } from '../src/cli/profile-lock.ts'
import { resolveCliHostInstallation } from '../src/cli/host-profile.ts'
import { createCliRuntime } from '../src/cli/runtime.ts'
import { registerSagSettings, SAG_CREDENTIAL_KEY, SagConnectionStore } from '../src/connection/store.ts'

const directories: string[] = []
const descriptor = {
  schemaVersion: 1 as const, name: 'Selected profile SAG',
  apiUrl: 'http://127.0.0.1:54321/api/v1', mcpUrl: 'http://127.0.0.1:54321/mcp/',
  accessToken: 'profile-secret-token', defaultSourceId: 'saved-source',
}

async function profile(name = 'chosen') {
  const home = await mkdtemp(join(tmpdir(), 'dsh-sag-selected-'))
  directories.push(home)
  const dir = join(home, 'profiles', name)
  const bundle = join(dir, 'node_modules', 'fixture-bundle')
  await mkdir(bundle, { recursive: true })
  const hostPackage = join(dir, 'node_modules', '@deepseek-ai', 'dsh')
  await mkdir(hostPackage, { recursive: true })
  await writeFile(join(hostPackage, 'package.json'), JSON.stringify({ name: '@deepseek-ai/dsh', version: '0.2.0-rc.2' }))
  await writeFile(join(dir, 'package.json'), JSON.stringify({ dsh: { profile: { bundles: ['fixture-bundle'] } } }))
  await writeFile(join(bundle, 'package.json'), JSON.stringify({ name: 'fixture-bundle', version: '1.0.0', peerDependencies: { '@deepseek-ai/dsh-settings': '0.2.0-rc.2' }, dsh: { bundle: { patch: 'cordis.patch.yml' } } }))
  await writeFile(join(bundle, 'cordis.patch.yml'), `- insert:
    - id: config-editor
      name: cordis:config-editor
    - id: settings
      name: cordis:settings
    - id: dsh-sag
      name: cordis:dsh-sag
      config:
        requestTimeoutMs: 23456
        discoveryUrls: [http://127.0.0.1:1]
`)
  await writeFile(join(dir, 'cordis.patch.yml'), '# keep this comment\n- id: dsh-sag\n  config:\n    requestTimeoutMs: 23456\n    discoveryUrls: [http://127.0.0.1:1]\n    maxReadChars: 12345\n- id: settings\n  disabled: false\n')
  await writeFile(join(dir, 'cordis.yml'), '[]\n')
  return { home, dir }
}

async function host(home: string, dir: string) {
  const profileContext = {
    name: 'chosen', home, dir, patchPath: join(dir, 'cordis.patch.yml'),
    cwd: dir, installAnchor: join(dir, 'package.json'), startedBundles: ['fixture-bundle'],
    overlays: [], telemetryDisabledEnv: undefined,
  } satisfies ProfileContext
  const ctx = await boot('dsh-sag-test', join(dir, 'cordis.yml'), readProfilePatches('dsh', profileContext), async child => {
    child.provide('profileContext', profileContext)
    await child.plugin(SystemPrompt)
    await child.plugin(ToolRuntime)
    await child.plugin(LocalCredentialProvider, { dshHome: home, watch: false })
    child.loader.builtins['config-editor'] = ConfigEditor
    child.loader.builtins.settings = SettingsForms
    child.loader.builtins['dsh-sag'] = DshSag
    child.provide('fs', { resolve: () => Promise.reject(new Error('no discovery file')), readText: () => Promise.reject(new Error('no discovery file')) } as never)
  })
  return { ctx, dispose: () => ctx.fiber.dispose() }
}

afterEach(async () => { for (const directory of directories.splice(0)) await rm(directory, { recursive: true, force: true }) })

describe('0.2 selected-profile connection persistence', () => {
  it('reads CLI setup from real SettingsForms after restart and preserves ordinary config', async () => {
    const { home, dir } = await profile()
    const cli = await createCliRuntime({ dshHome: home, profileDir: dir })
    try { await cli.store.save(descriptor) } finally { await cli.dispose() }
    const patch = await readFile(join(dir, 'cordis.patch.yml'), 'utf8')
    expect(patch).toContain('# keep this comment')
    expect(patch).not.toContain(descriptor.accessToken)
    expect(parse(patch)[0].config).toMatchObject({ maxReadChars: 12345, connection: { defaultSourceId: 'saved-source', apiUrl: descriptor.apiUrl } })
    const hosted = await host(home, dir)
    try {
      const row = [...hosted.ctx.loader.entries()].find(entry => entry.options.id === 'dsh-sag')!
      expect(row).toBeDefined()
      expect(row.fiber?.state).toBe(2)
      expect(hosted.ctx.settings.describe().find(form => form.ns === 'dsh-sag')?.value).toMatchObject({ connection: { defaultSourceId: 'saved-source' } })
      const store = new SagConnectionStore({ credentials: hosted.ctx.credentials, settings: registerSagSettings(hosted.ctx, row.fiber!.config.connection) })
      await expect(store.load()).resolves.toEqual(descriptor)
      await store.save({ ...descriptor, name: 'Host edit', defaultSourceId: 'host-source' })
      expect(row.fiber?.config.requestTimeoutMs).toBe(23456)
      expect(row.fiber?.config.maxReadChars).toBe(12345)
    } finally { await hosted.dispose() }
    const restarted = await createCliRuntime({ dshHome: home, profileDir: dir })
    try { await expect(restarted.store.load()).resolves.toEqual({ ...descriptor, name: 'Host edit', defaultSourceId: 'host-source' }) } finally { await restarted.dispose() }
    expect(await readFile(join(home, '.credentials.yaml'), 'utf8')).toContain('dsh-sag/local')
  })

  it('uses the installed host version in the standalone bundle despite the independent plugin version', async () => {
    const { home, dir } = await profile()
    const cli = await createCliRuntime({ dshHome: home, profileDir: dir })
    try { await cli.store.save(descriptor) } finally { await cli.dispose() }
    const bundleRoot = join(home, 'standalone-plugin')
    await mkdir(join(bundleRoot, 'lib'), { recursive: true })
    await writeFile(join(bundleRoot, 'package.json'), JSON.stringify({ type: 'module', version: '0.2.0' }))
    const { build } = await import('esbuild')
    const output = join(bundleRoot, 'lib', 'dsh-sag-cli.js')
    await build({
      entryPoints: [new URL('../src/cli.ts', import.meta.url).pathname], outfile: output,
      bundle: true, platform: 'node', format: 'esm', target: 'node22',
      banner: { js: "import { createRequire as dshSagCreateRequire } from 'node:module'; const require = dshSagCreateRequire(import.meta.url);" },
    })
    const result = spawnSync(process.execPath, [output, 'doctor'], {
      cwd: dir, encoding: 'utf8', env: { ...process.env, DSH_HOME: home },
    })
    expect(result.status).toBe(1)
    expect(`${result.stdout}\n${result.stderr}`).toMatch(/SAG 未就绪|SAG 检查失败/)
    expect(result.stderr).not.toMatch(/profile containing dsh-sag|Cannot find|package version/)
  })

  it('resolves the actual PATH symlink when the selected profile carries no host modules', async () => {
    const { home, dir } = await profile()
    await rm(join(dir, 'node_modules', '@deepseek-ai', 'dsh'), { recursive: true })
    const installed = join(home, 'installed-host', 'node_modules', '@deepseek-ai', 'dsh')
    const bin = join(home, 'installed-host', 'bin')
    await mkdir(join(installed, 'lib'), { recursive: true })
    await mkdir(bin)
    await writeFile(join(installed, 'package.json'), JSON.stringify({ name: '@deepseek-ai/dsh', version: '0.2.0-rc.2' }))
    await writeFile(join(installed, 'lib', 'bin.js'), '')
    await symlink(join(installed, 'lib', 'bin.js'), join(bin, process.platform === 'win32' ? 'dsh.cmd' : 'dsh'))
    expect(resolveCliHostInstallation(dir, { PATH: bin })).toEqual({
      anchor: await realpath(join(installed, 'package.json')), version: '0.2.0-rc.2',
    })
    await writeFile(join(installed, 'package.json'), JSON.stringify({ name: '@deepseek-ai/dsh' }))
    expect(() => resolveCliHostInstallation(dir, { PATH: bin })).toThrow(/cannot resolve the installed dsh package/)
  })

  it('refuses bundle peers that reject the installed host before touching profile or credential', async () => {
    const { home, dir } = await profile()
    const original = await readFile(join(dir, 'cordis.patch.yml'), 'utf8')
    await writeFile(join(dir, 'node_modules', '@deepseek-ai', 'dsh', 'package.json'), JSON.stringify({ name: '@deepseek-ai/dsh', version: '0.1.5-rc.2' }))
    const cli = await createCliRuntime({ dshHome: home, profileDir: dir })
    try {
      await expect(cli.store.save(descriptor)).rejects.toThrow(/incompatible with dsh 0.1.5-rc.2/)
      expect(await readFile(join(dir, 'cordis.patch.yml'), 'utf8')).toBe(original)
      await expect(cli.store.load()).resolves.toBeUndefined()
    } finally { await cli.dispose() }
  })

  it('does not borrow or delete an unrelated live writer lock with a forged run record', async () => {
    const { dir } = await profile()
    const writer = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' })
    if (writer.pid === undefined) throw new Error('writer fixture did not start')
    try {
      const lock = `${writer.pid}\n`
      await writeFile(join(dir, 'package.json.lock'), lock)
      await mkdir(join(dir, '.plugin-manager'))
      await writeFile(join(dir, '.plugin-manager', 'run.json'), JSON.stringify({ pid: process.ppid, grouped: false }))
      let wrote = false
      await expect(withCliProfileLock(dir, async () => { wrote = true }, { waitMs: 25 })).rejects.toThrow(/writer lock/)
      expect(wrote).toBe(false)
      expect(await readFile(join(dir, 'package.json.lock'), 'utf8')).toBe(lock)
      await rm(join(dir, 'package.json.lock'))
      await expect(withCliProfileLock(dir, async () => { wrote = true }, { waitMs: 25 })).resolves.toBeUndefined()
      expect(wrote).toBe(true)
    } finally {
      const stopped = new Promise<void>(resolve => writer.once('close', () => resolve()))
      writer.kill('SIGTERM')
      await stopped
    }
  })

  it('preserves inherited config when creating a new connection override', async () => {
    const { home, dir } = await profile()
    await writeFile(join(dir, 'cordis.patch.yml'), '# only unrelated profile policy\n- id: settings\n  disabled: false\n')
    const cli = await createCliRuntime({ dshHome: home, profileDir: dir })
    try { await cli.store.save(descriptor) } finally { await cli.dispose() }
    const rows = parse(await readFile(join(dir, 'cordis.patch.yml'), 'utf8'))
    expect(rows.find((row: { id: string }) => row.id === 'dsh-sag').config).toMatchObject({
      requestTimeoutMs: 23456, discoveryUrls: ['http://127.0.0.1:1'],
      connection: { defaultSourceId: 'saved-source' },
    })
    const hosted = await host(home, dir)
    try { expect(hosted.ctx.settings.describe().find(form => form.ns === 'dsh-sag')).toBeDefined() } finally { await hosted.dispose() }
  })

  it('refuses a home override before changing the profile or shared credential', async () => {
    const { home, dir } = await profile()
    const original = await readFile(join(dir, 'cordis.patch.yml'), 'utf8')
    await writeFile(join(home, 'cordis.patch.yml'), '- id: dsh-sag\n  config:\n    connection:\n      name: Home connection\n')
    const cli = await createCliRuntime({ dshHome: home, profileDir: dir })
    try {
      await expect(cli.store.save(descriptor)).rejects.toThrow(/home patch/)
      expect(await readFile(join(dir, 'cordis.patch.yml'), 'utf8')).toBe(original)
      await expect(cli.store.load()).resolves.toBeUndefined()
    } finally { await cli.dispose() }
  })

  it('does not overwrite embedded config or credential when setup targets an embedded profile', async () => {
    const { home, dir } = await profile()
    const original = '- id: dsh-sag\n  config:\n    mode: embedded\n    pythonCommand: python3\n    envFile: /existing.env\n    namespaces: [{id: docs, label: Docs}]\n'
    await writeFile(join(dir, 'cordis.patch.yml'), original)
    const cli = await createCliRuntime({ dshHome: home, profileDir: dir })
    try {
      await expect(cli.store.save(descriptor)).rejects.toThrow(/embedded/)
      expect(await readFile(join(dir, 'cordis.patch.yml'), 'utf8')).toBe(original)
      await expect(cli.store.load()).resolves.toBeUndefined()
    } finally { await cli.dispose() }
  })

  it('isolates endpoints in different profiles while retaining the existing shared credential key', async () => {
    const { home, dir } = await profile()
    const another = join(home, 'profiles', 'another')
    await mkdir(another)
    await writeFile(join(another, 'package.json'), JSON.stringify({ dsh: { profile: { bundles: [] } } }))
    await writeFile(join(another, 'cordis.patch.yml'), '[]\n')
    const selected = await createCliRuntime({ dshHome: home, profileDir: dir })
    try { await selected.store.save(descriptor) } finally { await selected.dispose() }
    expect(await readFile(join(another, 'cordis.patch.yml'), 'utf8')).toBe('[]\n')
    const hosted = await host(home, dir)
    try {
      await expect(hosted.ctx.credentials.readRecord(SAG_CREDENTIAL_KEY)).resolves.toMatchObject({ payload: { accessToken: descriptor.accessToken } })
      const status = await hosted.ctx.tools.execute({ callId: ToolCallId('saved-only'), name: 'sag_status', arguments: {}, signal: new AbortController().signal })
      expect(status.isError).toBe(true)
    } finally { await hosted.dispose() }
  })
})
