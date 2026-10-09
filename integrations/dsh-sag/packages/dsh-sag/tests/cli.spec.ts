import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import {
  isDirectCliEntry,
  runCli,
  type CliDependencies,
  type CliIo,
  type CliRuntime,
} from '../src/cli.js'
import { startCliRuntime } from '../src/cli/runtime.js'
import type { ConnectionInspection } from '../src/connection/manager.js'
import type { SagConnectionStore } from '../src/connection/store.js'
import type { SagConnectionDescriptor } from '../src/connection/types.js'

const descriptor: SagConnectionDescriptor = {
  schemaVersion: 1,
  name: '本地 SAG',
  apiUrl: 'http://127.0.0.1:9000/api/v1',
  mcpUrl: 'http://127.0.0.1:9000/mcp',
  accessToken: 'never-print-this-token',
  defaultSourceId: 'source-1',
}

const ready: ConnectionInspection = {
  status: 'ready',
  health: true,
  ready: true,
  sourceCount: 1,
  capabilities: {
    schemaVersion: 1,
    capabilities: ['search', 'read'],
    upload: { maxMb: 100, extensions: ['.pdf', '.md'] },
  },
  mcp: { tools: ['list_sources', 'read', 'search'], readTool: 'read' },
}

function output(): { io: CliIo; stdout: string[]; stderr: string[] } {
  const stdout: string[] = []
  const stderr: string[] = []
  return {
    io: { stdout: line => stdout.push(line), stderr: line => stderr.push(line) },
    stdout,
    stderr,
  }
}

function dependencies(options: {
  readonly saved?: SagConnectionDescriptor
  readonly discovered?: SagConnectionDescriptor
} = {}): { deps: CliDependencies; runtime: CliRuntime & { readonly discovery: ReturnType<typeof vi.fn>; readonly store: { readonly save: ReturnType<typeof vi.fn> } } } {
  const discovery = vi.fn(async () => ({
    source: options.discovered === undefined ? undefined : 'loopback' as const,
    descriptor: options.discovered,
    diagnostics: [],
  }))
  let stored = options.saved
  const store = {
    load: vi.fn(async () => stored),
    save: vi.fn(async (next: SagConnectionDescriptor) => { stored = next }),
  }
  const runtime = {
    store,
    discovery: { discover: discovery as CliRuntime['discovery']['discover'] },
    inspect: vi.fn(async () => ready),
    dispose: vi.fn(async () => undefined),
  }
  return { deps: { createRuntime: async () => runtime }, runtime }
}

describe('dsh-sag CLI', () => {
  it('recognizes a pnpm bin symlink as the direct CLI entry without treating an import as execution', () => {
    const moduleUrl = 'file:///profile/node_modules/@zleap-ai/dsh-sag/lib/cli.js'
    const resolveRealPath = vi.fn((path: string) => path === '/profile/node_modules/.bin/dsh-sag'
      ? '/profile/node_modules/@zleap-ai/dsh-sag/lib/cli.js'
      : path)

    expect(isDirectCliEntry(moduleUrl, '/profile/node_modules/.bin/dsh-sag', resolveRealPath)).toBe(true)
    expect(isDirectCliEntry(moduleUrl, '/test-runner/vitest.mjs', resolveRealPath)).toBe(false)
    expect(isDirectCliEntry(moduleUrl, undefined, resolveRealPath)).toBe(false)
  })

  it('discovers SAG during setup and only saves the ready connection', async () => {
    const { deps, runtime } = dependencies({ discovered: descriptor })
    const captured = output()

    await expect(runCli(['setup'], captured.io, deps)).resolves.toBe(0)

    expect(runtime.discovery.discover).toHaveBeenCalledOnce()
    expect(runtime.store.save).toHaveBeenCalledWith(descriptor)
    expect(runtime.store.save).toHaveBeenCalledOnce()
    expect(captured.stdout.join('')).toContain('SAG 已连接')
    expect(captured.stdout.join('')).not.toContain(descriptor.accessToken)
    expect(runtime.dispose).toHaveBeenCalledOnce()
  })

  it('reads an explicit descriptor file and saves it only after the full handshake is ready', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'dsh-sag-cli-'))
    const filename = join(directory, 'sag-dsh.json')
    await writeFile(filename, JSON.stringify(descriptor))
    const { deps, runtime } = dependencies()
    const captured = output()

    await expect(runCli(['setup', filename], captured.io, deps)).resolves.toBe(0)

    expect(runtime.store.save).toHaveBeenCalledWith(descriptor)
    expect(runtime.store.save).toHaveBeenCalledOnce()
    expect(captured.stdout.join('')).toContain('SAG 已连接')
  })

  it('rejects URL credentials from an imported descriptor without printing them', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'dsh-sag-cli-userinfo-'))
    const filename = join(directory, 'sag-dsh.json')
    const secret = 'file-password'
    await writeFile(filename, JSON.stringify({ ...descriptor, mcpUrl: `http://user:${secret}@127.0.0.1:9000/mcp/` }))
    const { deps, runtime } = dependencies()
    const captured = output()
    await expect(runCli(['setup', filename], captured.io, deps)).resolves.toBe(1)
    expect(runtime.store.save).not.toHaveBeenCalled()
    expect(captured.stderr.join('')).toContain('username or password')
    expect(captured.stderr.join('')).not.toContain(secret)
  })

  it('probes the supplied loopback URL for its descriptor instead of accepting a token argument', async () => {
    const { deps, runtime } = dependencies({ discovered: descriptor })
    const captured = output()

    await expect(runCli(['setup', '--url', 'http://127.0.0.1:9000'], captured.io, deps)).resolves.toBe(0)

    expect(runtime.discovery.discover).toHaveBeenCalledWith(expect.any(AbortSignal), {
      paths: [],
      urls: ['http://127.0.0.1:9000'],
    })
    expect(captured.stdout.join('')).toContain('SAG 已连接')
  })

  it('reports doctor capability details without changing the stored connection', async () => {
    const { deps, runtime } = dependencies({ saved: descriptor })
    const captured = output()

    await expect(runCli(['doctor'], captured.io, deps)).resolves.toBe(0)

    expect(captured.stdout.join('')).toContain('文件上传')
    expect(runtime.store.save).not.toHaveBeenCalled()
  })

  it('uses the profile-reachable setup command when doctor cannot find SAG', async () => {
    const { deps } = dependencies()
    const captured = output()

    await expect(runCli(['doctor'], captured.io, deps)).resolves.toBe(1)

    expect(captured.stderr.join('')).toContain('dsh plugin --profile web exec dsh-sag setup')
    expect(captured.stderr.join('')).not.toContain('使用 dsh-sag setup')
  })

  it('keeps actionable string diagnostics while redacting connector tokens', async () => {
    const secret = 'sag_local_never_print_this'
    const { deps, runtime } = dependencies({ saved: descriptor })
    const captured = output()
    runtime.inspect = vi.fn(async () => ({
      status: 'unreachable',
      health: false,
      ready: false,
      sourceCount: 0,
      errors: [`HTTP 401: renew credentials with Bearer ${secret}; then retry https://sag.test/?access_token=${secret}`],
    }))

    await expect(runCli(['doctor'], captured.io, deps)).resolves.toBe(1)

    expect(captured.stderr.join('')).toContain('HTTP 401: renew credentials')
    expect(captured.stderr.join('')).toContain('Bearer <redacted>')
    expect(captured.stderr.join('')).not.toContain(secret)
  })

  it('returns usage code 2 for unsupported arguments', async () => {
    const { deps } = dependencies()
    const captured = output()

    await expect(runCli(['setup', '--token', descriptor.accessToken], captured.io, deps)).resolves.toBe(2)

    expect(captured.stderr.join('')).toContain('用法')
    expect(captured.stderr.join('')).not.toContain(descriptor.accessToken)
  })

  it('bounds a half-open setup handshake and still disposes the runtime', async () => {
    vi.useFakeTimers()
    try {
      const { deps, runtime } = dependencies({ discovered: descriptor })
      runtime.inspect = vi.fn(() => new Promise(() => undefined))
      const captured = output()
      const running = runCli(['setup'], captured.io, { ...deps, requestTimeoutMs: 25 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(25)
      await expect(running).resolves.toBe(1)
      expect(runtime.dispose).toHaveBeenCalledOnce()
      expect(captured.stderr.join('')).toContain('连接 SAG 失败')
    } finally {
      vi.useRealTimers()
    }
  })

  it('redacts disposal failures and returns a non-zero code without rejecting', async () => {
    const { deps, runtime } = dependencies({ saved: descriptor })
    runtime.dispose.mockRejectedValue(new Error(`cleanup Bearer ${descriptor.accessToken} at https://u:${descriptor.accessToken}@sag.test/`))
    const captured = output()
    await expect(runCli(['doctor'], captured.io, deps)).resolves.toBe(1)
    expect(captured.stderr.join('')).toContain('清理 dsh-sag 运行环境失败')
    expect(captured.stderr.join('')).not.toContain(descriptor.accessToken)
  })

  it('remembers a newly accepted setup token before disposal closes the store', async () => {
    const oldDescriptor = { ...descriptor, accessToken: 'OLD_SETUP_SECRET' }
    const newDescriptor = { ...descriptor, accessToken: 'NEW_SETUP_SECRET' }
    const { deps, runtime } = dependencies({ saved: oldDescriptor, discovered: newDescriptor })
    let disposed = false
    runtime.store.load.mockImplementation(async () => {
      if (disposed) throw new Error('store is closed')
      return oldDescriptor
    })
    runtime.dispose.mockImplementation(async () => {
      disposed = true
      throw new Error(`cleanup ${newDescriptor.accessToken}`)
    })
    const captured = output()
    await expect(runCli(['setup'], captured.io, deps)).resolves.toBe(1)
    expect(captured.stderr.join('')).toContain('清理 dsh-sag 运行环境失败')
    expect(captured.stderr.join('')).not.toContain(newDescriptor.accessToken)
  })

  it('retains the command failure while redacting a second disposal failure', async () => {
    const { deps, runtime } = dependencies({ saved: descriptor })
    runtime.inspect = vi.fn(async () => { throw new Error(`command ${descriptor.accessToken}`) })
    runtime.dispose.mockRejectedValue(new Error(`dispose ${descriptor.accessToken}`))
    const captured = output()
    await expect(runCli(['doctor'], captured.io, deps)).resolves.toBe(1)
    expect(captured.stderr.join('')).toContain('SAG 检查失败')
    expect(captured.stderr.join('')).toContain('清理 dsh-sag 运行环境失败')
    expect(captured.stderr.join('')).not.toContain(descriptor.accessToken)
  })
})

describe('CLI runtime lifecycle', () => {
  it('disposes credentials and settings when registration startup fails', async () => {
    const settingsDisposed = Promise.withResolvers<void>()
    const credentialsDisposed = Promise.withResolvers<void>()
    const settings = { dispose: vi.fn(async () => settingsDisposed.promise) }
    const credentials = { dispose: vi.fn(async () => credentialsDisposed.promise) }
    const registrationFailure = new Error('registration startup failed')

    const starting = startCliRuntime({
      startSettings: async () => settings,
      startCredentials: async () => credentials,
      startRegistration: async () => { throw registrationFailure },
    })

    await vi.waitFor(() => expect(credentials.dispose).toHaveBeenCalledOnce())
    let settled = false
    void starting.catch(() => { settled = true })
    await Promise.resolve()
    expect(settled).toBe(false)
    credentialsDisposed.resolve()
    await vi.waitFor(() => expect(settings.dispose).toHaveBeenCalledOnce())
    settingsDisposed.resolve()
    await expect(starting).rejects.toBe(registrationFailure)

    expect(credentials.dispose).toHaveBeenCalledOnce()
    expect(settings.dispose).toHaveBeenCalledOnce()
  })

  it('continues provider cleanup when registration disposal fails', async () => {
    const settingsDisposed = Promise.withResolvers<void>()
    const credentialsDisposed = Promise.withResolvers<void>()
    const settings = { dispose: vi.fn(async () => settingsDisposed.promise) }
    const credentials = { dispose: vi.fn(async () => credentialsDisposed.promise) }
    const registration = { dispose: vi.fn(async () => { throw new Error('registration disposal failed') }) }
    const lifecycle = await startCliRuntime({
      startSettings: async () => settings,
      startCredentials: async () => credentials,
      startRegistration: async () => ({ layer: registration, store: {} as SagConnectionStore }),
    })

    const disposing = lifecycle.dispose()
    await vi.waitFor(() => expect(credentials.dispose).toHaveBeenCalledOnce())
    let settled = false
    void disposing.catch(() => { settled = true })
    await Promise.resolve()
    expect(settled).toBe(false)
    credentialsDisposed.resolve()
    await vi.waitFor(() => expect(settings.dispose).toHaveBeenCalledOnce())
    settingsDisposed.resolve()
    await expect(disposing).rejects.toThrow('registration disposal failed')

    expect(credentials.dispose).toHaveBeenCalledOnce()
    expect(settings.dispose).toHaveBeenCalledOnce()
  })
})
